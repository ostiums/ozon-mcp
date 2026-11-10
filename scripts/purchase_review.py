"""Reversible purchase review via the same MCP tools exposed to users.

The caller supplies an MCP call function and durable private snapshot writer.
Only the fixed review SKU may change quantity. No order/payment/configuration
operation is reachable. A complete cart snapshot precedes every mutation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SKU = "185618681"
ALLOWED_TOOLS = frozenset({
    "session_status",
    "product_details",
    "get_cart",
    "set_cart_quantity",
    "select_cart_items",
    "get_checkout",
})


def cart_state(cart: dict[str, Any]) -> dict[str, tuple[int, bool]]:
    items = cart.get("items")
    if not isinstance(items, list) or cart.get("total_items") != len(items) or cart.get("item_count") != len(items):
        raise ValueError("Cart is not a complete declared snapshot; no safe mutation/restoration baseline.")
    state = {}
    for item in items:
        sku, quantity, checked = item.get("id"), item.get("quantity"), item.get("checked")
        if not isinstance(sku, str) or not sku.isascii() or not sku.isdigit() or sku in state:
            raise ValueError("Missing or duplicate cart SKU.")
        if type(quantity) is not int or quantity <= 0 or type(checked) is not bool:
            raise ValueError("Unknown cart quantity or selection.")
        state[sku] = (quantity, checked)
    return state


async def review_purchase(
    call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    save_original: Callable[[dict[str, Any]], None],
    outcome: dict[str, Any],
) -> None:
    """Attempt one quote, always restore and verify after attempting a mutation.

    ``outcome`` is updated even when the quote raises. A failed restoration is
    a hard failure requiring operator attention, never a successful review.
    """
    status = await call("session_status", {})
    if not (
        status.get("signed_in")
        and status.get("cart_enabled")
        and status.get("writes_enabled") is False
        and status.get("orders_enabled") is False
    ):
        raise ValueError("Signed-in session with CART=1 WRITES=0 ORDERS=0 required.")
    product = await call("product_details", {"sku_or_url": SKU, "with_description": True})
    if product.get("sku") != SKU or not product.get("title"):
        raise ValueError("Exact review product was not established.")
    outcome["product"] = product
    original = await call("get_cart", {})
    baseline = cart_state(original)
    save_original(original)  # Must complete before the first mutation attempt.
    outcome["original_captured"] = True
    outcome["restoration_verified"] = False
    selected = [sku for sku, (_, checked) in baseline.items() if checked]
    try:
        outcome["mutation_attempted"] = True
        changed = await call("set_cart_quantity", {"sku": SKU, "quantity": 1})
        if changed.get("ok") is not True:
            raise ValueError("Target quantity was not confirmed.")
        await call("select_cart_items", {"skus": [SKU], "mode": "only"})
        before_quote = cart_state(await call("get_cart", {}))
        expected = {sku: (quantity, False) for sku, (quantity, _) in baseline.items()}
        expected[SKU] = (1, True)
        if before_quote != expected:
            raise ValueError("Review cart differs from one target and untouched original quantities.")
        quote = await call("get_checkout", {"shipment_items": True})
        if not quote.get("available") or quote.get("selected_items") != {SKU: 1}:
            raise ValueError("Checkout did not confirm exactly one review SKU.")
        outcome["quote"] = quote
        outcome["quote_complete"] = bool(
            quote.get("totals", {}).get("order_total")
            and quote.get("totals", {}).get("rows")
            and quote.get("deliveries")
            and any(p.get("selected") for p in quote.get("payment_options", []))
        )
    finally:
        # These idempotent assignments restore only the target's quantity and
        # the original selection. Unrelated quantities are never overwritten.
        await restore_cart(call, baseline, selected, outcome)


async def restore_cart(
    call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    baseline: dict[str, tuple[int, bool]],
    selected: list[str],
    outcome: dict[str, Any],
) -> None:
    """Attempt both restorations even if one request has an uncertain outcome."""
    operations = [
        ("set_cart_quantity", {"sku": SKU, "quantity": baseline.get(SKU, (0, False))[0]}),
        ("select_cart_items", {"skus": selected or None, "mode": "only" if selected else "none"}),
    ]
    for name, args in operations:
        try:
            await call(name, args)
        except Exception:  # ruff: ignore[blind-except] - still attempt remaining restoration and readback
            outcome.setdefault("restoration_request_errors", []).append(name)
    final = cart_state(await call("get_cart", {}))
    outcome["restoration_verified"] = final == baseline
    if final != baseline:
        raise ValueError("Cart restoration mismatch; operator attention required.")
