"""The owned page, the reader payload, and the parser are one chain.

A foreground about:blank tab is not the page this reader evaluates.
Live timing is not measured here.
"""

from __future__ import annotations

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import (
    DEADLINE_VISIBILITY,
    PUBLIC_PAGE_READER,
    products_from_document,
    url_class,
)
from ozon_mcp.parsing.visible_cards import PUBLIC_TILE_SELECTOR, PUBLIC_TILE_STAGES
from ozon_mcp.services import public_catalog
from ozon_mcp.session.transport import OzonSession

_GRID = """
<div data-widget="infiniteVirtualPaginator">
  <div class="tileGridDesktop">
    <div data-index="0" class="tile-root">
      <a target="_blank" href="/product/example-alpha-1600000001/">
        <span class="tsBody500Medium">Alpha isolate</span>
      </a>
      <span class="tsHeadline500Medium">100 ₽</span>
    </div>
  </div>
</div>
"""


class _Locator:
    def __init__(self, page: _Page, *, ready: bool, selector: str = "") -> None:
        self.page = page
        self.ready = ready
        self.selector = selector
        self.first = self
        self.visible_only = False
        self.wait: dict[str, object] = {}

    def wait_for(self, **kwargs: object) -> None:
        self.wait = kwargs
        self.page.on_wait()
        if not self.ready:
            message = "deadline"
            raise TimeoutError(message)

    def count(self) -> int:
        self.page.note_count(self.selector)
        counts = self.page.visible_counts if self.visible_only else self.page.attached_counts
        return counts.get(self.selector, 0)

    def filter(self, **kwargs: object) -> _Locator:
        self.page.filter_calls.append(kwargs)
        clone = _Locator(self.page, ready=self.ready, selector=self.selector)
        clone.visible_only = bool(kwargs.get("visible"))
        return clone

    def is_visible(self) -> bool:
        return self.page.first_visible


class _Page:
    def __init__(self, *, ready: bool = True, stay_blank: bool = False) -> None:
        self.url = "about:blank"
        self.ready = ready
        self.stay_blank = stay_blank
        self.calls: list[object] = []
        self.locator_selector = ""
        self.attached_counts: dict[str, int] = {}
        self.visible_counts: dict[str, int] = {}
        self.filter_calls: list[dict[str, object]] = []
        self.first_visible = False
        self.visibility_state = "visible"
        self.focused = True
        self.note_count = lambda _selector: None
        self.on_wait = lambda: None

    def is_closed(self) -> bool:
        return False

    def bring_to_front(self) -> None:
        self.calls.append("front")

    def goto(self, url: str, **_kwargs: object) -> None:
        self.calls.append(("goto", url))
        if not self.stay_blank:
            self.url = url

    def title(self) -> str:
        return "about:blank" if self.url == "about:blank" else "Example search"

    def locator(self, selector: str) -> _Locator:
        self.locator_selector = selector
        located = _Locator(self, ready=self.ready, selector=selector)
        self.calls.append(("wait", located))
        return located

    def get_by_text(self, _text: str, *, exact: bool = False) -> _Locator:
        del exact
        return _Locator(self, ready=False)

    def evaluate(self, script: str) -> dict[str, object]:
        self.calls.append(("evaluate", script))
        if script == DEADLINE_VISIBILITY:
            return {"visibility": self.visibility_state, "focused": self.focused}
        return {"title": self.title(), "jsonLd": [], "microdata": [], "html": _GRID}


def _session(page: _Page) -> OzonSession:
    session = OzonSession()
    session._page = page
    session._ensure_browser = lambda: None  # type: ignore[method-assign]
    session._raise_for_antibot_challenge = lambda: None  # type: ignore[method-assign]
    return session


def test_goto_and_tile_wait_share_the_public_read_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    page = _Page()
    session = _session(page)
    monkeypatch.setattr("ozon_mcp.session.transport.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "ozon_mcp.session.transport.get_settings",
        type("Settings", (), {"public_read_timeout": 20, "browser_timeout": 60}),
    )

    def goto(url: str, **kwargs: object) -> None:
        page.calls.append(("goto", kwargs.get("timeout")))
        clock["now"] += 8
        page.url = url

    page.goto = goto  # type: ignore[method-assign]
    session.read_public_page("/search/?text=alpha")
    goto_timeout = next(call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "goto")
    waited = [call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "wait" and call[1].wait]
    assert len(waited) == 1
    ready_timeout = waited[0].wait["timeout"]
    assert goto_timeout == 17_000
    assert ready_timeout == 9_000
    assert goto_timeout != 60_000
    assert ready_timeout != 60_000
    assert 8_000 + ready_timeout + 3_000 == 20_000


