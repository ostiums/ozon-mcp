"""Classify a storefront title without treating every block as a CAPTCHA."""

import re
from enum import StrEnum
from typing import Final

_NETWORK_ERROR: Final = re.compile(
    r"нет соединения|не уда[её]тся (?:получить доступ к сайту|подключиться)|"
    r"сайт недоступен|ошибка (?:соединения|сети)|network error|connection (?:error|failed)",
    re.IGNORECASE,
)
_ACCESS_DENIED: Final = re.compile(
    r"\bдоступ (?:ограничен|запрещ[её]н|заблокирован)\b|"
    r"\b(?:access denied|access restricted|access blocked|forbidden)\b",
    re.IGNORECASE,
)
_ANTIBOT: Final = re.compile(
    r"\b(?:antibot|anti-bot|captcha|капча|challenge)\b|"
    r"проверка (?:безопасности|на робота)|подтвердите,? что вы (?:человек|не робот)",
    re.IGNORECASE,
)


class PageHealth(StrEnum):
    """Why a page is not a normal storefront, if it is not."""

    OK = "ok"
    NETWORK = "network"
    ACCESS_DENIED = "access_denied"
    CHALLENGE = "challenge"


def classify_page_title(title: str) -> PageHealth:
    """Network titles win over challenge words in the same title."""
    text = " ".join((title or "").split())
    if _NETWORK_ERROR.search(text):
        return PageHealth.NETWORK
    if _ACCESS_DENIED.search(text):
        return PageHealth.ACCESS_DENIED
    if _ANTIBOT.search(text):
        return PageHealth.CHALLENGE
    return PageHealth.OK


def health_message(kind: PageHealth) -> str | None:
    """Fixed operator text. A network title is not reported as a CAPTCHA."""
    if kind is PageHealth.NETWORK:
        return (
            "Ozon shows a network or connection error. Login is paused. "
            "Check connectivity and site availability before retrying. "
            "This title does not establish an interactive CAPTCHA."
        )
    if kind is PageHealth.ACCESS_DENIED:
        return (
            "Ozon reports access denied or restricted. Login is paused. "
            "Retry only after access is restored. Do not assume a CAPTCHA is available."
        )
    if kind is PageHealth.CHALLENGE:
        return (
            "Ozon shows an antibot or challenge page. Login is paused. "
            "Complete a manual check only if one is actually shown, then retry."
        )
    return None
