"""FastMCP server: tool registration and the startup permission self-check."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from clover_mcp.client import CloverClient
from clover_mcp.config import Config, load_config
from clover_mcp.errors import CloverAPIError
from clover_mcp.prompts import register_prompts
from clover_mcp.remote import (
    auth_context,
    build_auth_provider,
    load_tenants,
    request_tenant_key,
    tenant_config,
)
from clover_mcp.resources import register_resources
from clover_mcp.tools.ai import detect_sales_anomalies as _detect_sales_anomalies
from clover_mcp.tools.ai import draft_customer_message as _draft_customer_message
from clover_mcp.tools.ai import inventory_reorder_suggestions as _inventory_reorder_suggestions
from clover_mcp.tools.ai import suggest_item_categories as _suggest_item_categories
from clover_mcp.tools.ai import summarize_sales as _summarize_sales
from clover_mcp.tools.customers import create_customer as _create_customer
from clover_mcp.tools.customers import get_customer as _get_customer
from clover_mcp.tools.customers import search_customers as _search_customers
from clover_mcp.tools.customers import update_customer as _update_customer
from clover_mcp.tools.employees import get_employee as _get_employee
from clover_mcp.tools.employees import list_active_shifts as _list_active_shifts
from clover_mcp.tools.employees import list_employees as _list_employees
from clover_mcp.tools.employees import list_roles as _list_roles
from clover_mcp.tools.employees import list_shifts as _list_shifts
from clover_mcp.tools.inventory import create_category as _create_category
from clover_mcp.tools.inventory import create_item as _create_item
from clover_mcp.tools.inventory import create_modifier as _create_modifier
from clover_mcp.tools.inventory import create_modifier_group as _create_modifier_group
from clover_mcp.tools.inventory import create_tag as _create_tag
from clover_mcp.tools.inventory import get_item as _get_item
from clover_mcp.tools.inventory import list_attributes as _list_attributes
from clover_mcp.tools.inventory import list_categories as _list_categories
from clover_mcp.tools.inventory import list_discounts as _list_discounts
from clover_mcp.tools.inventory import list_item_groups as _list_item_groups
from clover_mcp.tools.inventory import list_items as _list_items
from clover_mcp.tools.inventory import list_low_stock_items as _list_low_stock_items
from clover_mcp.tools.inventory import list_modifiers as _list_modifiers
from clover_mcp.tools.inventory import list_tags as _list_tags
from clover_mcp.tools.inventory import list_taxes as _list_taxes
from clover_mcp.tools.inventory import set_item_price_cents as _set_item_price_cents
from clover_mcp.tools.inventory import set_item_stock_quantity as _set_item_stock_quantity
from clover_mcp.tools.inventory import update_item_name as _update_item_name
from clover_mcp.tools.merchant import get_default_service_charge as _get_default_service_charge
from clover_mcp.tools.merchant import get_merchant_info as _get_merchant_info
from clover_mcp.tools.merchant import get_merchant_properties as _get_merchant_properties
from clover_mcp.tools.merchant import list_cash_events as _list_cash_events
from clover_mcp.tools.merchant import list_devices as _list_devices
from clover_mcp.tools.merchant import list_opening_hours as _list_opening_hours
from clover_mcp.tools.merchant import list_order_types as _list_order_types
from clover_mcp.tools.merchant import list_tenders as _list_tenders
from clover_mcp.tools.merchant import list_tip_suggestions as _list_tip_suggestions
from clover_mcp.tools.orders import add_line_item as _add_line_item
from clover_mcp.tools.orders import apply_order_discount as _apply_order_discount
from clover_mcp.tools.orders import create_order as _create_order
from clover_mcp.tools.orders import get_order as _get_order
from clover_mcp.tools.orders import list_open_orders as _list_open_orders
from clover_mcp.tools.orders import list_orders as _list_orders
from clover_mcp.tools.reporting import get_item_sales as _get_item_sales
from clover_mcp.tools.reporting import get_sales_by_employee as _get_sales_by_employee
from clover_mcp.tools.reporting import get_sales_by_hour as _get_sales_by_hour
from clover_mcp.tools.reporting import get_sales_summary as _get_sales_summary
from clover_mcp.tools.reporting import get_tips_by_employee as _get_tips_by_employee
from clover_mcp.tools.reporting import get_top_items as _get_top_items
from clover_mcp.tools.reporting import list_credits as _list_credits
from clover_mcp.tools.reporting import list_payments as _list_payments
from clover_mcp.tools.reporting import list_refunds as _list_refunds


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Run the startup permission self-check before the server serves tools."""
    await _check_permissions()
    yield