def test_navigation_timeout_returns_diagnostics_and_does_not_start_the_tile_wait() -> None:
    page = _Page()
    session = _session(page)

    def goto(url: str, **kwargs: object) -> None:
        del url, kwargs
        page.calls.append("goto")
        raise type("TimeoutError", (Exception,), {})("navigation deadline")

    page.goto = goto  # type: ignore[method-assign]
    with pytest.raises(OzonError, match="url_class=") as caught:
        session.read_public_page("/search/?text=alpha")
    message = str(caught.value)
    assert "tile_selector_ready=False" in message
    assert "html_present=False" in message
    assert "text=alpha" not in message
    assert page.calls == ["front", "goto"]


def test_a_full_navigation_grant_leaves_no_second_tile_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    page = _Page()
    session = _session(page)
    monkeypatch.setattr("ozon_mcp.session.transport.time.monotonic", lambda: clock["now"])

    def goto(url: str, **kwargs: object) -> None:
        page.calls.append(("goto", kwargs.get("timeout")))
        clock["now"] += int(kwargs["timeout"]) / 1000
        page.url = url

    page.goto = goto  # type: ignore[method-assign]
    with pytest.raises(OzonError, match="tile_selector_ready=False") as raised:
        session.read_public_page("/search/?text=alpha")
    text = str(raised.value)
    assert "deadline_probe=measured" in text
    assert "title_spans_attached=0" in text
    assert "dom_counts=not_taken" not in text
    scripts = [call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "evaluate"]
    assert scripts == [DEADLINE_VISIBILITY]
    assert not any(call[1].wait for call in page.calls if isinstance(call, tuple) and call[0] == "wait")


def test_reader_payload_is_what_the_parser_receives() -> None:
    page = _Page()
    other = _Page()
    other.evaluate = lambda _script: (_ for _ in ()).throw(AssertionError("other tab"))  # type: ignore[method-assign]
    document = _session(page).read_public_page("/search/?text=alpha")
    assert page.calls[0] == "front"
    assert page.calls[1][0] == "goto"
    waited = [call for call in page.calls if isinstance(call, tuple) and call[0] == "wait" and call[1].wait]
    assert len(waited) == 1
    evaluated = [call for call in page.calls if isinstance(call, tuple) and call[0] == "evaluate"]
    assert evaluated == [("evaluate", PUBLIC_PAGE_READER)]
    assert other.calls == []
    assert page.locator_selector == PUBLIC_TILE_SELECTOR
    assert "html" in PUBLIC_PAGE_READER
    assert "innerHTML" in PUBLIC_PAGE_READER
    products = products_from_document(document)
    assert products is not None
    assert products[0].sku == "1600000001"
    assert products[0].title == "Alpha isolate"
    assert products[0].displayed_price == "100 ₽"
    assert products[0].price is None
    assert document["diagnostics"]["url_class"] == "ozon-search"
    assert document["diagnostics"]["tile_selector_ready"] is True
    assert document["diagnostics"]["counts"]["product_hrefs"] == 1
    assert "text=alpha" not in str(document["diagnostics"])


def test_service_reads_the_owned_page_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _Page()
    monkeypatch.setattr(public_catalog, "get_session", lambda: _session(page))
    tiles = public_catalog.search_public("alpha", None, "popular", 1, None, 24)
    kinds = [call[0] for call in page.calls if isinstance(call, tuple)]
    assert kinds[0] == "goto"
    assert kinds[-1] == "evaluate"
    assert sum(1 for call in page.calls if isinstance(call, tuple) and call[0] == "wait" and call[1].wait) == 1
    assert tiles[0].sku == "1600000001"
    assert tiles[0].displayed_price == "100 ₽"
    assert tiles[0].price is None


def test_about_blank_owned_page_is_reported_and_not_parsed() -> None:
    page = _Page(stay_blank=True)
    with pytest.raises(OzonError, match="about:blank") as raised:
        _session(page).read_public_page("/search/?text=alpha")
    assert "evaluate" not in [call[0] for call in page.calls if isinstance(call, tuple)]
    assert "url_class=about:blank" in str(raised.value)
    assert "empty search result" in str(raised.value)
    assert "<div" not in str(raised.value)


