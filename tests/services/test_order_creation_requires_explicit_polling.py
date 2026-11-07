"""An uncertain order response must never silently submit another order."""

from typing import Any

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.models.checkout import Checkout, Totals
from ozon_mcp.services import checkout
from support import FakeSession


@pytest.fixture
def confirmed_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    state = Checkout(available=True, totals=Totals(order_total="777 ₽"), selected_items={"123": 1})
    monkeypatch.setattr(checkout, "start_checkout", lambda **_kwargs: state)
    monkeypatch.setattr(checkout.time, "sleep", lambda _delay: None)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"error": "temporary failure"},
        {"data": {}},
        {"data": {"poolingDetails": {}}},
        {"data": {"poolingDetails": {"delay": -1}}},
        {"data": {"poolingDetails": {"delay": "500"}}},
        {"data": {"poolingDetails": {"delay": float("inf")}}},
        {"data": {"poolingDetails": {"delay": True}}},
        {"data": {"createOrderResponse": {}}},
    ],
)
def test_uncertain_response_stops_after_one_submission(
    session: FakeSession, writes_on: None, confirmed_checkout: None, response: dict[str, Any]
) -> None:
    session.actions = {"createOrderV2": response}
    with pytest.raises(OzonError, match="check the orders list before retrying"):
        checkout.place_order("777 ₽", {"123": 1})
    assert len(session.performed) == 1


def test_only_an_explicit_valid_polling_instruction_resubmits(
    session: FakeSession, writes_on: None, confirmed_checkout: None
) -> None:
    responses = iter([
        {"data": {"poolingDetails": {"delay": 500}}},
        {"data": {"createOrderResponse": {"link": "?orderNumber=44563249-0900"}}},
    ])
    session.actions = {"createOrderV2": lambda: next(responses)}
    assert checkout.place_order("777 ₽", {"123": 1}).order_number == "44563249-0900"
    assert len(session.performed) == 2


def test_a_lost_reply_reports_uncertainty_without_resubmitting(
    session: FakeSession, writes_on: None, confirmed_checkout: None
) -> None:
    from ozon_mcp.errors import UpstreamError  # ruff: ignore[import-outside-top-level]

    def lost_reply() -> None:
        raise UpstreamError(0)

    session.actions = {"createOrderV2": lost_reply}
    with pytest.raises(OzonError, match="check the orders list before retrying"):
        checkout.place_order("777 ₽", {"123": 1})
    assert len(session.performed) == 1
