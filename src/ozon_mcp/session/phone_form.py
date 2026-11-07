"""Country-aware phone entry for the OzonID form.

National digits are filled only after one visible readonly country filter is
confirmed. Unknown shapes fail before fill or submit.
"""

import re
from typing import Any

from ozon_mcp.errors import OzonError

_PREFIX = re.compile(r"^\s*\+7\s*$")
_COUNTRY_FILTER = 'input[role="combobox"][name="filter"][readonly][autocomplete="off"][type="text"]'


def has_separate_russian_prefix(frame: Any) -> bool:
    """True only for one visible readonly filter whose parent shows ``+7``."""
    controls = frame.locator(_COUNTRY_FILTER)
    visible = [
        controls.nth(index)
        for index in range(controls.count())
        if controls.nth(index).is_visible() and controls.nth(index).is_enabled()
    ]
    if len(visible) != 1:
        return False
    group = visible[0].locator("xpath=..").filter(has_text=_PREFIX)
    if group.count() != 1 or not group.is_visible():
        return False
    labels = group.locator(":scope > span").filter(has_text=_PREFIX)
    return bool(labels.count() == 1 and labels.is_visible())


def normalize_phone_input(login: str, *, separate_russian_prefix: bool) -> str:
    """Return ten national digits, or the original ten, for a confirmed ``+7``."""
    if not separate_russian_prefix:
        message = (
            "Cannot verify a separate +7 country control; phone input was not filled. "
            "Inspect the country control and input format before retrying."
        )
        raise OzonError(message)
    value = login.strip()
    if not re.fullmatch(r"\+?[0-9 ()\-\u00a0]+", value):
        message = "Invalid phone format: use digits with optional leading + and spacing separators"
        raise OzonError(message)
    _reject_unbalanced(value)
    national = _national_digits(value, re.sub(r"[^0-9]", "", value))
    if national is None:
        message = (
            "Invalid phone format for selected +7: expected +7 and ten digits, "
            "domestic 8 and ten digits, or ten national digits"
        )
        raise OzonError(message)
    return national


def _reject_unbalanced(value: str) -> None:
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth not in {0, 1}:
            message = "Invalid phone format: unbalanced parentheses"
            raise OzonError(message)
    if depth:
        message = "Invalid phone format: unbalanced parentheses"
        raise OzonError(message)


def _national_digits(value: str, digits: str) -> str | None:
    if value.startswith("+"):
        if len(digits) == 11 and digits.startswith("7"):
            return digits[1:]
        return None
    if len(digits) == 10:
        return digits
    if len(digits) == 11 and digits.startswith("8"):
        return digits[1:]
    return None


def select_phone_input(frame: Any) -> Any:
    """Prefer a telephone field and reject hidden, readonly or ambiguous inputs."""
    for selector in (
        'input[type="tel"]',
        'input[inputmode="tel"]:not([type="email"]):not([type="hidden"])',
        'input:not([type]), input[type="text"]',
    ):
        candidates = frame.locator(selector)
        usable = []
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            field_type = (candidate.get_attribute("type") or "text").lower()
            if (
                candidate.is_visible()
                and candidate.is_enabled()
                and field_type in {"tel", "text"}
                and candidate.get_attribute("readonly") is None
                and candidate.get_attribute("autocomplete") != "one-time-code"
            ):
                usable.append(candidate)
        if len(usable) > 1:
            message = "Phone input is ambiguous; inspect the login form before retrying"
            raise OzonError(message)
        if usable:
            return usable[0]
    message = "No visible editable phone input found; inspect the login form before retrying"
    raise OzonError(message)


def raise_for_phone_format_error(frame: Any) -> None:
    """Recognize one fixed validation sentence and no other form text."""
    errors = frame.get_by_text("Некорректный формат телефона", exact=True)
    if any(errors.nth(index).is_visible() for index in range(errors.count())):
        message = (
            "Ozon rejected the phone format; no OTP step was confirmed. "
            "Check the country selector and phone-field format before retrying."
        )
        raise OzonError(message)


def login_code_channel(*, by_mail: bool) -> str:
    """Email is the channel we submitted. A phone form does not prove delivery."""
    if by_mail:
        return "email"
    return "unconfirmed"
