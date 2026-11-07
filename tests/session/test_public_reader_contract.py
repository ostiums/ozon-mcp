"""The owned page, the reader payload, and the parser are one chain.

A foreground about:blank tab is not the page this reader evaluates.
Live timing is not measured here.
"""

from __future__ import annotations

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import PUBLIC_PAGE_READER, products_from_document, url_class
from ozon_mcp.parsing.visible_cards import PUBLIC_TILE_SELECTOR
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
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.first = self
        self.wait: dict[str, object] = {}

    def wait_for(self, **kwargs: object) -> None:
        self.wait = kwargs
        if not self.ready:
            message = "deadline"
            raise TimeoutError(message)

    def count(self) -> int:
        return 0

    def is_visible(self) -> bool:
        return False


class _Page:
    def __init__(self, *, ready: bool = True, stay_blank: bool = False) -> None:
        self.url = "about:blank"
        self.ready = ready
        self.stay_blank = stay_blank
        self.calls: list[object] = []
        self.locator_selector = ""

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
        located = _Locator(ready=self.ready)
        self.calls.append(("wait", located))
        return located

    def get_by_text(self, _text: str, *, exact: bool = False) -> _Locator:
        del exact
        return _Locator(ready=False)

    def evaluate(self, script: str) -> dict[str, object]:
        self.calls.append(("evaluate", script))
        return {"title": self.title(), "jsonLd": [], "microdata": [], "html": _GRID}


def _session(page: _Page) -> OzonSession:
    session = OzonSession()
    session._page = page
    session._ensure_browser = lambda: None  # type: ignore[method-assign]
    session._raise_for_antibot_challenge = lambda: None  # type: ignore[method-assign]
    return session


def test_reader_payload_is_what_the_parser_receives() -> None:
    page = _Page()
    other = _Page()
    other.evaluate = lambda _script: (_ for _ in ()).throw(AssertionError("other tab"))  # type: ignore[method-assign]
    document = _session(page).read_public_page("/search/?text=alpha")
    assert page.calls[0] == "front"
    assert page.calls[1][0] == "goto"
    assert page.calls[2][0] == "wait"
    assert page.calls[3] == ("evaluate", PUBLIC_PAGE_READER)
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
    assert [call[0] for call in page.calls if isinstance(call, tuple)] == ["goto", "wait", "evaluate"]
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
    assert "evaluate" not in [call[0] for call in page.calls if isinstance(call, tuple)]
    text = str(raised.value)
    assert "url_class=ozon-search" in text
    assert "tile_selector_ready=False" in text
    assert "text=alpha" not in text
    assert url_class("about:blank") == "about:blank"
    assert url_class("https://www.ozon.ru/search/?text=secret") == "ozon-search"
