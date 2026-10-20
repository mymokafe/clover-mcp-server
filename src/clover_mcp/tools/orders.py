"""Tools: list_orders, get_order, list_open_orders.

Clover orders support a `state` filter (open, paid, etc.) and time-range
filtering via ms-epoch `createdTime`. All list tools use 90-day windowing.
get_order expands lineItems and payments but never customers.cards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from clover_mcp.client import CloverClient
from clover_mcp.confirm import confirm_write, confirmation_required
from clover_mcp.shaping import _shape_line_item, project, shape_order, shape_order_discount
from clover_mcp.windowing import date_to_ms, split_window

if TYPE_CHECKING:
    from fastmcp import Context

_DEFAULT_LIMIT = 50

# States documented in the Clover API
_VALID_STATES = frozenset({"open", "paid", "refunded", "partially_refunded"})


def _today_utc() -> date:
    return datetime.now(tz=UTC).date()


def _parse_date(value: str, param_name: str) -> date:
    """Parse an ISO-8601 date string (YYYY-MM-DD)."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{param_name} must be an ISO-8601 date (YYYY-MM-DD), got {value!r}"
        ) from exc


async def list_orders(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    state: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    fields: list[str] | None = None,
    offset: int = 0,
    include_line_items: bool = False,
) -> list[dict[str, Any]]:
    """List orders within an optional date window and/or state filter.

    Dates are the store's local business days. `offset` skips that many orders
    (newest first) so callers can page past `limit`; `include_line_items` adds
    each order's items. Every order also gets `created_local` (store time).

    Defaults to today (UTC) when no dates are supplied. Uses 90-day chunking
    so multi-month queries work transparently. Results are capped at `limit`
    total items (default 50, max 200).

    `state` must be one of: open, paid, refunded, partially_refunded.
    Leave empty to return all states.

    Allowlisted projection is applied — customer card data is never included.
    `fields` further narrows each result to the named keys (cannot widen past
    the allowlist). This tool is read-only and does NOT modify any order state.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be ≥ 0")

    if state is not None and state not in _VALID_STATES:
        raise ValueError(f"state must be one of {sorted(_VALID_STATES)}, got {state!r}")

    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()

    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    results: list[dict[str, Any]] = []
    skipped = 0
    tz = await client.merchant_tzinfo()

    # Newest window first, so offset/limit page newest-to-oldest across chunks.
    for chunk_start, chunk_end in reversed(split_window(d_from, d_to)):
        if len(results) >= limit:
            break
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        filters = [
            f"createdTime>={ts_from}",
            f"createdTime<={ts_to}",
        ]
        if state is not None:
            filters.append(f"state={state}")

        params: dict[str, Any] = {"filter": filters}
        if include_line_items:
            params["expand"] = "lineItems"

        async for raw in client.iterate("/orders", limit=100, max_pages=1000, **params):
            if skipped < offset:
                skipped += 1
                continue
            shaped = shape_order(raw)
            created = raw.get("createdTime")
            if isinstance(created, int):
                shaped["created_local"] = datetime.fromtimestamp(created / 1000, tz=tz).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            results.append(shaped)
            if len(results) >= limit:
                break

    return project(results, fields)


async def get_order(
    client: CloverClient,
    order_id: str,
) -> dict[str, Any]:
    """Fetch a single order by ID, including line items, payments, and discounts.

    Expands lineItems (with their modifications + discounts), payments, and
    order-level discounts. Customer card data is never expanded. Returns a 404
    error if the order_id does not exist.

    This tool is read-only and does NOT modify any order state.
    """
    if not order_id or not order_id.strip():
        raise ValueError("order_id must not be empty")

    raw = await client.get(
        f"/orders/{order_id}",
        expand="lineItems.modifications,lineItems.discounts,payments,discounts",
    )
    return shape_order(raw)


async def list_open_orders(
    client: CloverClient,
) -> list[dict[str, Any]]:
    """Return all currently open orders for this merchant.

    Convenience wrapper around list_orders(state='open') with no date filter.
    Returns up to 200 open orders; open order counts above 200 are unusual
    in a healthy POS environment.

    Allowlisted projection is applied — customer card data is never included.
    This tool is read-only and does NOT modify any order state.
    """
    # No date filter — scan all open orders regardless of age
    params: dict[str, Any] = {"filter": ["state=open"]}
    results: list[dict[str, Any]] = []

    async for raw in client.iterate("/orders", limit=100, **params):
        results.append(shape_order(raw))
        if len(results) >= 200:
            break

    return results


# ── Write tools ───────────────────────────────────────────────────────────────


async def create_order(
    client: CloverClient,
    ctx: Context | None,
    note: str | None = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Create a new open order (no line items yet).

    Previews on dry_run, then asks for confirmation (MCP elicitation, or
    confirm=True) before POSTing. Add items with add_line_item. Requires ORDERS_W.
    Does NOT take payment — this server never captures payments.
    """
    body: dict[str, Any] = {}
    if note and note.strip():
        body["note"] = note.strip()

    if dry_run:
        return {"ok": True, "dry_run": True, "would_post_path": "/orders", "would_post_body": body}

    approved, how = await confirm_write(ctx, "Create a new open order?", confirm=confirm)
    if not approved:
        return confirmation_required(how)

    raw = await client.post("/orders", json=body)
    return {"ok": True, "order": shape_order(raw)}


