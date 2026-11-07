"""Facet values stay intact and catalog rankings compare payable kopecks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ozon_mcp.constants import COMPOSER_URL
from ozon_mcp.services import catalog
from ozon_mcp.session.transport import OzonSession
from support import page

if TYPE_CHECKING:
    from support import FakeSession


def _tiles(*prices: str | None) -> dict[str, Any]:
    return page(
        tileGridDesktop={
            "items": [
                {
                    "sku": str(1000000 + index),
                    "price": [{"text": price, "textStyle": "PRICE"}] if price is not None else [],
                    "tileName": {"id": "name", "text": "Игровая мышь"},
                }
                for index, price in enumerate(prices)
            ]
        }
    )


@pytest.mark.parametrize("backend", ["composer", "entrypoint"])
def test_facets_reach_the_transport_without_becoming_query_parameters(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    requests: list[str] = []

    def request(self: OzonSession, method: str, url: str, *, backend: str) -> dict[str, Any]:
        requests.append(url)
        return _tiles("100 ₽")

    transport = OzonSession()
    monkeypatch.setattr(OzonSession, "_request", request)
    monkeypatch.setattr(catalog, "get_session", lambda: transport)
    facets = {
        "brand": "A&B&sorting=price_desc",
        "key&=fragment#": "чай + кофе / 50%;100",
        "empty": "",
    }
    catalog.search("кофе & чай", sort="cheap", filters=facets, limit=1)
    if backend == "entrypoint":
        transport.fetch(requests[0].removeprefix(COMPOSER_URL), backend=backend)
    path = requests[-1].split("?url=", 1)[1]
    params = parse_qs(urlsplit(path).query)
    assert params == {
        "page": ["1"],
        "text": ["кофе & чай"],
        "sorting": ["price"],
        "brand": [facets["brand"]],
        "key&=fragment#": [facets["key&=fragment#"]],
    }


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("cheap", ["1000003", "1000001", "1000000", "1000004", "1000002", "1000005"]),
        ("expensive", ["1000004", "1000000", "1000001", "1000003", "1000002", "1000005"]),
    ],
)
def test_price_sort_keeps_kopecks_zero_and_unknown_prices(session: FakeSession, sort: str, expected: list[str]) -> None:
    session.pages = {"/search/": _tiles("116 ₽", "115,50 ₽", None, "0 ₽", "20\u202f000\u00a0000 ₽", "Нет в наличии")}
    assert [tile.sku for tile in catalog.search("мышь", sort=sort, limit=6)] == expected


def test_find_cheaper_compares_the_base_and_all_offers_in_kopecks(session: FakeSession) -> None:
    session.pages = {
        "/product/9999999/": page(webProductHeading={"title": "Игровая мышь"}, webPrice={"cardPrice": "116 ₽"}),
        "otherOffersFromSellers": page(
            webSellerList={
                "sellers": [
                    {"sku": "2000000", "price": {"cardPrice": {"price": "115,50 ₽"}}},
                    {"sku": "2000001", "price": {"cardPrice": {"price": "0 ₽"}}},
                    {"sku": "2000002", "price": {"cardPrice": {"price": "116,01 ₽"}}},
                    {"sku": "2000003", "price": {}},
                ]
            }
        ),
        "/search/": _tiles("115,49 ₽", "116 ₽", None),
    }
    assert [tile.sku for tile in catalog.find_cheaper("9999999").cheaper] == ["2000001", "1000000", "2000000"]
