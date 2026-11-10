"""Bounded requests for existing account operations, on the owned browser only.

No new API routes are defined here. The transport validates the fixed API
origin; this layer establishes a healthy same-origin document before fetch.
Redirects are manual and an opaque redirect is rejected without its Location.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from ozon_mcp.constants import HOME_URL
from ozon_mcp.errors import OzonError
from ozon_mcp.utils.serde import dumps

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Page

ACCOUNT_FETCH = """async ({url, method, body, timeout}) => {
  if (location.origin !== 'https://www.ozon.ru') throw new Error('Invalid account origin');
  const target = new URL(url);
  if (target.origin !== location.origin || !target.pathname.startsWith('/api/'))
    throw new Error('Invalid account endpoint');
  const headers = {accept: 'application/json'};
  if (body !== null) headers['content-type'] = 'application/json';
  const response = await fetch(target.href, {
    method, headers, body, credentials: 'same-origin', redirect: 'manual',
    signal: AbortSignal.timeout(timeout)
  });
  if (response.type === 'opaqueredirect') return {status: 302, text: ''};
  return {status: response.status, text: await response.text()};
}"""


def account_origin(url: str) -> bool:
    """An authenticated origin, with no credentials or foreign host aliases."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc == "www.ozon.ru"


async def request_on_owned_page(
    page: Page,
    method: str,
    url: str,
    body: object,
    *,
    budget: float,
    check_title: Callable[[str], None],
) -> dict[str, Any]:
    """One shared deadline for origin bootstrap, title check and response body.

    Cold public mode intentionally has no first navigation. Navigate that same
    page to the public home first; do not adopt a sibling or launch a browser.
    A navigation away from Ozon, network page or challenge prevents the request.
    """
    deadline = time.monotonic() + budget
    try:
        async with asyncio.timeout(budget):
            await _ensure_origin(page, budget, check_title)
            return await page.evaluate(
                ACCOUNT_FETCH,
                {
                    "url": url,
                    "method": method,
                    "body": dumps(body) if body is not None else None,
                    "timeout": max(1, int((deadline - time.monotonic()) * 1000)),
                },
            )
    except OzonError:
        raise
    except (PlaywrightError, TimeoutError, RuntimeError):
        # Browser exceptions can echo URLs or account text. A failed command may
        # have committed upstream, so never replay it here.
        raise OzonError("Owned-browser account request failed or timed out; outcome is unknown. Not retried.") from None


async def _ensure_origin(page: Page, budget: float, check_title: Callable[[str], None]) -> None:
    if not account_origin(page.url):
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=max(1, int(budget * 1000)))
    if not account_origin(page.url):
        raise OzonError("Account bootstrap did not reach the HTTPS Ozon origin.")
    check_title(await page.title())
