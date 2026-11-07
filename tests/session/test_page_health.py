"""Network, denial and challenge titles stay distinct."""

from __future__ import annotations

import pytest

from ozon_mcp.session.page_health import PageHealth, classify_page_title
from ozon_mcp.session.transport import OzonSession


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Ozon — интернет-магазин", PageHealth.OK),
        ("Доступные товары", PageHealth.OK),
        ("HX Nutrition Zero Isolate - купить на OZON", PageHealth.OK),
        ("Похоже, нет соединения", PageHealth.NETWORK),
        ("Доступ ограничен", PageHealth.ACCESS_DENIED),
        ("CAPTCHA", PageHealth.CHALLENGE),
        ("Нет соединения — CAPTCHA", PageHealth.NETWORK),
    ],
)
def test_title_classification(title: str, kind: PageHealth) -> None:
    assert classify_page_title(title) is kind


class _Title:
    def __init__(self, text: str) -> None:
        self.text = text

    def title(self) -> str:
        return self.text


def test_network_title_pauses_login_without_calling_it_a_captcha() -> None:
    session = OzonSession()
    session._logging_in = True
    session._page = _Title("Похоже, нет соединения")
    with pytest.raises(RuntimeError, match="network or connection") as raised:
        session._raise_for_antibot_challenge()
    assert "does not establish" in str(raised.value)
    assert session._logging_in is False


@pytest.mark.parametrize(
    ("title", "fragment"),
    [
        ("Доступ ограничен", "access denied"),
        ("CAPTCHA", "challenge"),
    ],
)
def test_blocked_titles_pause_login(title: str, fragment: str) -> None:
    session = OzonSession()
    session._logging_in = True
    session._page = _Title(title)
    with pytest.raises(RuntimeError, match=fragment):
        session._raise_for_antibot_challenge()
    assert session._logging_in is False


def test_healthy_title_leaves_login_in_progress() -> None:
    session = OzonSession()
    session._logging_in = True
    session._page = _Title("Доступные товары")
    session._raise_for_antibot_challenge()
    assert session._logging_in is True
