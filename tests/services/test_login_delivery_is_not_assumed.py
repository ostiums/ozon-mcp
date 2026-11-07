"""A phone login does not claim a delivery channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ozon_mcp.models.enums import LoginStage
from ozon_mcp.services import session as session_service

if TYPE_CHECKING:
    import pytest


class _Login:
    def __init__(self, channel: str) -> None:
        self.channel = channel

    def begin_login(self, login: str) -> str:
        del login
        return self.channel


def test_unconfirmed_delivery_does_not_say_phone_or_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_service, "get_session", lambda: _Login("unconfirmed"))
    step = session_service.start_login("0000000000")
    assert step.stage is LoginStage.CODE_REQUESTED
    assert step.detail is not None
    assert "sending a code by phone" not in step.detail.lower()
    assert "sending a code by push" not in step.detail.lower()
    assert "did not show" in step.detail


def test_email_delivery_names_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_service, "get_session", lambda: _Login("email"))
    step = session_service.start_login("person@example.test")
    assert step.detail is not None
    assert "email" in step.detail