def _boot_config() -> Config | None:
    """Load config at import for the auth provider. Returns None if the env isn't
    configured (e.g. during tests / CI) so importing the server never requires a
    full environment — auth is simply absent in that case."""
    try:
        return load_config()
    except Exception:
        return None


_INSTRUCTIONS = (
    "Tools for querying and managing a Clover POS merchant: "
    "sales reporting, inventory, orders, and customers. "
    "IMPORTANT: This server does NOT support payment capture, refunds, "
    "voids, or charge creation — those actions must be performed in the "
    "Clover dashboard directly."
)

# Resolve the layer-1 OAuth provider once, at construction. FastMCP wires the
# Protected Resource Metadata routes from this — it can't be attached later.
_BOOT_CONFIG = _boot_config()
_auth = build_auth_provider(_BOOT_CONFIG) if _BOOT_CONFIG is not None else None

mcp: FastMCP = FastMCP(
    "Clover POS",
    instructions=_INSTRUCTIONS,
    lifespan=_lifespan,
    auth=_auth,
)

# Layer 3 — predefined prompt workflows (no LLM call; they drive the read tools).
register_prompts(mcp)
# Layer 4 — capability cheat-sheet resource (clover://capabilities).
register_resources(mcp)


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(_request: Any) -> Any:
    """Unauthenticated liveness probe for self-hosted HTTP behind a load balancer.

    The /mcp path requires a bearer token, so an LB has nothing else to probe.
    Deliberately makes NO Clover call — a probe must not spend rate limit or leak
    whether Clover creds are reachable. Harmless in stdio (no HTTP server runs)."""
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True})


async def create_server() -> FastMCP:
    """Hosted/remote entrypoint (e.g. FastMCP Cloud `fastmcp run server.py:create_server`).

    Fail-closed: always builds the layer-1 OAuth resource server and refuses to
    construct without an IdP — so a managed HTTP deploy can never accidentally
    serve unauthenticated, regardless of the CLOVER_TRANSPORT value. Tool
    definitions are copied from the module-level server so there's one source.
    """
    config = replace(load_config(), transport="http")  # force http → auth mandatory
    auth = build_auth_provider(config)  # raises without jwks_uri + issuer + public_url
    server: FastMCP = FastMCP(
        "Clover POS", instructions=_INSTRUCTIONS, lifespan=_lifespan, auth=auth
    )
    server.mount(mcp)  # expose all tools (no prefix)
    return server


# Cached config + clients. In single-merchant mode one client is reused; in
# multi-merchant mode one client is cached per resolved tenant identity.
_config: Config | None = None
_client: CloverClient | None = None
_clients: dict[str, CloverClient] = {}
_tenants_cache: dict[str, Any] | None = None


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = _BOOT_CONFIG or load_config()
    return _config


def _get_tenants() -> dict[str, Any]:
    """Parse the tenant map once per process (env blob + file store)."""
    global _tenants_cache
    if _tenants_cache is None:
        _tenants_cache = load_tenants(_get_config())
    return _tenants_cache


def _get_client() -> CloverClient:
    """Return the Clover client for the current call.

    Single-merchant (stdio or single http): a reused module-level client.
    Multi-tenant (http): resolve the tenant from the request's validated token
    identity and return a per-tenant client (built once, then cached).
    """
    config = _get_config()
    if config.multi_merchant:
        key = request_tenant_key(config)
        client = _clients.get(key)
        if client is None:
            client = CloverClient(tenant_config(config, _get_tenants(), key), tenant=key)
            _clients[key] = client
        return client

    global _client
    if _client is None:
        _client = CloverClient(config)
    return _client


# ── Startup permission self-check ─────────────────────────────────────────────


