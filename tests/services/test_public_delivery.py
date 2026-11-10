"""Delivery in public mode never falls through to composer."""

from unittest.mock import Mock

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import document_from_html
from ozon_mcp.services import catalog, public_catalog
from ozon_mcp.settings import OzonSettings

URL = "https://www.ozon.ru/product/185618681/"
HEADING = '<div data-widget="webProductHeading"><h1>Test product</h1></div>'


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<div data-widget="webDelivery">Доставим завтра</div>', "Доставим завтра"),
        ('<div data-widget="webDelivery" hidden>Доставим завтра</div>', None),
        ("<header>Доставим завтра</header>", None),
        (
            '<div data-widget="webDelivery">Доставим завтра</div><div data-widget="webDelivery">Доставка позже</div>',
            None,
        ),
        ("", None),
    ],
)
def test_public_delivery_is_scoped_optional_and_does_not_call_api(
    monkeypatch: pytest.MonkeyPatch, markup: str, expected: str | None
) -> None:
    settings = OzonSettings(storefront="public")
    monkeypatch.setattr(public_catalog, "get_settings", lambda: settings)
    document = document_from_html(HEADING + markup)
    document["url"] = URL
    session = Mock()
    session.read_public_page.return_value = document
    monkeypatch.setattr(public_catalog, "get_session", lambda: session)
    estimate = catalog.delivery_estimate("185618681")
    assert estimate.delivery == expected
    session.read_public_page.assert_called_once_with("/product/185618681/")
    session.fetch.assert_not_called()
    session.widget_state.assert_not_called()


def test_wrong_product_does_not_get_a_delivery_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Mock()
    session.read_public_page.return_value = {"url": URL, "html": "<p>Not a product</p>"}
    monkeypatch.setattr(public_catalog, "get_session", lambda: session)
    with pytest.raises(OzonError, match="requested SKU"):
        public_catalog.delivery_public("185618681")
