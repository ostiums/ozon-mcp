"""Explicit public-storefront reads. Composer stays the default elsewhere."""

from urllib.parse import quote

from ozon_mcp.constants import SEARCH_SORTS
from ozon_mcp.dependencies import get_session
from ozon_mcp.errors import OzonError
from ozon_mcp.models.catalog import OwnedSearchProbe, ProductCard, Tile
from ozon_mcp.parsing.public_storefront import (
    PublicProduct,
    format_public_diagnostics,
    products_from_document,
)
from ozon_mcp.settings import get_settings
from ozon_mcp.utils.money import to_kopecks


def public_storefront_enabled() -> bool:
    """True only when the operator selected the public page reader."""
    return get_settings().storefront == "public"


def require_composer(action: str) -> None:
    """Refuse a composer catalog read while the public reader is selected."""
    if not public_storefront_enabled():
        return
    message = (
        f"{action} is not on the public storefront. This mode does not call the composer API "
        "and does not fall back to it."
    )
    raise OzonError(message)


def search_public(
    query: str | None,
    category: str | None,
    sort: str,
    page: int,
    filters: dict[str, str] | None,
    limit: int,
) -> list[Tile]:
    """One public search page. Pagination through the composer API is not used."""
    if filters:
        message = "Public storefront search does not apply composer facets."
        raise OzonError(message)
    if not query and not category:
        message = "search needs a query, a category, or both"
        raise OzonError(message)
    path = _search_path(query, category, sort, page)
    products = _declared_products(path)
    tiles = [_tile(product) for product in products]
    if sort in {"cheap", "expensive"}:
        tiles.sort(key=lambda tile: _sort_key(tile, descending=sort == "expensive"))
    return tiles[:limit]


def product_public(sku: str) -> ProductCard:
    """The product page's own declared card. A different SKU is not returned."""
    products = [product for product in _declared_products(f"/product/{sku}/") if product.sku == sku]
    if len(products) != 1:
        message = "Public product page did not declare this SKU. Nothing was read."
        raise OzonError(message)
    product = products[0]
    return ProductCard(
        title=product.title,
        sku=product.sku,
        price=product.price,
        price_regular=product.price_regular,
        price_old=product.price_old,
        displayed_price=product.displayed_price,
        available=product.available,
        variants=product.variants,
        characteristics=product.characteristics,
        description=product.description,
    )


def _declared_products(path: str) -> list[PublicProduct]:
    document = get_session().read_public_page(path)
    if not isinstance(document, dict):
        document = {}
    products = products_from_document(document)
    if products is None:
        message = (
            "Public storefront page has no declared product list. Nothing was read. "
            "This is not an empty search result. No schema.org product and no visible "
            "link to /product/<sku>/ were found. "
            + format_public_diagnostics(document.get("diagnostics"))
        )
        raise OzonError(message)
    return products


_COUNT_FIELDS: tuple[str, ...] = (
    "paginator",
    "tile_grids",
    "tile_roots_attached",
    "product_anchors_attached",
    "title_spans_attached",
    "title_spans_visible",
    "first_title_visible",
)
_VISIBILITY: frozenset[str] = frozenset({"visible", "hidden", "prerender", "unread"})


def _count_token(value: object) -> str:
    if value is None:
        return "not_taken"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int) and value >= 0:
        return str(value)
    return "error"


def owned_search_probe() -> OwnedSearchProbe:
    """The current owned page only. No navigation and no other tab's body."""
    raw = get_session().probe_owned_search()
    payload: dict[str, object] = {
        "storefront": raw.get("storefront"),
        "public_read_timeout": raw.get("public_read_timeout"),
        "browser_timeout": raw.get("browser_timeout"),
        "navigated": False,
        "owned_page": raw.get("owned_page"),
        "dom_counts": raw.get("dom_counts"),
        "url_class": raw.get("url_class"),
        "query_keys": raw.get("query_keys"),
        "query_keys_dropped": raw.get("query_keys_dropped"),
        "context_pages": raw.get("context_pages"),
        "owned_index": raw.get("owned_index"),
        "other_search_pages": raw.get("other_search_pages"),
        "elapsed_ms": raw.get("elapsed_ms"),
        "document_visibility": raw.get("document_visibility")
        if raw.get("document_visibility") in _VISIBILITY
        else "unread",
        "document_focused": _count_token(raw.get("document_focused")),
    }
    for field in _COUNT_FIELDS:
        payload[field] = _count_token(raw.get(field))
    return OwnedSearchProbe.model_validate(payload)


def _search_path(query: str | None, category: str | None, sort: str, page: int) -> str:
    value = SEARCH_SORTS.get(sort, sort)
    base = f"/category/{category.strip('/')}/" if category else "/search/"
    path = f"{base}?page={page}"
    if query:
        path += f"&text={quote(query, safe='')}"
    if value:
        path += f"&sorting={value}"
    return path


def _tile(product: PublicProduct) -> Tile:
    return Tile(
        sku=product.sku,
        title=product.title,
        price=product.price,
        price_regular=product.price_regular,
        price_old=product.price_old,
        displayed_price=product.displayed_price,
        url=product.url,
    )


def _sort_key(tile: Tile, *, descending: bool) -> tuple[bool, int]:
    price = to_kopecks(tile.price)
    return price is None, -(price or 0) if descending else price or 0