def test_unread_tile_is_a_deadline_not_an_empty_catalog() -> None:
    page = _Page(ready=False)
    with pytest.raises(OzonError, match="not ready") as raised:
        _session(page).read_public_page("/search/?page=1&text=alpha")
    scripts = [call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "evaluate"]
    assert scripts == [DEADLINE_VISIBILITY]
    text = str(raised.value)
    assert "url_class=ozon-search" in text
    assert "tile_selector_ready=False" in text
    assert "deadline_probe=measured" in text
    assert "title_spans_attached=0" in text
    assert "html_present=False" in text
    assert "text=alpha" not in text
    assert "https://" not in text
    assert "<div" not in text
    assert url_class("about:blank") == "about:blank"
    assert url_class("https://www.ozon.ru/search/?text=secret") == "ozon-search"


def test_deadline_probe_separates_attached_visible_and_document_state() -> None:
    page = _Page(ready=False)
    page.visibility_state = "hidden"
    page.focused = False
    page.first_visible = False
    attached = {
        "paginator": 1,
        "tile_grids": 1,
        "tile_roots": 3,
        "product_anchors": 3,
        "title_spans": 3,
    }
    for name, selector in PUBLIC_TILE_STAGES:
        page.attached_counts[selector] = attached[name]
    page.visible_counts[PUBLIC_TILE_SELECTOR] = 2
    with pytest.raises(OzonError, match="not ready") as raised:
        _session(page).read_public_page("/search/?text=alpha")
    text = str(raised.value)
    assert "deadline_probe=measured" in text
    assert "bring_to_front=ok" in text
    assert "document_visibility=hidden" in text
    assert "document_focused=False" in text
    assert "paginator=1" in text
    assert "tile_grids=1" in text
    assert "tile_roots_attached=3" in text
    assert "product_anchors_attached=3" in text
    assert "title_spans_attached=3" in text
    assert "title_spans_visible=2" in text
    assert "first_title_visible=False" in text
    assert "html_present=False" in text
    assert "tile_roots=0" not in text
    assert "text=alpha" not in text
    assert "https://" not in text
    waits = [item[1] for item in page.calls if isinstance(item, tuple) and item[0] == "wait" and item[1].wait]
    assert len(waits) == 1
    assert waits[0].wait["state"] == "visible"
    assert waits[0].wait["timeout"] == 17_000
    assert page.filter_calls == [{"visible": True}, {"visible": True}]
    scripts = [call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "evaluate"]
    assert scripts == [DEADLINE_VISIBILITY]


class _Foreign:
    def __init__(self, url: str) -> None:
        self.url = url

    def locator(self, *_args: object, **_kwargs: object) -> object:
        message = "foreign tab"
        raise AssertionError(message)

    def title(self) -> str:
        message = "foreign title"
        raise AssertionError(message)

    def evaluate(self, *_args: object, **_kwargs: object) -> object:
        message = "foreign eval"
        raise AssertionError(message)


