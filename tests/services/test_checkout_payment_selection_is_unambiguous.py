"""A method selector must not choose an arbitrary saved card or unavailable method."""

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.models.checkout import PaymentOption
from ozon_mcp.services.checkout import _match_payment_option


@pytest.mark.parametrize("wanted", ["2044", "Сбер", "5898"])
def test_matching_several_cards_requires_a_unique_selector(wanted: str) -> None:
    options = [
        PaymentOption(payment_type=2044, kind="Sberpay", label="** 5898", apply_link="/gocheckout?card_token=a"),
        PaymentOption(payment_type=2044, kind="Sberpay", label="** 5898", apply_link="/gocheckout?card_token=b"),
    ]
    with pytest.raises(OzonError, match="several payment methods"):
        _match_payment_option(options, wanted)


def test_unselectable_payment_type_reports_an_error_instead_of_crashing() -> None:
    options = [
        PaymentOption(payment_type=2044, kind="Sberpay"),
        PaymentOption(kind="OzonCard", selected=True),
    ]
    with pytest.raises(OzonError, match="no payment method"):
        _match_payment_option(options, "2044")