async def _check_permissions() -> None:
    """Best-effort startup self-check: probe one read per required permission and
    log problems. It NEVER crashes the process — a hosted platform's pre-flight
    must be able to start the server, and upstream credential/scope/network issues
    surface as 401/403 on the first tool call instead of killing startup.
    """
    try:
        config = _get_config()
    except Exception as exc:  # noqa: BLE001 — bad/absent env: warn, still start
        print(f"WARNING: could not load configuration at startup: {exc}", file=sys.stderr)
        return

    if config.read_only:
        print(
            "clover-mcp is in READ-ONLY mode (CLOVER_READ_ONLY=true) — all write "
            "tools will refuse before any Clover API call.",
            file=sys.stderr,
        )

    if config.write_limit_count > 0:
        print(
            f"Write-velocity guard active: max {config.write_limit_count} writes per "
            f"{config.write_limit_window_s}s per tenant (CLOVER_WRITE_LIMIT_COUNT).",
            file=sys.stderr,
        )

    # Multi-merchant has no single startup merchant — credentials and scopes are
    # resolved (and surfaced as 403s) per request instead.
    if config.multi_merchant:
        print(
            "Starting in multi-merchant http mode; per-merchant permission checks "
            "happen on first use.",
            file=sys.stderr,
        )
        # SECURITY warning (non-fatal): forwarded-header routing without the trust
        # opt-in. The server still boots so `whoami` can run the header-spoofing
        # test, but every DATA call fails closed in request_tenant_key until the
        # gateway is verified and CLOVER_TRUST_IDENTITY_HEADER=true is set.
        if config.tenant_header and not config.trust_identity_header:
            print(
                f"WARNING: CLOVER_TENANT_HEADER={config.tenant_header!r} routes tenants by a "
                "forwarded header, but CLOVER_TRUST_IDENTITY_HEADER is not set. All data tools "
                "will FAIL CLOSED until you run the header-spoofing test (docs/SECURITY.md) and "
                "opt in. `whoami` still works so you can run that test.",
                file=sys.stderr,
            )
        return

    try:
        client = _get_client()
    except Exception as exc:  # noqa: BLE001 — surface at call time, don't crash startup
        print(f"WARNING: could not build Clover client at startup: {exc}", file=sys.stderr)
        return

    missing: list[str] = []

    # Required scopes gate startup. EMPLOYEES_R is optional (v1.1 employee/shift
    # tools are opt-in): a 403 only warns so merchants without it keep working.
    probes: list[tuple[str, str, bool]] = [
        ("MERCHANT_R", f"/v3/merchants/{client._config.merchant_id}", True),
        ("PAYMENTS_R", "/payments", True),
        ("ORDERS_R", "/orders", True),
        ("INVENTORY_R", "/items", True),
        ("CUSTOMERS_R", "/customers", True),
        ("EMPLOYEES_R", "/employees", False),
    ]

    for perm, path, required in probes:
        try:
            await client.get(path, limit=1)
        except CloverAPIError as exc:
            if exc.status_code == 403:
                if required:
                    missing.append(f"  • {perm}: {exc.message}")
                else:
                    print(
                        f"WARNING: {perm} not granted — its tools will return 403 "
                        "(employee/shift tools are optional).",
                        file=sys.stderr,
                    )
            elif exc.status_code == 401:
                print(
                    "WARNING: Invalid or expired Clover access token — tool calls will "
                    "fail until CLOVER_ACCESS_TOKEN is fixed.",
                    file=sys.stderr,
                )
                return
            else:
                # Transient (5xx / rate limit): don't block startup — tools surface it later.
                print(
                    f"WARNING: permission probe for {perm} failed: {exc.message}", file=sys.stderr
                )
        except Exception as exc:  # noqa: BLE001 — network error etc.; warn, don't crash startup
            print(f"WARNING: permission probe for {perm} skipped: {exc}", file=sys.stderr)

    if missing:
        print("WARNING: Missing required Clover permissions:", file=sys.stderr)
        for m in missing:
            print(m, file=sys.stderr)
        print(
            "Grant these permissions via the Clover Developer Dashboard and reinstall "
            "the app on the merchant account; affected tools will return 403 until then.",
            file=sys.stderr,
        )


# ── Tool behaviour annotations ────────────────────────────────────────────────
# Clients use these structured hints (not the prose docstrings) to gate
# confirmation prompts and to parallelize read-only tools. Every tool talks to
# the Clover REST API, so openWorldHint=True throughout.
_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_WRITE_ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_WRITE_SET = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)


# ── Tool registrations ────────────────────────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def whoami() -> dict[str, Any]:
    """Diagnostic: who is this request authenticated as, and is a Clover tenant
    mapped to them?

    Returns the authenticated identity, the *names* of available token claims
    (never their values), scopes, and whether a tenant is provisioned — no Clover
    data and no secrets. Use it when setting up multi-tenant to discover which
    identity claim your platform (e.g. FastMCP Cloud / Horizon) actually provides,
    so you can key CLOVER_TENANTS_JSON correctly.
    """
    return auth_context(_get_config(), _get_tenants())


@mcp.tool(annotations=_READ)
async def get_merchant_info() -> dict[str, Any]:
    """Return key information about this Clover merchant.

    Includes name, address, currency, timezone, country, and business type.
    Also primes the internal currency and timezone cache used by all other tools.
    """
    return await _get_merchant_info(_get_client())


