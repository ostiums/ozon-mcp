"""The public reader runs only when selected. Composer remains the default."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from ozon_mcp.errors import OzonError
from ozon_mcp.parsing.public_storefront import document_from_html
from ozon_mcp.services import catalog, public_catalog
from ozon_mcp.settings import OzonSettings
from support import page

if TYPE_CHECKING:
    from support import FakeSession

FIXTURE = Path("tests/fixtures/public-storefront-search.html")


def test_default_backend_is_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_STOREFRONT", raising=False)
    assert OzonSettings().storefront == "composer"
    with pytest.raises(ValidationError):
        OzonSettings(storefront="browser-scrape")


def test_public_search_reads_the_open_page_and_not_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Session:
        def read_public_page(self, path: str) -> dict[str, object]:
            calls.append(path)
            return document_from_html(FIXTURE.read_text(encoding="utf-8"))

        def fetch(self, path: str, backend: str = "composer") -> dict[str, object]:
            raise AssertionError(path + backend)

    settings = OzonSettings(storefront="public")
    monkeypatch.setattr(public_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(public_catalog, "get_session", Session)
    tiles = catalog.search("HX Nutrition Zero Isolate", sort="cheap", limit=24)
    assert calls == ["/search/?page=1&text=HX%20Nutrition%20Zero%20Isolate"]
    assert [tile.sku for tile in tiles] == ["1600000001", "1600000002"]
    assert tiles[0].price == "2499"
    assert tiles[1].price is None
    assert "brand_was_predicted" not in calls[0]


def test_public_product_keeps_only_the_requested_sku(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def read_public_page(self, path: str) -> dict[str, object]:
            return document_from_html(FIXTURE.read_text(encoding="utf-8"))

    settings = OzonSettings(storefront="public")
    monkeypatch.setattr(public_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(public_catalog, "get_session", Session)
    card = catalog.product_details("1600000001", with_description=True, with_reviews=True)
    assert card.sku == "1600000001"
    assert card.price == "2499"
    assert card.characteristics[0].name == "Белки"
    assert card.reviews is None
    with pytest.raises(OzonError, match="did not declare"):
        catalog.product_details("999999999")


def test_public_mode_does_not_fall_back_to_composer_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public_catalog, "get_settings", lambda: OzonSettings(storefront="public"))
    with pytest.raises(OzonError, match="does not call the composer API"):
        catalog.get_search_filters("isolate")


def test_composer_search_does_not_open_the_public_reader(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    monkeypatch.setattr(public_catalog, "get_settings", lambda: OzonSettings(storefront="composer"))
    session.pages = {
        "/search/": page(tileGridDesktop={"items": [{"sku": "1600000001", "tileName": {"text": "Изолят"}}]})
    }
    session.read_public_page = lambda path: (_ for _ in ()).throw(AssertionError(path))  # type: ignore[attr-defined]
    found = catalog.search("изолят", limit=1)
    assert found[0].sku == "1600000001"


def test_unhealthy_public_page_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(path: str) -> dict[str, object]:
        del path
        raise RuntimeError("Ozon shows an antibot or challenge page")

    monkeypatch.setattr(public_catalog, "get_settings", lambda: OzonSettings(storefront="public"))
    monkeypatch.setattr(public_catalog, "get_session", lambda: SimpleNamespace(read_public_page=blocked))
    with pytest.raises(RuntimeError, match="challenge"):
        catalog.search("isolate", limit=1)
