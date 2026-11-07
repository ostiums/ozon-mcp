"""Inspected search-tile boundaries. Values here are synthetic, not collected."""

from __future__ import annotations

from ozon_mcp.parsing.public_storefront import products_from_document

GRID = """
<div data-widget="infiniteVirtualPaginator">
  <div class="tileGridDesktop">
    <div data-index="0" class="tile-root qi8">
      <a target="_blank" href="/product/example-alpha-1600000001/">
        <span class="tsBody500Medium">Alpha isolate</span>
      </a>
      <span class="c35_6_4_a1 tsHeadline500Medium c35_6_4_b4">100 ₽</span>
    </div>
    <div data-index="1" class="tile-root">
      <a target="_blank" href="/product/example-beta-1600000002/">
        <span class="tsBody500Medium">Beta isolate</span>
      </a>
      <span class="tsHeadline500Medium">250 ₽</span>
      <span class="tsBody500Medium">не заголовок</span>
    </div>
  </div>
</div>
"""

OUTSIDE = """
<div class="tile-root" data-index="9">
  <a target="_blank" href="/product/outside-1600000009/">
    <span class="tsBody500Medium">Outside</span>
  </a>
  <span class="tsHeadline500Medium">9 ₽</span>
</div>
"""

HASH_ONLY = """
<div data-widget="infiniteVirtualPaginator">
  <div class="tileGridDesktop">
    <div data-index="0" class="tile-root">
      <a target="_blank" href="/product/hash-1600000003/">
        <span class="tsBody500Medium">Hash title</span>
      </a>
      <span class="c35_6_4_a1 c35_6_4_b4">999 ₽</span>
    </div>
  </div>
</div>
"""

AMBIGUOUS = """
<div data-widget="infiniteVirtualPaginator">
  <div class="tileGridDesktop">
    <div data-index="0" class="tile-root">
      <a target="_blank" href="/product/one-1600000004/"><span class="tsBody500Medium">One</span></a>
      <a target="_blank" href="/product/two-1600000005/"><span class="tsBody500Medium">Two</span></a>
      <span class="tsHeadline500Medium">10 ₽</span>
    </div>
    <div data-index="1" class="tile-root">
      <a target="_blank" href="/product/ok-1600000006/"><span class="tsBody500Medium">Ok</span></a>
      <span class="tsHeadline500Medium">20 ₽</span>
      <span class="tsHeadline500Medium">30 ₽</span>
    </div>
  </div>
</div>
"""


def test_price_stays_inside_its_own_tile_and_is_not_a_bank_price() -> None:
    products = products_from_document({"html": GRID + OUTSIDE})
    assert products is not None
    by_sku = {product.sku: product for product in products}
    assert set(by_sku) == {"1600000001", "1600000002"}
    alpha = by_sku["1600000001"]
    beta = by_sku["1600000002"]
    assert alpha.title == "Alpha isolate"
    assert alpha.url.endswith("/example-alpha-1600000001/")
    assert alpha.displayed_price == "100 ₽"
    assert alpha.price is None
    assert beta.title == "Beta isolate"
    assert beta.displayed_price == "250 ₽"
    assert beta.price is None
    assert "9 ₽" not in {alpha.displayed_price, beta.displayed_price}


def test_hashed_price_class_is_not_enough() -> None:
    products = products_from_document({"html": HASH_ONLY})
    assert products is not None
    assert products[0].title == "Hash title"
    assert products[0].displayed_price is None
    assert products[0].price is None


def test_ambiguous_tile_is_dropped_and_two_headlines_leave_price_unknown() -> None:
    products = products_from_document({"html": AMBIGUOUS})
    assert products is not None
    assert [product.sku for product in products] == ["1600000006"]
    assert products[0].title == "Ok"
    assert products[0].displayed_price is None
    assert products[0].url.endswith("/ok-1600000006/")


def test_missing_grid_is_not_an_empty_result() -> None:
    assert products_from_document({"html": "<div><a href='/product/x-1600000001/'>Loose</a></div>"}) is None
    assert products_from_document({"html": "<main>По вашему запросу товаров не найдено</main>"}) == []
