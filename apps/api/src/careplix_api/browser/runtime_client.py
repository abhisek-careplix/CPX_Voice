"""Client for the browser runtime service (apps/browser). Private Cloud Run: Google identity token
plus a shared service token. Tests swap in an in-memory runtime with the same methods."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


class RuntimeUnavailable(Exception):
    pass


class BrowserRuntime(Protocol):
    configured: bool

    async def open(self, start_url: str, allowed_hosts: list[str], state: dict | None) -> str: ...
    async def screenshot(self, session_id: str, fmt: str = "png") -> dict[str, Any]: ...
    async def inspect(self, session_id: str, x: int | None, y: int | None) -> dict | None: ...
    async def act(self, session_id: str, action: dict[str, Any]) -> dict[str, Any]: ...
    async def allow(self, session_id: str, hosts: list[str]) -> None: ...
    async def storage_state(self, session_id: str) -> dict: ...
    async def close(self, session_id: str) -> None: ...


class HttpRuntime:
    def __init__(
        self,
        base_url: str,
        token: str,
        auth: str = "none",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.auth = auth  # none | google_id_token
        self.configured = bool(base_url and token)
        self._id_token: str | None = None
        self._id_expires = 0.0
        self._client = httpx.AsyncClient(timeout=60, transport=transport)

    async def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.auth == "google_id_token":
            # Cloud Run IAM reads the identity token from X-Serverless-Authorization and passes
            # Authorization (our service token) through to the runtime untouched.
            headers["X-Serverless-Authorization"] = f"Bearer {await self._google_id_token()}"
        return headers

    async def _google_id_token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        if self._id_token and time.time() < self._id_expires - 60:
            return self._id_token
        parts = urlsplit(self.base_url)
        audience = f"{parts.scheme}://{parts.netloc}"
        self._id_token = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        self._id_expires = time.time() + 3000
        return self._id_token

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        if not self.configured:
            raise RuntimeUnavailable("The browser runtime isn't configured.")
        try:
            r = await self._client.request(
                method, self.base_url + path, headers=await self._headers(), **kwargs
            )
        except httpx.HTTPError as exc:
            raise RuntimeUnavailable(f"Browser runtime unreachable: {type(exc).__name__}") from exc
        if r.status_code == 404:
            raise RuntimeUnavailable("The browser session has ended.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            if not isinstance(detail, str):  # e.g. a crash page or a validation error list
                detail = r.text[:200]
            raise RuntimeUnavailable(f"Browser runtime error {r.status_code}: {detail}")
        return r.json()

    async def open(self, start_url, allowed_hosts, state):
        body = {"start_url": start_url, "allowed_hosts": allowed_hosts, "storage_state": state}
        return (await self._call("POST", "/sessions", json=body))["id"]

    async def screenshot(self, session_id, fmt="png"):
        return await self._call("GET", f"/sessions/{session_id}/screenshot", params={"fmt": fmt})

    async def inspect(self, session_id, x, y):
        body = {"x": x, "y": y}
        return (await self._call("POST", f"/sessions/{session_id}/inspect", json=body))["element"]

    async def act(self, session_id, action):
        return await self._call("POST", f"/sessions/{session_id}/act", json=action)

    async def allow(self, session_id, hosts):
        await self._call("POST", f"/sessions/{session_id}/allow", json={"hosts": hosts})

    async def storage_state(self, session_id):
        return await self._call("GET", f"/sessions/{session_id}/storage-state")

    async def close(self, session_id):
        with contextlib.suppress(RuntimeUnavailable):
            await self._call("DELETE", f"/sessions/{session_id}")