@mcp.tool(annotations=_READ)
async def get_sales_summary(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return an aggregated sales summary for the given date window.

    Defaults to today (UTC) when no dates are supplied. Uses 90-day chunking
    so multi-month or full-year queries work transparently (emits progress logs
    when more than one window is scanned).

    Rules: only result=SUCCESS payments counted; voids/refunds reported separately;
    tips, taxes broken out; offline payments flagged; currency from merchant record.
    This tool does NOT support payment capture, refund, or void actions.
    """
    return await _get_sales_summary(_get_client(), date_from=date_from, date_to=date_to, ctx=ctx)


@mcp.tool(annotations=_READ)
async def list_payments(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List payments within an optional date window (default: today, limit 50).

    Only result=SUCCESS payments. Card transaction details never included.
    fields: optional list of field names to keep (narrows the response; cannot widen past the allowlist).
    This tool does NOT support payment capture, refund, or void actions.
    """
    return await _list_payments(
        _get_client(), date_from=date_from, date_to=date_to, limit=limit, fields=fields
    )


@mcp.tool(annotations=_READ)
async def list_refunds(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List refunds within an optional date window (default: today, limit 50).

    Clover refunds are separate objects with a positive amount (cents), not
    negative payments. Card/transaction detail is never included. Requires
    PAYMENTS_R. This tool does NOT issue refunds.
    """
    return await _list_refunds(_get_client(), date_from=date_from, date_to=date_to, limit=limit)


@mcp.tool(annotations=_READ)
async def list_credits() -> dict[str, Any]:
    """Return the merchant's credits (store/account-credit adjustments, up to 1000).

    Element shape is unverified (sandbox has none provisioned) — shaped
    conservatively. Requires PAYMENTS_R.
    """
    return await _list_credits(_get_client())


@mcp.tool(annotations=_READ)
async def get_sales_by_employee(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return gross sales grouped by employee for a date window (default: today).

    Requires PAYMENTS_R; employee name enrichment additionally requires
    EMPLOYEES_R (degrades to IDs-only with a note if not granted).
    """
    return await _get_sales_by_employee(_get_client(), date_from=date_from, date_to=date_to)


@mcp.tool(annotations=_READ)
async def get_tips_by_employee(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return a tip-out sheet: tips collected grouped by employee (default: today).

    Requires PAYMENTS_R; employee name enrichment additionally requires
    EMPLOYEES_R (degrades to IDs-only with a note if not granted).
    """
    return await _get_tips_by_employee(_get_client(), date_from=date_from, date_to=date_to)


@mcp.tool(annotations=_READ)
async def get_sales_by_hour(date: str | None = None) -> dict[str, Any]:
    """Return gross sales bucketed by local hour-of-day for a single day (default: today).

    Buckets use the merchant's local timezone, falling back to UTC with a note
    if unavailable. Requires PAYMENTS_R.
    """
    return await _get_sales_by_hour(_get_client(), date=date)


@mcp.tool(annotations=_READ)
async def list_orders(
    date_from: str | None = None,
    date_to: str | None = None,
    state: str | None = None,
    limit: int = 50,
    fields: list[str] | None = None,
    offset: int = 0,
    include_line_items: bool = False,
) -> list[dict[str, Any]]:
    """List orders, newest first, for store-local days (default: today, limit 50, max 1000).

    state: open | paid | refunded | partially_refunded (omit for all states).
    offset: skip that many orders to page further back. include_line_items:
    add each order's items. Each order has created_local (store time).
    Customer card data is never included. This tool is read-only.
    fields: optional list of field names to keep (narrows the response; cannot widen past the allowlist).
    """
    return await _list_orders(
        _get_client(),
        date_from=date_from,
        date_to=date_to,
        state=state,
        limit=limit,
        fields=fields,
        offset=offset,
        include_line_items=include_line_items,
    )


@mcp.tool(annotations=_READ)
async def get_order(order_id: str) -> dict[str, Any]:
    """Fetch a single order by ID, including line items and payment summary.

    Expands lineItems and payments only. Customer card data never expanded.
    Returns a 404 error if the order_id does not exist. This tool is read-only.
    """
    return await _get_order(_get_client(), order_id=order_id)


@mcp.tool(annotations=_READ)
async def list_open_orders() -> list[dict[str, Any]]:
    """Return all currently open orders for this merchant (up to 200).

    Convenience wrapper — no date filter, state=open only.
    Customer card data is never included. This tool is read-only.
    """
    return await _list_open_orders(_get_client())


@mcp.tool(annotations=_READ)
async def list_items(
    query: str | None = None,
    category_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return a page of inventory items. Filter by name (query) or category (category_id).

    Requires INVENTORY_R.
    fields: optional list of field names to keep (narrows the response; cannot widen past the allowlist).
    """
    return await _list_items(
        _get_client(),
        query=query,
        category_id=category_id,
        limit=limit,
        offset=offset,
        fields=fields,
    )


@mcp.tool(annotations=_READ)
async def get_item(item_id: str, include: list[str] | None = None) -> dict[str, Any]:
    """Return a single inventory item by ID, including stock quantity.

    Pass include=["modifier_groups"], ["tax_rates"], ["categories"], and/or
    ["tags"] to opt in to those association details. Requires INVENTORY_R.
    """
    return await _get_item(_get_client(), item_id, include=include)


@mcp.tool(annotations=_READ)
async def list_low_stock_items(threshold: int = 5) -> dict[str, Any]:
    """Return all items whose stock quantity is at or below threshold.

    Items with no stock tracking are excluded. Requires INVENTORY_R.
    """
    return await _list_low_stock_items(_get_client(), threshold=threshold)


@mcp.tool(annotations=_READ)
async def list_categories() -> dict[str, Any]:
    """Return all inventory categories. Requires INVENTORY_R."""
    return await _list_categories(_get_client())


@mcp.tool(annotations=_READ)
async def list_modifiers() -> dict[str, Any]:
    """Return all modifier groups with their modifiers. Requires INVENTORY_R."""
    return await _list_modifiers(_get_client())


@mcp.tool(annotations=_READ)
async def list_item_groups() -> dict[str, Any]:
    """Return item groups (sets of item variants, e.g. size/color). Requires INVENTORY_R."""
    return await _list_item_groups(_get_client())


@mcp.tool(annotations=_READ)
async def list_taxes() -> dict[str, Any]:
    """Return the merchant's tax rates (raw rate + computed percent). Requires INVENTORY_R."""
    return await _list_taxes(_get_client())


@mcp.tool(annotations=_READ)
async def list_discounts() -> dict[str, Any]:
    """Return the merchant's discount catalogue (fixed-amount or percentage). Requires INVENTORY_R."""
    return await _list_discounts(_get_client())


@mcp.tool(annotations=_READ)
async def list_devices() -> dict[str, Any]:
    """Return the merchant's Clover devices/terminals. Requires MERCHANT_R."""
    return await _list_devices(_get_client())


@mcp.tool(annotations=_READ)
async def list_tenders() -> dict[str, Any]:
    """Return the merchant's tender types (payment methods: cash, credit, custom). Requires MERCHANT_R."""
    return await _list_tenders(_get_client())


@mcp.tool(annotations=_READ)
async def get_merchant_properties() -> dict[str, Any]:
    """Return the merchant's POS configuration (currency, tips, stock tracking,
    closeout, locale, support contacts). Banking/account fields are never returned.
    Requires MERCHANT_R."""
    return await _get_merchant_properties(_get_client())


@mcp.tool(annotations=_READ)
async def list_order_types() -> dict[str, Any]:
    """Return the merchant's order types (Dine In, Take Out, …). Requires MERCHANT_R."""
    return await _list_order_types(_get_client())


@mcp.tool(annotations=_READ)
async def list_opening_hours() -> dict[str, Any]:
    """Return the merchant's opening-hours sets (per-day time ranges). Requires MERCHANT_R."""
    return await _list_opening_hours(_get_client())


@mcp.tool(annotations=_READ)
async def list_tip_suggestions() -> dict[str, Any]:
    """Return the merchant's tip-suggestion presets (percentage or flat amount). Requires MERCHANT_R."""
    return await _list_tip_suggestions(_get_client())


@mcp.tool(annotations=_READ)
async def get_default_service_charge() -> dict[str, Any]:
    """Return the merchant's default service charge configuration (name, enabled, percentage).
    Requires MERCHANT_R."""
    return await _get_default_service_charge(_get_client())


@mcp.tool(annotations=_READ)
async def list_cash_events(limit: int = 50) -> dict[str, Any]:
    """Return recent cash-drawer events (paid in/out, no-sale, deposits).

    Capped at `limit` (default 50, max 500). Requires MERCHANT_R.
    """
    return await _list_cash_events(_get_client(), limit=limit)


@mcp.tool(annotations=_READ)
async def list_attributes() -> dict[str, Any]:
    """Return item attributes (variant axes like Size/Color) with options. Requires INVENTORY_R."""
    return await _list_attributes(_get_client())


@mcp.tool(annotations=_READ)
async def list_tags() -> dict[str, Any]:
    """Return the merchant's tags/labels used to group items. Requires INVENTORY_R."""
    return await _list_tags(_get_client())


@mcp.tool(annotations=_READ)
async def get_top_items(
    date_from: str | None = None,
    date_to: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Return the best-selling items in a date window, ranked by units sold.

    Defaults to today (store local day); 90-day chunked. Each line item counts as one unit.
    For exact counts of all items or a time-of-day window, use get_item_sales.
    Requires ORDERS_R.
    """
    return await _get_top_items(_get_client(), date_from=date_from, date_to=date_to, top_n=top_n)


@mcp.tool(annotations=_READ)
async def get_item_sales(
    date_from: str | None = None,
    date_to: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    name_contains: list[str] | None = None,
) -> dict[str, Any]:
    """Count EVERY item sold in a window (no top-N cap), by the store's local time.

    Use this for exact item counts ("how many lattes today", "since opening",
    "6am-11am"). date_from/date_to: local business days YYYY-MM-DD (default
    today). start_time/end_time: optional local 24-hour 'HH:MM'. name_contains:
    optional case-insensitive substrings, e.g. ["latte"]; total_units then sums
    only matching items. Also returns order_count and first/last order times.
    Requires ORDERS_R.
    """
    return await _get_item_sales(
        _get_client(),
        date_from=date_from,
        date_to=date_to,
        start_time=start_time,
        end_time=end_time,
        name_contains=name_contains,
    )


@mcp.tool(annotations=_READ)
async def list_employees(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Return a page of employees (PINs never returned). Requires EMPLOYEES_R."""
    return await _list_employees(_get_client(), limit=limit, offset=offset)


@mcp.tool(annotations=_READ)
async def get_employee(employee_id: str) -> dict[str, Any]:
    """Return a single employee by ID (PINs never returned). Requires EMPLOYEES_R."""
    return await _get_employee(_get_client(), employee_id)


@mcp.tool(annotations=_READ)
async def list_shifts(
    employee_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """List shifts, optionally by employee and/or date window. Requires EMPLOYEES_R.

    Without employee_id, aggregates shifts across all employees.
    """
    return await _list_shifts(
        _get_client(), employee_id=employee_id, date_from=date_from, date_to=date_to
    )


@mcp.tool(annotations=_READ)
async def list_active_shifts() -> dict[str, Any]:
    """Return currently open shifts (clocked in, not out) across all employees. Requires EMPLOYEES_R."""
    return await _list_active_shifts(_get_client())


@mcp.tool(annotations=_READ)
async def list_roles() -> dict[str, Any]:
    """Return the merchant's employee roles (name + system role category). Requires EMPLOYEES_R."""
    return await _list_roles(_get_client())


@mcp.tool(annotations=_READ)
async def search_customers(
    query: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    limit: int = 50,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Search customers by full name (query), phone, or email.

    Cards are never returned. Requires CUSTOMERS_R.
    fields: optional list of field names to keep (narrows the response; cannot widen past the allowlist).
    """
    return await _search_customers(
        _get_client(), query=query, phone=phone, email=email, limit=limit, fields=fields
    )


@mcp.tool(annotations=_READ)
async def get_customer(customer_id: str, include: list[str] | None = None) -> dict[str, Any]:
    """Return a single customer by ID.

    Pass include=["addresses"] or include=["orders"] to opt in to optional fields.
    Cards are never returned. Requires CUSTOMERS_R.
    """
    return await _get_customer(_get_client(), customer_id, include=include)


# ── AI/LLM tools (Layer 2 — MCP sampling) ─────────────────────────────────────
# These gather Clover data, then ask the CONNECTED CLIENT's model to reason via
# ctx.sample() — the server holds no LLM key. Read-only (annotated _READ): they
# never write the model's output back. If the client can't sample, they return
# the raw data with a note instead of failing.


@mcp.tool(annotations=_READ)
async def summarize_sales(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """AI: plain-language sales briefing for a date window (default: today).

    Gathers the sales summary + top items, then asks your client's model to write
    a short narrative. Read-only suggestion. Needs a sampling-capable client; if
    yours can't sample, returns the raw data with a note. Requires ORDERS_R/PAYMENTS_R.
    """
    return await _summarize_sales(_get_client(), ctx, date_from=date_from, date_to=date_to)


@mcp.tool(annotations=_READ)
async def suggest_item_categories(ctx: Context, limit: int = 100) -> dict[str, Any]:
    """AI: suggest categories for uncategorized items from the merchant's own taxonomy.

    Suggestion only — applying a category is a separate, confirmed write. Needs a
    sampling-capable client (graceful fallback otherwise). Requires INVENTORY_R.
    """
    return await _suggest_item_categories(_get_client(), ctx, limit=limit)


@mcp.tool(annotations=_READ)
async def inventory_reorder_suggestions(
    ctx: Context,
    threshold: int = 5,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """AI: prioritized reorder list — low-stock items crossed with sales velocity.

    Read-only suggestion. Needs a sampling-capable client (graceful fallback
    otherwise). Requires INVENTORY_R and ORDERS_R.
    """
    return await _inventory_reorder_suggestions(
        _get_client(), ctx, threshold=threshold, date_from=date_from, date_to=date_to
    )


@mcp.tool(annotations=_READ)
async def detect_sales_anomalies(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """AI: flag unusual refund / discount / sales patterns in a window (default: today).

    Read-only analysis. Needs a sampling-capable client (graceful fallback
    otherwise). Requires PAYMENTS_R and ORDERS_R.
    """
    return await _detect_sales_anomalies(_get_client(), ctx, date_from=date_from, date_to=date_to)


@mcp.tool(annotations=_READ)
async def draft_customer_message(ctx: Context, customer_id: str, intent: str) -> dict[str, Any]:
    """AI: draft a customer message (promo / win-back / thank-you) for the given intent.

    Returns a DRAFT only — never sends anything. Needs a sampling-capable client
    (graceful fallback otherwise). Requires CUSTOMERS_R.
    """
    return await _draft_customer_message(_get_client(), ctx, customer_id=customer_id, intent=intent)


# NOTE: the process entry point is clover_mcp.__main__:main (calls mcp.run()).
# Startup permission validation runs via the FastMCP lifespan above.


# ── Write tools (M3) ──────────────────────────────────────────────────────────
# Write permissions (CUSTOMERS_W, INVENTORY_W) are NOT probed at startup — a
# write probe would mutate data. Missing write scopes surface as a 403 at call
# time. Every write tool description begins with "Modifies merchant data." so
# MCP surfaces prompt for confirmation, and supports dry_run to preview payloads.


@mcp.tool(annotations=_WRITE_ADD)
async def create_customer(
    first_name: str,
    last_name: str,
    email: str | None = None,
    phone: str | None = None,
    marketing_allowed: bool | None = None,
    confirm_duplicate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Create a new customer record in Clover.

    Idempotency guard: searches for an existing customer with the same email or
    phone before creating. If a match is found and confirm_duplicate is False,
    the call is refused and the existing match is returned.

    dry_run=True returns the would-be POST payload without sending it.
    Requires CUSTOMERS_R (duplicate check) and CUSTOMERS_W (write).
    """
    return await _create_customer(
        _get_client(),
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        marketing_allowed=marketing_allowed,
        confirm_duplicate=confirm_duplicate,
        dry_run=dry_run,
    )


@mcp.tool(annotations=_WRITE_SET)
async def set_item_price_cents(
    item_id: str,
    new_price_cents: int,
    expected_current_price_cents: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Set an item's price (in cents, absolute value).

    Optimistic lock: refuses the write unless the item's current price equals
    expected_current_price_cents (prevents stale-context overwrites). Bounds:
    0 <= new_price_cents <= 100_000_000. dry_run=True previews the PUT body
    (still performs one read to fetch current price/name) and never writes.
    Requires INVENTORY_R and INVENTORY_W.
    """
    return await _set_item_price_cents(
        _get_client(), item_id, new_price_cents, expected_current_price_cents, dry_run
    )


@mcp.tool(annotations=_WRITE_SET)
async def set_item_stock_quantity(
    item_id: str,
    new_quantity: int,
    expected_current_quantity: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Set an item's stock to an ABSOLUTE quantity (not a delta).

    Optimistic lock: refuses unless current stock equals expected_current_quantity.
    Bounds: 0 <= new_quantity <= 1_000_000. dry_run=True previews the PUT body
    (still performs one read to fetch current stock) and never writes.
    Requires INVENTORY_R and INVENTORY_W.
    """
    return await _set_item_stock_quantity(
        _get_client(), item_id, new_quantity, expected_current_quantity, dry_run
    )


# ── Guarded creates + updates (Layer 1 writes + Layer 4 elicitation) ───────────
# Each validates, supports dry_run preview, and confirms before writing via MCP
# elicitation (ctx.elicit) — or an explicit confirm=True override. They never
# capture payments, issue refunds, or delete records.


@mcp.tool(annotations=_WRITE_ADD)
async def create_category(
    ctx: Context, name: str, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Create a new inventory category.

    Previews on dry_run; confirms via your client's prompt (MCP elicitation) or
    confirm=True before writing. Requires INVENTORY_W.
    """
    return await _create_category(_get_client(), ctx, name, dry_run=dry_run, confirm=confirm)


@mcp.tool(annotations=_WRITE_ADD)
async def create_item(
    ctx: Context, name: str, price_cents: int, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Create a new inventory item (name + price in cents).

    Bounds 0–100_000_000 cents. Previews on dry_run; confirms via MCP elicitation
    or confirm=True before writing. Requires INVENTORY_W.
    """
    return await _create_item(
        _get_client(), ctx, name, price_cents, dry_run=dry_run, confirm=confirm
    )


@mcp.tool(annotations=_WRITE_ADD)
async def create_order(
    ctx: Context, note: str | None = None, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Create a new open order (no line items, no payment).

    Previews on dry_run; confirms via MCP elicitation or confirm=True before
    writing. Add items with add_line_item. Requires ORDERS_W.
    """
    return await _create_order(_get_client(), ctx, note=note, dry_run=dry_run, confirm=confirm)


@mcp.tool(annotations=_WRITE_ADD)
async def add_line_item(
    ctx: Context, order_id: str, item_id: str, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Add a catalog item as a line item to an order.

    Name/price are copied from the catalog item. Previews on dry_run; confirms via
    MCP elicitation or confirm=True before writing. Requires ORDERS_W. No payment.
    """
    return await _add_line_item(
        _get_client(), ctx, order_id, item_id, dry_run=dry_run, confirm=confirm
    )


@mcp.tool(annotations=_WRITE_SET)
async def update_customer(
    ctx: Context,
    customer_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    marketing_allowed: bool | None = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Update a customer's name and/or marketing opt-in.

    Only the fields you pass change. Previews on dry_run; confirms via MCP
    elicitation or confirm=True before writing. Requires CUSTOMERS_W.
    """
    return await _update_customer(
        _get_client(),
        ctx,
        customer_id,
        first_name=first_name,
        last_name=last_name,
        marketing_allowed=marketing_allowed,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=_WRITE_SET)
async def apply_order_discount(
    ctx: Context,
    order_id: str,
    name: str | None = None,
    percentage: int | None = None,
    amount_cents: int | None = None,
    catalogue_discount_id: str | None = None,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Apply an order-level discount.

    Exactly one of percentage (1-100), amount_cents (1-100_000_000, positive —
    Clover requires a negative amount on the wire, this tool negates it for you),
    or catalogue_discount_id must be given. Catalogue discounts are resolved
    client-side (name + percentage/amount fetched from list_discounts and sent
    inline). Previews on dry_run (includes current discounts + a client-computed
    line-item subtotal — Clover has no computed order total); confirms via MCP
    elicitation or confirm=True before writing. Requires ORDERS_W.
    """
    return await _apply_order_discount(
        _get_client(),
        ctx,
        order_id,
        name=name,
        percentage=percentage,
        amount_cents=amount_cents,
        catalogue_discount_id=catalogue_discount_id,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=_WRITE_SET)
async def update_item_name(
    ctx: Context,
    item_id: str,
    new_name: str,
    expected_current_name: str,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Rename an inventory item (price/other fields preserved).

    Optimistic lock: refuses unless the item's current name equals
    expected_current_name. Bounds: non-empty, <= 127 characters. dry_run=True
    previews the POST body (still performs one read) and never writes.
    Requires INVENTORY_R and INVENTORY_W.
    """
    return await _update_item_name(
        _get_client(),
        ctx,
        item_id,
        new_name,
        expected_current_name,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=_WRITE_ADD)
async def create_modifier_group(
    ctx: Context, name: str, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Create a new modifier group (e.g. "Milk options").

    Duplicate guard: refuses if a group with the same name (case-insensitive)
    already exists. Previews on dry_run; confirms via MCP elicitation or
    confirm=True before writing. Requires INVENTORY_W.
    """
    return await _create_modifier_group(_get_client(), ctx, name, dry_run=dry_run, confirm=confirm)


@mcp.tool(annotations=_WRITE_ADD)
async def create_modifier(
    ctx: Context,
    modifier_group_id: str,
    name: str,
    price_cents: int,
    dry_run: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Modifies merchant data. Create a new modifier within a modifier group.

    Pre-check: verifies the modifier group exists (404 -> clear error). Bounds:
    0 <= price_cents <= 100_000_000. Previews on dry_run; confirms via MCP
    elicitation or confirm=True before writing. Requires INVENTORY_W.
    """
    return await _create_modifier(
        _get_client(),
        ctx,
        modifier_group_id,
        name,
        price_cents,
        dry_run=dry_run,
        confirm=confirm,
    )


@mcp.tool(annotations=_WRITE_ADD)
async def create_tag(
    ctx: Context, name: str, dry_run: bool = False, confirm: bool = False
) -> dict[str, Any]:
    """Modifies merchant data. Create a new tag/label used to group items.

    Duplicate guard: refuses if a tag with the same name (case-insensitive)
    already exists. Previews on dry_run; confirms via MCP elicitation or
    confirm=True before writing. Requires INVENTORY_W.
    """
    return await _create_tag(_get_client(), ctx, name, dry_run=dry_run, confirm=confirm)
