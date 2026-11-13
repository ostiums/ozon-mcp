"""Offline regression suite; no Ozon/network requests or authenticated profile access."""

import asyncio
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from curl_cffi.const import CurlOpt
from curl_cffi.requests.utils import set_curl_options
from playwright.async_api import Error
from playwright.sync_api import sync_playwright

from ozon_mcp.errors import OzonError, UpstreamError
from ozon_mcp.parsing.public_storefront import (
    PUBLIC_PAGE_READER,
    assert_reader_is_passive,
    document_from_html,
    format_public_diagnostics,
    products_from_document,
    query_key_names,
    step_budget_ms,
)
from ozon_mcp.parsing.visible_cards import PUBLIC_TILE_SELECTOR, PUBLIC_TILE_STAGES, visible_products
from ozon_mcp.parsing.visible_product import visible_product
from ozon_mcp.services.public_catalog import _sort_key, _tile
from ozon_mcp.session.phone_form import normalize_phone_input as _normalize_phone_input
from ozon_mcp.session.public_reader import PROBE, matches_public_url, read_owned_public
from ozon_mcp.session.transport import OzonSession, _is_auth_frame
from ozon_mcp.settings import OzonSettings

_HOME_PROXY = "http://proxy.example:9"

ROOT = Path(__file__).parent
URL = "https://www.ozon.ru/search/?page=1&text=HX%20Nutrition%20Zero%20Isolate&sorting=price"
CANON = (
    "https://www.ozon.ru/search/?brand_was_predicted=true&deny_category_pre"
    "diction=true&from_global=true&sorting=price&text=HX+Nutrition+Zero+Iso"
    "late"
)
TITLE = "HX Nutrition Zero Isolate"
TILE = (
    (
        '<div class="tile-root" data-index="0"><a target="_blank" href="/produc'
        't/isolate-123456789/"><span class="tsBody500Medium">'
    )
    + TITLE
    + '</span></a><span class="tsHeadline500Medium">1 234 ₽</span></div>'
)


def grid(tile: str = TILE, *, widget: bool = True) -> Any:
    return (
        '<div data-widget="infiniteVirtualPaginator"><div '
        + ('data-widget="tileGridDesktop"' if widget else 'class="tileGridDesktop"')
        + ">"
        + tile
        + "</div></div>"
    )


