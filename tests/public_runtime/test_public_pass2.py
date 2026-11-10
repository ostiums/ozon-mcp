"""Pass-two offline contract tests. Live examples are synthetic unless noted.

No authenticated profile or live site is opened. Browser tests fulfill fixed
public URLs from local fixtures and abort every other network request.
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Page as AsyncPage
from playwright.sync_api import sync_playwright

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import (
    PUBLIC_PAGE_READER,
    assert_reader_is_passive,
    document_from_html,
    products_from_document,
)
from ozon_mcp.parsing.visible_product import visible_product
from ozon_mcp.services.public_catalog import _search_path, product_public, search_public
from ozon_mcp.session.public_reader import (
    PROBE,
    READINESS,
    _failure_probe,
    matches_public_url,
    read_deadlines,
    read_owned_public,
)
from public_runtime import test_public_fix as old

ROOT = Path(__file__).parent
SEARCH = "https://www.ozon.ru/search/?page=1&text=HX%20Nutrition%20Zero%20Isolate"
BRAND = "https://www.ozon.ru/brand/hx-nutrition-12345/?category_was_predicted=true"
CATEGORY = "https://www.ozon.ru/category/sportivnoe-pitanie-11635/?category_was_predicted=true"
PRODUCT = "https://www.ozon.ru/product/123456789/"
HEADING = '<div data-widget="webProductHeading"><h1>Fixture isolate 1000 g</h1></div>'


def characteristics(rows: Any) -> Any:
    return (
        '<div data-widget="webCharacteristics">'
        + "".join("<dl><dt>" + k + "</dt><dd>" + v + "</dd></dl>" for k, v in rows)
        + "</div>"
    )


def nutrient(basis: Any = "в 1 порции", protein: Any = "25.5") -> Any:
    return (
        '<div data-widget="webNutritionInfo"><div>Пищевая ценность продукта '
        + basis
        + ":</div><div><div><div>"
        + protein
        + "</div><div>белки</div></div><div><div>0</div><div>жиры</div></div></div></div>"
    )


def card(html: Any) -> Any:
    return visible_product(HEADING + html, PRODUCT)


class FakePage:
    def __init__(
        self, clock: Any, url: str = BRAND, *, ready: bool = True, empty: bool = False, probe_delay: Any = 0
    ) -> None:
        self.clock = clock
        self.url = url
        self.ready = ready
        self.empty = empty
        self.probe_delay = probe_delay
        self.calls = []

    async def bring_to_front(self) -> None:
        pass

    async def goto(self, url: Any, **kwargs: Any) -> None:
        self.calls.append("goto")
        self.clock.now += 0.1

    async def evaluate(self, script: Any) -> Any:
        sample = {
            "url": self.url,
            "documentEpoch": 1,
            "title": "PRIVATE TITLE",
            "tile_ready": self.ready,
            "product_ready": False,
            "explicit_empty": self.empty,
        }
        if script == READINESS:
            self.calls.append("readiness")
        elif script == PROBE:
            self.calls.append("failure_probe")
            self.clock.now += self.probe_delay
            sample["counts"] = {
                "paginator": 1,
                "tile_grids": 1 if self.ready else 0,
                "tile_roots": 15 if self.ready else 0,
            }
        elif script == PUBLIC_PAGE_READER:
            self.calls.append("snapshot")
            sample.update(html=old.grid() if self.ready else "", jsonLd=[], microdata=[])
        else:
            raise AssertionError("Unexpected browser script")
        return sample


class Redirects(unittest.IsolatedAsyncioTestCase):
    async def read(self, page: Any, clock: Any, total: Any = 40000) -> Any:
        return await read_owned_public(
            page,
            SEARCH,
            deadline=total / 1000,
            total_ms=total,
            check_title=lambda _: None,
            clock=clock,
            sleep=clock.sleep,
        )

    async def test_brand_grid_snapshots_immediately_without_probe(self) -> None:
        clock = old.Clock()
        page = FakePage(clock)
        doc = await self.read(page, clock)
        assert len(products_from_document(doc)) == 1
        assert doc["diagnostics"]["url_class"] == "ozon-other"
        assert "failure_probe" not in page.calls
        assert page.calls.count("snapshot") == 1
        assert clock.now < 1

    async def test_category_grid_accepted(self) -> None:
        clock = old.Clock()
        page = FakePage(clock, CATEGORY)
        assert len(products_from_document(await self.read(page, clock))) == 1

    async def test_other_public_landing_grid_accepted(self) -> None:
        clock = old.Clock()
        page = FakePage(clock, "https://www.ozon.ru/highlight/isolate-123/")
        assert len(products_from_document(await self.read(page, clock))) == 1

    async def test_rewritten_query_keys_values_and_page_no_longer_gate_ready_grid(self) -> None:
        clock = old.Clock()
        page = FakePage(clock, "https://www.ozon.ru/search/?text=NEW&sorting=rating&page=2&category_was_predicted=true")
        await self.read(page, clock)
        assert clock.now < 1
        assert "failure_probe" not in page.calls

    async def test_external_and_blank_rejected_even_with_grid(self) -> None:
        for url in [
            "https://evil.example/search/",
            "about:blank",
            "https://www.ozon.ru.evil/brand/a/",
            "http://www.ozon.ru/brand/a/",
            "https://user@www.ozon.ru/brand/a/",
        ]:
            clock = old.Clock()
            page = FakePage(clock, url)
            with pytest.raises(OzonError):
                await self.read(page, clock, total=1000)
            assert "snapshot" not in page.calls

    async def test_api_account_home_product_are_not_search_landings(self) -> None:
        for path in ["/api/foo/", "/%61pi/foo/", "/my/main/", "/cart/", "/checkout/", "/", "/product/123456789/"]:
            assert not matches_public_url("https://www.ozon.ru" + path, SEARCH)

    async def test_no_grid_no_empty_is_failure(self) -> None:
        clock = old.Clock()
        page = FakePage(clock, ready=False)
        with pytest.raises(OzonError) as exc:
            await self.read(page, clock, total=1000)
        assert "snapshot" not in page.calls
        assert page.calls.count("failure_probe") == 1
        assert "PRIVATE" not in str(exc.value)
        assert "category_was_predicted=true" not in str(exc.value)

    async def test_explicit_empty_on_brand_page_is_allowed(self) -> None:
        clock = old.Clock()
        page = FakePage(clock, ready=False, empty=True)
        assert products_from_document(await self.read(page, clock)) == []
        assert "failure_probe" not in page.calls

    async def test_failure_probe_never_spends_snapshot_reserve(self) -> None:
        assert read_deadlines(40, 40000) == (36, 37)
        clock = old.Clock()
        page = FakePage(clock, ready=False, probe_delay=1)
        with pytest.raises(OzonError):
            await self.read(page, clock)
        assert clock.now <= 37.000001
        assert page.calls.count("failure_probe") == 1
        assert "snapshot" not in page.calls

    async def test_failure_probe_hard_one_second_cap(self) -> None:
        async def hang(_: Any) -> None:
            await asyncio.Event().wait()

        start = time.monotonic()
        sample = {"url": BRAND}
        answer = await _failure_probe(SimpleNamespace(evaluate=hang), sample, deadline=start + 40)
        assert answer is sample
        assert time.monotonic() - start < 1.3
        assert time.monotonic() - start >= 0.9

    async def test_failure_probe_short_remaining_budget(self) -> None:
        async def hang(_: Any) -> None:
            await asyncio.Event().wait()

        start = time.monotonic()
        await _failure_probe(SimpleNamespace(evaluate=hang), {}, deadline=start + 0.025)
        assert time.monotonic() - start < 0.2

    async def test_failure_probe_skips_after_reserved_boundary(self) -> None:
        async def forbidden(_: Any) -> Never:  # ruff: ignore[unused-async] - async page stub
            raise AssertionError("must not evaluate")

        now = time.monotonic()
        sample = {"url": BRAND}
        assert await _failure_probe(SimpleNamespace(evaluate=forbidden), sample, deadline=now - 1) is sample

    async def test_snapshot_uses_full_remaining_budget_not_probe_partition(self) -> None:
        clock = old.Clock()
        page = FakePage(clock)
        original = page.evaluate

        async def evaluate(script: Any) -> Any:
            data = await original(script)
            if script == PUBLIC_PAGE_READER:
                clock.now = 39.5
            return data

        page.evaluate = evaluate
        await self.read(page, clock)
        assert clock.now == pytest.approx(39.5)
        assert "failure_probe" not in page.calls

    def test_scripts_are_passive_and_counts_only_on_failure_script(self) -> None:
        assert_reader_is_passive(READINESS)
        assert_reader_is_passive(PROBE)
        assert "const counts" not in READINESS
        assert "const counts" in PROBE


class SearchOrdering(unittest.TestCase):
    def test_price_sorts_absent_from_public_navigation(self) -> None:
        for sort in ["cheap", "expensive", "price", "price_desc", "popular"]:
            for category in [None, "protein-123/"]:
                q = parse_qs(urlsplit(_search_path("HX Nutrition Zero Isolate", category, sort, 1)).query)
                assert "sorting" not in q
                assert q["text"] == ["HX Nutrition Zero Isolate"]
                assert set(q) == {"page", "text"}

    def test_nonprice_requested_sort_preserved(self) -> None:
        for name, expected in [("new", "new"), ("rating", "rating"), ("discount", "discount")]:
            assert parse_qs(urlsplit(_search_path("isolate", None, name, 1)).query)["sorting"] == [expected]

    def test_sort_is_local_and_one_read_only(self) -> None:
        markup = old.grid(
            old.TILE.replace("1 234 ₽", "900 ₽")
            + old.TILE.replace("123456789", "987654321").replace("1 234 ₽", "100 ₽")
        )
        for sort, first in [("cheap", "987654321"), ("expensive", "123456789")]:
            fake = SimpleNamespace(read_public_page=lambda _path: document_from_html(markup))
            with (
                patch("ozon_mcp.services.public_catalog.get_session", return_value=fake),
                patch.object(fake, "read_public_page", wraps=fake.read_public_page) as read,
            ):
                result = search_public("isolate", None, sort, 1, None, 24)
            assert result[0].sku == first
            assert read.call_count == 1
            assert "sorting=" not in read.call_args.args[0]


class Nutrition(unittest.TestCase):  # ruff: ignore[too-many-public-methods] - independent regression cases
    def test_per_serving_and_size_in_characteristics(self) -> None:
        p = card(characteristics([("Белок: грамм в 1 порции", "25.5"), ("Размер порции, г", "30")]))
        assert p.protein_per_100g == "85 г/100 г (расчёт: 25.5 г белка / порция 30 г)"

    def test_size_in_grams_value_and_decimal_comma(self) -> None:
        p = card(characteristics([("Белки в порции", "22,5"), ("Масса порции", "30 г")]))
        assert p.protein_per_100g == "75 г/100 г (расчёт: 22.5 г белка / порция 30 г)"

    def test_explicit_size_in_nutrition_heading(self) -> None:
        assert card(nutrient("в 1 порции (30 г)")).protein_per_100g == "85 г/100 г (расчёт: 25.5 г белка / порция 30 г)"

    def test_explicit_per100_nutrient_cards(self) -> None:
        assert card(nutrient("на 100 г", "80")).protein_per_100g == "80 г/100 г (указано на странице)"

    def test_explicit_per100_characteristic(self) -> None:
        assert (
            card(characteristics([("Белки на 100 г", "82,3 г")])).protein_per_100g
            == "82.3 г/100 г (указано на странице)"
        )

    def test_explicit_preferred_over_computed(self) -> None:
        html = nutrient("на 100 г", "80") + characteristics([
            ("Белок: грамм в 1 порции", "25.5"),
            ("Размер порции, г", "30"),
        ])
        assert card(html).protein_per_100g == "80 г/100 г (указано на странице)"

    def test_per100_table_in_description(self) -> None:
        table = (
            '<div data-widget="webDescription"><table><tr><th>Показатель</th><th>На'
            " 100 г</th></tr><tr><td>Белки, г</td><td>81.7</td></tr><tr><td>Жиры</t"
            "d><td>3</td></tr></table></div>"
        )
        assert card(table).protein_per_100g == "81.7 г/100 г (указано на странице)"

    def test_table_with_two_bases_prefers_explicit(self) -> None:
        table = (
            '<div data-widget="webNutritionInfo"><table><tr><th>Показатель</th><th>'
            "На 100 г</th><th>В порции (30 г)</th></tr><tr><td>Белки</td><td>80</td"
            "><td>25.5</td></tr></table></div>"
        )
        assert card(table).protein_per_100g == "80 г/100 г (указано на странице)"

    def test_per100_table_in_characteristics(self) -> None:
        table = (
            '<div data-widget="webCharacteristics"><table><tr><th>Показатель</th><t'
            "h>На 100 г</th></tr><tr><td>Белки, г</td><td>80</td></tr></table></div"
            ">"
        )
        assert card(table).protein_per_100g == "80 г/100 г (указано на странице)"

    def test_units_in_labels_are_not_silently_assumed_grams(self) -> None:
        assert card(characteristics([("Белки, мг на 100 г", "80")])).protein_per_100g is None
        table = (
            '<div data-widget="webNutritionInfo"><table><tr><th>Показатель</th><th>'
            "На 100 г</th></tr><tr><td>Белки, %</td><td>80</td></tr></table></div>"
        )
        assert card(table).protein_per_100g is None

    def test_english_table(self) -> None:
        table = (
            '<div data-widget="webNutritionInfo"><table><tr><th>Nutrient</th><th>Pe'
            "r serving (30 g)</th></tr><tr><td>Protein (g)</td><td>24 g</td></tr></"
            "table></div>"
        )
        assert card(table).protein_per_100g == "80 г/100 г (расчёт: 24 г белка / порция 30 г)"

    def test_no_pack_division(self) -> None:
        html = nutrient() + characteristics([("Вес товара, г", "907"), ("Количество порций", "30")])
        assert card(html).protein_per_100g is None

    def test_no_title_serving_inference(self) -> None:
        html = HEADING.replace("1000 g", "порция 30 г") + nutrient()
        assert visible_product(html, PRODUCT).protein_per_100g is None

    def test_missing_protein_or_size(self) -> None:
        assert card(characteristics([("Размер порции, г", "30")])).protein_per_100g is None
        assert card(nutrient()).protein_per_100g is None

    def test_missing_size_unit_not_assumed(self) -> None:
        assert card(nutrient() + characteristics([("Размер порции", "30")])).protein_per_100g is None

    def test_zero_protein(self) -> None:
        assert card(nutrient("в порции (30 г)", "0")).protein_per_100g == "0 г/100 г (расчёт: 0 г белка / порция 30 г)"

    def test_rounding_is_deterministic(self) -> None:
        assert (
            card(nutrient("в порции (30 г)", "25")).protein_per_100g
            == "83.33 г/100 г (расчёт: 25 г белка / порция 30 г)"
        )

    def test_invalid_units_bounds_and_ranges(self) -> None:
        for value in ["25000 мг", "25–30 г", "-1", "101", "85%"]:
            assert card(nutrient("на 100 г", value)).protein_per_100g is None
        assert card(nutrient("в порции (0 г)", "25")).protein_per_100g is None
        assert card(nutrient("в порции (20 г)", "25")).protein_per_100g is None

    def test_conflicting_sizes_or_protein_fail_closed(self) -> None:
        assert (
            card(nutrient() + characteristics([("Размер порции, г", "30"), ("Вес порции, г", "40")])).protein_per_100g
            is None
        )
        assert card(nutrient("в порции (30 г)", "25") + nutrient("в порции (30 г)", "20")).protein_per_100g is None

    def test_conflicting_explicit_values_do_not_fall_back_to_computed(self) -> None:
        html = nutrient("на 100 г", "80") + nutrient("на 100 г", "82") + nutrient("в порции (30 г)", "24")
        assert card(html).protein_per_100g is None

    def test_hidden_nutrition_does_not_contribute(self) -> None:
        hidden = "<div hidden>" + nutrient("на 100 г", "80") + "</div>"
        assert card(hidden).protein_per_100g is None

    def test_unscoped_recommendation_table_does_not_contribute(self) -> None:
        html = "<table><tr><th>Показатель</th><th>На 100 г</th></tr><tr><td>Белки</td><td>80</td></tr></table>"
        assert card(html).protein_per_100g is None


class Seller(unittest.TestCase):
    def widget(self, body: Any) -> Any:
        return '<div data-widget="webCurrentSeller">' + body + "</div>"

    def test_present(self) -> None:
        assert (
            card(self.widget('<a href="/seller/nutrition-shop-123/">Nutrition Shop</a><span>4.9</span>')).seller
            == "Nutrition Shop"
        )

    def test_absent(self) -> None:
        assert card(nutrient()).seller is None

    def test_duplicate_same_name_same_seller_is_unambiguous(self) -> None:
        a = '<a href="/seller/shop-123/">Shop</a>'
        assert card(self.widget(a + a)).seller == "Shop"

    def test_ambiguous_name_or_seller_stays_null(self) -> None:
        for b in ['<a href="/seller/shop-234/">Shop</a>', '<a href="/seller/shop-123/">Another name</a>']:
            assert card(self.widget('<a href="/seller/shop-123/">Shop</a>' + b)).seller is None

    def test_hidden_external_unscoped_sellers_excluded(self) -> None:
        a = '<a href="/seller/shop-123/">Shop</a>'
        for html in [
            "<div hidden>" + self.widget(a) + "</div>",
            self.widget(a.replace("/seller/shop-123/", "https://evil.example/seller/shop/")),
            a,
            self.widget("Продавец неизвестен 4.9"),
        ]:
            assert card(html).seller is None

    def test_absolute_ozon_seller_link(self) -> None:
        assert card(self.widget('<a href="https://www.ozon.ru/seller/shop-123/?source=card">Shop</a>')).seller == "Shop"

    def test_product_dto_keeps_new_fields(self) -> None:
        html = HEADING + nutrient("на 100 г", "80") + self.widget('<a href="/seller/shop-123/">Shop</a>')
        doc = document_from_html(html)
        doc["url"] = PRODUCT
        with patch(
            "ozon_mcp.services.public_catalog.get_session", return_value=SimpleNamespace(read_public_page=lambda _: doc)
        ):
            p = product_public("123456789")
        assert p.seller == "Shop"
        assert p.protein_per_100g == "80 г/100 г (указано на странице)"


class OfflineBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.context = cls.browser.new_context(offline=True)
        cls.context.route("**/*", lambda route: route.abort())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.close()
        cls.browser.close()
        cls.pw.stop()

    def test_real_brand_redirect_snapshots_without_probe(self) -> None:

        page = self.context.new_page()
        seen = []

        def route(route: Any) -> None:
            if route.request.url == SEARCH:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<script>location.replace(" + json.dumps(BRAND) + ")</script>",
                )
            elif route.request.url == BRAND:
                route.fulfill(status=200, content_type="text/html", body=old.grid())
            else:
                route.abort()

        page.route("**/*", route)
        ap = AsyncPage(page._impl_obj)

        class Recorder:
            async def bring_to_front(self) -> Any:
                return await ap.bring_to_front()

            async def goto(self, *a: Any, **kw: Any) -> Any:
                return await ap.goto(*a, **kw)

            async def evaluate(self, script: Any) -> Any:
                seen.append(script)
                return await ap.evaluate(script)

        start = time.monotonic()
        data = page._sync(
            read_owned_public(Recorder(), SEARCH, deadline=start + 4, total_ms=4000, check_title=lambda _: None)
        )
        assert time.monotonic() - start < 2
        assert PROBE not in seen
        assert seen.count(PUBLIC_PAGE_READER) == 1
        assert data["url"] == BRAND
        assert len(products_from_document(data)) == 1
        page.close()

    def test_readiness_ignores_grid_lookalikes_and_hidden_grid(self) -> None:
        page = self.context.new_page()
        for html in [old.TILE, '<div style="display:none">' + old.grid() + "</div>"]:
            page.set_content(html)
            assert not page.evaluate(READINESS)["tile_ready"]
        page.close()

    def test_seller_visibility_and_nutrition_survive_real_snapshot(self) -> None:
        page = self.context.new_page()
        html = (
            HEADING
            + nutrient("в порции (30 г)", "24")
            + '<div data-widget="webCurrentSeller"><a href="/seller/shop-123/">Shop</a></div>'
        )
        page.set_content(html)
        doc = page.evaluate(PUBLIC_PAGE_READER)
        doc["url"] = PRODUCT
        p = products_from_document(doc)[0]
        assert p.seller == "Shop"
        assert "80 г/100 г" in p.protein_per_100g
        page.set_content(
            HEADING
            + (
                '<section style="display:none"><div data-widget="webCurrentSeller"><a h'
                'ref="/seller/other-234/">Hidden Shop</a></div></section>'
            )
        )
        doc = page.evaluate(PUBLIC_PAGE_READER)
        doc["url"] = PRODUCT
        assert products_from_document(doc)[0].seller is None
        page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
