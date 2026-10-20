"""Clover REST API HTTP client.

Wraps httpx.AsyncClient with:
  - Automatic Authorization / User-Agent / Accept headers
  - /v3/merchants/{mId} path prefix for relative paths
  - 401 → token refresh (oauth_refresh mode only)
  - 429 → single auto-retry if Retry-After ≤ 5s
  - 5xx → single retry on reads; NO retry on writes
  - Pagination helper: iterate()
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from clover_mcp import __version__
from clover_mcp.auth import TokenStore, refresh_access_token
from clover_mcp.config import Config
from clover_mcp.errors import ReadOnlyError, WriteVelocityError, raise_for_status
from clover_mcp.observability import audit, note, traced

_USER_AGENT = f"clover-mcp/{__version__} (+https://github.com/SBolivarLoL/clover-mcp-server)"

# Clover permits ~5 concurrent requests per access token. One client exists per
# token (per tenant in multi-merchant mode), so a per-client semaphore keeps an
# eager agent — parallel read tools, 90-day fan-outs — from tripping 429s.
_MAX_CONCURRENT_REQUESTS = 5

# Safety ceiling for iterate(): a backstop against an unbounded page walk (a
# runaway aggregation, or an API that never returns a short final page). At the
# common limit=100 this is 100k rows per call — far above any real window; when
# it fires we emit a `note` so a truncated result is never silent.
_MAX_PAGES = 1000


class CloverClient:
    """Async HTTP client for the Clover REST API."""

    def __init__(self, config: Config, tenant: str | None = None) -> None:
        self._config = config
        # Identity of the caller this client serves (multi-tenant key); recorded
        # in write audit records so the trail answers "who", not just "which merchant".
        self._tenant = tenant
        self._access_token = self._load_initial_token()
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        # Monotonic timestamps of recent successful write starts, for the
        # write-velocity guard. Per-client == per-tenant (one client per token).
        # ponytail: in-memory per-process window; a shared store is only needed
        # if you run >1 worker and want the cap enforced across them.
        self._write_times: deque[float] = deque()
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=30,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def _load_initial_token(self) -> str:
        """Load token from store (oauth_refresh) or config (token mode)."""
        if self._config.auth_mode == "oauth_refresh":
            stored = TokenStore(self._config.token_store).load()
            if stored.get("access_token"):
                return stored["access_token"]
        return self._config.access_token

    def _url(self, path: str) -> str:
        """Expand and validate a request path within this merchant boundary."""
        if (
            not path
            or "%" in path
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(char.isspace() or ord(char) < 32 for char in path)
        ):
            raise ValueError(
                "Request path must not contain encoded, query, fragment, whitespace, or backslash delimiters"
            )

        if any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError("Request path contains a traversal segment")

        merchant_prefix = f"/v3/merchants/{self._config.merchant_id}"
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc:
            base = urlsplit(self._config.base_url)
            if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
                raise ValueError("Request URL must use the configured Clover host")
            path = parsed.path
        if path.startswith("/v3/merchants/"):
            if not (path == merchant_prefix or path.startswith(f"{merchant_prefix}/")):
                raise ValueError("Request URL must stay within the configured merchant")
            return path
        if path.startswith("/v3"):
            raise ValueError("Request URL must use the configured merchant path")
        if not path.startswith("/"):
            raise ValueError("Request path must be absolute or start with '/'")
        return f"{merchant_prefix}{path}"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _refresh_and_retry(
        self, method: str, url: str, failed_token: str, **kwargs: Any
    ) -> httpx.Response:
        """Refresh the OAuth access token and retry the request once."""
        self._access_token = await refresh_access_token(self._config, failed_token)
        return await self._http.request(method, url, headers=self._auth_headers(), **kwargs)

    def _audit_write(self, event: str, method: str, path: str, **fields: Any) -> None:
        """Emit write metadata without allowing logging failures to alter a write."""
        metadata: dict[str, Any] = {
            "method": method,
            "path": path,
            "merchant": self._config.merchant_id,
        }
        if self._tenant is not None:
            metadata["tenant"] = self._tenant
        metadata.update(fields)
        with suppress(Exception):
            audit(event, **metadata)

    def _check_write_velocity(self, method: str, path: str) -> None:
        """Sliding-window cap on write-tool calls per tenant. Refuses (before any
        HTTP) once `write_limit_count` writes have started within the window, and
        records this attempt. Disabled when the count is 0."""
        cap = self._config.write_limit_count
        if cap <= 0:
            return
        window = self._config.write_limit_window_s
        now = time.monotonic()
        cutoff = now - window
        while self._write_times and self._write_times[0] < cutoff:
            self._write_times.popleft()
        if len(self._write_times) >= cap:
            self._audit_write("write_refused", method, path, reason="write_velocity")
            raise WriteVelocityError(
                f"Write refused: {cap} writes already in the last {window}s "
                f"(CLOVER_WRITE_LIMIT_COUNT). No data was modified. Stop writing and have "
                "the operator review the audit log before continuing."
            )
        self._write_times.append(now)

    async def _send(
        self,
        method: str,
        path: str,
        is_write: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        url = self._url(path)
        context = f"{method} {path}"

        if is_write:
            # Global read-only kill switch first — a read-only server refuses every
            # write regardless of velocity, and a refused write must not count toward
            # the velocity window (checked second).
            if self._config.read_only:
                self._audit_write("write_refused", method, path, reason="read_only")
                raise ReadOnlyError(
                    f"Server is in read-only mode (CLOVER_READ_ONLY=true); refused {method} {path}. "
                    "No data was modified. Unset CLOVER_READ_ONLY to enable writes."
                )
            self._check_write_velocity(method, path)

        # oauth_refresh may start with no access token (e.g. a tenant configured
        # with only a refresh token, or an ephemeral host with an empty store).
        # Bootstrap one first — an empty `Bearer ` header is rejected before it's
        # even sent, so the 401→refresh path below would never get a chance.
        if not self._access_token and self._config.auth_mode == "oauth_refresh":
            self._access_token = await refresh_access_token(self._config, "")

        # Trace the whole request (incl. retries) — a real OTel span if the operator
        # configured an exporter, otherwise a no-op with an optional latency line.
        # The semaphore bounds in-flight requests per token (incl. retry waits).
        try:
            async with self._sem, traced("clover.http", method=method, path=path):
                request_token = self._access_token
                resp = await self._http.request(
                    method, url, headers={"Authorization": f"Bearer {request_token}"}, **kwargs
                )

                # 401 → refresh once (oauth_refresh only)
                if resp.status_code == 401 and self._config.auth_mode == "oauth_refresh":
                    resp = await self._refresh_and_retry(method, url, request_token, **kwargs)

                # 429 → single auto-retry if short wait
                if resp.status_code == 429:
                    raw = resp.headers.get("Retry-After", "")
                    wait = int(raw) if raw.isdigit() else None
                    if wait is not None and wait <= 5:
                        await asyncio.sleep(wait)
                        resp = await self._http.request(
                            method, url, headers=self._auth_headers(), **kwargs
                        )

                # 5xx reads → single retry with 1s backoff; writes never retry
                if resp.status_code >= 500 and not is_write:
                    await asyncio.sleep(1)
                    resp = await self._http.request(
                        method, url, headers=self._auth_headers(), **kwargs
                    )
        except BaseException:
            if is_write:
                self._audit_write("write_uncertain", method, path, outcome="uncertain")
            raise

        # Audit every mutation attempt — including failures — with the final status.
        # No request bodies or secrets; path may carry resource ids (not sensitive).
        if is_write:
            self._audit_write("write", method, path, status=resp.status_code)

        raise_for_status(resp, context=context, auth_mode=self._config.auth_mode)
        return resp

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._send("GET", path, params=params)
        return resp.json()  # type: ignore[no-any-return]

    async def post(self, path: str, json: Any = None, **params: Any) -> dict[str, Any]:
        resp = await self._send("POST", path, is_write=True, json=json, params=params)
        if not resp.content:
            return {}
        return resp.json()  # type: ignore[no-any-return]

    async def put(self, path: str, json: Any = None, **params: Any) -> dict[str, Any]:
        resp = await self._send("PUT", path, is_write=True, json=json, params=params)
        try:
            return resp.json()  # type: ignore[no-any-return]
        except Exception:
            return {}  # Clover sometimes returns 200 with empty body on PUT

    async def delete(self, path: str, **params: Any) -> None:
        await self._send("DELETE", path, is_write=True, params=params)

    async def iterate(
        self, path: str, *, limit: int = 100, max_pages: int = _MAX_PAGES, **params: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every element across paginated Clover list responses.

        Stops after `max_pages` as a safety backstop; if the cap is hit before the
        data is exhausted, emits a `note` (so a truncated aggregate is never silent)
        and stops rather than walking forever.
        """
        offset = 0
        for _page in range(max_pages):
            body = await self.get(path, limit=limit, offset=offset, **params)
            elements: list[dict[str, Any]] = body.get("elements", [])
            for el in elements:
                yield el
            if len(elements) < limit:
                return
            offset += limit
        note("pagination_capped", path=path, max_pages=max_pages, rows=offset)

    async def close(self) -> None:
        await self._http.aclose()

    # Context manager support
    async def __aenter__(self) -> CloverClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Merchant info cache ───────────────────────────────────────────────────

    _merchant_cache: dict[str, Any] | None = None

    async def get_merchant_info(self) -> dict[str, Any]:
        """Fetch and cache merchant info (currency, timezone, country).

        Live merchants often omit currency/timezone on the base merchant record;
        Clover keeps them on /properties, so missing keys are filled from there.
        """
        if self._merchant_cache is None:
            info = dict(await self.get(f"/v3/merchants/{self._config.merchant_id}"))
            if not (info.get("defaultCurrency") or info.get("currency")) or not info.get(
                "timezone"
            ):
                try:
                    props = await self.get(f"/v3/merchants/{self._config.merchant_id}/properties")
                except Exception:  # noqa: BLE001 - properties are a best-effort fallback
                    props = {}
                for key in ("defaultCurrency", "timezone"):
                    if not info.get(key) and props.get(key):
                        info[key] = props[key]
            self._merchant_cache = info
        return self._merchant_cache

    async def merchant_tzinfo(self) -> ZoneInfo:
        """The merchant's local timezone (falls back to UTC if unknown/invalid)."""
        name = await self.merchant_timezone()
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    async def merchant_today(self) -> date:
        """Today's date in the merchant's local timezone (the store's business day)."""
        return datetime.now(tz=await self.merchant_tzinfo()).date()

    async def merchant_currency(self) -> str:
        info = await self.get_merchant_info()
        currency = info.get("defaultCurrency") or info.get("currency")
        if not currency:
            raise ValueError(
                f"Merchant {self._config.merchant_id} response is missing currency metadata; "
                "cannot safely label monetary amounts."
            )
        return str(currency)

    async def merchant_timezone(self) -> str:
        info = await self.get_merchant_info()
        return str(info.get("timezone") or "UTC")
