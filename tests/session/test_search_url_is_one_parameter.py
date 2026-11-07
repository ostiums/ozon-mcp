"""The search path is one composer ``url`` value. Redirects stay unread.

The live HTTP 302 was not replayed. Proxy inheritance and a stale client were
ruled out separately, and the observed redirect class was an API path, not the
public search page the browser rendered.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, urlsplit

import pytest

from ozon_mcp.errors import UpstreamError
from ozon_mcp.services import catalog
from session.test_redirect_classification import _RecordingHttp
from session.test_upstream_failure_is_not_an_empty_page import PAGE, _Response, _Session

QUERY = "HX Nutrition Zero Isolate"


def _search_path() -> str:
    return f"/search/?page=1&text={quote(QUERY, safe='')}&sorting=price"


def _page_query(wire: str) -> dict[str, list[str]]:
    # The transport leaves the page query raw after ``?url=``. A standards parser
    # of the outer request would split it; this is the contract the suite locks.
    path = wire.split("?url=", 1)[1]
    return parse_qs(urlsplit(path).query)


def test_search_text_and_sort_stay_in_the_page_query() -> None:
    http = _RecordingHttp(_Response(200, PAGE))
    _Session(http).fetch(_search_path())
    wire = http.urls[0]
    params = _page_query(wire)
    assert params["text"] == [QUERY]
    assert params["sorting"] == ["price"]
    assert params["page"] == ["1"]
    assert "brand_was_predicted" not in wire
    assert "deny_category_prediction" not in wire
    assert "from_global" not in wire
    assert "%25" not in wire


def test_a_cyrillic_phrase_is_not_encoded_twice() -> None:
    path = "/search/?text=" + quote("тунец", safe="")
    http = _RecordingHttp(_Response(200, PAGE))
    _Session(http).fetch(path)
    wire = http.urls[0]
    assert "%25" not in wire
    assert _page_query(wire)["text"] == ["тунец"]


def test_an_ampersand_in_the_phrase_stays_inside_text() -> None:
    path = "/search/?text=" + quote("кофе & чай", safe="")
    http = _RecordingHttp(_Response(200, PAGE))
    _Session(http).fetch(path)
    assert _page_query(http.urls[0])["text"] == ["кофе & чай"]


def test_order_list_path_has_no_page_query() -> None:
    http = _RecordingHttp(_Response(200, PAGE))
    _Session(http).fetch("/my/orderlist")
    assert http.urls[0].split("?url=", 1)[1] == "/my/orderlist"


def test_search_redirect_is_not_followed_after_the_path_is_intact() -> None:
    marker = "DO_NOT_EMIT"
    location = f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/&token={marker}"
    http = _RecordingHttp(_Response(302, "{}", {"Location": location}))
    with pytest.raises(UpstreamError, match="path=api-path") as raised:
        _Session(http).fetch(_search_path())
    assert marker not in str(raised.value)
    assert http.calls == 1


def test_catalog_search_does_not_read_the_public_page_in_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.browser: list[str] = []
            self.actions: list[str] = []

        def fetch(self, path: str, backend: str = "composer") -> dict[str, object]:
            del path, backend
            raise UpstreamError(302, "Redirect destination: host=same-canonical-ozon; path=api-path.")

        def page_extract(self, path: str, js: str, *, scroll: bool = False) -> str:
            del js, scroll
            self.browser.append(path)
            return "tiles"

        def action(self, name: str, body: object = None) -> dict[str, object]:
            del body
            self.actions.append(name)
            return {}

    session = Session()
    monkeypatch.setattr(catalog, "get_session", lambda: session)
    with pytest.raises(UpstreamError, match="HTTP 302"):
        catalog.search(QUERY, sort="cheap", limit=24)
    assert session.browser == []
    assert session.actions == []
