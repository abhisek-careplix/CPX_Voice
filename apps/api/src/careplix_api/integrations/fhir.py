"""Minimal FHIR R4 HTTP client for payer endpoints.

Auth modes:
- ``none``: sandboxes only (the CarePlix Payer Sandbox, Inferno simulations).
- ``smart_backend``: SMART Backend Services. A signed JWT client assertion (RS384) is exchanged at
  the token endpoint for a short-lived access token. The private key is read from the environment
  variable named in the connection's ``auth_ref.key_env``; it is never stored in the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

FHIR_JSON = "application/fhir+json"


class FhirError(Exception):
    def __init__(self, status: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status


@dataclass
class FhirResponse:
    http_status: int
    body: dict[str, Any]
    duration_ms: int
    request_hash: str | None = None


@dataclass
class AuthConfig:
    type: str = "none"
    client_id: str = ""
    token_url: str = ""
    key_env: str = ""
    kid: str = ""
    scope: str = "system/*.read system/Claim.write"
    _token: str | None = field(default=None, repr=False)
    _expires_at: float = 0.0

    @classmethod
    def from_ref(cls, auth_type: str, ref: dict[str, Any] | None) -> AuthConfig:
        ref = ref or {}
        return cls(
            type=auth_type,
            client_id=ref.get("client_id", ""),
            token_url=ref.get("token_url", ""),
            key_env=ref.get("key_env", ""),
            kid=ref.get("kid", ""),
            scope=ref.get("scope", cls.scope),
        )


def request_hash(body: dict[str, Any]) -> str:
    """Stable digest for the audit log; the payload itself is never logged."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


class FhirClient:
    def __init__(
        self,
        base_url: str,
        auth: AuthConfig,
        *,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        headers = {"Accept": FHIR_JSON, "Content-Type": FHIR_JSON}
        if self.auth.type == "smart_backend":
            headers["Authorization"] = f"Bearer {await self._access_token()}"
        elif self.auth.type == "google_id_token":
            headers["Authorization"] = f"Bearer {await self._google_id_token()}"
        elif self.auth.type != "none":
            raise FhirError("error", f"Unsupported auth type {self.auth.type}")
        return headers

    async def _google_id_token(self) -> str:
        """Identity token for a private Cloud Run endpoint (the synthetic payer sandbox)."""
        import asyncio
        from urllib.parse import urlsplit

        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        a = self.auth
        if a._token and time.time() < a._expires_at - 60:
            return a._token
        parts = urlsplit(self.base_url)
        audience = f"{parts.scheme}://{parts.netloc}"
        a._token = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        a._expires_at = time.time() + 3000
        return a._token

    async def _access_token(self) -> str:
        a = self.auth
        if a._token and time.time() < a._expires_at - 30:
            return a._token
        key = os.environ.get(a.key_env, "")
        if not (a.client_id and a.token_url and key):
            raise FhirError("error", "SMART Backend Services credentials are not configured.")
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": a.client_id,
                "sub": a.client_id,
                "aud": a.token_url,
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + 300,
            },
            key,
            algorithm="RS384",
            headers={"kid": a.kid} if a.kid else None,
        )
        response = await self._http.post(
            a.token_url,
            data={
                "grant_type": "client_credentials",
                "scope": a.scope,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            },
        )
        if response.status_code != 200:
            raise FhirError("error", "Token request was rejected.", response.status_code)
        token = response.json()
        a._token, a._expires_at = (
            token["access_token"],
            time.time() + int(token.get("expires_in", 300)),
        )
        return a._token

    async def _send(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> FhirResponse:
        started = time.perf_counter()
        try:
            response = await self._http.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                json=body,
                headers=await self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise FhirError("timeout", "The payer endpoint did not answer in time.") from exc
        except httpx.TransportError as exc:
            raise FhirError(
                "error", f"Could not reach the payer endpoint ({type(exc).__name__})."
            ) from exc
        duration = round((time.perf_counter() - started) * 1000)
        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        if response.status_code >= 400:
            raise FhirError(
                "error",
                f"Payer endpoint returned HTTP {response.status_code}.",
                response.status_code,
            )
        return FhirResponse(
            response.status_code, parsed, duration, request_hash(body) if body else None
        )

    async def metadata(self) -> FhirResponse:
        return await self._send("GET", "metadata")

    async def operation(self, path: str, bundle: dict[str, Any]) -> FhirResponse:
        return await self._send("POST", path, bundle)
