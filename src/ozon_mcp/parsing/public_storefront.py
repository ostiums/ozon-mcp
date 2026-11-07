"""Read a public Ozon page from schema.org data the page already rendered.

Only JSON-LD and schema.org microdata are accepted. A price is kept only when
it belongs to the same product. Missing title, price, variants or nutrition
stay missing. This parser does not call an API and does not invent fields.
Live pages were not read while writing it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final, override
from urllib.parse import urlsplit

from ozon_mcp.models.catalog import Characteristic, Variant, VariantOption

_PRODUCT_URL: Final = re.compile(r"/product/(?:[a-z0-9-]*-)?(\d{6,})/?$")
_ORIGIN = "https://www.ozon.ru"

# The browser only copies schema.org nodes. It does not read cookies, storage,
# network traffic, or the user agent.
PUBLIC_PAGE_READER = """() => {
  const jsonLd = [];
  for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
    jsonLd.push(node.textContent || "");
  }
  const microdata = [];
  const scopes = [...document.querySelectorAll("[itemscope]")].filter((scope) =>
    (scope.getAttribute("itemtype") || "").includes("schema.org/Product"));
  for (const scope of scopes) {
    const parent = scope.parentElement && scope.parentElement.closest("[itemtype*='schema.org/Product']");
    if (parent) continue;
    const props = {};
    const prices = [];
    for (const node of scope.querySelectorAll("[itemprop]")) {
      if (node.closest("[itemtype*='schema.org/Product']") !== scope) continue;
      const key = node.getAttribute("itemprop");
      const value = node.getAttribute("content") || node.getAttribute("href") || (node.textContent || "").trim();
      if (!key || !value) continue;
      if (key === "price") prices.push(value);
      else if (!Object.hasOwn(props, key)) props[key] = value;
    }
    if (prices.length === 1) props.price = prices[0];
    microdata.push({ type: scope.getAttribute("itemtype") || "", props, nested: false });
  }
  return { title: document.title || "", jsonLd, microdata };
}"""


@dataclass
class PublicProduct:
    """One product declared by the page, with nothing filled by guesswork."""

    sku: str
    url: str
    title: str | None = None
    price: str | None = None
    available: bool | None = None
    description: str | None = None
    characteristics: list[Characteristic] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)


def assert_reader_is_passive(script: str = PUBLIC_PAGE_READER) -> None:
    """The fixed page reader must not collect secrets or call the network."""
    lowered = script.lower()
    for forbidden in ("cookie", "localstorage", "sessionstorage", "fetch(", "xmlhttprequest", "useragent", "websocket"):
        if forbidden in lowered:
            message = "public page reader uses a forbidden browser API"
            raise ValueError(message)


def document_from_html(markup: str) -> dict[str, Any]:
    """The same shape the browser reader returns, from a fixture page."""
    parser = _PageParser()
    parser.feed(markup)
    parser.close()
    return {"title": parser.title, "jsonLd": parser.json_ld, "microdata": parser.microdata}


def products_from_document(document: dict[str, Any]) -> list[PublicProduct] | None:
    """Declared products, an explicit empty list, or None when the page declared none.

    None means the page had no product list. An empty list means it declared one
    and the list was empty. Callers must not treat None as "no search hits".
    """
    found, saw_list = _json_ld_products(document.get("jsonLd") or [])
    found.extend(_microdata_products(document.get("microdata") or []))
    if not found and not saw_list:
        return None
    return _dedupe(found)


def _json_ld_products(blocks: object) -> tuple[list[PublicProduct], bool]:
    found: list[PublicProduct] = []
    saw_list = False
    if not isinstance(blocks, list):
        return found, saw_list
    for text in blocks:
        if not isinstance(text, str):
            continue
        for node in _json_nodes(text):
            types = _types(node)
            if "ItemList" in types:
                saw_list = True
                found.extend(_list_products(node))
            elif "Product" in types:
                product = _product(node)
                if product is not None:
                    found.append(product)
    return found, saw_list


def _microdata_products(records: object) -> list[PublicProduct]:
    found: list[PublicProduct] = []
    if not isinstance(records, list):
        return found
    for record in records:
        if not isinstance(record, dict) or record.get("nested"):
            continue
        if "schema.org/Product" not in str(record.get("type") or ""):
            continue
        product = _microdata_product(record)
        if product is not None:
            found.append(product)
    return found


def _json_nodes(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return _flatten(data)


def _flatten(data: object) -> list[dict[str, Any]]:
    if isinstance(data, list):
        nodes: list[dict[str, Any]] = []
        for item in data:
            nodes.extend(_flatten(item))
        return nodes
    if not isinstance(data, dict):
        return []
    nodes = [data]
    graph = data.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            nodes.extend(_flatten(item))
    return nodes


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {value.rsplit("/", 1)[-1] for value in values if isinstance(value, str) and value}


def _list_products(node: dict[str, Any]) -> list[PublicProduct]:
    elements = node.get("itemListElement") or []
    if isinstance(elements, dict):
        elements = [elements]
    products: list[PublicProduct] = []
    if not isinstance(elements, list):
        return products
    for element in elements:
        if not isinstance(element, dict):
            continue
        item = element.get("item") if isinstance(element.get("item"), dict) else element
        if isinstance(item, dict):
            product = _product(item)
            if product is not None:
                products.append(product)
    return products


def _product(node: dict[str, Any]) -> PublicProduct | None:
    url = _product_url(node.get("url"))
    sku = _sku(node.get("sku"), url)
    if sku is None or url is None or _sku(None, url) not in {None, sku}:
        return None
    if _sku(None, url) != sku:
        return None
    return PublicProduct(
        sku=sku,
        url=url,
        title=_text(node.get("name")),
        price=_one_price(node.get("offers")),
        available=_availability(node.get("offers")),
        description=_text(node.get("description")),
        characteristics=_properties(node.get("additionalProperty")),
        variants=_variants(node.get("hasVariant")),
    )


def _microdata_product(record: dict[str, Any]) -> PublicProduct | None:
    raw_props = record.get("props")
    props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    url = _product_url(props.get("url"))
    sku = _sku(props.get("sku"), url)
    if sku is None or url is None or _sku(None, url) != sku:
        return None
    price = _text(props.get("price"))
    return PublicProduct(sku=sku, url=url, title=_text(props.get("name")), price=price)


def _product_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("/"):
        path = urlsplit(raw).path
        if _PRODUCT_URL.search(path) is None:
            return None
        return _ORIGIN + path
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.netloc != "www.ozon.ru":
        return None
    if _PRODUCT_URL.search(parsed.path) is None:
        return None
    return _ORIGIN + parsed.path


def _sku(value: object, url: str | None) -> str | None:
    declared: str | None = None
    if isinstance(value, int | str) and not isinstance(value, bool):
        text = str(value).strip()
        if text.isdigit() and len(text) >= 6:
            declared = text
    match = _PRODUCT_URL.search(urlsplit(url).path) if url else None
    from_url = str(match.group(1)) if match is not None else None
    if declared is not None and from_url is not None and declared != from_url:
        return None
    if declared is not None:
        return declared
    return from_url


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _one_price(offers: object) -> str | None:
    prices = [price for price in _offer_values(offers, "price") if price]
    unique = set(prices)
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _availability(offers: object) -> bool | None:
    values = _offer_values(offers, "availability")
    states = {value.rsplit("/", 1)[-1] for value in values}
    if states == {"InStock"}:
        return True
    if states == {"OutOfStock"}:
        return False
    return None


def _offer_values(offers: object, key: str) -> list[str]:
    rows = offers if isinstance(offers, list) else [offers]
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            values.append(str(value))
        elif isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _properties(raw: object) -> list[Characteristic]:
    rows = raw if isinstance(raw, list) else [raw]
    properties: list[Characteristic] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        value = row.get("value")
        if name and isinstance(value, int | float | str) and not isinstance(value, bool) and str(value).strip():
            properties.append(Characteristic(name=name, value=str(value).strip()))
    return properties


def _variants(raw: object) -> list[Variant]:
    rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    grouped: dict[str, list[VariantOption]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _product_url(row.get("url"))
        sku = _sku(row.get("sku"), url)
        props = _properties(row.get("additionalProperty"))
        if len(props) != 1 or sku is None:
            continue
        prop = props[0]
        grouped.setdefault(prop.name, []).append(VariantOption(sku=sku, label=prop.value, link=url))
    return [Variant(name=name, options=options) for name, options in grouped.items()]


def _dedupe(products: list[PublicProduct]) -> list[PublicProduct]:
    by_sku: dict[str, PublicProduct] = {}
    for product in products:
        current = by_sku.get(product.sku)
        if current is None:
            by_sku[product.sku] = product
            continue
        if current.price != product.price:
            current.price = None
        if current.title != product.title:
            current.title = None
        if current.url != product.url:
            current.url = _product_url(f"/product/{product.sku}/") or current.url
    return list(by_sku.values())


class _PageParser(HTMLParser):
    """Collect the page title and JSON-LD blocks from a fixture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.json_ld: list[str] = []
        self.microdata: list[dict[str, Any]] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_ld = False
        self._ld_parts: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
            self._title_parts = []
        if tag == "script" and attr.get("type") == "application/ld+json":
            self._in_ld = True
            self._ld_parts = []

    @override
    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_ld:
            self._ld_parts.append(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self.title = "".join(self._title_parts).strip()
            self._in_title = False
        if tag == "script" and self._in_ld:
            self.json_ld.append("".join(self._ld_parts))
            self._in_ld = False
