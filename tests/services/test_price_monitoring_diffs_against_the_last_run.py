"""The favorites price watch compares against the previous call, not a schedule.

The comparison is only as old as the last snapshot, so the first call has
nothing to say and every later one speaks against what it was handed before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.services import favorites, monitoring
from ozon_mcp.settings import get_settings
from support import FakeSession, page

if TYPE_CHECKING:
    from pathlib import Path

TITLES = {"1": "Первый", "2": "Второй"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "price_history.json"
    monkeypatch.setattr(get_settings(), "monitor_store", path)
    return path


def test_the_first_run_has_nothing_to_compare_with(store: Path) -> None:
    first = monitoring.record({"1": 100}, TITLES)
    assert first.drops == []
    assert first.rises == []
    assert store.exists()


def test_a_drop_and_a_rise_are_told_apart(store: Path) -> None:
    monitoring.record({"1": 100, "2": 100}, TITLES)
    changed = monitoring.record({"1": 80, "2": 150}, TITLES)
    assert [(change.sku, change.was, change.now, change.delta) for change in changed.drops] == [("1", 100, 80, -20)]
    assert [(change.sku, change.was, change.now, change.delta) for change in changed.rises] == [("2", 100, 150, 50)]
    # The title travels with the snapshot, so a diff can be read without a lookup.
    assert changed.drops[0].title == "Первый"


def test_an_unchanged_price_is_not_a_change(store: Path) -> None:
    monitoring.record({"1": 100}, TITLES)
    same = monitoring.record({"1": 100}, TITLES)
    assert same.drops == []
    assert same.rises == []


def test_a_price_that_vanished_is_not_a_drop_to_zero(store: Path) -> None:
    monitoring.record({"1": 100}, TITLES)
    gone = monitoring.record({}, TITLES)
    assert gone.drops == []
    assert gone.rises == []


def test_favorite_monitoring_reads_prices_after_the_first_page(store: Path, session: FakeSession) -> None:
    session.pages = {
        "/my/favorites": page(
            tileGridDesktop={"items": [{"sku": "1", "price": [{"text": "100 ₽", "textStyle": "PRICE"}]}]},
            favoriteCounter={"iconHeader": {"counter": {"text": "2"}}},
            paginator={"nextPage": "/favorites-next"},
        ),
        "/favorites-next": page(
            tileGridDesktop={"items": [{"sku": "2", "price": [{"text": "80 ₽", "textStyle": "PRICE"}]}]}
        ),
    }
    monitoring.record({"1": 100, "2": 100}, TITLES)
    changed = favorites.check_favorite_price_drops()
    assert [(change.sku, change.now) for change in changed.drops] == [("2", 80)]
    assert changed.removed == []


def test_favorite_monitoring_keeps_kopecks_in_ruble_amounts(store: Path, session: FakeSession) -> None:
    monitoring.record({"1": 100}, TITLES)
    session.pages = {
        "/my/favorites": page(
            tileGridDesktop={"items": [{"sku": "1", "price": [{"text": "99,50 ₽", "textStyle": "PRICE"}]}]}
        )
    }
    changed = favorites.check_favorite_price_drops()
    assert [(change.was, change.now, change.delta) for change in changed.drops] == [(100, 99.5, -0.5)]


def test_a_known_zero_price_is_still_compared(store: Path) -> None:
    monitoring.record({"1": 1}, TITLES)
    changed = monitoring.record({"1": 0}, TITLES)
    assert [(change.was, change.now, change.delta) for change in changed.drops] == [(1, 0, -1)]
    assert changed.removed == []


def test_an_incomplete_favorites_read_does_not_replace_the_snapshot(store: Path, session: FakeSession) -> None:
    monitoring.record({"1": 100, "2": 100}, TITLES)
    before = store.read_text(encoding="utf-8")
    session.pages = {
        "/my/favorites": page(
            tileGridDesktop={"items": [{"sku": "1", "price": [{"text": "100 ₽", "textStyle": "PRICE"}]}]},
            favoriteCounter={"iconHeader": {"counter": {"text": "2"}}},
            paginator={"nextPage": "/favorites-next"},
        )
    }
    with pytest.raises(OzonError, match="incomplete"):
        favorites.check_favorite_price_drops()
    assert store.read_text(encoding="utf-8") == before
