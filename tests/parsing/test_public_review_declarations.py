"""Schema.org review data must belong to the requested product."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ozon_mcp.parsing.public_reviews import reviews_from_document
from ozon_mcp.parsing.public_storefront import document_from_html

SKU = "2859492815"
URL = f"https://www.ozon.ru/product/{SKU}/reviews/?sort=usefulness_desc"


def parse_json(node: Any) -> Any:
    document = document_from_html('<script type="application/ld+json">' + json.dumps(node) + "</script>")
    document["url"] = URL
    return reviews_from_document(document, SKU)


def test_standalone_review_requires_matching_item_reviewed() -> None:
    review = {
        "@type": "Review",
        "reviewBody": "Купил и доволен",
        "author": "Иван",
        "datePublished": "2026-11-10T14:20:00+03:00",
        "reviewRating": {"@type": "Rating", "ratingValue": "4"},
    }
    assert parse_json(review) is None
    review["itemReviewed"] = {"@type": "Product", "sku": SKU}
    page = parse_json(review)
    assert page is not None
    assert page.answer.reviews[0].text == "Купил и доволен"
    assert page.answer.reviews[0].date == "2026-11-10"
    assert page.answer.reviews[0].score == 4


def test_rating_count_is_not_review_count() -> None:
    page = parse_json({
        "@type": "Product",
        "sku": SKU,
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4.9, "ratingCount": 90},
        "review": {"@type": "Review", "reviewBody": "Работает"},
    })
    assert page is not None
    assert page.answer.count is None
    assert page.answer.score == pytest.approx(4.9)


def test_declared_pros_cons_votes_are_kept() -> None:
    page = parse_json({
        "@type": "Product",
        "sku": SKU,
        "review": {
            "@type": "Review",
            "reviewBody": "Нормально",
            "positiveNotes": {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "name": "Тихий"}]},
            "negativeNotes": "Короткий кабель",
            "interactionStatistic": [
                {
                    "@type": "InteractionCounter",
                    "interactionType": "https://schema.org/LikeAction",
                    "userInteractionCount": 7,
                },
                {
                    "@type": "InteractionCounter",
                    "interactionType": "https://schema.org/DislikeAction",
                    "userInteractionCount": 2,
                },
            ],
        },
    })
    assert page is not None
    result = page.answer.reviews[0]
    assert result.positive == "Тихий"
    assert result.negative == "Короткий кабель"
    assert result.useful == 7
    assert result.unuseful == 2


def test_nested_microdata_keeps_review_fields_out_of_recommendations() -> None:
    html = """
    <div itemscope itemtype="https://schema.org/Product">
      <meta itemprop="sku" content="2859492815">
      <div itemprop="review" itemscope itemtype="https://schema.org/Review">
        <div itemprop="author" itemscope itemtype="https://schema.org/Person"><span itemprop="name">Анна</span></div>
        <time itemprop="datePublished" datetime="2026-11-11">11 ноября 2026</time>
        <div itemprop="reviewRating" itemscope itemtype="https://schema.org/Rating">
          <meta itemprop="ratingValue" content="5">
        </div>
        <p itemprop="reviewBody">Хорошая покупка</p>
      </div>
      <div itemprop="review" itemscope itemtype="https://schema.org/Review" hidden>
        <p itemprop="reviewBody">Скрытый отзыв</p>
      </div>
    </div>
    <div itemscope itemtype="https://schema.org/Product">
      <meta itemprop="sku" content="999999999">
      <div itemprop="review" itemscope itemtype="https://schema.org/Review">
        <p itemprop="reviewBody">Другой товар</p>
      </div>
    </div>
    """
    document = document_from_html(html)
    document["url"] = URL
    page = reviews_from_document(document, SKU)
    assert page is not None
    assert len(page.answer.reviews) == 1
    result = page.answer.reviews[0]
    assert (result.author, result.score, result.date, result.text) == ("Анна", 5, "2026-11-11", "Хорошая покупка")


@pytest.mark.parametrize("score", [0, 6, 100, True, "NaN"])
def test_invalid_stars_are_not_presented_as_ozon_stars(score: Any) -> None:
    page = parse_json({
        "@type": "Product",
        "sku": SKU,
        "review": {"@type": "Review", "reviewBody": "Комментарий", "reviewRating": {"ratingValue": score}},
    })
    assert page is not None
    assert page.answer.reviews[0].score is None


def test_product_sku_and_url_must_agree() -> None:
    assert (
        parse_json({
            "@type": "Product",
            "sku": SKU,
            "url": "https://www.ozon.ru/product/999999999/",
            "review": {"@type": "Review", "reviewBody": "Чужой отзыв"},
        })
        is None
    )
