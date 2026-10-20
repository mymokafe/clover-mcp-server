"""Tools: get_sales_summary, list_payments, list_refunds, list_credits,
get_sales_by_employee, get_tips_by_employee, get_sales_by_hour.

get_sales_summary/list_payments/list_refunds use 90-day windowing so arbitrary
date ranges work transparently. Payments are filtered to result=SUCCESS for
counts; voids and refunds are reported separately per the plan's Sales
Semantics spec. list_credits is a simple paginated listing (no date window —
mirrors list_item_groups/list_discounts). All require PAYMENTS_R except
get_top_items (ORDERS_R, in this module for its aggregation logic).

get_sales_by_employee/get_tips_by_employee/get_sales_by_hour are thin
aggregators over the same SUCCESS-payments window used by get_sales_summary —
they add grouping (by employee or by local hour) but reuse the same filter
and windowing pattern.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clover_mcp.client import CloverClient
from clover_mcp.errors import CloverAPIError
from clover_mcp.formatting import format_money
from clover_mcp.shaping import project, shape_credit, shape_payment, shape_refund
from clover_mcp.windowing import date_to_ms, split_window

if TYPE_CHECKING:
    from fastmcp import Context

_DEFAULT_LIMIT = 50
_CREDITS_MAX = 1000


async def _log(ctx: Context | None, message: str) -> None:
    """Best-effort progress log to the client — never fails the tool if the
    client doesn't support logging."""
    if ctx is None:
        return
    with contextlib.suppress(Exception):  # logging is optional; ignore unsupported/transport
        await ctx.info(message)


def _today_utc() -> date:
    return datetime.now(tz=UTC).date()