class Parsing(unittest.TestCase):
    def test_live_grid_fixture(self) -> None:
        products = products_from_document(document_from_html((ROOT / "observed-search.html").read_text()))
        assert [p.sku for p in products] == ["4450965452", "4450964023", "4450964031"]
        assert all(p.title and p.displayed_price and p.url for p in products)

    def test_both_observed_grid_contracts(self) -> None:
        for widget in (True, False):
            assert len(visible_products(grid(widget=widget))[0]) == 1

    def test_same_element_is_not_descendant(self) -> None:
        html = '<div data-widget="infiniteVirtualPaginator" class="tileGridDesktop">' + TILE + "</div>"
        assert visible_products(html) == ([], False)

    def test_ambiguous_links_skip(self) -> None:
        html = grid(TILE.replace("</a>", '</a><a target="_blank" href="/product/987654321/">Other</a>'))
        assert visible_products(html) == ([], False)

    def test_ambiguous_title_price_skip(self) -> None:
        for duplicate in (
            '<span class="tsHeadline500Medium">2 000 ₽</span>',
            '<a target="_blank" href="/product/isolate-123456789/"><span class="tsBody500Medium">Other</span></a>',
        ):
            assert visible_products(grid(TILE.replace("</div>", duplicate + "</div>"))) == ([], False)

    def test_missing_fields_skip(self) -> None:
        for cls in ("tsHeadline500Medium", "tsBody500Medium"):
            assert visible_products(grid().replace(cls, "absent")) == ([], False)

    def test_nonrelative_link_not_accepted(self) -> None:
        assert visible_products(grid().replace('href="/product/', 'href="https://www.ozon.ru/product/')) == ([], False)

    def test_hidden_void_nodes_do_not_swallow_later_tiles(self) -> None:
        html = grid("<div hidden><img><span>hidden</span></div>" + TILE)
        assert len(visible_products(html)[0]) == 1

    def test_hidden_tiles_skipped(self) -> None:
        for attr in ("hidden", 'aria-hidden="true"', 'style="display: none"'):
            assert visible_products(grid(TILE.replace('data-index="0"', 'data-index="0" ' + attr))) == ([], False)

    def test_invalid_grid_not_rescued_by_empty_schema(self) -> None:
        d = document_from_html(grid().replace("tsHeadline500Medium", "missing"))
        d["jsonLd"] = ['{"@type":"ItemList","itemListElement":[]}']
        assert products_from_document(d) is None

    def test_no_grid_is_failure(self) -> None:
        assert products_from_document(document_from_html("<div>Loading</div>")) is None

    def test_explicit_empty(self) -> None:
        assert products_from_document({"explicit_empty": True}) == []

    def test_price_sort_uses_displayed(self) -> None:
        low = visible_products(grid().replace("1 234 ₽", "100 ₽"))[0][0]
        high = visible_products(grid())[0][0]
        assert min([_tile(high), _tile(low)], key=lambda t: _sort_key(t, descending=False)).displayed_price == "100 ₽"

    def test_product_fixture_with_no_schema(self) -> None:
        p = visible_product((ROOT / "observed-product.html").read_text(), "https://www.ozon.ru/product/4450965452/")
        assert p.pack_weight == "907 г"
        assert p.price == "7\u2009519\u2009₽"
        assert p.price_regular == "8\u2009355\u2009₽"
        assert "в 1 порции" in p.nutrition
        assert "25.5" in p.nutrition
        assert p.protein_per_100g is None

    def test_wrong_product_sku_fails(self) -> None:
        assert (
            visible_product((ROOT / "observed-product.html").read_text(), "https://www.ozon.ru/product/999999999/")
            is None
        )

    def test_no_title_no_product(self) -> None:
        assert (
            visible_product(
                (ROOT / "observed-product.html").read_text().replace("webProductHeading", "missing"),
                "https://www.ozon.ru/product/4450965452/",
            )
            is None
        )

    def test_ambiguous_weight_and_prices_are_missing(self) -> None:
        html = (ROOT / "observed-product.html").read_text()
        extra = '<div data-widget="webCharacteristics"><dl><dt>Вес товара, г</dt><dd>800</dd></dl></div>'
        p = visible_product(html + extra, "https://www.ozon.ru/product/4450965452/")
        assert p.pack_weight is None
        # Duplicated price role with a different amount must not choose arbitrarily.
        p = visible_product(
            html
            + (
                '<div data-widget="webPrice"><div><span class="tsHeadline600Large">1 00'
                '0 ₽</span><span class="tsBody400Small">С банками</span></div></div>'
            ),
            "https://www.ozon.ru/product/4450965452/",
        )
        assert p.price is None
        assert p.displayed_price is None

    def test_missing_nutrition_is_missing(self) -> None:
        html = (ROOT / "observed-product.html").read_text().replace("webNutritionInfo", "unknown")
        p = visible_product(html, "https://www.ozon.ru/product/4450965452/")
        assert p.nutrition is None
        assert p.protein_per_100g is None

    def test_reader_passive(self) -> None:
        assert_reader_is_passive()
        assert_reader_is_passive(PROBE)


