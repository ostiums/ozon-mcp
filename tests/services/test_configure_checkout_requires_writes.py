"""Every checkout configuration is gated before reading or changing account state."""

from typing import Any

import pytest

from ozon_mcp.errors import WritesDisabledError
from ozon_mcp.services import checkout
from ozon_mcp.settings import get_settings
from support import FakeSession


@pytest.mark.parametrize("orders_enabled", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"payment": "СБП"},
        {"pickup_point": "№1449460"},
        {"points": 100},
        {"points": 0},
        {"pay_after_receipt": True},
        {"pay_after_receipt": False},
    ],
)
def test_disabled_writes_prevent_every_checkout_option(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any], *, orders_enabled: bool
) -> None:
    monkeypatch.setattr(get_settings(), "enable_orders", orders_enabled)
    monkeypatch.setattr(get_settings(), "enable_writes", False)
    with pytest.raises(WritesDisabledError):
        checkout.configure_checkout(**options)
    assert session.fetched == []
    assert session.posted == []
    assert session.performed == []


async def test_tool_schema_requires_both_cost_and_contents_confirmation() -> None:
    from ozon_mcp.main import mcp  # ruff: ignore[import-outside-top-level]

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools["place_order"].inputSchema["required"]) == {"confirm_total", "confirm_items"}
    assert "GATED by writes_enabled" in (tools["configure_checkout"].description or "")
