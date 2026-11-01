"""Stedi clearinghouse client: eligibility (270/271), payer directory and webhook verification.

The 2026-06-01 eligibility API returns ``plans[].benefits`` grouped as ``statuses``, ``coPayment``,
``coInsurance``, ``deductible`` and ``outOfPocket`` (verified live on 31 Oct 2026). Test keys reach
mock payers only, and only Stedi's published mock members succeed (``MOCK_SUBSCRIBER``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import httpx

ELIGIBILITY_PATH = "/2026-06-01/eligibility-check"
PAYER_SEARCH_PATH = "/2024-04-01/payers/search"
USER_AGENT = "CarePlix-Voice/1.0"

# Stedi's documented mock request (Aetna). Test mode sends this member instead of the patient.
MOCK_PAYER_ID = "60054"
MOCK_PROVIDER_NPI = "1999999984"
MOCK_SUBSCRIBER = {
    "first_name": "Jane",
    "last_name": "Doe",
    "member_id": "AETNA12345",
    "date_of_birth": "2004-04-04",
}


class StediError(Exception):
    def __init__(self, status: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.status, self.message, self.http_status = status, message, http_status


class StediClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 45.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.calls = 0
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Key {api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[dict[str, Any], int]:
        started = time.perf_counter()
        self.calls += 1
        try:
            response = await self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException:
            raise StediError("timeout", f"{method} {path} timed out") from None
        except httpx.HTTPError as exc:
            raise StediError("network", type(exc).__name__) from None
        ms = round((time.perf_counter() - started) * 1000)
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            status = "auth" if response.status_code in (401, 403) else "http"
            raise StediError(status, message or response.reason_phrase, response.status_code)
        return body, ms

    async def eligibility(self, request: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return await self._request("POST", ELIGIBILITY_PATH, json=request)

    async def search_payers(self, query: str) -> list[dict[str, Any]]:
        body, _ = await self._request("GET", PAYER_SEARCH_PATH, params={"query": query})
        return [item.get("payer") or {} for item in body.get("items") or []]


def eligibility_request(
    *,
    payer_id: str,
    provider_npi: str,
    provider_name: str,
    subscriber: dict[str, str],
    service_type: str = "30",
) -> dict[str, Any]:
    return {
        "payerId": payer_id,
        "provider": {"name": {"organization": provider_name}, "npi": provider_npi},
        "subscriber": {
            "name": {
                "person": {
                    "firstName": subscriber["first_name"],
                    "lastName": subscriber["last_name"],
                }
            },
            "memberId": subscriber["member_id"],
            "dateOfBirth": subscriber["date_of_birth"],
        },
        "encounter": {"services": [{"value": service_type, "system": "STC"}]},
    }


def _lines(response: dict[str, Any], group: str) -> list[dict[str, Any]]:
    return [
        line
        for plan in response.get("plans") or []
        for line in (plan.get("benefits") or {}).get(group) or []
    ]


def _pick(
    lines: list[dict[str, Any]],
    services: tuple[str, ...],
    *,
    period: str | None = None,
    network: str = "IN_NETWORK",
) -> dict[str, Any] | None:
    for service in services:
        for line in lines:
            if (
                (line.get("service") or {}).get("value") == service
                and (line.get("network") or {}).get("indicator")
                in (network, "IN_AND_OUT_OF_NETWORK")
                and (period is None or line.get("timePeriod") == period)
            ):
                return line
    return None


def _auth_flags(node: Any, found: list[str], service: str | None = None) -> None:
    """Collect services the payer marks as needing prior authorization, in any response layout."""
    if isinstance(node, dict):
        service = (node.get("service") or {}).get("definition") or service
        for key, value in node.items():
            lowered = key.lower()
            flagged = ("auth" in lowered or "certification" in lowered) and value in (
                True,
                "Y",
                "YES",
                "REQUIRED",
            )
            if flagged and service and service not in found:
                found.append(service)
            _auth_flags(value, found, service)
    elif isinstance(node, list):
        for item in node:
            _auth_flags(item, found, service)


def summarize_eligibility(response: dict[str, Any]) -> dict[str, Any]:
    """Outcome fields only (copays, status, plan name): no member identifiers (SEC08)."""
    statuses = _lines(response, "statuses")
    status_values = sorted({s.get("status") for s in statuses if s.get("status")})
    if not status_values:  # legacy layout
        status_values = sorted(
            {
                "ACTIVE_COVERAGE" if s.get("statusCode") == "1" else str(s.get("status"))
                for s in response.get("planStatus") or []
            }
        )
    errors = [str(e.get("code") or e.get("description")) for e in response.get("errors") or []]
    if errors and not status_values:
        coverage = "error"
    elif "ACTIVE_COVERAGE" in status_values:
        coverage = "active"
    elif status_values:
        coverage = "inactive"
    else:
        coverage = "unknown"
    plan = next(
        (s.get("planCoverageDescription") for s in statuses if s.get("planCoverageDescription")),
        None,
    )
    office = ("98", "30")
    copay = _pick(_lines(response, "coPayment"), office)
    coinsurance = _pick(_lines(response, "coInsurance"), office)
    deductible = _lines(response, "deductible")
    oop = _lines(response, "outOfPocket")
    auth: list[str] = []
    _auth_flags(response.get("plans") or response.get("benefitsInformation") or [], auth)
    payer = response.get("payer") or {}
    return {
        "coverage": coverage,
        "statuses": status_values,
        "plan": plan,
        "insurance_type": next(
            (s.get("insuranceType") for s in statuses if s.get("insuranceType")), None
        ),
        "office_copay": copay.get("amount") if copay else None,
        "office_coinsurance_pct": coinsurance.get("percent") if coinsurance else None,
        "deductible": {
            "total": (_pick(deductible, ("30",), period="CONTRACT") or {}).get("amount"),
            "remaining": (_pick(deductible, ("30",), period="REMAINING") or {}).get("amount"),
        },
        "out_of_pocket": {
            "total": (_pick(oop, ("30",), period="CONTRACT") or {}).get("amount"),
            "remaining": (_pick(oop, ("30",), period="REMAINING") or {}).get("amount"),
        },
        "auth_required_services": auth,
        "payer": (payer.get("name") or {}).get("organization")
        if isinstance(payer.get("name"), dict)
        else payer.get("name"),
        "errors": errors,
    }


def verify_webhook(
    secret: str, headers: dict[str, str], body: bytes, tolerance_s: int = 300
) -> bool:
    """Standard Webhooks check: ``webhook-id``, ``webhook-timestamp``, ``webhook-signature``."""
    msg_id, stamp, signatures = (
        headers.get("webhook-id"),
        headers.get("webhook-timestamp"),
        headers.get("webhook-signature"),
    )
    if not (secret and msg_id and stamp and signatures):
        return False
    try:
        if abs(time.time() - int(stamp)) > tolerance_s:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(hmac.compare_digest(sig.split(",", 1)[-1], expected) for sig in signatures.split())
