"""Public widget declarations use the shared parser and public route constraints."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_reviews import reviews_from_document
from review_fixtures import review_states

SKU = "185618681"
URL = f"https://www.ozon.ru/product/{SKU}/reviews/?sort=usefulness_desc"
LIST = "webListReviews-5603940-default-1"
SCORE = "webReviewProductScore-14003865-default-1"


def document(states: dict[str, Any]) -> dict[str, Any]:
    return {"url": URL, "widgetStates": {key: json.dumps(value) for key, value in states.items()}}


def test_captured_widget_ids_and_paging() -> None:
    result = reviews_from_document(document(review_states(SKU)), SKU)
    assert result is not None
    assert result.next_path == (
        f"/product/{SKU}/reviews/?page=2&page_key=CPy23UQSDAjwiemPBhDIx8q1AxgF&sort=usefulness_desc"
    )
    assert result.review_ids[0] == "018a9f7c-4089-2ebd-61f3-be50ce1bf58e"
    assert len(result.review_ids) == result.answer.fetched == 30


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/product/185618681/reviews/?page=2",
        "//evil.example/product/185618681/reviews/?page=2",
        "/api/entrypoint-api.bx/page/json/v2?url=/product/185618681/reviews/",
        "/product/999999999/reviews/?page=2",
        "/product/185618681/",
        "?page=2&sort=score_asc",
    ],
)
def test_widget_next_link_must_keep_public_product_and_sort(link: str) -> None:
    states = review_states(SKU)
    states[LIST]["paging"]["nextButton"] = link
    with pytest.raises(OzonError):
        reviews_from_document(document(states), SKU)


def test_widget_next_link_inherits_sort() -> None:
    states = review_states(SKU)
    states[LIST]["paging"]["nextButton"] = "?page=2&page_key=opaque%2Btoken"
    result = reviews_from_document(document(states), SKU)
    assert result is not None
    assert result.next_path == f"/product/{SKU}/reviews/?page=2&page_key=opaque%2Btoken&sort=usefulness_desc"


@pytest.mark.parametrize(
    ("field", "value"), [("itemId", "999999999"), ("requestedPath", "/product/999999999/reviews/")]
)
def test_widget_product_identity_must_match(field: str, value: str) -> None:
    states = review_states(SKU)
    states[LIST][field] = value
    with pytest.raises(OzonError, match="different product"):
        reviews_from_document(document(states), SKU)


@pytest.mark.parametrize(
    "malformed", ["not json", [], {"reviews": "not a list"}, {"reviews": [{"content": "bad"}]}, {"reviews": [{}]}]
)
def test_unreadable_widgets_allow_schema_fallback(malformed: Any) -> None:
    doc = document({LIST: malformed})
    assert reviews_from_document(doc, SKU) is None
    doc["jsonLd"] = [
        json.dumps({
            "@type": "Product",
            "sku": SKU,
            "review": [{"@type": "Review", "author": "Анна", "reviewBody": "Товар работает"}],
        })
    ]
    result = reviews_from_document(doc, SKU)
    assert result is not None
    assert result.answer.reviews[0].text == "Товар работает"


@pytest.mark.parametrize("count", [None, 0, 12])
def test_empty_widget_list_needs_explicit_zero(count: int | None) -> None:
    result = reviews_from_document(document({LIST: {"reviews": []}, SCORE: {"reviewsCount": count}}), SKU)
    if count == 0:
        assert result is not None
        assert result.answer.count == result.answer.fetched == 0
    else:
        assert result is None


def test_widget_reviews_cannot_have_zero_count() -> None:
    states = review_states(SKU)
    states[SCORE]["reviewsCount"] = 0
    with pytest.raises(OzonError, match="zero review count"):
        reviews_from_document(document(states), SKU)


def test_no_reviews_error_declaration_returns_empty() -> None:
    result = reviews_from_document(
        document({
            "error-3036935-default-1": {
                "title": "Такой страницы не существует",
                "subtitle": "потому что у товара нет отзывов",
            },
        }),
        SKU,
    )
    assert result is not None
    assert result.answer.score is None
    assert result.answer.count == result.answer.fetched == 0
    assert result.answer.reviews == []
    assert result.next_path is None
    assert result.review_ids == []


@pytest.mark.parametrize(
    "state",
    [
        {"title": "Такой страницы не существует"},
        {"subtitle": "потому что у товара нет отзывов"},
        {"title": "Ошибка", "subtitle": "потому что у товара нет отзывов"},
        {"title": "Такой страницы не существует", "subtitle": "потому что товар удалён"},
        {"title": "Такой страницы не существует", "subtitle": ["потому что у товара нет отзывов"]},
        {"title": ["Такой страницы не существует"], "subtitle": "потому что у товара нет отзывов"},
        {"title": "Такой страницы не существует", "subtitle": "потому что у товара нет отзывов ещё"},
        None,
        [],
        "not json",
    ],
)
def test_other_error_declarations_are_not_empty_reviews(state: Any) -> None:
    assert reviews_from_document(document({"error-3036935-default-1": state}), SKU) is None


@pytest.mark.parametrize("name", ["banner-3036935-default-1", "errorBanner-3036935-default-1"])
def test_no_reviews_words_in_unrelated_state_are_not_empty_reviews(name: str) -> None:
    state = {"title": "Такой страницы не существует", "subtitle": "потому что у товара нет отзывов"}
    assert reviews_from_document(document({name: state}), SKU) is None
