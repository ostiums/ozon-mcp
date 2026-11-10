"""Cold public startup must not fetch relative to about:blank or import auth."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError, async_playwright

from ozon_mcp.constants import COMPOSER_URL, HOME_URL
from ozon_mcp.errors import OzonError, SessionExpiredError, UpstreamError
from ozon_mcp.session.browser_account import ACCOUNT_FETCH, account_origin, request_on_owned_page
from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import OzonSettings


def owned_page(url: str = "about:blank") -> SimpleNamespace:
    page = SimpleNamespace(url=url, evaluate=AsyncMock(return_value={"status": 200, "text": "{}"}))

    async def goto(target: str, **_kwargs: object) -> None:  # ruff: ignore[unused-async] - async page stub
        page.url = target

    page.goto = AsyncMock(side_effect=goto)
    page.title = AsyncMock(return_value="Ozon")
    return page


async def test_cold_account_request_navigates_before_fetch() -> None:
    page = owned_page()
    title = Mock()
    result = await request_on_owned_page(page, "GET", COMPOSER_URL + "/cart", None, budget=2, check_title=title)
    page.goto.assert_awaited_once_with(HOME_URL, wait_until="domcontentloaded", timeout=2000)
    title.assert_called_once_with("Ozon")
    script, payload = page.evaluate.call_args.args
    assert script == ACCOUNT_FETCH
    assert payload["url"] == COMPOSER_URL + "/cart"
    assert 0 < payload["timeout"] <= 2000
    assert payload["body"] is None
    assert result["status"] == 200
    assert "/my/orderlist" not in script


async def test_existing_healthy_owned_origin_needs_no_navigation() -> None:
    page = owned_page(HOME_URL + "product/185618681/")
    await request_on_owned_page(page, "POST", COMPOSER_URL + "/cart", {}, budget=2, check_title=Mock())
    page.goto.assert_not_awaited()
    assert page.evaluate.call_args.args[1]["body"] == "{}"


@pytest.mark.parametrize(
    "url", ["about:blank", "https://evil.example/", "https://www.ozon.ru.evil/", "http://www.ozon.ru/"]
)
async def test_failed_origin_bootstrap_never_fetches(url: str) -> None:
    page = owned_page(url)
    page.goto = AsyncMock()
    with pytest.raises(OzonError, match="HTTPS Ozon origin"):
        await request_on_owned_page(page, "GET", COMPOSER_URL + "/cart", None, budget=2, check_title=Mock())
    page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("url", ["https://user:secret@www.ozon.ru/", "https://www.ozon.ru:444/", "https://[bad/"])
def test_account_origin_rejects_credentials_and_noncanonical_urls(url: str) -> None:
    assert not account_origin(url)


async def test_network_or_challenge_title_prevents_account_request() -> None:
    page = owned_page(HOME_URL)
    with pytest.raises(OzonError, match="Not retried"):
        await request_on_owned_page(
            page,
            "GET",
            COMPOSER_URL + "/cart",
            None,
            budget=2,
            check_title=Mock(side_effect=RuntimeError("PRIVATE TITLE")),
        )
    page.evaluate.assert_not_awaited()


async def test_navigation_and_fetch_share_a_hard_budget() -> None:
    page = owned_page()

    async def hangs(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(5)

    page.goto.side_effect = hangs
    with pytest.raises(OzonError, match="timed out"):
        async with asyncio.timeout(0.5):
            await request_on_owned_page(page, "GET", COMPOSER_URL + "/cart", None, budget=0.02, check_title=Mock())
    page.evaluate.assert_not_awaited()


async def test_uncertain_post_is_not_retried_and_error_is_sanitized() -> None:
    page = owned_page(HOME_URL)
    page.evaluate.side_effect = PlaywrightError("PRIVATE URL AND TOKEN")
    with pytest.raises(OzonError, match="Not retried") as caught:
        await request_on_owned_page(page, "POST", COMPOSER_URL + "/cart", {}, budget=2, check_title=Mock())
    assert "PRIVATE" not in str(caught.value)
    assert page.evaluate.await_count == 1


def test_public_dispatch_avoids_curl_bootstrap_and_cookie_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ozon_mcp.session.transport.get_settings", lambda: OzonSettings(storefront="public"))
    session = OzonSession()
    session._request_in_browser = Mock(return_value={"widgetStates": {}})
    session._bootstrap = Mock(side_effect=AssertionError("HTTP bootstrap"))
    session.fetch("/cart")
    session._request_in_browser.assert_called_once_with("GET", COMPOSER_URL + "/cart", None, "composer")
    session._context = Mock()
    session._http = Mock()
    session.save_state()
    session._context.add_cookies.assert_not_called()


def test_signed_out_public_account_does_not_restore_or_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ozon_mcp.session.transport.get_settings", lambda: OzonSettings(storefront="public"))
    session = OzonSession()
    session._ensure_browser = Mock()
    session._page = Mock()
    session._page.is_closed.return_value = False
    session._browser_is_signed_in = Mock(return_value=False)
    session.restore_profile = Mock()
    with pytest.raises(SessionExpiredError):
        session.fetch("/cart")
    session.restore_profile.assert_not_called()
    session._page.evaluate.assert_not_called()


async def test_real_browser_cold_fetch_and_redirect_refusal() -> None:
    """Offline Chromium, empty temporary context, fulfilled routes only."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(offline=True, service_workers="block")
        calls: list[str] = []

        async def route_request(route: object) -> None:
            calls.append(route.request.url)
            if route.request.url == HOME_URL:
                await route.fulfill(status=200, content_type="text/html", body="<title>Ozon</title>")
            elif route.request.url == COMPOSER_URL + "/cart":
                await route.fulfill(status=200, content_type="application/json", body='{"widgetStates":{}}')
            elif route.request.url == COMPOSER_URL + "/redirect":
                await route.fulfill(status=302, headers={"Location": "/api/never-follow"})
            else:
                await route.abort()

        await context.route("**/*", route_request)
        page = await context.new_page()
        result = await request_on_owned_page(page, "GET", COMPOSER_URL + "/cart", None, budget=2, check_title=Mock())
        assert result["status"] == 200
        assert calls == [HOME_URL, COMPOSER_URL + "/cart"]
        redirect = await request_on_owned_page(
            page, "GET", COMPOSER_URL + "/redirect", None, budget=2, check_title=Mock()
        )
        assert redirect == {"status": 302, "text": ""}
        assert HOME_URL + "api/never-follow" not in calls
        with pytest.raises(UpstreamError):
            OzonSession()._page_from(redirect["text"], redirect["status"], "composer", 0)
        await context.close()
        await browser.close()