class Safety(unittest.TestCase):
    def test_phone_normalization(self) -> None:
        for value in ("+7 (912) 345-67-89", "8 (912) 345-67-89", "9123456789"):
            assert _normalize_phone_input(value, separate_russian_prefix=True) == "9123456789"
        for value in ("79123456789", "+791234", "+19123456789", "912)3456789", "abc"):
            with pytest.raises(OzonError):
                _normalize_phone_input(value, separate_russian_prefix=True)
        with pytest.raises(OzonError):
            _normalize_phone_input("+79123456789", separate_russian_prefix=False)

    def test_auth_origin(self) -> None:
        assert _is_auth_frame("https://www.ozon.ru/otp-lite")
        for url in (
            "https://evil.example/otp-lite",
            "https://www.ozon.ru.evil/ozonid-lite",
            "http://www.ozon.ru/otp-lite",
        ):
            assert not _is_auth_frame(url)

    def test_network_title_is_not_captcha(self) -> None:
        s = OzonSession()
        with pytest.raises(RuntimeError, match="network or connection error") as e:
            s._raise_for_antibot_challenge("Похоже, нет соединения")
        assert "Похоже" not in str(e.value)
        with pytest.raises(RuntimeError, match="challenge"):
            s._raise_for_antibot_challenge("Проверка безопасности")

    def test_safe_diagnostics(self) -> None:
        for title, expected in [("personal query", "present"), ("", "empty"), ("about:blank", "about:blank")]:
            line = format_public_diagnostics({"title": title, "url_class": "ozon-search"})
            assert "title='" + expected + "'" in line
            assert "personal query" not in line
        assert query_key_names("https://www.ozon.ru/search/?text=SECRET&sorting=SECRET")[0] == "sorting,text"

    def test_302_never_followed_or_retried(self) -> None:
        s = OzonSession()
        s._http = Mock()
        s._headers = {"accept": "application/json"}
        s._http.request.return_value = SimpleNamespace(
            status_code=302, text="", headers={"Location": "/api/DO-NOT-FOLLOW?token=SECRET"}
        )
        s._ensure_http = Mock()
        s.save_state = Mock()
        s._rebootstrap = Mock()
        with pytest.raises(UpstreamError) as e:
            s._request("GET", "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/")
        assert s._http.request.call_count == 1
        assert not s._http.request.call_args.kwargs["allow_redirects"]
        s._rebootstrap.assert_not_called()
        assert "api-path" in str(e.value)
        assert "SECRET" not in str(e.value)

    def test_proxy_option_builder_ignores_environment(self) -> None:

        c = Mock()
        c.impersonate.return_value = 0
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://bad.invalid:1080",
                "NO_PROXY": "*",
                "https_proxy": "http://bad.invalid:1080",
                "no_proxy": "*",
            },
        ):
            set_curl_options(
                c,
                "GET",
                "https://www.ozon.ru/search/",
                proxy=_HOME_PROXY,
                curl_options={CurlOpt.NOPROXY: ""},
                allow_redirects=False,
                params_list=[None, None],
                headers_list=[None, None],
                cookies_list=[None, None],
                proxies_list=[None, None],
                verify_list=[True, True],
            )
        opts = {call.args[0]: call.args[1] for call in c.setopt.call_args_list}
        assert opts[CurlOpt.PROXY] == _HOME_PROXY
        assert opts[CurlOpt.HTTPPROXYTUNNEL] == 1
        assert not opts[CurlOpt.NOPROXY]

    def test_public_launch_delegates_profile_ownership_without_importing_state(self) -> None:

        s = OzonSession()
        s._profile = MagicMock()
        (s._profile / "Default").exists.return_value = True
        s._seed_from_state = Mock()
        s._clear_stale_lock = Mock()
        context = Mock()
        context.pages = [Mock()]
        engine = Mock()
        engine.chromium.launch_persistent_context.return_value = context
        with (
            patch(
                "ozon_mcp.session.transport.get_settings",
                return_value=OzonSettings(storefront="public", proxy_url=_HOME_PROXY, proxy_remote_dns=True),
            ),
            patch("ozon_mcp.session.transport.sync_playwright") as pw,
            patch("ozon_mcp.session.transport._make_private") as private,
            patch("ozon_mcp.session.transport.acquire_browser") as acquire,
        ):
            pw.return_value.start.return_value = engine
            acquire.return_value.attach.return_value = (context, context.pages[0])
            s._launch_browser()
            private.assert_not_called()
            s._seed_from_state.assert_not_called()
            s._clear_stale_lock.assert_not_called()
            context.pages[0].goto.assert_not_called()
        assert acquire.call_args.args[1]["proxy"] == {"server": _HOME_PROXY}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> Any:
        return self.now

    async def sleep(self, seconds: Any) -> None:
        self.now += seconds


