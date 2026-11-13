"""The storefront: search, product cards, reviews, delivery estimates."""

from typing import Annotated

from pydantic import Field

from ozon_mcp.dependencies import run_blocking
from ozon_mcp.mcp_server import mcp
from ozon_mcp.models.catalog import (
    Cheaper,
    DeliveryEstimate,
    Description,
    OwnedSearchProbe,
    ProductCard,
    Reviews,
    SearchFilter,
    Tile,
)
from ozon_mcp.services import catalog
from ozon_mcp.services.public_catalog import owned_search_probe as probe_owned_search
from ozon_mcp.utils.annotations import Limit, Page, ReviewSort, SearchSort, SkuOrUrl


@mcp.tool()
async def search(
    query: Annotated[str | None, Field(description="Search text. Give this, a category, or both.")] = None,
    category: Annotated[
        str | None,
        Field(description='A category slug from a category URL, e.g. "produkty-dlya-doma-9200".'),
    ] = None,
    sort: SearchSort = "popular",
    page: Page = 1,
    filters: Annotated[
        dict[str, str] | None,
        Field(
            description="Facets from get_search_filters(): {key: option_value} for a checkbox or category, "
            '{key: "min;max"} for a range, e.g. {"currency_price": "200;600"}.'
        ),
    ] = None,
    limit: Limit = 36,
) -> list[Tile]:
    """Storefront search → product tiles (sku/title/price/url). Give a text
    query, a category slug ("produkty-dlya-doma-9200"), or both.
    In public mode, cheap/expensive navigate in site relevance order (no price
    sorting parameter), then sort the returned tiles by displayed_price. This
    reads one rendered page, capped by limit; it is not a site-wide cheapest
    crawl. Only explicitly requested non-price sorts go in the navigation URL.
    Facets and composer fallback are disabled in public mode.
    In composer mode, limit is depth, not page size: pages are walked until there are that many
    results, so raise it when looking for the cheapest — a page holds a few
    dozen and the cheapest lot is often further down. sort="cheap" ranks on the
    payable price (Ozon's own order goes by a different figure).
    Ozon's text search is literal about words: a lot whose own title omits the
    brand does not come back for a query that includes it, so search the model
    ("Basilisk V3 X HyperSpeed") rather than brand-plus-model, and confirm what a
    lot actually is with product_details() — a tile's title is the seller's
    wording and may name no model at all.
    A tile is not a card: for variants, characteristics, photos and stock call
    product_details() with its sku.
    filters comes from get_search_filters() — {key: option_value} for a
    checkbox or category facet, {key: "min;max"} for a range, e.g.
    {"currency_price": "200;600"}. Narrowing by price is a filter, not a sort.
    """
    return await run_blocking(lambda: catalog.search(query, category, sort, page, filters, limit))


@mcp.tool()
async def get_search_filters(
    query: Annotated[str, Field(description="The same search text whose facets you want.")],
) -> list[SearchFilter]:
    """Facets available for a query: {name, key, type, options|range}. Apply them
    with search(filters={key: value}) — checkbox/category value = option.value,
    range value = "min;max". Flow: search → get_search_filters → search(filters).
    """
    return await run_blocking(lambda: catalog.get_search_filters(query))


@mcp.tool()
async def product_details(
    sku_or_url: SkuOrUrl,
    with_description: Annotated[
        bool,
        Field(description="Also fetch the description text and its images (one extra request)."),
    ] = False,
    with_reviews: Annotated[
        bool,
        Field(description="Also fetch reviews, walking public review pages when needed."),
    ] = False,
) -> ProductCard:
    """Product card: title, three prices, variants, characteristics, gallery photos.
    Public mode reads the product page's observed widgets, including pack_weight
    and nutrition with its declared basis, plus a unique visible seller.
    protein_per_100g prefers an explicit declaration; otherwise it is calculated
    only from explicit serving grams and protein, with the basis in the string.
    Missing or conflicting data stays null; pack weight/count is never a serving size.
    Money as Ozon prints it: `price` is «С банками» — what the account actually
    pays and the figure to compare lots on — `price_regular` is «С другими
    банками», `price_old` the struck-through comparison. `available` says whether
    it is on sale at all. `cheaper_offers` / `cheaper_from` are Ozon's own count
    and lowest price for other offers of this product; find_cheaper() lists them.
    Each variant carries its own sku, price and availability — that sku is what
    goes into the cart, and for apparel it is the only one that will add.
    with_description / with_reviews fetch those too (separate requests each);
    get_description() and get_reviews() do the same on their own.
    """
    return await run_blocking(
        lambda: catalog.product_details(sku_or_url, with_description=with_description, with_reviews=with_reviews)
    )