def _parse_date(value: str, param_name: str) -> date:
    """Parse an ISO-8601 date string (YYYY-MM-DD); raise ValueError with context."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{param_name} must be an ISO-8601 date (YYYY-MM-DD), got {value!r}"
        ) from exc


def _money(cents: int, currency: str) -> dict[str, Any]:
    return {"amount": cents, "formatted": format_money(cents, currency)}


async def get_sales_summary(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return an aggregated sales summary for the given date window.

    Defaults to today (UTC) when no dates are supplied. Uses 90-day chunking
    so multi-month or full-year queries work transparently.

    Rules applied:
    - Only result=SUCCESS payments are counted in payment_count and gross_sales.
    - Refunds come from the dedicated /refunds endpoint (Clover refunds are
      separate objects, not negative-amount payments). Voids are counted from
      voided payments. Both are tallied separately (refund_count/refund_amount,
      void_count) — never silently netted into payment_count.
    - net_sales = gross_sales - refund_amount.
    - Tips and taxes are broken out as their own line items.
    - Service charges are NOT reported separately: Clover exposes them on the
      order only as a percentage (no per-order computed amount), and the amount
      customers actually paid is already included in gross_sales via the payment
      totals. Breaking them out would require recomputing percentage × subtotal
      per order, which is out of scope for this summary.
    - Offline payments are included; a warning flag is set if any are present.
    - Currency is fetched from the merchant record — never defaulted to USD.

    Requires PAYMENTS_R (covers payments and refunds).
    This tool does NOT support payment capture, refund, or void actions.
    """
    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()

    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    currency = await client.merchant_currency()
    timezone = await client.merchant_timezone()

    gross_cents = 0
    tip_cents = 0
    tax_cents = 0
    payment_count = 0
    refund_count = 0
    void_count = 0
    refund_cents = 0
    has_offline = False
    by_tender: dict[str, int] = {}

    # Collect all SUCCESS payments across windowed chunks
    chunks = list(split_window(d_from, d_to))
    if len(chunks) > 1:
        await _log(ctx, f"Aggregating sales over {len(chunks)} 90-day windows…")
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        if len(chunks) > 1:
            await _log(ctx, f"Window {i}/{len(chunks)}: {chunk_start} → {chunk_end}")
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        params: dict[str, Any] = {
            "filter": [
                f"createdTime>={ts_from}",
                f"createdTime<={ts_to}",
                "result=SUCCESS",
            ],
        }

        async for raw in client.iterate("/payments", limit=100, **params):
            # Counts only SUCCESS payments — FAIL/AUTH/PRE_AUTH excluded by filter
            gross_cents += raw.get("amount", 0)
            tip_cents += raw.get("tipAmount", 0)
            tax_cents += raw.get("taxAmount", 0)
            payment_count += 1

            if raw.get("offline"):
                has_offline = True

            tender_label = "UNKNOWN"
            if isinstance(raw.get("tender"), dict):
                tender_label = raw["tender"].get("label") or raw["tender"].get("id") or "UNKNOWN"
            by_tender[tender_label] = by_tender.get(tender_label, 0) + raw.get("amount", 0)

    # Collect voided payments in the same windows
    for chunk_start, chunk_end in split_window(d_from, d_to):
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        void_params: dict[str, Any] = {
            "filter": [
                f"createdTime>={ts_from}",
                f"createdTime<={ts_to}",
                "voided=true",
            ],
        }

        async for _ in client.iterate("/payments", limit=100, **void_params):
            void_count += 1

    # Refunds — the dedicated /refunds endpoint is authoritative. Clover refunds
    # are separate objects with a positive `amount` (cents), not negative-amount
    # payments. Covered by PAYMENTS_R.
    for chunk_start, chunk_end in split_window(d_from, d_to):
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        refund_params: dict[str, Any] = {
            "filter": [
                f"createdTime>={ts_from}",
                f"createdTime<={ts_to}",
            ],
        }

        async for raw in client.iterate("/refunds", limit=100, **refund_params):
            refund_count += 1
            refund_cents += raw.get("amount", 0)  # positive cents

    net_cents = gross_cents - refund_cents

    avg_ticket = (gross_cents // payment_count) if payment_count > 0 else 0

    by_tender_fmt = {label: _money(amount, currency) for label, amount in by_tender.items()}

    result: dict[str, Any] = {
        "window": {
            "from": d_from.isoformat(),
            "to": d_to.isoformat(),
            "timezone": timezone,
        },
        "currency": currency,
        "gross_sales": _money(gross_cents, currency),
        "net_sales": _money(net_cents, currency),
        "tax_collected": _money(tax_cents, currency),
        "tips_collected": _money(tip_cents, currency),
        "refund_amount": _money(refund_cents, currency),
        "payment_count": payment_count,
        "refund_count": refund_count,
        "void_count": void_count,
        "average_ticket": _money(avg_ticket, currency),
        "by_tender": by_tender_fmt,
    }

    if has_offline:
        result["note"] = "includes offline payments pending sync"

    return result


async def get_top_items(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Return the best-selling items in a date window, ranked by units sold.

    Aggregates line items across orders in the window (default: today, UTC),
    90-day chunked. Each line item counts as one unit and its `price` (cents) as
    revenue — Clover represents quantity via repeated line items, so per-unit
    weighted items (unitQty) are approximated as a single unit.

    Requires ORDERS_R.
    """
    if top_n < 1 or top_n > 100:
        raise ValueError("top_n must be between 1 and 100")

    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()
    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    currency = await client.merchant_currency()

    # name -> [units, revenue_cents]
    tally: dict[str, list[int]] = {}
    for chunk_start, chunk_end in split_window(d_from, d_to):
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())
        params: dict[str, Any] = {
            "filter": [f"createdTime>={ts_from}", f"createdTime<={ts_to}"],
        }
        async for order in client.iterate("/orders", limit=100, expand="lineItems", **params):
            line_items = order.get("lineItems", {})
            elements = line_items.get("elements", []) if isinstance(line_items, dict) else []
            for li in elements:
                if not isinstance(li, dict) or li.get("refunded"):
                    continue
                name = li.get("name") or "(unnamed)"
                entry = tally.setdefault(name, [0, 0])
                entry[0] += 1
                entry[1] += li.get("price", 0) or 0

    ranked = sorted(tally.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)[:top_n]
    items = [
        {"name": name, "units_sold": units, "revenue": _money(rev, currency)}
        for name, (units, rev) in ranked
    ]

    return {
        "window": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "currency": currency,
        "items": items,
        "count": len(items),
    }


async def list_payments(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List payments within an optional date window.

    Defaults to today (UTC) when no dates are supplied. Uses 90-day chunking
    so multi-month queries work transparently. Results are limited to `limit`
    total items (default 50, max 200). Only result=SUCCESS payments are
    returned; use get_sales_summary for void/refund counts.

    Allowlisted fields only — card transaction details are never included.
    `fields` further narrows each result to the named keys (cannot widen past
    the allowlist). This tool does NOT support payment capture, refund, or
    void actions.
    """
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")

    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()

    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    results: list[dict[str, Any]] = []

    for chunk_start, chunk_end in split_window(d_from, d_to):
        if len(results) >= limit:
            break
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        params: dict[str, Any] = {
            "filter": [
                f"createdTime>={ts_from}",
                f"createdTime<={ts_to}",
                "result=SUCCESS",
            ],
        }

        chunk_limit = min(100, limit - len(results))
        async for raw in client.iterate("/payments", limit=chunk_limit, **params):
            results.append(shape_payment(raw))
            if len(results) >= limit:
                break

    return project(results, fields)


async def list_refunds(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """List refunds within an optional date window.

    Defaults to today (UTC) when no dates are supplied. Uses 90-day chunking
    so multi-month queries work transparently. Results are limited to `limit`
    total items (default 50, max 200). Clover refunds are separate objects with
    a positive `amount` (cents) — not negative payments.

    Allowlisted fields only — card/transaction detail is never included.
    Requires PAYMENTS_R. This tool does NOT issue refunds.
    """
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")

    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()

    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    results: list[dict[str, Any]] = []

    for chunk_start, chunk_end in split_window(d_from, d_to):
        if len(results) >= limit:
            break
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())

        params: dict[str, Any] = {
            "filter": [f"createdTime>={ts_from}", f"createdTime<={ts_to}"],
        }

        chunk_limit = min(100, limit - len(results))
        async for raw in client.iterate("/refunds", limit=chunk_limit, **params):
            results.append(shape_refund(raw))
            if len(results) >= limit:
                break

    return results


async def _employee_names(client: CloverClient) -> tuple[dict[str, str | None], str | None]:
    """Best-effort {employee_id: name} map. Degrades to an empty map (names
    reported as None) with a note if EMPLOYEES_R hasn't been granted."""
    from clover_mcp.tools.employees import list_employees

    try:
        body = await list_employees(client)
    except CloverAPIError:
        return {}, "employee names unavailable (EMPLOYEES_R not granted)"
    names = {emp["id"]: emp.get("name") for emp in body["employees"]}
    return names, None


def _group_by_employee(
    payments: list[dict[str, Any]], amount_key: str
) -> dict[str, tuple[int, int]]:
    """Group raw payments by employee.id, summing `amount_key` per group.

    Returns {employee_id: (total_cents, payment_count)}; unattributed payments
    are grouped under "unassigned".
    """
    groups: dict[str, list[int]] = {}
    for raw in payments:
        employee = raw.get("employee")
        employee_id = employee.get("id") if isinstance(employee, dict) else None
        key = employee_id or "unassigned"
        entry = groups.setdefault(key, [0, 0])
        entry[0] += raw.get(amount_key, 0)
        entry[1] += 1
    return {key: (total, count) for key, (total, count) in groups.items()}


async def _collect_success_payments(
    client: CloverClient, d_from: date, d_to: date
) -> list[dict[str, Any]]:
    """Collect raw result=SUCCESS payments across 90-day windowed chunks."""
    payments: list[dict[str, Any]] = []
    for chunk_start, chunk_end in split_window(d_from, d_to):
        ts_from = date_to_ms(chunk_start, end_of_day=False, tz=await client.merchant_tzinfo())
        ts_to = date_to_ms(chunk_end, end_of_day=True, tz=await client.merchant_tzinfo())
        params: dict[str, Any] = {
            "filter": [
                f"createdTime>={ts_from}",
                f"createdTime<={ts_to}",
                "result=SUCCESS",
            ],
        }
        async for raw in client.iterate("/payments", limit=100, **params):
            payments.append(raw)
    return payments


async def get_sales_by_employee(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return gross sales grouped by employee for the given date window.

    Defaults to today (UTC); 90-day chunked. Only result=SUCCESS payments are
    counted; payments with no employee attribution are grouped as "unassigned".
    Employee names are best-effort (requires EMPLOYEES_R) — if unavailable,
    only IDs are returned and a note explains why.

    Requires PAYMENTS_R.
    """
    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()
    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    currency = await client.merchant_currency()
    timezone = await client.merchant_timezone()

    payments = await _collect_success_payments(client, d_from, d_to)
    grouped = _group_by_employee(payments, "amount")
    names, note = await _employee_names(client)

    ranked = sorted(grouped.items(), key=lambda kv: kv[1][0], reverse=True)
    by_employee = [
        {
            "employee_id": employee_id,
            "employee_name": names.get(employee_id),
            "gross_sales": _money(total, currency),
            "payment_count": count,
        }
        for employee_id, (total, count) in ranked
    ]

    result: dict[str, Any] = {
        "window": {"from": d_from.isoformat(), "to": d_to.isoformat(), "timezone": timezone},
        "currency": currency,
        "by_employee": by_employee,
        "payment_count": len(payments),
    }
    if note:
        result["note"] = note
    return result


async def get_tips_by_employee(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return a tip-out sheet: tips collected grouped by employee.

    Defaults to today (UTC); 90-day chunked. Only result=SUCCESS payments are
    counted; payments with no employee attribution are grouped as "unassigned".
    Employee names are best-effort (requires EMPLOYEES_R) — if unavailable,
    only IDs are returned and a note explains why.

    Requires PAYMENTS_R.
    """
    d_from = _parse_date(date_from, "date_from") if date_from else await client.merchant_today()
    d_to = _parse_date(date_to, "date_to") if date_to else await client.merchant_today()
    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    currency = await client.merchant_currency()
    timezone = await client.merchant_timezone()

    payments = await _collect_success_payments(client, d_from, d_to)
    grouped = _group_by_employee(payments, "tipAmount")
    names, note = await _employee_names(client)

    ranked = sorted(grouped.items(), key=lambda kv: kv[1][0], reverse=True)
    by_employee = [
        {
            "employee_id": employee_id,
            "employee_name": names.get(employee_id),
            "tips": _money(total, currency),
            "payment_count": count,
        }
        for employee_id, (total, count) in ranked
    ]
    total_tips = sum(total for total, _count in grouped.values())

    result: dict[str, Any] = {
        "window": {"from": d_from.isoformat(), "to": d_to.isoformat(), "timezone": timezone},
        "currency": currency,
        "by_employee": by_employee,
        "total_tips": _money(total_tips, currency),
        "payment_count": len(payments),
    }
    if note:
        result["note"] = note
    return result


async def get_sales_by_hour(
    client: CloverClient,
    date: str | None = None,
) -> dict[str, Any]:
    """Return gross sales bucketed by local hour-of-day for a single day.

    Defaults to today (UTC). Only result=SUCCESS payments are counted. Hours
    are bucketed in the merchant's local timezone; if the merchant timezone is
    missing or invalid, buckets fall back to UTC and a note is added. Only
    hours with at least one payment are included.

    Requires PAYMENTS_R.
    """
    d = _parse_date(date, "date") if date else await client.merchant_today()

    currency = await client.merchant_currency()
    tz_name = await client.merchant_timezone()

    note: str | None = None
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        if not tz_name:
            note = "merchant timezone unavailable — hour buckets are in UTC"
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
        note = "merchant timezone unavailable — hour buckets are in UTC"

    # Fetch a UTC window padded ±1 day: the local day for any timezone offset
    # falls entirely inside [d-1, d+1] UTC. We then keep only payments whose
    # LOCAL date is d, so evening-local sales (which land on the next UTC day)
    # are counted and early-UTC sales from the prior local day are excluded.
    payments = await _collect_success_payments(client, d - timedelta(days=1), d + timedelta(days=1))

    buckets: dict[int, list[int]] = {}
    for raw in payments:
        created_ms = raw.get("createdTime", 0)
        local_dt = datetime.fromtimestamp(created_ms / 1000, tz=tz)
        if local_dt.date() != d:
            continue
        entry = buckets.setdefault(local_dt.hour, [0, 0])
        entry[0] += raw.get("amount", 0)
        entry[1] += 1

    by_hour = [
        {"hour": hour, "gross_sales": _money(total, currency), "payment_count": count}
        for hour, (total, count) in sorted(buckets.items())
    ]

    result: dict[str, Any] = {
        "date": d.isoformat(),
        "timezone": tz_name if not note else "UTC",
        "currency": currency,
        "by_hour": by_hour,
    }
    if note:
        result["note"] = note
    return result


async def list_credits(client: CloverClient) -> dict[str, Any]:
    """Return the merchant's credits (store/account-credit adjustments, up to 1000).

    Sandbox-verified 2026-07-05: `GET /credits` returns 200 with a standard
    paginated `{elements:[],href}` container; the sandbox has no credits
    provisioned so the element shape is UNVERIFIED (same situation as
    list_item_groups/list_discounts before them). Shaped conservatively via
    shape_credit — revisit the allowlist once a live credit exists to audit.

    Requires PAYMENTS_R.
    """
    credits_list: list[dict[str, Any]] = []
    async for el in client.iterate("/credits", limit=100):
        credits_list.append(shape_credit(el))
        if len(credits_list) >= _CREDITS_MAX:
            break
    return {"credits": credits_list, "count": len(credits_list)}


def _parse_hhmm(value: str, param_name: str) -> tuple[int, int]:
    """Parse a local clock time 'HH:MM' (24-hour) into (hour, minute)."""
    try:
        hour_s, minute_s = value.strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{param_name} must be a 24-hour time HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{param_name} must be a 24-hour time HH:MM, got {value!r}")
    return hour, minute


async def get_item_sales(
    client: CloverClient,
    date_from: str | None = None,
    date_to: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    name_contains: list[str] | None = None,
) -> dict[str, Any]:
    """Return EVERY item sold in a window, with units and revenue — no top-N cap.

    Dates are the store's LOCAL business days (merchant timezone). Optional
    start_time/end_time ('HH:MM', 24-hour, local) narrow the window on the
    first/last day, e.g. date_from=today, start_time='06:00'. Walks every order
    in the window (all pages). Refunded line items are excluded.

    name_contains: optional list of case-insensitive substrings; when given,
    only items whose name contains any of them are returned, and
    `matching_units` totals them (e.g. ["latte"] counts every latte).
    """
    tz = await client.merchant_tzinfo()
    today = await client.merchant_today()
    d_from = _parse_date(date_from, "date_from") if date_from else today
    d_to = _parse_date(date_to, "date_to") if date_to else d_from
    if d_from > d_to:
        raise ValueError(f"date_from ({d_from}) must be ≤ date_to ({d_to})")

    sh, sm = _parse_hhmm(start_time, "start_time") if start_time else (0, 0)
    start_dt = datetime(d_from.year, d_from.month, d_from.day, sh, sm, tzinfo=tz)
    if end_time:
        eh, em = _parse_hhmm(end_time, "end_time")
        end_dt = datetime(d_to.year, d_to.month, d_to.day, eh, em, 59, 999000, tzinfo=tz)
    else:
        end_dt = datetime(d_to.year, d_to.month, d_to.day, 23, 59, 59, 999000, tzinfo=tz)
    if start_dt > end_dt:
        raise ValueError("start of window must be before end of window")
    start_ms, end_ms = int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

    currency = await client.merchant_currency()
    needles = [n.lower() for n in (name_contains or []) if n and n.strip()]

    tally: dict[str, list[int]] = {}
    order_count = 0
    first_ms: int | None = None
    last_ms: int | None = None
    for chunk_start, chunk_end in split_window(d_from, d_to):
        lo = max(start_ms, date_to_ms(chunk_start, end_of_day=False, tz=tz))
        hi = min(end_ms, date_to_ms(chunk_end, end_of_day=True, tz=tz))
        params: dict[str, Any] = {"filter": [f"createdTime>={lo}", f"createdTime<={hi}"]}
        async for order in client.iterate(
            "/orders", limit=100, max_pages=1000, expand="lineItems", **params
        ):
            order_count += 1
            created = order.get("createdTime")
            if isinstance(created, int):
                first_ms = created if first_ms is None else min(first_ms, created)
                last_ms = created if last_ms is None else max(last_ms, created)
            line_items = order.get("lineItems", {})
            elements = line_items.get("elements", []) if isinstance(line_items, dict) else []
            for li in elements:
                if not isinstance(li, dict) or li.get("refunded"):
                    continue
                name = li.get("name") or "(unnamed)"
                entry = tally.setdefault(name, [0, 0])
                entry[0] += 1
                entry[1] += li.get("price", 0) or 0

    if needles:
        tally = {k: v for k, v in tally.items() if any(n in k.lower() for n in needles)}
    ranked = sorted(tally.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
    items = [
        {"name": name, "units_sold": units, "revenue": _money(rev, currency)}
        for name, (units, rev) in ranked
    ]

    def _local(ms: int | None) -> str | None:
        if ms is None:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=tz).strftime("%Y-%m-%d %H:%M")

    result: dict[str, Any] = {
        "window": {
            "from": start_dt.strftime("%Y-%m-%d %H:%M"),
            "to": end_dt.strftime("%Y-%m-%d %H:%M"),
            "timezone": str(tz),
        },
        "currency": currency,
        "order_count": order_count,
        "first_order_local": _local(first_ms),
        "last_order_local": _local(last_ms),
        "total_units": sum(i["units_sold"] for i in items),
        "total_revenue": _money(sum(v[1] for v in tally.values()), currency),
        "items": items,
        "item_count": len(items),
    }
    if needles:
        result["matching"] = needles
    return result