class Page:
    def __init__(self, clock: Any, samples: Any = None) -> None:
        self.clock = clock
        self.calls = []
        self.samples = samples
        self.i = 0

    async def bring_to_front(self) -> None:
        self.calls.append("front")

    async def goto(self, url: Any, **kw: Any) -> None:
        self.calls.append(("goto", kw))
        self.clock.now += 0.1

    async def evaluate(self, script: Any) -> Any:
        self.calls.append("snapshot" if script == PUBLIC_PAGE_READER else "probe")
        sample = {
            "url": CANON,
            "documentEpoch": 1,
            "title": "private query title",
            "counts": {},
            "tile_ready": True,
            "product_ready": False,
        }
        if self.samples:
            sample.update(self.samples[min(self.i, len(self.samples) - 1)])
            self.i += 1
        if script == PUBLIC_PAGE_READER:
            sample["html"] = grid()
        return sample


class Budget(unittest.IsolatedAsyncioTestCase):
    def test_url_rewrite_matching(self) -> None:
        assert matches_public_url(CANON, URL)
        assert matches_public_url(CANON.replace("HX+", "OTHER+"), URL)
        assert matches_public_url(CANON.replace("sorting=price", "sorting=rating"), URL)
        assert not matches_public_url(CANON.replace("www.ozon.ru", "evil.example"), URL)
        assert matches_public_url(
            "https://www.ozon.ru/product/title-123456789/", "https://www.ozon.ru/product/123456789/"
        )
        assert not matches_public_url(
            "https://www.ozon.ru/product/title-987654321/", "https://www.ozon.ru/product/123456789/"
        )

    def test_shared_clock_math(self) -> None:
        assert step_budget_ms(40000, 0) == 37000
        assert step_budget_ms(40000, 35000) == 2000
        assert step_budget_ms(40000, 40000) == 0

    async def test_url_settles_on_same_page(self) -> None:
        clock = Clock()
        page = Page(clock, [{"url": URL}, {"url": CANON}])
        data = await read_owned_public(
            page, URL, deadline=2, total_ms=2000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
        )
        assert data["url"] == CANON
        assert page.calls.count("snapshot") == 1
        assert clock.now >= 0.55
        assert next(c for c in page.calls if isinstance(c, tuple))[1]["wait_until"] == "commit"

    async def test_product_readiness_is_not_search_readiness(self) -> None:
        url = "https://www.ozon.ru/product/123456789/"
        clock = Clock()
        page = Page(clock, [{"url": url, "tile_ready": False, "product_ready": True}])
        data = await read_owned_public(
            page, url, deadline=2, total_ms=2000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
        )
        assert data["url"] == url

    async def test_same_url_new_document_restarts_settle(self) -> None:
        clock = Clock()
        page = Page(clock, [{"documentEpoch": 1}, {"documentEpoch": 2}])
        await read_owned_public(
            page, URL, deadline=2, total_ms=2000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
        )
        assert clock.now >= 0.55

    async def test_snapshot_navigation_race_retries_within_budget(self) -> None:
        clock = Clock()
        page = Page(clock)
        old = page.evaluate
        changed = False

        async def evaluate(script: Any) -> Any:
            nonlocal changed
            data = await old(script)
            if script == PUBLIC_PAGE_READER and not changed:
                data["documentEpoch"] = 2
                changed = True
            return data

        page.evaluate = evaluate
        await read_owned_public(
            page, URL, deadline=3, total_ms=3000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
        )
        assert page.calls.count("snapshot") == 2

    async def test_missing_tiles_no_snapshot_and_safe_diagnostics(self) -> None:
        clock = Clock()
        page = Page(clock, [{"tile_ready": False}])
        with pytest.raises(OzonError) as e:
            await read_owned_public(
                page, URL, deadline=1, total_ms=1000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
            )
        assert "snapshot" not in page.calls
        assert clock.now <= 1
        assert "private query" not in str(e.value)
        assert "HX" not in str(e.value)

    async def test_hanging_evaluate_is_hard_bounded(self) -> None:
        page = SimpleNamespace(bring_to_front=AsyncMock(), goto=AsyncMock())

        async def hang(script: Any) -> None:
            await asyncio.Event().wait()

        page.evaluate = hang
        start = time.monotonic()
        with pytest.raises(OzonError):
            await read_owned_public(page, URL, deadline=start + 0.04, total_ms=40, check_title=lambda _title: None)
        assert time.monotonic() - start < 0.3

    async def test_destroyed_context_retries_passive_read_only(self) -> None:

        clock = Clock()
        page = Page(clock)
        old = page.evaluate
        first = True

        async def evaluate(script: Any) -> Any:
            nonlocal first
            if first:
                first = False
                raise Error("Execution context was destroyed, most likely because of a navigation")
            return await old(script)

        page.evaluate = evaluate
        data = await read_owned_public(
            page, URL, deadline=2, total_ms=2000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
        )
        assert data["url"] == CANON
        assert sum(isinstance(c, tuple) for c in page.calls) == 1

    async def test_navigation_spends_same_budget(self) -> None:
        clock = Clock()
        page = Page(clock)

        async def goto(*a: Any, **kw: Any) -> None:  # ruff: ignore[unused-async] - async page stub
            clock.now = 0.9

        page.goto = goto
        with pytest.raises(OzonError):
            await read_owned_public(
                page, URL, deadline=1, total_ms=1000, check_title=lambda _title: None, clock=clock, sleep=clock.sleep
            )
        assert "snapshot" not in page.calls
        assert clock.now <= 1


