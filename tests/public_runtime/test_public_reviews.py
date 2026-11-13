"""Real browser extraction from synthetic public pages with all network intercepted."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from ozon_mcp.errors import OzonError
from ozon_mcp.services import catalog, public_catalog
from ozon_mcp.session import transport
from ozon_mcp.session.public_reader import matches_public_url
from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import OzonSettings
from review_fixtures import review_markup, review_states

if TYPE_CHECKING:
    from collections.abc import Iterator

URL = "https://www.ozon.ru/product/2859492815/reviews/?sort=usefulness_desc"
FIXTURE = Path("tests/fixtures/public-reviews-schema.html").read_text(encoding="utf-8")


@pytest.fixture
def browser_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, list[str]]]:
    settings = OzonSettings(storefront="public", public_read_timeout=2)
    monkeypatch.setattr(public_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(transport, "get_settings", lambda: settings)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        session = OzonSession()
        session._page = page
        monkeypatch.setattr(session, "_ensure_browser", lambda: None)
        monkeypatch.setattr(public_catalog, "get_session", lambda: session)
        requests: list[str] = []
        yield page, requests
        browser.close()


def test_browser_snapshot_and_service_return_reviews_across_pages(browser_session: Any) -> None:
    page, requests = browser_session

    def respond(route: Any) -> None:
        url = route.request.url
        assert urlsplit(url).netloc == "www.ozon.ru"
        requests.append(url)
        markup = FIXTURE
        if parse_qs(urlsplit(url).query).get("page") == ["2"]:
            markup = markup.replace("Анна", "Борис").replace("Хороший товар", "Товар работает")
        route.fulfill(status=200, content_type="text/html", body=markup)

    page.route("**/*", respond)
    result = catalog.get_reviews("2859492815", limit=2)
    assert [(r.author, r.text, r.score, r.date) for r in result.reviews] == [
        ("Анна", "Хороший товар", 5, "2026-11-10"),
        ("Борис", "Товар работает", 5, "2026-11-10"),
    ]
    assert result.count == 20
    assert result.fetched == 2
    assert len(requests) == 2


def test_browser_review_read_does_not_stop_at_product_heading(browser_session: Any) -> None:
    page, _ = browser_session
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body='<title>Товар</title><div data-widget="webProductHeading"><h1>Товар</h1></div>',
        ),
    )
    with pytest.raises(OzonError, match="not an empty"):
        catalog.get_reviews("2859492815", limit=1)


def test_browser_review_challenge_propagates(browser_session: Any) -> None:
    page, _ = browser_session
    page.route(
        "**/*",
        lambda route: route.fulfill(status=403, content_type="text/html", body="<title>Antibot challenge</title>"),
    )
    with pytest.raises(OzonError, match="antibot"):
        catalog.get_reviews("2859492815", limit=1)


@pytest.mark.parametrize(
    "actual",
    [
        "https://www.ozon.ru/product/2859492815/",
        "https://www.ozon.ru/product/999999999/reviews/?sort=usefulness_desc",
        "https://www.ozon.ru.evil.example/product/2859492815/reviews/?sort=usefulness_desc",
        "https://www.ozon.ru/product/2859492815/reviews/?sort=score_asc",
        "https://www.ozon.ru/product/2859492815/reviews/",
    ],
)
def test_review_reader_requires_same_product_review_route_and_sort(actual: str) -> None:
    assert not matches_public_url(actual, URL)


def test_review_reader_rejects_pagination_reset() -> None:
    assert not matches_public_url(URL, URL + "&page=2")


def test_review_reader_accepts_canonical_slug_with_requested_query() -> None:
    assert matches_public_url(URL.replace("/2859492815/", "/example-2859492815/") + "&__rr=1", URL)


@pytest.mark.parametrize(
    ("sku", "score", "count", "fetched", "author", "text", "date", "likes"),
    [
        ("185618681", 4.9, 398, 30, "Андрей п.", "Продукт рекомендую.", "2023-09-16", 1),
        ("3841862824", 4.5, 14, 14, "Александр Г.", "Хороший телефон,", "2026-07-16", 17),
        ("507239415", 5.0, 810, 30, "Расул Х.", "Очень хорошие фильтры,", "2024-03-27", 5),
    ],
)
def test_browser_reads_captured_client_state_reviews(
    browser_session: Any,
    sku: str,
    score: float,
    count: int,
    fetched: int,
    author: str,
    text: str,
    date: str,
    likes: int,
) -> None:
    page, requests = browser_session

    def respond(route: Any) -> None:
        requests.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body=review_markup(review_states(sku)))

    page.route("**/*", respond)
    result = catalog.get_reviews(sku)
    assert (result.score, result.count, result.fetched) == (score, count, fetched)
    first = result.reviews[0]
    assert (first.author, first.score, first.date, first.useful) == (author, 5, date, likes)
    assert first.text is not None
    assert first.text.startswith(text)
    assert first.photos
    assert result.photos
    assert result.distribution
    if sku == "185618681":
        assert first.positive == (
            "Вкус приятный, не приторный. Банка компактных размеров. "
            "Удобная мерная ложка, которая без верха отмеряет ровно рекомендуемую порцию."
        )
        assert first.negative is not None
        assert "краситель в составе не заявлен" in first.negative
        assert first.photos[0] == "https://ir.ozone.ru/s3/rp-photo-2/fc022d62-038a-4bdc-b072-1c6f70772df4.jpeg"
    assert requests == [f"https://www.ozon.ru/product/{sku}/reviews/?sort=usefulness_desc"]


@pytest.mark.parametrize(("limit", "fetched", "pages"), [(5, 5, 1), (25, 25, 2), (100, 30, 2)])
def test_browser_follows_captured_widget_paging(browser_session: Any, limit: int, fetched: int, pages: int) -> None:
    page, requests = browser_session
    sku = "185618681"
    base = f"https://www.ozon.ru/product/{sku}/reviews/"
    next_query = "?page=2&page_key=CPy23UQSDAjwiemPBhDIx8q1AxgF&sort=usefulness_desc"

    def respond(route: Any) -> None:
        requests.append(route.request.url)
        states = review_states(sku)
        listing = states["webListReviews-5603940-default-1"]
        if route.request.url == base + next_query:
            listing["reviews"] = listing["reviews"][19:]
            listing["paging"]["nextButton"] = ""
        else:
            assert route.request.url == base + "?sort=usefulness_desc"
            listing["reviews"] = listing["reviews"][:20]
        route.fulfill(status=200, content_type="text/html", body=review_markup(states))

    page.route("**/*", respond)
    result = catalog.get_reviews(sku, limit=limit)
    assert result.fetched == fetched
    assert len(requests) == pages
    if pages == 2:
        assert requests[1] == base + next_query
    assert [r.author for r in result.reviews] == [
        row["author"]["firstName"]
        for row in review_states(sku)["webListReviews-5603940-default-1"]["reviews"][:fetched]
    ]
