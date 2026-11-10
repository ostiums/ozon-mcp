"""Product fields from public widgets inspected on 2026-11-10.

No hashed selectors, APIs, serving-size inference, or recommendation cards.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ozon_mcp.models.catalog import Characteristic
from ozon_mcp.parsing.public_nutrition import protein_per_100g
from ozon_mcp.parsing.public_storefront import PublicProduct, product_link
from ozon_mcp.parsing.visible_cards import _has_class, _Node, _one, _parse, _text, _walk
from ozon_mcp.utils.money import to_kopecks

PUBLIC_PRODUCT_SELECTOR = 'div[data-widget="webProductHeading"] h1'
PUBLIC_PRODUCT_WIDGETS = (
    "webProductHeading",
    "webPrice",
    "webShortCharacteristics",
    "webCharacteristics",
    "webNutritionInfo",
    "webDescription",
    "webCurrentSeller",
)


def visible_product(markup: str, url: str) -> PublicProduct | None:
    identified = product_link(url)
    if identified is None:
        return None
    root = _parse(markup)
    widgets = {
        name: [n for n in _walk(root) if n.tag == "div" and n.attrs.get("data-widget") == name]
        for name in PUBLIC_PRODUCT_WIDGETS
    }
    title = _one([_text(n) for w in widgets["webProductHeading"] for n in _walk(w) if n.tag == "h1" and _text(n)])
    if not title:
        return None
    props = _characteristics(widgets)
    unique = {key: _one(vals) for key, vals in props.items()}
    declared_sku = unique.get("Артикул")
    if "Артикул" in unique and declared_sku != identified[1]:
        return None
    price, regular, displayed = _prices(widgets["webPrice"])
    weight = unique.get("Вес товара, г")
    weight = weight + " г" if weight and re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", weight) else None
    nutrition = _one([_text(w) for w in widgets["webNutritionInfo"] if _text(w)])

    protein = protein_per_100g(
        props, widgets["webNutritionInfo"], widgets["webDescription"] + widgets["webCharacteristics"]
    )
    return PublicProduct(
        sku=identified[1],
        url=identified[0],
        title=title,
        price=price,
        price_regular=regular,
        displayed_price=displayed,
        pack_weight=weight,
        nutrition=nutrition,
        protein_per_100g=protein,
        seller=_seller(widgets["webCurrentSeller"]),
        characteristics=[Characteristic(name=k, value=v) for k, v in unique.items() if v is not None],
    )


def _prices(widgets: list[_Node]) -> tuple[str | None, str | None, str | None]:
    labeled: dict[str, list[str]] = {"С банками": [], "С другими банками": []}
    displays: list[str] = []
    for widget in widgets:
        amounts = [
            n
            for n in _walk(widget)
            if n.tag == "span"
            and (_has_class(n, "tsHeadline600Large") or _has_class(n, "tsHeadline500Medium"))
            and "₽" in _text(n)
            and to_kopecks(_text(n)) is not None
        ]
        displays.extend(_text(n) for n in amounts if _has_class(n, "tsHeadline600Large"))
        for label in _walk(widget):
            name = _text(label)
            if label.tag != "span" or not _has_class(label, "tsBody400Small") or name not in labeled:
                continue
            # Nearest common container with one amount and one price-role label.
            parent = label.parent
            while parent is not None:
                members = _walk(parent)
                candidates = [n for n in amounts if n in members]
                roles = [
                    n for n in members if n.tag == "span" and _has_class(n, "tsBody400Small") and _text(n) in labeled
                ]
                if candidates:
                    if len(candidates) == len(roles) == 1:
                        labeled[name].append(_text(candidates[0]))
                    break
                if parent is widget:
                    break
                parent = parent.parent
    return _one(labeled["С банками"]), _one(labeled["С другими банками"]), _one(displays)


def _seller(widgets: list[_Node]) -> str | None:
    """Unique named public seller link inside the current-seller widget only.

    No whole-widget text fallback: ratings, badges and chat controls are not
    seller names. Hidden nodes were pruned by the fixed reader/parser.
    """
    candidates: set[tuple[str, str]] = set()
    for widget in widgets:
        for node in _walk(widget):
            if node.tag != "a" or not _text(node):
                continue
            href = node.attrs.get("href", "")
            try:
                url = urlsplit(href)
            except ValueError:
                continue
            if (url.scheme or url.netloc) and (url.scheme != "https" or url.netloc != "www.ozon.ru"):
                continue
            if re.fullmatch(r"/seller/[^/]+/?", url.path):
                candidates.add((url.path.rstrip("/"), _text(node)))
    return next(iter(candidates))[1] if len(candidates) == 1 else None


def _characteristics(widgets: dict[str, list[_Node]]) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for widget in widgets["webCharacteristics"]:
        for row in _walk(widget):
            if row.tag != "dl":
                continue
            keys = [n for n in row.children if isinstance(n, _Node) and n.tag == "dt"]
            vals = [n for n in row.children if isinstance(n, _Node) and n.tag == "dd"]
            if len(keys) == len(vals) == 1 and _text(keys[0]) and _text(vals[0]):
                props.setdefault(_text(keys[0]).rstrip(" :"), []).append(_text(vals[0]))
    # The short table uses paired divs and semantic type classes, observed live.
    for widget in widgets["webShortCharacteristics"]:
        for label in _walk(widget):
            if label.tag != "span" or not _has_class(label, "tsBodyM"):
                continue
            row = label.parent.parent.parent if label.parent and label.parent.parent else None
            if row is None:
                continue
            cells = [n for n in row.children if isinstance(n, _Node)]
            if len(cells) == 2 and label in _walk(cells[0]) and _text(cells[1]):
                props.setdefault(_text(label), []).append(_text(cells[1]))
    return props