@mcp.tool()
async def get_reviews(sku_or_url: SkuOrUrl, limit: Limit = 30, sort: ReviewSort = "useful") -> Reviews:
    """Rating and reviews: `score` (e.g. 4.9), `count` — Ozon's own total — the
    `distribution` per star, and `limit` reviews with `fetched` saying how many
    came back. count and fetched are different numbers: reporting the second as
    the first turns 155 847 reviews into 30.
    Public mode reads declared review widget states with schema.org fallback
    and follows declared next links up to 40 pages. An explicit no-reviews
    declaration returns an empty result. Missing scalar fields stay null and
    missing collections stay empty. Unreadable pages and antibot blocks raise
    errors, with no composer fallback.
    In composer mode, reviews arrive thirty at a time and pages are walked to meet `limit`. There
    is no filter by star, so the one-star ones are reached with sort="worst" plus
    enough depth.
    Each review keeps «Достоинства» and «Недостатки» apart from the comment,
    carries its useful votes, and names the variant it is about — a card's
    reviews cover its sizes and colours, so some are about a different one.
    """
    return await run_blocking(lambda: catalog.get_reviews(sku_or_url, limit, sort))


@mcp.tool()
async def get_description(sku_or_url: SkuOrUrl) -> Description:
    """Product description text plus the images embedded in it."""
    return await run_blocking(lambda: catalog.get_description(sku_or_url))


@mcp.tool()
async def delivery_estimate(sku_or_url: SkuOrUrl) -> DeliveryEstimate:
    """When a product would arrive, to which of the account's addresses, and
    from which warehouse ("Завтра, 2 сентября" / "ул. Данилова, 17" / "Со
    склада Ozon"). The date is relative to that address, so quote both.
    This is per product, before ordering; for an existing order the dates are
    in list_orders(), and for an order being formed in get_checkout().
    """
    return await run_blocking(lambda: catalog.delivery_estimate(sku_or_url))


@mcp.tool()
async def find_cheaper(sku_or_url: SkuOrUrl, limit: Limit = 10) -> Cheaper:
    """The cheapest lots of the same thing, ranked by payable price — top `limit`
    (default 10).
    Looks in both places Ozon keeps them: its own «Есть дешевле или быстрее»
    offers for this exact product, and a price-sorted search by the card's
    title, which reaches the same product listed separately. Entries carry
    `seller` and `delivery` when they came from the offers list, and offers have
    no title — confirm the model with product_details() before quoting one.
    Raises when the base price cannot be read, rather than answering "nothing is
    cheaper" for a product it failed to price.
    """
    return await run_blocking(lambda: catalog.find_cheaper(sku_or_url, limit))


@mcp.tool()
async def owned_search_probe() -> OwnedSearchProbe:
    """Read the page this process already owns. Call it with no arguments:
    owned_search_probe().

    It does not navigate, does not launch a browser, does not adopt another
    tab, and does not evaluate caller-supplied script. Other tabs contribute
    only how many of them are classified as an Ozon search URL. Structural
    counts are taken only when the owned page itself is an Ozon search page.
    The result has query parameter names, not values, and no URL, title, HTML,
    or account data. public_read_timeout and browser_timeout are the configured
    budgets. This does not repair search.
    """
    return await run_blocking(probe_owned_search)
