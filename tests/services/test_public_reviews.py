"""Public review contracts using synthetic schema.org pages, not live captures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import document_from_html
from ozon_mcp.services import catalog, public_catalog
from ozon_mcp.settings import OzonSettings

if TYPE_CHECKING:
    from collections.abc import Callable

SKU = "2859492815"
URL = f"https://www.ozon.ru/product/{SKU}/reviews/"


def review(name: str = "Анна", text: str = "Хороший товар", stars: int = 5) -> dict[str, Any]:
    return {
        "@type": "Review",
        "@id": f"urn:review:{name}",
        "author": {"@type": "Person", "name": name},
        "reviewBody": text,
        "reviewRating": {"@type": "Rating", "ratingValue": stars, "bestRating": 5, "worstRating": 1},
        "datePublished": "2026-11-10",
    }


def document(rows: list[dict[str, Any]], *, following: str = "", count: int = 8, sku: str = SKU) -> dict[str, Any]:
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "sku": sku,
        "url": f"https://www.ozon.ru/product/{sku}/",
        "name": "Товар",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "reviewCount": count},
        "review": rows,
    }
    html = '<script type="application/ld+json">' + json.dumps(product) + "</script>"
    if following:
        html += f'<a rel="next" href="{following}">Дальше</a>'
    result = document_from_html(html)
    result["url"] = URL + "?sort=usefulness_desc"
    return result


@pytest.fixture
def public_session(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[Any]], list[str]]:
    monkeypatch.setattr(public_catalog, "get_settings", lambda: OzonSettings(storefront="public"))

    def install(pages: list[Any]) -> list[str]:
        calls: list[str] = []
        remaining = iter(pages)

        class Session:
            def read_public_page(self, path: str) -> dict[str, Any]:
                calls.append(path)
                result = next(remaining)
                if isinstance(result, Exception):
                    raise result
                result = dict(result)
                result["url"] = "https://www.ozon.ru" + path
                return result

            def fetch(self, *_args: Any, **_kwargs: Any) -> Any:
                pytest.fail("Public review reads must not call composer")

        monkeypatch.setattr(public_catalog, "get_session", Session)
        return calls

    return install


def test_public_reviews_preserve_text_rating_date_author(public_session: Any) -> None:
    calls = public_session([document([review()])])
    result = catalog.get_reviews(f"https://www.ozon.ru/product/example-{SKU}/", limit=1)
    assert result.fetched == 1
    assert result.count == 8
    assert result.score == pytest.approx(4.8)
    assert result.reviews[0].text == "Хороший товар"
    assert result.reviews[0].author == "Анна"
    assert result.reviews[0].score == 5
    assert result.reviews[0].date == "2026-11-10"
    assert len(calls) == 1


def test_public_reviews_walk_pages_deduplicate_and_honor_limit(public_session: Any) -> None:
    calls = public_session([
        document([review()], following="?page=2&sort=usefulness_desc"),
        document([review(), review("Борис"), review("Вера")], following="?page=3&sort=usefulness_desc"),
    ])
    result = catalog.get_reviews(SKU, limit=2)
    assert [item.author for item in result.reviews] == ["Анна", "Борис"]
    assert result.fetched == 2
    assert result.count == 8
    assert len(calls) == 2
    assert "page=2" in calls[1]


def test_public_reviews_stop_at_repeated_page(public_session: Any) -> None:
    calls = public_session([
        document([review()], following="?page=2&sort=usefulness_desc"),
        document([review()], following="?page=3&sort=usefulness_desc"),
    ])
    result = catalog.get_reviews(SKU, limit=100)
    assert result.fetched == 1
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("sort", "expected"), [("useful", "usefulness_desc"), ("best", "score_desc"), ("worst", "score_asc")]
)
def test_public_review_sort_reaches_public_page(public_session: Any, sort: str, expected: str) -> None:
    calls = public_session([document([review()])])
    catalog.get_reviews(SKU, sort=sort, limit=1)
    assert f"sort={expected}" in calls[0]


def test_public_reviews_only_return_empty_when_declared(public_session: Any) -> None:
    public_session([document([], count=0)])
    assert catalog.get_reviews(SKU).count == 0
    public_session([document([], count=8)])
    with pytest.raises(OzonError):
        catalog.get_reviews(SKU)


def test_public_reviews_reject_other_products(public_session: Any) -> None:
    public_session([document([review()], sku="999999999")])
    with pytest.raises(OzonError):
        catalog.get_reviews(SKU)


@pytest.mark.parametrize("after_first", [False, True])
def test_public_review_block_is_an_error_even_after_a_successful_page(
    public_session: Any, *, after_first: bool
) -> None:
    pages = [document([review()], following="?page=2&sort=usefulness_desc")] if after_first else []
    public_session([*pages, OzonError("Ozon shows an antibot or challenge page")])
    with pytest.raises(OzonError, match="antibot"):
        catalog.get_reviews(SKU, limit=5)


@pytest.mark.parametrize(
    "link",
    ["https://evil.example/product/2859492815/reviews/?page=2", "/my/orders/", "/product/999999999/reviews/?page=2"],
)
def test_public_reviews_never_follow_unsafe_next_links(public_session: Any, link: str) -> None:
    calls = public_session([document([review()], following=link)])
    try:
        result = catalog.get_reviews(SKU, limit=3)
    except OzonError:
        pass
    else:
        assert result.fetched == 1
    assert len(calls) == 1


def test_public_product_details_includes_requested_reviews(public_session: Any) -> None:
    public_session([document([]), document([review()])])
    card = catalog.product_details(SKU, with_reviews=True)
    assert card.sku == SKU
    assert card.reviews is not None
    assert card.reviews.reviews[0].text == "Хороший товар"


def test_public_reviews_merge_schema_copies_before_satisfying_limit(public_session: Any) -> None:
    first = document([review()], following="?page=2&sort=usefulness_desc")
    first["html"] += """
    <div itemscope itemtype="https://schema.org/Product">
      <meta itemprop="sku" content="2859492815">
      <div itemprop="review" itemscope itemtype="https://schema.org/Review">
        <span itemprop="author">Анна</span>
        <meta itemprop="datePublished" content="2026-11-10">
        <div itemprop="reviewRating" itemscope itemtype="https://schema.org/Rating">
          <meta itemprop="ratingValue" content="5">
        </div>
        <p itemprop="reviewBody">Хороший товар</p>
      </div>
    </div>
    """
    calls = public_session([first, document([review("Борис")])])
    result = catalog.get_reviews(SKU, limit=2)
    assert [item.author for item in result.reviews] == ["Анна", "Борис"]
    assert len(calls) == 2


def test_public_reviews_keep_distinct_ids_with_identical_fields(public_session: Any) -> None:
    first, second = review(), review()
    second["@id"] = "urn:review:different-person-with-the-same-name"
    public_session([document([first, second])])
    result = catalog.get_reviews(SKU, limit=2)
    assert result.fetched == 2
