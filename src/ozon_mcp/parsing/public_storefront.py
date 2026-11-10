"""Read a public Ozon page from schema.org data the page already rendered.

Schema.org and inspected public DOM widgets are accepted. A price is kept only when
it belongs to the same product. Missing title, price, variants or nutrition
stay missing. This parser does not call an API and does not invent fields.
DOM fixtures were inspected on 2026-11-10.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final, override
from urllib.parse import parse_qsl, urlsplit

from ozon_mcp.models.catalog import Characteristic, Variant, VariantOption

_PRODUCT_URL: Final = re.compile(r"/product/(?:[a-z0-9-]*-)?(\d{6,})/?$")
_ORIGIN = "https://www.ozon.ru"
_SNAPSHOT_RESERVE_MS: Final = 3_000
_QUERY_KEY: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
_URL_CLASSES: Final = frozenset({
    "about:blank",
    "ozon-search",
    "ozon-product",
    "ozon-home",
    "ozon-other",
    "external",
    "missing",
})

# The browser copies schema.org and inspected public DOM nodes. It does not read cookies, storage,
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
  const selectors = [
    'div[data-widget="infiniteVirtualPaginator"]',
    ...["webProductHeading", "webPrice", "webShortCharacteristics", "webCharacteristics",
        "webNutritionInfo", "webDescription", "webCurrentSeller", "webDelivery"]
      .map(name => 'div[data-widget="' + name + '"]')
  ];
  const visible = document.createElement("div");
  for (const original of document.querySelectorAll(selectors.join(','))) {
    if (!original.getClientRects().length) continue;
    const copy = original.cloneNode(true);
    const sources = [original, ...original.querySelectorAll('*')];
    const copies = [copy, ...copy.querySelectorAll('*')];
    for (let i = 0; i < sources.length; i++) {
      const style = getComputedStyle(sources[i]);
      if (sources[i].hidden || style.display === 'none' || style.visibility === 'hidden') {
        copies[i].setAttribute('hidden', '');
      }
    }
    copy.querySelectorAll("script, style, noscript, template, iframe, [hidden]").forEach(n => n.remove());
    if (!copy.hidden) visible.appendChild(copy);
  }
  return { title: document.title || "", url: location.href, documentEpoch: performance.timeOrigin,
           jsonLd, microdata, html: visible.innerHTML || "" };
}"""

# Owned-page state at a tile deadline. No markup, URL, cookies, or storage.
DEADLINE_VISIBILITY: Final = """() => ({
  visibility: document.visibilityState,
  focused: document.hasFocus()
})"""


@dataclass
class PublicProduct:
    """One product declared by the page, with nothing filled by guesswork."""

    sku: str
    url: str
    title: str | None = None
    price: str | None = None
    price_regular: str | None = None
    price_old: str | None = None
    displayed_price: str | None = None
    pack_weight: str | None = None
    nutrition: str | None = None
    protein_per_100g: str | None = None
    seller: str | None = None
    available: bool | None = None
    description: str | None = None
    characteristics: list[Characteristic] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)


