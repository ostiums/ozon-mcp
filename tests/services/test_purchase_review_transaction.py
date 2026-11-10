"""Purchase review orchestration never reaches an order/payment operation."""

from __future__ import annotations

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

_SPEC = spec_from_file_location("purchase_review", Path(__file__).parents[2] / "scripts/purchase_review.py")
assert _SPEC is not None
assert _SPEC.loader is not None
review = module_from_spec(_SPEC)
_SPEC.loader.exec_module(review)


@pytest.mark.parametrize(
    ("target_quantity", "target_checked", "fail_quote"), [(0, False, False), (3, False, False), (2, True, True)]
)
async def test_exact_target_review_and_finally_restoration(  # ruff: ignore[complex-structure] - scripted tool responses
    target_quantity: int, *, target_checked: bool, fail_quote: bool
) -> None:
    state = {"999999": (2, True)}
    if target_quantity:
        state[review.SKU] = (target_quantity, target_checked)
    baseline = deepcopy(state)
    saved: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {}
    calls: list[str] = []

    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:  # ruff: ignore[unused-async] - MCP double
        assert name in review.ALLOWED_TOOLS
        calls.append(name)
        if name == "session_status":
            return {"signed_in": True, "cart_enabled": True, "writes_enabled": False, "orders_enabled": False}
        if name == "product_details":
            return {"sku": review.SKU, "title": "BinaSport test fixture"}
        if name == "get_cart":
            return {
                "total_items": len(state),
                "item_count": len(state),
                "items": [{"id": sku, "quantity": qty, "checked": checked} for sku, (qty, checked) in state.items()],
            }
        if name == "set_cart_quantity":
            assert saved
            assert args["sku"] == review.SKU
            if args["quantity"]:
                state[review.SKU] = (args["quantity"], state.get(review.SKU, (0, False))[1])
            else:
                state.pop(review.SKU, None)
            return {"ok": True}
        if name == "select_cart_items":
            for sku, (qty, _) in state.items():
                state[sku] = (qty, sku in (args["skus"] or []))
            return {}
        if fail_quote:
            raise ValueError("quote unavailable")
        return {
            "available": True,
            "selected_items": {review.SKU: 1},
            "totals": {"order_total": "5087 ₽", "rows": [{"title": "Доставка", "value": "0 ₽"}]},
            "deliveries": [{"mode": "pickup"}],
            "payment_options": [{"kind": "bankcard", "selected": True}],
        }

    if fail_quote:
        with pytest.raises(ValueError, match="quote unavailable"):
            await review.review_purchase(call, saved.append, outcome)
    else:
        await review.review_purchase(call, saved.append, outcome)
        assert outcome["quote_complete"] is True
    assert state == baseline
    assert outcome["restoration_verified"] is True
    assert calls.count("get_checkout") == 1


@pytest.mark.parametrize(
    "cart",
    [
        {"error": "not a snapshot"},
        {"items": [], "total_items": None, "item_count": 0},
        {"items": [{"id": "185618681", "quantity": 1, "checked": None}], "total_items": 1, "item_count": 1},
    ],
)
def test_incomplete_original_cart_is_not_a_snapshot(cart: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"snapshot|quantity|selection"):
        review.cart_state(cart)


async def test_restoration_attempts_selection_after_quantity_failure() -> None:
    calls = []
    outcome = {}

    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:  # ruff: ignore[unused-async] - MCP double
        calls.append(name)
        if name == "set_cart_quantity":
            raise RuntimeError("uncertain quantity restoration")
        if name == "get_cart":
            return {"total_items": 1, "item_count": 1, "items": [{"id": review.SKU, "quantity": 1, "checked": False}]}
        return {}

    with pytest.raises(ValueError, match="restoration mismatch"):
        await review.restore_cart(call, {}, [], outcome)
    assert calls == ["set_cart_quantity", "select_cart_items", "get_cart"]
    assert outcome["restoration_verified"] is False
    assert outcome["restoration_request_errors"] == ["set_cart_quantity"]
