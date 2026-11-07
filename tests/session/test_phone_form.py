"""Synthetic phone DOM. No number is transmitted."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET  # ruff: ignore[suspicious-xml-etree-import]
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.session.phone_form import (
    has_separate_russian_prefix,
    login_code_channel,
    normalize_phone_input,
    select_phone_input,
)
from ozon_mcp.session.transport import OzonSession

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

CASES = json.loads(Path("tests/fixtures/phone-form-cases.json").read_text(encoding="utf-8"))
OBSERVED = CASES[0]["html"]
NATIONAL = "0" * 10


class Locator:
    def __init__(self, frame: Frame, nodes: list[Element]) -> None:
        self.frame = frame
        self.nodes = nodes

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> Locator:
        return Locator(self.frame, [self.nodes[index]])

    @property
    def first(self) -> Locator:
        return self

    def is_visible(self) -> bool:
        assert len(self.nodes) == 1
        node: Element | None = self.nodes[0]
        while node is not None:
            if node.get("hidden") is not None:
                return False
            node = self.frame.parents.get(node)
        return True

    def is_enabled(self) -> bool:
        return self.nodes[0].get("disabled") is None

    def get_attribute(self, key: str) -> str | None:
        assert key != "value"
        return self.nodes[0].get(key)

    def filter(self, *, has_text: object) -> Locator:
        pattern = has_text
        return Locator(
            self.frame,
            [node for node in self.nodes if pattern.search("".join(node.itertext()))],  # type: ignore[attr-defined]
        )

    def locator(self, selector: str) -> Locator:
        if selector == "xpath=..":
            return Locator(self.frame, [self.frame.parents[node] for node in self.nodes])
        if selector == ":scope > span":
            return Locator(self.frame, [child for node in self.nodes for child in node if child.tag == "span"])
        message = f"unexpected selector {selector}"
        raise AssertionError(message)

    def fill(self, value: str) -> None:
        self.frame.fills.append((self.nodes[0].get("id"), value))

    def click(self, **_kwargs: object) -> None:
        return None


class Frame:
    def __init__(self, markup: str) -> None:
        self.url = "https://www.ozon.ru/ozonid-lite"
        self.root = ET.fromstring(markup)  # ruff: ignore[suspicious-xml-element-tree-usage]
        self.parents = {child: node for node in self.root.iter() for child in node}
        self.fills: list[tuple[str | None, str]] = []
        self.submit = Mock()

    def locator(self, selector: str) -> Locator:
        inputs = list(self.root.iter("input"))
        if selector == 'input[role="combobox"][name="filter"][readonly][autocomplete="off"][type="text"]':
            nodes = [
                node
                for node in inputs
                if node.get("role") == "combobox"
                and node.get("name") == "filter"
                and node.get("readonly") is not None
                and node.get("autocomplete") == "off"
                and node.get("type") == "text"
            ]
        elif selector == 'input[type="tel"]':
            nodes = [node for node in inputs if node.get("type") == "tel"]
        elif selector == 'input[inputmode="tel"]:not([type="email"]):not([type="hidden"])':
            nodes = [
                node
                for node in inputs
                if node.get("inputmode") == "tel" and node.get("type") not in {"email", "hidden"}
            ]
        elif selector == 'input:not([type]), input[type="text"]':
            nodes = [node for node in inputs if node.get("type") in {None, "text"}]
        elif selector == "input[type=email]":
            nodes = [node for node in inputs if node.get("type") == "email"]
        else:
            message = f"unexpected selector {selector}"
            raise AssertionError(message)
        return Locator(self, nodes)

    def get_by_text(self, text: str, *, exact: bool = False) -> Locator:
        assert exact
        return Locator(self, [node for node in self.root.iter("span") if "".join(node.itertext()).strip() == text])

    def get_by_role(self, *_args: object, **_kwargs: object) -> Mock:
        return self.submit


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_country_prefix_shapes(case: dict[str, object]) -> None:
    assert has_separate_russian_prefix(Frame(str(case["html"]))) is case["expected_prefix"]


def test_phone_selector_priority_and_rejections() -> None:
    assert select_phone_input(Frame(OBSERVED)).get_attribute("id") == "phone"
    markup = (
        '<form><input type="text" id="generic" />'
        '<input type="text" inputmode="tel" id="mode" /><input type="tel" id="tel" /></form>'
    )
    assert select_phone_input(Frame(markup)).get_attribute("id") == "tel"
    unsafe = [
        '<input type="email" inputmode="tel" />',
        '<input type="hidden" inputmode="tel" />',
        '<input type="tel" hidden="hidden" />',
        '<input type="tel" readonly="readonly" />',
        '<input type="text" autocomplete="one-time-code" />',
        '<input type="tel" /><input type="tel" />',
    ]
    for item in unsafe:
        with pytest.raises(OzonError):
            select_phone_input(Frame(f"<form>{item}</form>"))


def test_normalization_accepts_confirmed_shapes_only() -> None:
    for value in ("+7" + NATIONAL, "8" + NATIONAL, NATIONAL, "+7 (000) 000-00-00", "8 (000) 000-00-00"):
        assert normalize_phone_input(value, separate_russian_prefix=True) == NATIONAL
    assert normalize_phone_input("8" + "0" * 9, separate_russian_prefix=True) == "8" + "0" * 9
    for value in ("", "8" + "0" * 8, "+7" + "0" * 11, "+44" + NATIONAL, "letters", "++7" + NATIONAL, "+7(000"):
        with pytest.raises(OzonError):
            normalize_phone_input(value, separate_russian_prefix=True)
    with pytest.raises(OzonError, match="not filled"):
        normalize_phone_input("+7" + NATIONAL, separate_russian_prefix=False)


def test_delivery_channel_is_email_or_unconfirmed() -> None:
    assert login_code_channel(by_mail=True) == "email"
    assert login_code_channel(by_mail=False) == "unconfirmed"


class _Page:
    def __init__(self, frame: Frame) -> None:
        self.frames = [frame]

    def title(self) -> str:
        return "Ozon"

    def goto(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def get_by_role(self, *_args: object, **_kwargs: object) -> object:
        class _Button:
            def click(self, **_kwargs: object) -> None:
                return None

        return type("Role", (), {"first": _Button()})()


def test_phone_login_reuses_the_browser_and_fills_national_digits() -> None:
    session = OzonSession()
    closed: list[bool] = []

    def remember_close() -> None:
        closed.append(True)

    session._close_browser = remember_close  # type: ignore[method-assign]
    frame = Frame(OBSERVED)

    def show_code(**_kwargs: object) -> None:
        frame.url = "https://www.ozon.ru/otp-lite"

    frame.submit.first.click = show_code
    session._page = _Page(frame)
    channel = session._begin_login_form("+7" + NATIONAL)
    assert closed == []
    assert frame.fills == [("phone", NATIONAL)]
    assert channel == "unconfirmed"


def test_ambiguous_prefix_does_not_fill_or_submit() -> None:
    session = OzonSession()
    frame = Frame('<form><input type="tel" id="phone" /></form>')
    session._page = _Page(frame)
    session._logging_in = True
    with pytest.raises(OzonError, match="not filled"):
        session.begin_login("+7" + NATIONAL)
    assert frame.fills == []
    frame.submit.first.click.assert_not_called()
    assert session._logging_in is False
