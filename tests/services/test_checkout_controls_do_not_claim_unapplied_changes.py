"""Missing checkout controls cannot be mistaken for an already applied option."""

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.models.checkout import Checkout, PayAfterReceipt, PaymentOption, PointsOption
from ozon_mcp.services import checkout


def test_disabled_points_choice_is_not_treated_as_applied() -> None:
    state = Checkout(points=[PointsOption(amount=100, selected=False)])
    with pytest.raises(OzonError, match="not selectable"):
        checkout._set_points(state, 100)


def test_missing_switch_does_not_silently_keep_the_opposite_state() -> None:
    state = Checkout(pay_after_receipt=PayAfterReceipt(available=True, enabled=False))
    with pytest.raises(OzonError, match="not selectable"):
        checkout._set_pay_after_receipt(state, enabled=True)


def test_missing_payment_control_is_not_treated_as_selected(writes_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    state = Checkout(available=True, payment_options=[PaymentOption(kind="Sberpay", selected=False)])
    monkeypatch.setattr(checkout, "get_checkout", lambda: state)
    monkeypatch.setattr(checkout, "_read", lambda: state)
    with pytest.raises(OzonError, match="not selectable"):
        checkout.configure_checkout(payment="Сбер")
