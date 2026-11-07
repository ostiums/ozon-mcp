"""Manual completion clears login state only when the browser is signed in."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ozon_mcp.errors import OzonError
from session.test_upstream_failure_is_not_an_empty_page import PAGE, _Http, _Response, _Session


def _signed(value: str, frames: list[object]) -> _Session:
    session = _Session(_Http(_Response(200, PAGE)))
    session._logging_in = True
    session._page = SimpleNamespace(frames=frames, title=lambda: "Ozon")
    session._context = SimpleNamespace(cookies=lambda: [{"name": "__Secure-user-id", "value": value}])
    return session


def test_signed_in_without_an_auth_frame_allows_the_read() -> None:
    session = _signed("42", [])
    page = session.fetch("/my/orderlist")
    assert page["widgetStates"]
    assert session._logging_in is False


def test_code_form_still_blocks_the_read() -> None:
    session = _signed("42", [SimpleNamespace(url="https://www.ozon.ru/otp-lite")])
    with pytest.raises(OzonError, match="login is in progress"):
        session.fetch("/my/orderlist")
    assert session._logging_in is True


def test_signed_out_profile_is_not_cleared_by_a_healthy_title() -> None:
    session = _signed("0", [])
    with pytest.raises(OzonError, match="login is in progress"):
        session.fetch("/my/orderlist")
    assert session._logging_in is True
