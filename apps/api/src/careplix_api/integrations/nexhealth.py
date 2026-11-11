"""NexHealth Synchronizer client: one API across the practice's EHR (EMR design §1.1).

- ``POST /authenticates`` exchanges the API key for a bearer JWT (1 h). The token is cached per
  key for the whole process, so every client for the same key shares one token.
- Every request carries ``subdomain``; location-level records also need ``location_id``.
- ``Nex-Api-Version: v3.0.0`` is the only version header (the v2 ``Accept`` header is retired).
- Lists are cursor-paginated (``end_cursor`` / ``page_info.has_next_page``, up to 1000 per page).
  ``list_all`` reads every page or raises: a partial list must never advance a sync cursor.
- Limits are per key (2,000/min overall, 1,000/min on ``GET /patients`` and ``GET /appointments``,
  50/s). A process-wide throttle keeps each key under them.
- Retries: reads retry 429 and 5xx; writes retry only 429 (the request was refused, not
  processed), always honouring ``Retry-After``. A write that may have landed is never resent.
- Each call is billable ($0.10 in production), so the client counts them for the cost meter.
- NexHealth's edge rejects the default Python user agent, so we always send our own.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

API_VERSION = "v3.0.0"
USER_AGENT = "CarePlix-Voice/1.0"
TOKEN_TTL_S = 50 * 60  # tokens last 1 h; refresh early
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 30.0  # a longer wait fails the call instead of holding a caller
MAX_PAGES = 5000  # 5M rows at 1000 a page: past this a list is treated as broken, not done

# Per-key limits (rate-limiting reference). Paths in ROUTE_LIMITS have their own minute budget.
LIMIT_PER_MINUTE = 2000
LIMIT_PER_SECOND = 50
ROUTE_LIMITS = {("GET", "/patients"): 1000, ("GET", "/appointments"): 1000}

# Replaced in tests so retries and throttling don't actually wait.
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
clock: Callable[[], float] = time.monotonic


class NexHealthError(Exception):
    def __init__(self, status: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status


def _key_id(base_url: str, api_key: str) -> str:
    """Cache key for one API key at one base URL; the key itself is never kept as a dict key."""
    return hashlib.sha256(f"{base_url}\0{api_key}".encode()).hexdigest()


_TOKENS: dict[str, tuple[str, float]] = {}


class Throttle:
    """Sliding-window limiter for one API key: waits until a request fits every budget."""

    def __init__(self):
        self.second: deque[float] = deque()
        self.minute: deque[float] = deque()
        self.routes: dict[tuple[str, str], deque[float]] = {}

    @staticmethod
    def _wait(window: deque[float], limit: int, span: float, now: float) -> float:
        while window and now - window[0] >= span:
            window.popleft()
        return 0.0 if len(window) < limit else span - (now - window[0])

    async def acquire(self, method: str, path: str) -> None:
        route = (method, path)
        route_limit = ROUTE_LIMITS.get(route)
        while True:
            now = clock()
            wait = max(
                self._wait(self.second, LIMIT_PER_SECOND, 1.0, now),
                self._wait(self.minute, LIMIT_PER_MINUTE, 60.0, now),
                self._wait(self.routes.setdefault(route, deque()), route_limit, 60.0, now)
                if route_limit
                else 0.0,
            )
            if wait <= 0:
                break
            await sleep(wait)
        now = clock()
        self.second.append(now)
        self.minute.append(now)
        if route_limit:
            self.routes[route].append(now)


_THROTTLES: dict[str, Throttle] = {}


def reset_process_state() -> None:
    """Forget cached tokens and throttle windows (tests; a key rotation in a running process)."""
    _TOKENS.clear()
    _THROTTLES.clear()


def retry_after(response: httpx.Response) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date)."""
    value = (response.headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class NexHealthClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        subdomain: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ):
        self.base_url, self.subdomain = base_url.rstrip("/"), subdomain
        self._key = api_key
        self._key_id = _key_id(self.base_url, api_key)
        self.calls = 0
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={"Nex-Api-Version": API_VERSION, "User-Agent": USER_AGENT},
        )

    @property
    def throttle(self) -> Throttle:
        return _THROTTLES.setdefault(self._key_id, Throttle())

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, refresh: bool = False) -> str:
        cached = _TOKENS.get(self._key_id)
        if cached and not refresh and clock() < cached[1]:
            return cached[0]
        body = await self._send("POST", "/authenticates", auth=self._key)
        token = (body.get("data") or {}).get("token")
        if not token:
            raise NexHealthError("auth", "NexHealth did not return a token")
        _TOKENS[self._key_id] = (token, clock() + TOKEN_TTL_S)
        return token

    async def _send(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Exchanging the key for a token changes nothing, so it retries like a read.
        safe = method == "GET" or path == "/authenticates"
        for attempt in range(MAX_ATTEMPTS):
            await self.throttle.acquire(method, path)
            self.calls += 1
            try:
                response = await self._http.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": auth},
                )
            except httpx.TimeoutException:
                # A write that timed out may have landed: its outcome is unknown, never resent.
                if safe and attempt < MAX_ATTEMPTS - 1:
                    await sleep(0.5 * 2**attempt)
                    continue
                raise NexHealthError("timeout", f"{method} {path} timed out") from None
            except httpx.HTTPError as exc:
                if safe and attempt < MAX_ATTEMPTS - 1 and isinstance(exc, httpx.ConnectError):
                    await sleep(0.5 * 2**attempt)
                    continue
                raise NexHealthError("network", type(exc).__name__) from None
            code = response.status_code
            retryable = code == 429 or (safe and code >= 500)
            if retryable and attempt < MAX_ATTEMPTS - 1:
                wait = retry_after(response)
                if wait is None:
                    wait = 0.5 * 2**attempt
                if wait <= MAX_RETRY_AFTER_S:
                    await sleep(wait)
                    continue
            break
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 400:
            errors = body.get("error") if isinstance(body, dict) else None
            message = (
                "; ".join(errors) if isinstance(errors, list) and errors else response.reason_phrase
            )
            status = {401: "auth", 403: "auth", 429: "rate_limited"}.get(
                response.status_code, "http"
            )
            raise NexHealthError(status, f"{method} {path}: {message}", response.status_code)
        return body

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        *,
        scoped: bool = True,
    ) -> dict[str, Any]:
        """One call. ``scoped=False`` leaves ``subdomain`` off (key-level routes)."""
        params = {"subdomain": self.subdomain, **(params or {})} if scoped else params
        try:
            return await self._send(
                method, path, auth=f"Bearer {await self._bearer()}", params=params, json=json
            )
        except NexHealthError as err:
            if err.http_status != 401:
                raise
            # Token expired early or was revoked: re-authenticate once. A 401 means the request
            # was refused before it was processed, so resending a write is safe here.
            return await self._send(
                method,
                path,
                auth=f"Bearer {await self._bearer(refresh=True)}",
                params=params,
                json=json,
            )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.request("GET", path, params)).get("data")

    async def list_all(
        self, path: str, params: dict[str, Any] | None = None, *, max_pages: int = MAX_PAGES
    ) -> tuple[list[dict[str, Any]], int]:
        """Follow ``end_cursor`` until ``has_next_page`` is false. Returns (rows, pages).

        Raises ``NexHealthError("incomplete")`` rather than return a partial list, so a caller
        that advances a high-water mark only does so after reading everything.
        """
        rows: list[dict[str, Any]] = []
        query = {"per_page": 1000, **(params or {})}
        for page in range(1, max_pages + 1):
            body = await self.request("GET", path, query)
            data = body.get("data") or []
            rows.extend(data if isinstance(data, list) else [])
            info = body.get("page_info") or {}
            if not info.get("has_next_page"):
                return rows, page
            if not info.get("end_cursor"):
                raise NexHealthError("incomplete", f"GET {path}: next page without a cursor")
            query = {**query, "end_cursor": info["end_cursor"]}
        raise NexHealthError("incomplete", f"GET {path}: more than {max_pages} pages")

    async def institutions(self) -> list[dict[str, Any]]:
        return (await self.request("GET", "/institutions", scoped=False)).get("data") or []
