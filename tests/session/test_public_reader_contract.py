"""The owned page, the reader payload, and the parser are one chain.

A foreground about:blank tab is not the page this reader evaluates.
Live timing is not measured here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ozon_mcp.parsing.public_storefront import (
    DEADLINE_VISIBILITY,
)
from ozon_mcp.parsing.visible_cards import PUBLIC_TILE_STAGES
from ozon_mcp.services import public_catalog
from ozon_mcp.session.transport import OzonSession

if TYPE_CHECKING:
    import pytest

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
