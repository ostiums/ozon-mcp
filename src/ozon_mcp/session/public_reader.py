"""One owned document, one wall-clock budget, fixed passive DOM readers.

The sync transport bridges once to Playwright's async public Page wrapper so
asyncio.timeout bounds navigation, probes AND evaluate (sync evaluate has no
timeout parameter). No new thread, page, context, browser, or HTTP client.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from playwright.async_api import Error as PlaywrightError, Page

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_reviews import review_link, review_urls_match
from ozon_mcp.parsing.public_storefront import (
    PUBLIC_PAGE_READER,
    format_public_diagnostics,
    product_link,
    query_key_names,
    snapshot_counts,
    snapshot_reserve_ms,
    url_class,
)
from ozon_mcp.parsing.visible_cards import EMPTY_RESULT_PHRASES, PUBLIC_TILE_SELECTOR, PUBLIC_TILE_STAGES
from ozon_mcp.parsing.visible_product import PUBLIC_PRODUCT_SELECTOR

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def matches_public_url(actual: str, requested: str) -> bool:
    """URL eligibility, not readiness: caller also requires a grid/explicit empty.

    A public search can resolve to a brand, category or other public landing
    page. Its rewritten path/query is not a product identity. Product reads
    still require the exact requested SKU. No page/tab is adopted here.
    """
    try:
        target, seen = urlsplit(requested), urlsplit(actual)
    except ValueError:
        return False
    if seen.scheme != "https" or seen.netloc != "www.ozon.ru":
        return False
    if target.path.startswith("/product/"):
        a, b = product_link(actual), product_link(requested)
        return review_urls_match(actual, requested) if review_link(requested) else bool(a and b and a[1] == b[1])
    if not (target.path.rstrip("/") == "/search" or target.path.startswith("/category/")):
        return False
    # These documents are not search results, even if they recommend products.
    if url_class(actual) not in {"ozon-search", "ozon-other"}:
        return False
    segment = unquote(seen.path).strip("/").split("/", 1)[0].lower()
    return segment not in {
        "api",
        "my",
        "cart",
        "order",
        "orders",
        "checkout",
        "login",
        "auth",
        "ozonid",
        "ozonid-lite",
        "otp-lite",
    }


# The normal loop asks only whether the known grid/title exists. Expensive
# structural diagnostics are a separate script, called once ONLY on failure.
READINESS = (
    """() => {
 const visible = n => !!n && n.getClientRects().length > 0 &&
     getComputedStyle(n).visibility !== 'hidden' && getComputedStyle(n).display !== 'none';
 const tiles = [...document.querySelectorAll(TITLE)].some(visible);
 const product = [...document.querySelectorAll(PRODUCT)].some(visible);
 const reviewSchema = node => {
     if (!node || typeof node !== 'object') return false;
     if (Array.isArray(node)) return node.some(reviewSchema);
     const types = [].concat(node['@type'] || []).map(t => String(t).split('/').pop());
     if (types.includes('Review')) return true;
     if (types.includes('Product') &&
         ([].concat(node.review || []).length || String(node.aggregateRating?.reviewCount) === '0')) return true;
     return reviewSchema(node['@graph']);
 };
 const reviewWidgets = [...document.querySelectorAll(
     'div.client-state [id^="state-webListReviews-"][data-state]')].some(n => {
       try {
         const state = JSON.parse(n.getAttribute('data-state'));
         return Array.isArray(state?.reviews) && (state.reviews.length > 0 || state.paging?.total === 0 ||
           [...document.querySelectorAll(
             'div.client-state [id^="state-webReviewProductScore-"][data-state]')].some(score => {
               try { return JSON.parse(score.getAttribute('data-state'))?.reviewsCount === 0; }
               catch { return false; }
             }));
       } catch { return false; }
     });
 const reviews = reviewWidgets || [...document.querySelectorAll('[itemscope][itemtype$="/Review"]')].some(visible) ||
     [...document.querySelectorAll('[itemscope][itemtype$="/Product"] [itemprop="reviewCount"]')]
       .some(n => (n.getAttribute('content') || n.textContent || '').trim() === '0') ||
     [...document.querySelectorAll('script[type="application/ld+json"]')].some(n => {
       try { return reviewSchema(JSON.parse(n.textContent || '')); } catch { return false; }
     });
 const empty = !tiles && !product && [...document.querySelectorAll('h1,h2,h3,p,span')].some(n =>
     visible(n) && EMPTY.some(phrase => (n.textContent || '').trim().toLowerCase() === phrase));
 return {title: document.title || '', url: location.href, documentEpoch: performance.timeOrigin,
     tile_ready: tiles, product_ready: product, review_ready: reviews, explicit_empty: empty};
}"""
    .replace("TITLE", json.dumps(PUBLIC_TILE_SELECTOR))
    .replace("PRODUCT", json.dumps(PUBLIC_PRODUCT_SELECTOR))
    .replace("EMPTY", json.dumps(EMPTY_RESULT_PHRASES))
)

PROBE = """() => {
 const counts = Object.fromEntries(STAGES.map(([name, selector]) =>
     [name, document.querySelectorAll(selector).length]));
 return {title: document.title || '', url: location.href, documentEpoch: performance.timeOrigin,
     counts, visibility: document.visibilityState, focused: document.hasFocus()};
}""".replace("STAGES", json.dumps(PUBLIC_TILE_STAGES))

FAILURE_PROBE_SECONDS = 1.0


def read_deadlines(deadline: float, total_ms: int) -> tuple[float, float]:
    """Readiness, then a failure-only probe, then an untouched snapshot reserve.

    For 40s: readiness ends at 36s, failure probe by 37s, snapshot reserve 3s.
    A success skips the failure probe and snapshots immediately, using any
    remaining time up to the overall deadline. Small test budgets scale down.
    """
    snapshot = snapshot_reserve_ms(total_ms) / 1000
    failure = min(FAILURE_PROBE_SECONDS, max(0, total_ms) / 40000)
    probe_deadline = deadline - snapshot
    return probe_deadline - failure, probe_deadline


async def _evaluate_document(page: Page, script: str) -> dict[str, Any] | None:
    """A committed document can be replaced while a passive read is in flight."""
    try:
        return await page.evaluate(script)
    except PlaywrightError as exc:
        if any(
            message in str(exc)
            for message in (
                "Execution context was destroyed",
                "Cannot find context with specified id",
                "Cannot find context with id",
            )
        ):
            return None
        raise


def _diagnostics(sample: dict[str, Any], *, ready: bool, elapsed: int) -> dict[str, Any]:
    keys, dropped = query_key_names(sample.get("url", ""))
    counts = sample.get("counts", {})
    return {
        "title": sample.get("title", ""),
        "url_class": url_class(sample.get("url", "")),
        "tile_selector_ready": ready,
        "deadline_probe": "measured" if "counts" in sample else "skipped",
        "bring_to_front": "ok",
        "document_visibility": sample.get("visibility", "unread"),
        "document_focused": sample.get("focused"),
        "paginator": counts.get("paginator"),
        "tile_grids": counts.get("tile_grids"),
        "tile_roots_attached": counts.get("tile_roots"),
        "product_anchors_attached": counts.get("product_anchors"),
        "title_spans_attached": counts.get("title_spans"),
        "probe_mark": {
            "ms": elapsed,
            "url_class": url_class(sample.get("url", "")),
            "query_keys": keys,
            "query_keys_dropped": dropped,
        },
    }


async def _failure_probe(
    page: Page, sample: dict[str, Any], *, deadline: float, clock: Callable[[], float] = time.monotonic
) -> dict[str, Any]:
    """One diagnostic attempt, at most one second and never in snapshot reserve."""
    remaining = min(FAILURE_PROBE_SECONDS, max(0, deadline - clock()))
    if remaining <= 0:
        return sample
    try:
        async with asyncio.timeout(remaining):
            measured = await _evaluate_document(page, PROBE)
        return measured if isinstance(measured, dict) and "counts" in measured else sample
    except (PlaywrightError, TimeoutError):
        return sample  # Not measured; never substitute placeholder zero counts.


@dataclass
class _PublicRead:
    page: Page
    url: str
    deadline: float
    total_ms: int
    check_title: Callable[[str], None]
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    sample: dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    stage: str = "navigation"

    def diagnostics(self) -> dict[str, Any]:
        started = self.deadline - self.total_ms / 1000
        return _diagnostics(self.sample, ready=self.ready, elapsed=int((self.clock() - started) * 1000))

    async def snapshot(self, identity: tuple[Any, Any]) -> dict[str, Any] | None:
        self.stage = "snapshot"
        async with asyncio.timeout(max(0, self.deadline - self.clock())):
            data = await _evaluate_document(self.page, PUBLIC_PAGE_READER)
        if data is None or (data.get("url"), data.get("documentEpoch")) != identity:
            self.stage = "readiness"
            return None
        data["explicit_empty"] = bool(self.sample.get("explicit_empty"))
        diag = self.diagnostics()
        diag.pop("deadline_probe", None)
        diag["counts"] = snapshot_counts(data.get("html"))
        data["diagnostics"] = diag
        return data

    def readable(self) -> bool:
        try:
            self.check_title(self.sample.get("title", ""))
        except RuntimeError as exc:
            raise OzonError(str(exc)) from None  # Only the fixed safe title classifier.
        product = urlsplit(self.url).path.startswith("/product/")
        ready_key = (
            "review_ready" if review_link(self.url) is not None else "product_ready" if product else "tile_ready"
        )
        self.ready = bool(self.sample.get(ready_key))
        return bool(self.ready or (not product and self.sample.get("explicit_empty"))) and matches_public_url(
            self.sample.get("url", ""), self.url
        )

    async def run(self, readiness_deadline: float) -> dict[str, Any] | None:
        async with asyncio.timeout(max(0, readiness_deadline - self.clock())):
            await self.page.bring_to_front()
            await self.page.goto(
                self.url, wait_until="commit", timeout=max(1, int((readiness_deadline - self.clock()) * 1000))
            )
        self.stage = "readiness"
        last_identity = None
        ready_since = None
        while self.clock() < readiness_deadline:
            async with asyncio.timeout(max(0, readiness_deadline - self.clock())):
                current = await _evaluate_document(self.page, READINESS)
            if current is None:
                ready_since = None
            else:
                self.sample = current
                identity = (current.get("url"), current.get("documentEpoch"))
                if identity != last_identity:
                    last_identity, ready_since = identity, None
                ready_since = (ready_since if ready_since is not None else self.clock()) if self.readable() else None
                if ready_since is not None and self.clock() - ready_since >= 0.3:
                    data = await self.snapshot(identity)
                    if data is not None:
                        return data
                    ready_since = None
            await self.sleep(min(0.15, max(0, readiness_deadline - self.clock())))
        return None


async def read_owned_public(
    page: Page,
    url: str,
    *,
    deadline: float,
    total_ms: int,
    check_title: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """One owned document; short settle, immediate snapshot, failure-only probe."""
    state = _PublicRead(page, url, deadline, total_ms, check_title, clock, sleep)
    readiness_deadline, probe_deadline = read_deadlines(deadline, total_ms)
    failed = False
    try:
        data = await state.run(readiness_deadline)
        if data is not None:
            return data
    except TimeoutError:
        pass
    except PlaywrightError:
        failed = True  # Do not echo browser errors containing URL/account text.
    state.sample = await _failure_probe(page, state.sample, deadline=probe_deadline, clock=clock)
    lead = (
        "Public read failed during " + state.stage
        if failed
        else "Public " + state.stage + " did not finish within the shared budget"
    )
    raise OzonError(lead + ". This is not an empty result. " + format_public_diagnostics(state.diagnostics())) from None