def test_goto_and_probe_marks_identify_the_owned_page(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    page = _Page(ready=False)
    foreign = _Foreign("https://www.ozon.ru/search/?text=secret")
    session = _session(page)
    session._context = type("Context", (), {"pages": [foreign, page]})()
    monkeypatch.setattr("ozon_mcp.session.transport.time.monotonic", lambda: clock["now"])
    grid = next(selector for name, selector in PUBLIC_TILE_STAGES if name == "tile_grids")
    paginator = next(selector for name, selector in PUBLIC_TILE_STAGES if name == "paginator")

    def on_wait() -> None:
        clock["now"] += 5
        page.url += "&brand_was_predicted=true"
        page.attached_counts[paginator] = 1
        page.attached_counts[grid] = 4

    page.on_wait = on_wait
    with pytest.raises(OzonError, match="not ready") as raised:
        session.read_public_page("/search/?page=1&text=alpha&sorting=price")
    text = str(raised.value)
    assert "goto_ms=" in text
    assert "probe_ms=" in text
    assert "goto_context_pages=2" in text
    assert "probe_context_pages=2" in text
    assert "goto_owned_index=1" in text
    assert "probe_owned_index=1" in text
    assert "goto_other_search_pages=1" in text
    assert "goto_query_keys=page,sorting,text" in text
    assert "probe_query_keys=brand_was_predicted,page,sorting,text" in text
    assert "goto_tile_grids=0" in text
    assert "tile_grids=4" in text
    assert "goto_paginator=0" in text
    assert "paginator=1" in text
    assert "alpha" not in text
    assert "secret" not in text
    assert "https://" not in text


def test_owned_search_probe_does_not_navigate_or_read_other_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_PUBLIC_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("OZON_BROWSER_TIMEOUT", raising=False)
    monkeypatch.delenv("OZON_STOREFRONT", raising=False)
    page = _Page()
    page.url = "https://www.ozon.ru/search/?page=1&text=alpha&sorting=price&brand_was_predicted=true"
    for name, selector in PUBLIC_TILE_STAGES:
        page.attached_counts[selector] = 1 if name == "paginator" else 0
    foreign = _Foreign("https://www.ozon.ru/search/?text=secret&from_global=true")
    session = _session(page)
    session._context = type("Context", (), {"pages": [foreign, page]})()
    session._ensure_browser = lambda: (_ for _ in ()).throw(AssertionError("launch"))  # type: ignore[method-assign]
    page.goto = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("goto"))  # type: ignore[method-assign]
    monkeypatch.setattr(public_catalog, "get_session", lambda: session)
    probed = public_catalog.owned_search_probe()
    dumped = probed.model_dump_json()
    assert probed.navigated is False
    assert probed.owned_page == "present"
    assert probed.dom_counts == "measured"
    assert probed.owned_index == 1
    assert probed.context_pages == 2
    assert probed.other_search_pages == 1
    assert probed.url_class == "ozon-search"
    assert probed.query_keys == "brand_was_predicted,page,sorting,text"
    assert probed.paginator == "1"
    assert probed.tile_grids == "0"
    assert probed.public_read_timeout == 20
    assert "alpha" not in dumped
    assert "secret" not in dumped
    assert "https://" not in dumped
    assert "title" not in probed.model_dump()

    page.url = "https://www.ozon.ru/"
    page.locator = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("counted"))  # type: ignore[method-assign]
    home = public_catalog.owned_search_probe()
    assert home.url_class == "ozon-home"
    assert home.dom_counts == "not_taken"
    assert home.query_keys == "not_taken"
    assert home.tile_grids == "not_taken"
    assert home.navigated is False

    session._page = None
    absent = public_catalog.owned_search_probe()
    assert absent.owned_page == "absent"
    assert absent.navigated is False
    assert absent.dom_counts == "not_taken"


def test_deadline_probe_stops_at_the_shared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    page = _Page(ready=False)
    session = _session(page)
    monkeypatch.setattr("ozon_mcp.session.transport.time.monotonic", lambda: clock["now"])

    def goto(url: str, **_kwargs: object) -> None:
        clock["now"] += 18.5
        page.url = url

    def note(selector: str) -> None:
        if selector:
            clock["now"] += 2

    page.goto = goto  # type: ignore[method-assign]
    page.note_count = note
    with pytest.raises(OzonError, match="deadline_probe=skipped") as raised:
        session.read_public_page("/search/?text=alpha")
    text = str(raised.value)
    assert "goto_paginator=0" in text
    assert "goto_tile_grids=not_taken" in text
    assert "dom_counts=not_taken" in text
    assert "title_spans_attached=0" not in text
    scripts = [call[1] for call in page.calls if isinstance(call, tuple) and call[0] == "evaluate"]
    assert scripts == []


def test_deadline_probe_is_skipped_when_the_budget_is_already_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    page = _Page(ready=False)
    session = _session(page)
    monkeypatch.setattr("ozon_mcp.session.transport.time.monotonic", lambda: clock["now"])

    def goto(url: str, **_kwargs: object) -> None:
        clock["now"] += 20
        page.url = url

    page.goto = goto  # type: ignore[method-assign]
    with pytest.raises(OzonError, match="deadline_probe=skipped") as raised:
        session.read_public_page("/search/?text=alpha")
    text = str(raised.value)
    assert "dom_counts=not_taken" in text
    assert "goto_title_spans_attached=not_taken" in text
    assert "title_spans_attached=0" not in text
    assert "tile_roots=0" not in text
    assert page.calls == ["front"]
