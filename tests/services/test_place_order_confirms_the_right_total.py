"""Placing an order requires the full cost and the selected item quantities.

On a pay-on-delivery order today's charge is 0 ₽, so accepting only that figure
had the caller confirm "0 ₽" for an order of several thousand.
"""

from __future__ import annotations

import pytest

from ozon_mcp.errors import OrdersDisabledError, OzonError, TotalMismatchError
from ozon_mcp.models.checkout import Checkout, Totals
from ozon_mcp.services import checkout
from support import FakeSession, page

TOTAL = {
    "summary": {
        "footer": {"price": "0 ₽ сегодня"},
        "prices": [{"left": {"title": "Товары (1)"}, "right": {"price": "777 ₽"}}],
    },
    "totalPrice": 777,
}
PAYMENT = {"payments": [{"title": {"text": "Ozon Карта"}, "automatizationDescription": "OzonCard", "isSelected": True}]}
CHECKOUT_PAGE = page(total=TOTAL, paymentInfoV2=PAYMENT)
# Placing enters checkout first, which reads the cart to know that something is
# ticked at all — see test_checkout_is_entered_before_it_is_read.
CART_PAGE = page(
    cartSplit={
        "cartItems": [
            {
                "product": {"id": "3077454533", "titleColumn": {"text": "Товар"}, "priceColumn": {"price": "777 ₽"}},
                "controls": {"quantity": {"current": 1, "maximum": 5}},
                "checkbox": {"isChecked": True},
            }
        ]
    }
)
PAGES = {"/cart": CART_PAGE, "/gocheckout": CHECKOUT_PAGE}


def test_without_the_orders_gate_nothing_is_sent(session: FakeSession) -> None:
    session.pages = dict(PAGES)
    with pytest.raises(OrdersDisabledError) as raised:
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert "operator's setting" in str(raised.value)
    assert session.performed == []


def test_the_order_total_is_accepted(session: FakeSession, writes_on: None) -> None:
    session.pages = dict(PAGES)
    session.actions = {"createOrderV2": {"data": {"createOrderResponse": {"link": "?orderNumber=44563249-0900"}}}}
    placed = checkout.place_order("777 ₽", {"3077454533": 1})
    assert placed.order_number == "44563249-0900"
    assert placed.order_total == "777 ₽"


def test_todays_zero_charge_does_not_confirm_the_order(session: FakeSession, writes_on: None) -> None:
    session.pages = dict(PAGES)
    with pytest.raises(TotalMismatchError):
        checkout.place_order("0 ₽ сегодня", {"3077454533": 1})
    assert session.performed == []


def test_a_stale_total_is_refused_and_nothing_is_ordered(session: FakeSession, writes_on: None) -> None:
    session.pages = dict(PAGES)
    with pytest.raises(TotalMismatchError) as raised:
        checkout.place_order("999 ₽", {"3077454533": 1})
    assert "777 ₽" in str(raised.value)
    assert "Nothing was ordered" in str(raised.value)
    assert session.performed == []


@pytest.mark.parametrize("confirmed", ["777 ₽", "777,00 ₽", "777,0 ₽"])
def test_equivalent_money_formats_confirm_the_same_cost(session: FakeSession, writes_on: None, confirmed: str) -> None:
    session.pages = dict(PAGES)
    session.actions = {"createOrderV2": {"data": {"createOrderResponse": {"link": "?orderNumber=44563249-0900"}}}}
    assert checkout.place_order(confirmed, {"3077454533": 1}).order_total == "777 ₽"


@pytest.mark.parametrize(
    "confirmed", ["7,77 ₽", "777,01 ₽", "", "unknown", "777,1,2 ₽", "-777 ₽", "7.77 ₽", "777,0099 ₽", "7not_money77 ₽"]
)
def test_invalid_or_different_amounts_cannot_place_an_order(
    session: FakeSession, writes_on: None, confirmed: str
) -> None:
    session.pages = dict(PAGES)
    with pytest.raises(TotalMismatchError):
        checkout.place_order(confirmed, {"3077454533": 1})
    assert session.performed == []


@pytest.mark.parametrize("full_total", [None, "unknown"])
def test_missing_full_cost_never_falls_back_to_todays_charge(
    session: FakeSession, writes_on: None, monkeypatch: pytest.MonkeyPatch, full_total: str | None
) -> None:
    state = Checkout(
        available=True, totals=Totals(total="777 ₽", order_total=full_total), selected_items={"3077454533": 1}
    )
    monkeypatch.setattr(checkout, "start_checkout", lambda **_kwargs: state)
    with pytest.raises(TotalMismatchError):
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert session.performed == []


@pytest.mark.parametrize("confirmed", [{}, {"another-sku": 1}, {"3077454533": 2}, {"3077454533": True}])
def test_equal_cost_does_not_confirm_changed_items_or_quantities(
    session: FakeSession, writes_on: None, confirmed: dict[str, int]
) -> None:
    session.pages = dict(PAGES)
    with pytest.raises(OzonError, match="item quantities"):
        checkout.place_order("777 ₽", confirmed)
    assert session.performed == []


def test_unknown_item_quantities_refuse_ordering(session: FakeSession, writes_on: None) -> None:
    session.pages = dict(PAGES)
    session.pages["/cart"] = page(
        cartSplit={"cartItems": [{"product": {"id": "3077454533"}, "checkbox": {"isChecked": True}}]}
    )
    with pytest.raises(OzonError, match="incomplete"):
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert session.performed == []


def test_a_page_with_only_todays_charge_does_not_establish_the_full_cost(session: FakeSession, writes_on: None) -> None:
    session.pages = {"/cart": CART_PAGE, "/gocheckout": page(total={"summary": {"footer": {"price": "0 ₽ сегодня"}}})}
    with pytest.raises(TotalMismatchError):
        checkout.place_order("0 ₽", {"3077454533": 1})
    assert session.performed == []


@pytest.mark.parametrize("quantity", [0, -1, None])
def test_nonpositive_or_missing_cart_quantities_are_unconfirmable(
    session: FakeSession, writes_on: None, quantity: int | None
) -> None:
    session.pages = dict(PAGES)
    session.pages["/cart"] = page(
        cartSplit={
            "cartItems": [
                {
                    "product": {"id": "3077454533"},
                    "controls": {"quantity": {"current": quantity}},
                    "checkbox": {"isChecked": True},
                }
            ]
        }
    )
    with pytest.raises(OzonError, match="incomplete"):
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert session.performed == []


def test_duplicate_selected_lines_are_unconfirmable(session: FakeSession, writes_on: None) -> None:
    line = {
        "product": {"id": "3077454533"},
        "controls": {"quantity": {"current": 1}},
        "checkbox": {"isChecked": True},
    }
    session.pages = dict(PAGES)
    session.pages["/cart"] = page(cartSplit={"cartItems": [line, line]})
    with pytest.raises(OzonError, match="incomplete"):
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert session.performed == []


def test_unknown_selection_status_cannot_hide_a_possible_order_line(session: FakeSession, writes_on: None) -> None:
    session.pages = dict(PAGES)
    session.pages["/cart"] = page(
        cartSplit={
            "cartItems": [
                {
                    "product": {"id": "3077454533"},
                    "controls": {"quantity": {"current": 1}},
                    "checkbox": {"isChecked": True},
                },
                {"product": {"id": "3077454534"}, "controls": {"quantity": {"current": 1}}},
            ]
        }
    )
    with pytest.raises(OzonError, match="incomplete"):
        checkout.place_order("777 ₽", {"3077454533": 1})
    assert session.performed == []
