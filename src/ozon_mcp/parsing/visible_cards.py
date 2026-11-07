"""Search tiles from the inspected public grid, and nothing outside it.

The grid is ``div[data-widget=infiniteVirtualPaginator]`` containing
``div.tileGridDesktop``. Each tile is ``div.tile-root[data-index]``. The title
is ``span.tsBody500Medium`` inside ``a[target=_blank][href^=/product/]``. The
amount is ``span.tsHeadline500Medium`` in that same tile. Hashed classes are
ignored. The amount is the displayed price, not a bank price: the inspected
tile had no «С банками» label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final, override

from ozon_mcp.parsing.public_storefront import PublicProduct, product_link

EMPTY_RESULT_PHRASES: Final = (
    "ничего не нашлось",
    "по вашему запросу товаров не найдено",
    "товары не найдены",
)
_SKIP_TAGS: Final = frozenset({"script", "style", "noscript", "template", "iframe"})
_VOID_TAGS: Final = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
_TITLE_CLASS: Final = "tsBody500Medium"
_PRICE_CLASS: Final = "tsHeadline500Medium"
# The inspected public card. Hashed classes are intentionally absent.
PUBLIC_TILE_SELECTOR: Final = (
    'div[data-widget="infiniteVirtualPaginator"] '
    "div.tileGridDesktop div.tile-root[data-index] "
    'a[target="_blank"][href^="/product/"] span.tsBody500Medium'
)
# Same selector, stopped at each boundary. Counts at a deadline use these.
# They are not a second parser.
PUBLIC_TILE_STAGES: Final = (
    ("paginator", 'div[data-widget="infiniteVirtualPaginator"]'),
    ("tile_grids", 'div[data-widget="infiniteVirtualPaginator"] div.tileGridDesktop'),
    (
        "tile_roots",
        'div[data-widget="infiniteVirtualPaginator"] div.tileGridDesktop div.tile-root[data-index]',
    ),
    (
        "product_anchors",
        ('div[data-widget="infiniteVirtualPaginator"] div.tileGridDesktop '
        'div.tile-root[data-index] a[target="_blank"][href^="/product/"]'),
    ),
    ("title_spans", PUBLIC_TILE_SELECTOR),
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None
    children: list[_Node | str] = field(default_factory=list)


def visible_products(markup: str) -> tuple[list[PublicProduct], bool]:
    """Tiles inside the known grid. Empty is returned only for a stated empty page."""
    if not markup.strip():
        return [], False
    root = _parse(markup)
    tiles = _tiles(root)
    if not tiles:
        return [], _states_empty(_text(root))
    products = [product for product in (_tile_product(tile) for tile in tiles) if product is not None]
    if not products:
        return [], False
    return products, False


def _tiles(root: _Node) -> list[_Node]:
    tiles: list[_Node] = []
    for paginator in _walk(root):
        if paginator.tag != "div" or paginator.attrs.get("data-widget") != "infiniteVirtualPaginator":
            continue
        for grid in _walk(paginator):
            if grid.tag != "div" or not _has_class(grid, "tileGridDesktop"):
                continue
            tiles.extend(node for node in _walk(grid) if _is_tile(node))
    return tiles


def _is_tile(node: _Node) -> bool:
    return node.tag == "div" and "data-index" in node.attrs and _has_class(node, "tile-root")


def _tile_product(tile: _Node) -> PublicProduct | None:
    links = _product_links(tile)
    skus = {sku for _url, sku in links}
    urls = {url for url, _sku in links}
    if len(skus) != 1 or len(urls) != 1:
        return None
    url = next(iter(urls))
    sku = next(iter(skus))
    return PublicProduct(sku=sku, url=url, title=_title(tile, sku), displayed_price=_displayed_price(tile))


def _product_links(tile: _Node) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for node in _walk(tile):
        if node.tag != "a" or node.attrs.get("target") != "_blank":
            continue
        identified = product_link(node.attrs.get("href", ""))
        if identified is not None:
            found.append(identified)
    return found


def _title(tile: _Node, sku: str) -> str | None:
    texts: list[str] = []
    for node in _walk(tile):
        if node.tag != "a" or node.attrs.get("target") != "_blank":
            continue
        identified = product_link(node.attrs.get("href", ""))
        if identified is None or identified[1] != sku:
            continue
        for span in _walk(node):
            if span.tag == "span" and _has_class(span, _TITLE_CLASS):
                text = _text(span)
                if text:
                    texts.append(text)
    return _one(texts)


def _displayed_price(tile: _Node) -> str | None:
    texts = [_text(node) for node in _walk(tile) if node.tag == "span" and _has_class(node, _PRICE_CLASS)]
    return _one([text for text in texts if text])


def _one(values: list[str]) -> str | None:
    distinct = set(values)
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def _has_class(node: _Node, name: str) -> bool:
    return name in node.attrs.get("class", "").split()


def _states_empty(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    return any(phrase in folded for phrase in EMPTY_RESULT_PHRASES)


def _text(node: _Node) -> str:
    parts = [child if isinstance(child, str) else _text(child) for child in node.children]
    return " ".join(part.strip() for part in parts if part.strip())


def _walk(node: _Node) -> list[_Node]:
    nodes = [node]
    for child in node.children:
        if isinstance(child, _Node):
            nodes.extend(_walk(child))
    return nodes


def _parse(markup: str) -> _Node:
    parser = _Tree()
    parser.feed(markup)
    parser.close()
    return parser.root


class _Tree(HTMLParser):
    """Element tree that ignores scripts and hidden subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, None)
        self._stack: list[_Node] = [self.root]
        self._skip: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, void=tag in _VOID_TAGS)

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, void=True)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, void: bool) -> None:
        if self._skip:
            self._skip.append(tag)
            return
        attr = {key: value or "" for key, value in attrs}
        if tag in _SKIP_TAGS or "hidden" in attr:
            self._skip.append(tag)
            return
        node = _Node(tag, attr, self._stack[-1])
        self._stack[-1].children.append(node)
        if not void:
            self._stack.append(node)

    @override
    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if self._skip[-1] == tag:
                self._skip.pop()
            return
        while len(self._stack) > 1 and self._stack[-1].tag != tag:
            self._stack.pop()
        if len(self._stack) > 1 and self._stack[-1].tag == tag:
            self._stack.pop()

    @override
    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip():
            return
        self._stack[-1].children.append(data)