def snapshot_reserve_ms(total_ms: int) -> int:
    """Time held back so a snapshot or a diagnostic return is not starved."""
    if total_ms <= 0:
        return 0
    return min(_SNAPSHOT_RESERVE_MS, total_ms // 5)


def step_budget_ms(total_ms: int, elapsed_ms: int) -> int:
    """Milliseconds left for the next wait on one shared clock.

    Navigation and tile readiness both use this. Spending the whole
    ``total_ms`` on navigation leaves no second full timeout. The snapshot
    reserve is not given to either wait.
    """
    spent = max(0, elapsed_ms)
    return max(0, total_ms - spent - snapshot_reserve_ms(total_ms))


def query_key_names(url: object) -> tuple[str, int]:
    """Sorted parameter names, and how many names were dropped.

    Values are never returned. A name that is not a short token is counted
    and omitted, so a secret stuffed into a name is not copied out.
    """
    if not isinstance(url, str) or not url.strip():
        return "none", 0
    pairs = parse_qsl(urlsplit(url.strip()).query, keep_blank_values=True)
    kept: set[str] = set()
    dropped = 0
    for key, _value in pairs:
        if _QUERY_KEY.fullmatch(key):
            kept.add(key)
        else:
            dropped += 1
    if not kept:
        return "none", dropped
    return ",".join(sorted(kept)), dropped


def url_class(url: object) -> str:
    """Classify a page URL without returning the URL or its query."""
    kind = "ozon-other"
    if not isinstance(url, str) or not url.strip() or url.strip() == "about:blank":
        kind = "about:blank"
    else:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        if parsed.scheme == "about" or (not parsed.scheme and not parsed.netloc):
            kind = "about:blank"
        elif host != "www.ozon.ru":
            kind = "external"
        elif path == "/search" or path.startswith("/search/"):
            kind = "ozon-search"
        elif path.startswith("/product/"):
            kind = "ozon-product"
        elif path == "/":
            kind = "ozon-home"
    return kind


def snapshot_counts(html: object) -> dict[str, int | bool]:
    """Counts only. The markup itself is not returned."""
    if not isinstance(html, str) or not html:
        return {"html_present": False, "tile_roots": 0, "product_hrefs": 0, "headline_prices": 0}
    return {
        "html_present": True,
        "tile_roots": len(re.findall(r"\btile-root\b", html)),
        "product_hrefs": len(re.findall(r"""href=["'](?:https://www\.ozon\.ru)?/product/""", html)),
        "headline_prices": html.count("tsHeadline500Medium"),
    }


_PROBE_FIELDS: Final = (
    "bring_to_front",
    "document_visibility",
    "document_focused",
    "paginator",
    "tile_grids",
    "tile_roots_attached",
    "product_anchors_attached",
    "title_spans_attached",
    "title_spans_visible",
    "first_title_visible",
)
_VISIBILITY_VALUES: Final = frozenset({"visible", "hidden", "prerender", "unread"})
_FRONT_VALUES: Final = frozenset({"ok", "failed", "missing"})


def _diagnostic_head(diagnostics: dict[str, Any]) -> str:
    title = diagnostics.get("title")
    title_text = (
        "about:blank" if title == "about:blank" else "present" if isinstance(title, str) and title.strip() else "empty"
    )
    ready = bool(diagnostics.get("tile_selector_ready"))
    return f"title={title_text!r}; url_class={diagnostics.get('url_class', 'missing')}; tile_selector_ready={ready}"


def _probe_token(field: str, value: object) -> str:
    """A count or state token. Page text is never copied through."""
    if value is None:
        return "not_taken"
    if field == "document_visibility" and value in _VISIBILITY_VALUES:
        return str(value)
    if field == "bring_to_front" and value in _FRONT_VALUES:
        return str(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "error"


def _format_measured_probe(diagnostics: dict[str, Any]) -> str:
    parts = [f"{field}={_probe_token(field, diagnostics.get(field))}" for field in _PROBE_FIELDS]
    return "deadline_probe=measured; " + "; ".join(parts)


def _query_keys_token(value: object) -> str:
    if value in {"none", "not_taken"}:
        return str(value)
    if isinstance(value, str) and value and all(_QUERY_KEY.fullmatch(part) for part in value.split(",")):
        return value
    return "error"


def _mark_token(field: str, value: object) -> str:
    if field == "query_keys":
        return _query_keys_token(value)
    if field == "url_class":
        return str(value) if isinstance(value, str) and value in _URL_CLASSES else "error"
    return _probe_token(field, value)


def _format_mark(prefix: str, mark: object, *, with_counts: bool) -> str:
    if not isinstance(mark, dict):
        return f"{prefix}_mark=missing"
    fields = (
        "ms",
        "context_pages",
        "owned_index",
        "other_search_pages",
        "url_class",
        "query_keys",
        "query_keys_dropped",
    )
    parts = [f"{prefix}_{field}={_mark_token(field, mark.get(field))}" for field in fields]
    if with_counts:
        parts.extend(f"{prefix}_{field}={_probe_token(field, mark.get(field))}" for field in _PROBE_FIELDS[3:])
    return "; ".join(parts)


def _format_read_marks(diagnostics: dict[str, Any]) -> str:
    parts: list[str] = []
    if "goto_mark" in diagnostics:
        parts.append(_format_mark("goto", diagnostics.get("goto_mark"), with_counts=True))
    if "probe_mark" in diagnostics:
        parts.append(_format_mark("probe", diagnostics.get("probe_mark"), with_counts=False))
    return "; ".join(parts)


def format_public_diagnostics(diagnostics: object) -> str:
    """One stable status line. It never includes markup or the page URL."""
    if not isinstance(diagnostics, dict):
        return "diagnostics=missing"
    head = _diagnostic_head(diagnostics)
    probe = diagnostics.get("deadline_probe")
    if probe == "skipped":
        line = f"{head}; deadline_probe=skipped; dom_counts=not_taken; html_present=False"
    elif probe == "measured":
        line = f"{head}; {_format_measured_probe(diagnostics)}; html_present=False"
    else:
        raw_counts = diagnostics.get("counts")
        counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
        line = (
            f"{head}; html_present={bool(counts.get('html_present'))}; "
            f"tile_roots={int(counts.get('tile_roots') or 0)}; "
            f"product_hrefs={int(counts.get('product_hrefs') or 0)}; "
            f"headline_prices={int(counts.get('headline_prices') or 0)}"
        )
    marks = _format_read_marks(diagnostics)
    if marks:
        return f"{line}; {marks}"
    return line


def product_link(href: str) -> tuple[str, str] | None:
    """Own Ozon product URL and its SKU, or nothing when they disagree."""
    url = _product_url(href)
    sku = _sku(None, url)
    if url is None or sku is None:
        return None
    return url, sku


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
    return {"title": parser.title, "jsonLd": parser.json_ld, "microdata": parser.microdata, "html": markup}


def products_from_document(document: dict[str, Any]) -> list[PublicProduct] | None:
    """Declared products, an explicit empty list, or None when the page declared none.

    None means the page had no product list. An empty list means it declared one
    and the list was empty. Callers must not treat None as "no search hits".
    """
    from ozon_mcp.parsing.visible_cards import visible_products  # ruff: ignore[import-outside-top-level]

    html = document.get("html")
    html = html if isinstance(html, str) else ""
    current_url = document.get("url", "")
    if url_class(current_url) == "ozon-product":
        from ozon_mcp.parsing.visible_product import (  # ruff: ignore[import-outside-top-level] - parser cycle
            visible_product,
        )

        product = visible_product(html, current_url)
        if product is not None:
            return [product]
        declared, _ = _json_ld_products(document.get("jsonLd") or [])
        declared.extend(_microdata_products(document.get("microdata") or []))
        identity = product_link(current_url)
        return _dedupe([p for p in declared if identity and p.sku == identity[1]]) or None
    # A rendered grid is authoritative: invalid/ambiguous tiles cannot be
    # rescued by an unrelated schema Product or an empty ItemList.
    from ozon_mcp.parsing.visible_cards import (  # ruff: ignore[import-outside-top-level] - parser cycle
        _parse,
        _walk,
    )

    has_grid = any(n.attrs.get("data-widget") == "infiniteVirtualPaginator" for n in _walk(_parse(html)))
    visible, saw_empty = visible_products(html)
    if has_grid:
        return _dedupe(visible) if visible else None
    if document.get("explicit_empty") is True or saw_empty:
        return []
    found, _saw_list = _json_ld_products(document.get("jsonLd") or [])
    found.extend(_microdata_products(document.get("microdata") or []))
    if found:
        return _dedupe(found)
    return None


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


def _merge(primary: list[PublicProduct], extra: list[PublicProduct]) -> list[PublicProduct]:
    by_sku = {product.sku: product for product in primary}
    ordered = list(primary)
    for product in extra:
        current = by_sku.get(product.sku)
        if current is None:
            by_sku[product.sku] = product
            ordered.append(product)
            continue
        _fill_gap(current, product)
    return ordered


def _fill_gap(current: PublicProduct, extra: PublicProduct) -> None:
    current.price = _prefer(current.price, extra.price)
    current.price_regular = _prefer(current.price_regular, extra.price_regular)
    current.price_old = _prefer(current.price_old, extra.price_old)
    current.displayed_price = _prefer(current.displayed_price, extra.displayed_price)
    if current.title is None:
        current.title = extra.title
    elif extra.title is not None and current.title != extra.title:
        current.title = None
    if not current.characteristics:
        current.characteristics = extra.characteristics
    if not current.variants:
        current.variants = extra.variants


def _prefer(current: str | None, extra: str | None) -> str | None:
    if current is None:
        return extra
    if extra is None or _same_amount(current, extra):
        return current
    return None


def _same_amount(left: str, right: str) -> bool:
    from ozon_mcp.utils.money import to_kopecks  # ruff: ignore[import-outside-top-level]

    first, second = to_kopecks(left), to_kopecks(right)
    return first is not None and first == second


def _dedupe(products: list[PublicProduct]) -> list[PublicProduct]:
    return _merge([], products)


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
