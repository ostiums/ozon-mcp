"""Synthetic public pages. Live Ozon DOM was not used."""

from __future__ import annotations

from pathlib import Path

import pytest

from ozon_mcp.parsing.public_storefront import (
    PUBLIC_PAGE_READER,
    assert_reader_is_passive,
    document_from_html,
    products_from_document,
)

FIXTURE = Path("tests/fixtures/public-storefront-search.html").read_text(encoding="utf-8")


def test_reader_does_not_touch_secrets_or_the_network() -> None:
    assert_reader_is_passive()
    assert "querySelectorAll" in PUBLIC_PAGE_READER
    assert "application/ld+json" in PUBLIC_PAGE_READER
    assert "script, style, noscript, template, iframe" in PUBLIC_PAGE_READER


def test_prices_stay_on_their_own_products() -> None:
    products = products_from_document(document_from_html(FIXTURE))
    assert products is not None
    by_sku = {product.sku: product for product in products}
    vanilla = by_sku["1600000001"]
    chocolate = by_sku["1600000002"]
    assert vanilla.title == "Zero Isolate ваниль"
    assert vanilla.price == "2499"
    assert vanilla.url == "https://www.ozon.ru/product/zero-isolate-vanil-1600000001/"
    assert vanilla.available is True
    assert [(item.name, item.value) for item in vanilla.characteristics] == [("Белки", "90 г")]
    assert vanilla.variants[0].name == "Вкус"
    assert vanilla.variants[0].options[0].sku == "1600000002"
    assert vanilla.variants[0].options[0].label == "Шоколад"
    assert chocolate.title == "Zero Isolate шоколад"
    assert chocolate.price is None
    assert "10 ₽" not in {vanilla.price, chocolate.price}


def test_missing_title_and_price_stay_missing() -> None:
    document = {
        "jsonLd": [
            """
            {"@type":"Product","sku":"1600000003",
             "url":"https://www.ozon.ru/product/plain-1600000003/"}
            """
        ]
    }
    products = products_from_document(document)
    assert products is not None
    assert products[0].title is None
    assert products[0].price is None
    assert products[0].characteristics == []
    assert products[0].variants == []


def test_foreign_and_conflicting_identity_are_dropped() -> None:
    document = {
        "jsonLd": [
            """
            {"@type":"ItemList","itemListElement":[
              {"@type":"Product","name":"Чужой","sku":"1600000004",
               "url":"https://evil.example/product/x-1600000004/","offers":{"price":"1"}},
              {"@type":"Product","name":"Не тот sku","sku":"1600000005",
               "url":"https://www.ozon.ru/product/x-1600000006/","offers":{"price":"2"}}
            ]}
            """
        ]
    }
    assert products_from_document(document) == []


def test_explicit_empty_list_is_empty_and_a_bare_page_is_undeclared() -> None:
    empty = products_from_document({"jsonLd": ['{"@type":"ItemList","itemListElement":[]}']})
    assert empty == []
    assert products_from_document({"jsonLd": [], "microdata": []}) is None


def test_microdata_price_is_not_taken_from_another_product() -> None:
    document = {
        "microdata": [
            {
                "type": "https://schema.org/Product",
                "nested": False,
                "props": {
                    "name": "Первый",
                    "sku": "1600000007",
                    "url": "https://www.ozon.ru/product/pervyy-1600000007/",
                    "price": "100",
                },
            },
            {
                "type": "https://schema.org/Product",
                "nested": False,
                "props": {
                    "name": "Второй",
                    "sku": "1600000008",
                    "url": "https://www.ozon.ru/product/vtoroy-1600000008/",
                    "price": "800",
                },
            },
        ]
    }
    products = products_from_document(document)
    assert products is not None
    assert {product.sku: product.price for product in products} == {"1600000007": "100", "1600000008": "800"}


def test_passive_reader_rejects_a_script_that_reads_cookies() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        assert_reader_is_passive("() => document.cookie")
