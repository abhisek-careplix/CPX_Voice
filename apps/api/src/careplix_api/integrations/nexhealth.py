"""NexHealth Synchronizer client: one API across the practice's EHR (EMR design §1.1).

- ``POST /authenticates`` exchanges the API key for a bearer JWT (1 h in production), cached here.
- Every request carries ``subdomain``; location-level records also need ``location_id``.
- Lists are cursor-paginated (``end_cursor`` / ``page_info.has_next_page``, up to 1000 per page).
- Each call is billable ($0.10), so the client counts them for the cost meter.
- NexHealth's edge rejects the default Python user agent, so we always send our own.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

API_VERSION = "v3.0.0"
USER_AGENT = "CarePlix-Voice/1.0"
TOKEN_TTL_S = 50 * 60  # production tokens last 1 h; refresh early


class NexHealthError(Exception):
    def __init__(self, status: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status


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
        self._token: tuple[str, float] | None = None
        self.calls = 0
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={
                "Nex-Api-Version": API_VERSION,
                "Accept": "application/vnd.Nexhealth+json;version=2",
                "User-Agent": USER_AGENT,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token[1]:
            return self._token[0]
        body = await self._send("POST", "/authenticates", auth=self._key)
        token = (body.get("data") or {}).get("token")
        if not token:
            raise NexHealthError("auth", "NexHealth did not return a token")
        self._token = (token, time.monotonic() + TOKEN_TTL_S)
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
        for attempt in range(3):
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
                raise NexHealthError("timeout", f"{method} {path} timed out") from None
            except httpx.HTTPError as exc:
                raise NexHealthError("network", type(exc).__name__) from None
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.5 * 2**attempt)
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
            status = "auth" if response.status_code in (401, 403) else "http"
            raise NexHealthError(status, f"{method} {path}: {message}", response.status_code)
        return body

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = {"subdomain": self.subdomain, **(params or {})}
        try:
            return await self._send(
                method, path, auth=f"Bearer {await self._bearer()}", params=params, json=json
            )
        except NexHealthError as err:
            if err.http_status != 401:
                raise
            # Token expired early or was revoked: re-authenticate once.
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
        self, path: str, params: dict[str, Any] | None = None, *, max_pages: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """Follow ``end_cursor`` until the last page. Returns (rows, pages)."""
        rows: list[dict[str, Any]] = []
        query = {"per_page": 1000, **(params or {})}
        for page in range(1, max_pages + 1):
            body = await self.request("GET", path, query)
            data = body.get("data") or []
            rows.extend(data if isinstance(data, list) else [])
            info = body.get("page_info") or {}
            if not info.get("has_next_page") or not info.get("end_cursor"):
                return rows, page
            query = {**query, "end_cursor": info["end_cursor"]}
        return rows, max_pages

    async def institutions(self) -> list[dict[str, Any]]:
        body = await self._send("GET", "/institutions", auth=f"Bearer {await self._bearer()}")
        return body.get("data") or []