class BrowserContract(unittest.TestCase):
    """Real selector/JS fixture checks in one empty temporary offline browser."""

    @classmethod
    def setUpClass(cls) -> None:

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.context = cls.browser.new_context()
        cls.context.route("**/*", lambda route: route.abort())
        cls.page = cls.context.new_page()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.close()
        cls.browser.close()
        cls.pw.stop()

    def test_css_parser_parity(self) -> None:
        cases = [
            grid(),
            grid(widget=False),
            '<div data-widget="infiniteVirtualPaginator" class="tileGridDesktop">' + TILE + "</div>",
            TILE,
        ]
        for html in cases:
            self.page.set_content(html)
            assert self.page.locator(PUBLIC_TILE_SELECTOR).count() == len(visible_products(html)[0])
            assert (
                self.page.evaluate(PROBE)["counts"]["title_spans"]
                == self.page.locator(PUBLIC_TILE_STAGES[-1][1]).count()
            )

    def test_live_fixture_css(self) -> None:
        self.page.set_content((ROOT / "observed-search.html").read_text())
        assert self.page.locator(PUBLIC_TILE_SELECTOR).count() == 3

    def test_reader_prunes_hidden_product_nodes_and_account_header(self) -> None:
        html = "<header>PRIVATE ACCOUNT</header>" + grid(
            TILE.replace("1 234 ₽", '<b style="display:none">PRIVATE</b>1 234 ₽')
        )
        self.page.set_content(html)
        d = self.page.evaluate(PUBLIC_PAGE_READER)
        assert "PRIVATE" not in d["html"]
        assert len(products_from_document(d)) == 1

    def test_product_js_reader_keeps_observed_fields(self) -> None:
        self.page.set_content((ROOT / "observed-product.html").read_text())
        d = self.page.evaluate(PUBLIC_PAGE_READER)
        d["url"] = "https://www.ozon.ru/product/4450965452/"
        assert products_from_document(d)[0].pack_weight == "907 г"


if __name__ == "__main__":
    unittest.main(verbosity=2)
