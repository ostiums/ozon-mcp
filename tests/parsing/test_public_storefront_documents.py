"""Synthetic public pages. Live Ozon DOM was not used."""

from __future__ import annotations

from pathlib import Path

import pytest

from ozon_mcp.parsing.public_storefront import (
    DEADLINE_VISIBILITY,
    PUBLIC_PAGE_READER,
    assert_reader_is_passive,
    document_from_html,
    format_public_diagnostics,
    products_from_document,
    snapshot_reserve_ms,
    step_budget_ms,
)
from ozon_mcp.settings import OzonSettings

FIXTURE = Path("tests/fixtures/public-storefront-search.html").read_text(encoding="utf-8")


def test_public_read_budget_is_shared_not_two_full_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_PUBLIC_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("OZON_BROWSER_TIMEOUT", raising=False)
    settings = OzonSettings()
    assert settings.public_read_timeout == 20
    assert settings.browser_timeout == 60
    total = 20_000
    navigation = step_budget_ms(total, 0)
    readiness = step_budget_ms(total, navigation)
    assert navigation == 17_000
    assert readiness == 0
    assert navigation + readiness + snapshot_reserve_ms(total) == total
    assert navigation + step_budget_ms(total, 0) != 120_000
    after_eight_seconds = step_budget_ms(total, 8_000)
    assert after_eight_seconds == 9_000
    assert 8_000 + after_eight_seconds + snapshot_reserve_ms(total) == total


def test_reader_does_not_touch_secrets_or_the_network() -> None:
    assert_reader_is_passive()
    assert_reader_is_passive(DEADLINE_VISIBILITY)
    assert "innerHTML" not in DEADLINE_VISIBILITY
    assert "cookie" not in DEADLINE_VISIBILITY.lower()
    assert "querySelectorAll" in PUBLIC_PAGE_READER
    assert "application/ld+json" in PUBLIC_PAGE_READER
    assert "script, style, noscript, template, iframe" in PUBLIC_PAGE_READER


def test_deadline_probe_line_has_counts_and_no_page_text() -> None:
    line = format_public_diagnostics(
        {
            "title": "HX " + ("x" * 200),
            "url_class": "ozon-search",
            "tile_selector_ready": False,
            "deadline_probe": "measured",
            "bring_to_front": "ok",
            "document_visibility": "hidden",
            "document_focused": False,
            "paginator": 1,
            "tile_grids": 1,
            "tile_roots_attached": 3,
            "product_anchors_attached": 3,
            "title_spans_attached": 3,
            "title_spans_visible": 0,
            "first_title_visible": False,
            "counts": {"html_present": False, "tile_roots": 0},
            "leak": "https://www.ozon.ru/search/?text=secret",
        }
    )
    assert "deadline_probe=measured" in line
    assert "document_visibility=hidden" in line
    assert "title_spans_visible=0" in line
    assert "tile_roots=0" not in line
    assert "https://" not in line
    assert "secret" not in line
    assert len(line.split("title='", 1)[1].split("'", 1)[0]) == 120
    skipped = format_public_diagnostics(
        {"title": "present", "url_class": "ozon-search", "tile_selector_ready": False, "deadline_probe": "skipped"}
    )
    assert "dom_counts=not_taken" in skipped
    assert "tile_roots=" not in skipped


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
