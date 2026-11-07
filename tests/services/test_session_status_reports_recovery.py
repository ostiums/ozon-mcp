"""A signed-out session must say so, and say whether it can self-heal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ozon_mcp.parsing.visible_cards import PUBLIC_TILE_STAGES
from ozon_mcp.services.session import session_status
from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import get_settings

if TYPE_CHECKING:
    import pytest


class _FakeSession:
    def __init__(self, user: str | None, *, backup: bool) -> None:
        self._user, self._backup = user, backup

    def signed_in_user(self) -> str | None:
        return self._user

    def has_backup(self) -> bool:
        return self._backup


class _Count:
    def __init__(self, *, present: bool) -> None:
        self.present = present
        self.first = self

    def count(self) -> int:
        return 1 if self.present else 0

    def filter(self, **_kwargs: object) -> _Count:
        return _Count(present=False)

    def is_visible(self) -> bool:
        return False


class _OpenPage:
    def __init__(self, paginator: str) -> None:
        self.url = "https://www.ozon.ru/search/?sorting=price&text=alpha"
        self.paginator = paginator
        self.gotos = 0

    def is_closed(self) -> bool:
        return False

    def title(self) -> str:
        return "Example search"

    def goto(self, *_args: object, **_kwargs: object) -> None:
        self.gotos += 1
        message = "goto"
        raise AssertionError(message)

    def locator(self, selector: str) -> _Count:
        return _Count(present=selector == self.paginator)

    def evaluate(self, _script: str) -> dict[str, object]:
        return {"visibility": "visible", "focused": True}


class _Cookies:
    def __init__(self, page: _OpenPage) -> None:
        self.pages = [page]

    def cookies(self) -> list[dict[str, str]]:
        return [{"name": "__Secure-user-id", "value": "42"}]


def test_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ozon_mcp.services.session.get_session", lambda: _FakeSession("44563249", backup=True))
    status = session_status()
    assert status.signed_in is True
    assert status.user_id == "44563249"


def test_signed_out_with_a_backup_says_it_will_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ozon_mcp.services.session.get_session", lambda: _FakeSession(None, backup=True))
    status = session_status()
    assert status.signed_in is False
    assert status.detail is not None
    assert "automatically" in status.detail


def test_owned_search_is_absent_unless_the_flag_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_PUBLIC_DIAGNOSTICS", raising=False)
    get_settings.cache_clear()

    class Session(_FakeSession):
        def probe_owned_search(self) -> dict[str, object]:
            message = "probe"
            raise AssertionError(message)

    monkeypatch.setattr("ozon_mcp.services.session.get_session", lambda: Session("7", backup=False))
    try:
        status = session_status()
    finally:
        get_settings.cache_clear()
    assert status.owned_search is None
    assert "owned_search" not in status.model_dump()


def test_diagnostic_flag_reads_the_open_page_and_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZON_PUBLIC_DIAGNOSTICS", "1")
    get_settings.cache_clear()
    paginator = next(selector for name, selector in PUBLIC_TILE_STAGES if name == "paginator")
    page = _OpenPage(paginator)
    session = OzonSession()
    session._page = page
    session._context = _Cookies(page)
    session._launch_browser = lambda: (_ for _ in ()).throw(AssertionError("launch"))  # type: ignore[method-assign]
    monkeypatch.setattr("ozon_mcp.services.session.get_session", lambda: session)
    monkeypatch.setattr("ozon_mcp.services.public_catalog.get_session", lambda: session)
    try:
        status = session_status()
    finally:
        get_settings.cache_clear()
    assert session._page is page
    assert page.gotos == 0
    assert status.signed_in is True
    assert status.owned_search is not None
    assert status.owned_search.navigated is False
    assert status.owned_search.owned_page == "present"
    assert status.owned_search.dom_counts == "measured"
    assert status.owned_search.paginator == "1"
    assert status.owned_search.tile_grids == "0"
    assert status.owned_search.query_keys == "sorting,text"
    dumped = status.owned_search.model_dump_json()
    assert "alpha" not in dumped
    assert "https://" not in dumped
    assert "42" not in dumped


def test_signed_out_without_a_backup_asks_for_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ozon_mcp.services.session.get_session", lambda: _FakeSession(None, backup=False))
    status = session_status()
    assert status.detail is not None
    assert "start_login" in status.detail
