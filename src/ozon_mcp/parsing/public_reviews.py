"""Public schema.org review declarations and their same-product next links.

Live review markup was unavailable during development. This parser accepts
JSON-LD and semantic microdata, not unverified Ozon classes or widget state.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from ozon_mcp.errors import OzonError
from ozon_mcp.models.catalog import Review, Reviews
from ozon_mcp.parsing.public_storefront import _json_nodes, product_link
from ozon_mcp.parsing.visible_cards import _Node, _parse, _text, _walk

_ORIGIN = "https://www.ozon.ru"
_REVIEW_PATH = re.compile(r"/product/(?:[a-z0-9-]*-)?(\d{6,})/reviews/?$")


@dataclass
class PublicReviewPage:
    answer: Reviews
    next_path: str | None
    review_keys: list[str] = field(default_factory=list)


def review_link(href: str) -> tuple[str, str] | None:
    """An exact public review route, retaining its declared query."""
    if not href or "\\" in href or any(ord(char) < 32 for char in href) or href.startswith("//"):
        return None
    try:
        parsed = urlsplit(_ORIGIN + href if href.startswith("/") else href)
    except ValueError:
        return None
    match = _REVIEW_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "www.ozon.ru" or match is None:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")), str(match[1])


def review_urls_match(actual: str, requested: str) -> bool:
    actual_link, target_link = review_link(actual), review_link(requested)
    if actual_link is None or target_link is None or actual_link[1] != target_link[1]:
        return False
    target_query = parse_qs(urlsplit(requested).query, keep_blank_values=True)
    actual_query = parse_qs(urlsplit(actual).query, keep_blank_values=True)
    return all(actual_query.get(key) == values for key, values in target_query.items())


def reviews_from_document(document: dict[str, Any], sku: str) -> PublicReviewPage | None:
    identity = review_link(str(document.get("url") or ""))
    if identity is None or identity[1] != sku:
        return None
    nodes = [node for block in document.get("jsonLd") or [] if isinstance(block, str) for node in _json_nodes(block)]
    root = _parse(str(document.get("html") or ""))
    nodes.extend(_microdata(node) for node in _walk(root) if "itemscope" in node.attrs and _outer_scope(node))
    raw_reviews, aggregates = _product_declarations(nodes, sku)
    reviews: list[Review] = []
    keys: list[str] = []
    for raw in raw_reviews:
        if "itemReviewed" in raw and not _is_product(raw["itemReviewed"], sku):
            continue
        review = _review(raw)
        if review is None:
            continue
        key = _string(raw.get("@id")) or review.model_dump_json()
        if key not in keys:
            keys.append(key)
            reviews.append(review)
    count = _unique([_integer(item.get("reviewCount")) for item in aggregates])
    score = _unique([_rating(item, integer=False) for item in aggregates])
    if not reviews and count != 0:
        return None
    if reviews and count == 0:
        raise OzonError("Public review declarations conflict with their zero review count.")
    return PublicReviewPage(
        answer=Reviews(
            score=score,
            count=count,
            fetched=len(reviews),
            reviews=reviews,
            photos=list(dict.fromkeys(photo for review in reviews for photo in review.photos)),
        ),
        next_path=_next_path(root, document.get("reviewLinks"), identity[0], sku),
        review_keys=keys,
    )


def _product_declarations(nodes: list[dict[str, Any]], sku: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_reviews: list[dict[str, Any]] = []
    aggregates: list[dict[str, Any]] = []
    for node in nodes:
        if _has_type(node, "Product") and _is_product(node, sku):
            raw_reviews.extend(item for item in _list(node.get("review")) if isinstance(item, dict))
            aggregate = node.get("aggregateRating")
            if isinstance(aggregate, dict):
                aggregates.append(aggregate)
        elif _has_type(node, "Review") and _is_product(node.get("itemReviewed"), sku):
            raw_reviews.append(node)
    return raw_reviews, aggregates


def _has_type(node: dict[str, Any], name: str) -> bool:
    return any(isinstance(kind, str) and kind.rsplit("/", 1)[-1] == name for kind in _list(node.get("@type")))


def _is_product(raw: object, sku: str) -> bool:
    if isinstance(raw, str):
        identity = product_link(raw)
        return bool(identity and identity[1] == sku)
    if not isinstance(raw, dict):
        return False
    declared = raw.get("sku")
    if declared is not None and str(declared) != sku:
        return False
    url = raw.get("url") or raw.get("@id")
    if url is not None:
        identity = product_link(str(url).split("#", 1)[0])
        return bool(identity and identity[1] == sku)
    return declared is not None and str(declared) == sku


def _review(raw: dict[str, Any]) -> Review | None:
    author = raw.get("author")
    author = author.get("name") if isinstance(author, dict) else author
    rating = raw.get("reviewRating")
    score = _rating(rating, integer=True) if isinstance(rating, dict) else None
    review = Review(
        author=_string(author),
        text=_string(raw.get("reviewBody")),
        score=int(score) if score is not None else None,
        date=_date(raw.get("datePublished")),
        positive=_notes(raw.get("positiveNotes")),
        negative=_notes(raw.get("negativeNotes")),
        photos=_photos(raw.get("image")),
        useful=_votes(raw, "LikeAction"),
        unuseful=_votes(raw, "DislikeAction"),
        answers=_integer(raw.get("commentCount")),
    )
    return (
        review
        if any(value is not None for value in (review.text, review.score, review.positive, review.negative))
        else None
    )


def _photos(raw: object) -> list[str]:
    photos: list[str] = []
    for image in _list(raw):
        value = image.get("contentUrl") or image.get("url") if isinstance(image, dict) else image
        if isinstance(value, str) and value.startswith("https://") and value not in photos:
            photos.append(value)
    return photos


def _votes(raw: dict[str, Any], action: str) -> int | None:
    for statistic in _list(raw.get("interactionStatistic")):
        if not isinstance(statistic, dict):
            continue
        kind = statistic.get("interactionType")
        if isinstance(kind, dict):
            kind = kind.get("@type")
        if isinstance(kind, str) and kind.rsplit("/", 1)[-1] == action:
            return _integer(statistic.get("userInteractionCount"))
    return None


def _notes(raw: object) -> str | None:
    if isinstance(raw, str):
        return _string(raw)
    if not isinstance(raw, dict):
        return None
    notes = [
        text
        for item in _list(raw.get("itemListElement"))
        if isinstance(item, dict) and (text := _string(item.get("name")))
    ]
    return "\n".join(notes) or None


def _date(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _rating(raw: dict[str, Any], *, integer: bool) -> float | None:
    value = _number(raw.get("ratingValue"))
    if _number(raw.get("bestRating", 5)) != 5 or _number(raw.get("worstRating", 1)) != 1:
        return None
    if value is None or not 1 <= value <= 5 or (integer and not value.is_integer()):
        return None
    return value


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, str | int | float):
        return None
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _integer(raw: object) -> int | None:
    value = _number(raw)
    return int(value) if value is not None and value >= 0 and value.is_integer() else None


def _string(raw: object) -> str | None:
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else [] if raw is None else [raw]


def _unique[T](values: list[T | None]) -> T | None:
    known = {value for value in values if value is not None}
    return next(iter(known)) if len(known) == 1 else None


def _outer_scope(node: _Node) -> bool:
    parent = node.parent
    while parent is not None:
        if "itemscope" in parent.attrs:
            return False
        parent = parent.parent
    return True


def _microdata(scope: _Node) -> dict[str, Any]:
    result: dict[str, Any] = {"@type": scope.attrs.get("itemtype", "").split()}
    if scope.attrs.get("itemid"):
        result["@id"] = scope.attrs["itemid"]

    def collect(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                continue
            props = child.attrs.get("itemprop", "").split()
            nested = "itemscope" in child.attrs
            value = (
                _microdata(child)
                if nested
                else next(
                    (child.attrs[key] for key in ("content", "datetime", "href", "src") if key in child.attrs),
                    _text(child),
                )
            )
            for prop in props:
                if prop in result:
                    result[prop] = [*_list(result[prop]), value]
                else:
                    result[prop] = value
            if not nested:
                collect(child)

    collect(scope)
    return result


def _next_path(root: _Node, declared: object, current: str, sku: str) -> str | None:
    links = [
        node.attrs.get("href", "")
        for node in _walk(root)
        if node.tag in {"a", "link"} and "next" in node.attrs.get("rel", "").split()
    ]
    links.extend(link for link in _list(declared) if isinstance(link, str))
    paths: set[str] = set()
    current_sort = parse_qs(urlsplit(current).query).get("sort")
    for href in links:
        if "\\" in href or href.startswith("//") or any(ord(char) < 32 for char in href):
            raise OzonError("Public reviews contain an unsafe next-page link.")
        identity = review_link(urljoin(current, href))
        if identity is None or identity[1] != sku:
            raise OzonError("Public reviews next-page link is not for this product's public reviews.")
        parsed = urlsplit(identity[0])
        query = parse_qs(parsed.query, keep_blank_values=True)
        if current_sort and query.get("sort", current_sort) != current_sort:
            raise OzonError("Public reviews next-page link changes the requested sort.")
        if current_sort and "sort" not in query:
            parsed = parsed._replace(
                query=parsed.query + ("&" if parsed.query else "") + urlencode({"sort": current_sort[0]})
            )
        paths.add(urlunsplit(("", "", parsed.path, parsed.query, "")))
    if len(paths) > 1:
        raise OzonError("Public reviews declare conflicting next-page links.")
    return next(iter(paths)) if paths else None