async def add_line_item(
    client: CloverClient,
    ctx: Context | None,
    order_id: str,
    item_id: str,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Add a catalog item as a line item to an order.

    The line item's name/price are copied from the catalog item by Clover.
    Previews on dry_run, then asks for confirmation (MCP elicitation, or
    confirm=True) before POSTing. Requires ORDERS_W. Does NOT take payment.
    """
    if not order_id or not order_id.strip():
        raise ValueError("order_id must not be empty")
    if not item_id or not item_id.strip():
        raise ValueError("item_id must not be empty")

    path = f"/orders/{order_id}/line_items"
    body = {"item": {"id": item_id}}
    if dry_run:
        return {"ok": True, "dry_run": True, "would_post_path": path, "would_post_body": body}

    approved, how = await confirm_write(
        ctx, f"Add item {item_id} to order {order_id}?", confirm=confirm
    )
    if not approved:
        return confirmation_required(how)

    raw = await client.post(path, json=body)
    return {"ok": True, "line_item": _shape_line_item(raw)}


_DISCOUNT_AMOUNT_MAX = 100_000_000  # $1 000 000.00 in cents


async def apply_order_discount(
    client: CloverClient,
    ctx: Context | None,
    order_id: str,
    name: str | None = None,
    percentage: int | None = None,
    amount_cents: int | None = None,
    catalogue_discount_id: str | None = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Apply an order-level discount.

    Exactly one discount source must be given:
      - `percentage` (1–100, whole number), OR
      - `amount_cents` (1–100_000_000, a POSITIVE number of cents), OR
      - `catalogue_discount_id` (an id from list_discounts).

    Clover's API requires the `amount` field in the POST body to be NEGATIVE —
    this tool accepts a POSITIVE `amount_cents` and sends the negated value on
    the wire, so the caller never has to think in negative money.

    Catalogue path: Clover does NOT resolve `discount:{id}` server-side. This
    tool fetches the catalogue discount first (GET /discounts) and sends its
    name + percentage/amount inline alongside the `discount` reference. Fails
    clearly if the catalogue discount has neither field set.

    Pre-check: fetches the order (expand=lineItems,discounts) and previews the
    current discounts + a CLIENT-COMPUTED line-item subtotal — Clover exposes no
    computed order total, so the discount's effect on the total is never
    server-computed and is not returned here.

    Previews on dry_run, then asks for confirmation (MCP elicitation, or
    confirm=True) before POSTing. Requires ORDERS_W.
    """
    if not order_id or not order_id.strip():
        raise ValueError("order_id must not be empty")

    sources = [percentage is not None, amount_cents is not None, catalogue_discount_id is not None]
    if sum(sources) != 1:
        raise ValueError(
            "exactly one of percentage, amount_cents, or catalogue_discount_id must be given"
        )

    if percentage is not None and not (1 <= percentage <= 100):
        return {
            "ok": False,
            "reason": "bounds_violation",
            "message": f"percentage {percentage} is out of bounds (must be 1 – 100).",
        }
    if amount_cents is not None and not (1 <= amount_cents <= _DISCOUNT_AMOUNT_MAX):
        return {
            "ok": False,
            "reason": "bounds_violation",
            "message": (
                f"amount_cents {amount_cents} is out of bounds (must be 1 – {_DISCOUNT_AMOUNT_MAX})."
            ),
        }

    # Pre-check: fetch the order for a diff-style preview. Clover computes no
    # order total server-side, so we sum lineItems[].price ourselves and say so.
    order = await get_order(client, order_id)
    subtotal_cents = sum(li.get("price", 0) for li in order.get("line_items", []))
    current_discounts = order.get("discounts", [])

    body: dict[str, Any] = {}
    if catalogue_discount_id is not None:
        catalogue_raw = await client.get("/discounts")
        elements: list[dict[str, Any]] = catalogue_raw.get("elements", [])
        match = next((d for d in elements if d.get("id") == catalogue_discount_id), None)
        if match is None:
            return {
                "ok": False,
                "reason": "not_found",
                "message": f"catalogue_discount_id {catalogue_discount_id!r} not found in /discounts.",
            }
        cat_name = match.get("name")
        cat_percentage = match.get("percentage")
        cat_amount = match.get("amount")
        if cat_percentage is None and cat_amount is None:
            return {
                "ok": False,
                "reason": "invalid_catalogue_discount",
                "message": (
                    f"Catalogue discount {catalogue_discount_id!r} has neither "
                    "percentage nor amount set — cannot apply it."
                ),
            }
        body["discount"] = {"id": catalogue_discount_id}
        if cat_name is not None:
            body["name"] = cat_name
        if cat_percentage is not None:
            body["percentage"] = cat_percentage
        else:
            body["amount"] = cat_amount
    elif percentage is not None:
        if name:
            body["name"] = name
        body["percentage"] = percentage
    else:
        assert amount_cents is not None
        if name:
            body["name"] = name
        # Clover 400s on a positive amount — negate the caller-facing positive value.
        body["amount"] = -amount_cents

    path = f"/orders/{order_id}/discounts"

    preview = {
        "current_discounts": current_discounts,
        "line_item_subtotal_cents": subtotal_cents,
        "note": (
            "Clover exposes no computed order total — this subtotal is summed "
            "client-side from line-item prices. The discount's effect on the "
            "order total is not server-computed."
        ),
    }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_post_path": path,
            "would_post_body": body,
            "preview": preview,
        }

    approved, how = await confirm_write(
        ctx, f"Apply discount to order {order_id}: {body}?", confirm=confirm
    )
    if not approved:
        return confirmation_required(how)

    raw = await client.post(path, json=body)
    return {"ok": True, "discount": shape_order_discount(raw), "preview": preview}
