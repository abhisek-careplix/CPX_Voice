"""Webhook ingress (the ``careplix-webhooks`` service): verify, dedupe, record IDs, return fast.

This is the only public, non-IAP surface. It holds no business logic and stores no payloads:
each event records the resource IDs it names (and, for appointment edits, the *names* of the
fields in ``previous_attributes``), and the practice worker re-reads those records from the source.

- NexHealth: each delivery is signed. ``signature`` is the hex HMAC-SHA256, keyed with the
  endpoint's ``secret_key`` (stored sealed), of ``timestamp + "." + base64(raw body)``;
  ``timestamp`` must be within five minutes. The URL also carries an unguessable per-connection
  token (only its hash is stored), kept as a second check. Payloads carry no event id, and a
  retried delivery differs only in ``delivery_errors``, so events dedupe on their identity, not
  the body.
- Stedi: Standard Webhooks signature with the event destination secret.
- Endpoint health: NexHealth pauses an endpoint after more than 10 failed deliveries in a row and
  deactivates it (and its subscriptions) once the last 100 all failed. ``check_health`` reads the
  endpoint and its subscriptions and surfaces both, plus deliveries NexHealth had to retry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import stedi
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import PayerConnection, WebhookEvent
from careplix_api.phi import PhiBox
from careplix_api.practice.connections import EHR, nexhealth_client
from careplix_api.settings import Settings, get_settings

log = logging.getLogger("careplix.webhooks")
router = APIRouter(prefix="/webhooks")

SIGNATURE_TOLERANCE = timedelta(minutes=5)
HEALTH_EVERY = timedelta(hours=1)
SUBSCRIPTIONS = [
    ("Appointment", "appointment_created"),
    ("Appointment", "appointment_updated"),
    ("Appointment", "appointment_requested"),
    ("Appointment", "appointment_insertion"),
    ("Patient", "patient_created"),
    ("Patient", "patient_updated"),
    ("PatientInsuranceCoverage", "patient_insurance_created"),
    ("PatientInsuranceCoverage", "patient_insurance_updated"),
    ("Document", "document_insertion"),
    ("SyncStatus", "sync_status_read_change"),
    ("SyncStatus", "sync_status_write_change"),
]
# Insertion results for the other writes NexHealth documents (payments and adjustments). Only
# practices with ledger sync have them, so a refusal here never fails the registration.
OPTIONAL_SUBSCRIPTIONS = [
    ("Payment", "payment_insertion"),
    ("Adjustment", "adjustment_insertion"),
]
# Field names from ``previous_attributes`` worth keeping (the ones a PATCH can change).
CHANGE_FIELDS = {
    "cancelled",
    "confirmed",
    "checkin_at",
    "start_time",
    "end_time",
    "operatory_id",
    "note",
    "patient_id",
}


def token_hash(token: str) -> str:
    return hashlib.sha256(f"nexhealth-webhook:{token}".encode()).hexdigest()


async def _session(request: Request):
    async with request.app.state.db.sessions() as session:
        yield session


def resource_ids(data: Any) -> list[str]:
    """IDs from ``data.<resource>`` (single) or ``data.<resources>`` (batch)."""
    ids: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            items = value if isinstance(value, list) else [value]
            ids += [str(i["id"]) for i in items if isinstance(i, dict) and i.get("id") is not None]
    return ids[:200]


def _records(data: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for value in data.values():
            out += [
                i for i in (value if isinstance(value, list) else [value]) if isinstance(i, dict)
            ]
    return out


def changed_fields(event: dict[str, Any]) -> list[str]:
    """Names (never values) of the fields in ``previous_attributes``, wherever NexHealth puts it."""
    names: set[str] = set()
    sources = [
        event.get("previous_attributes"),
        (event.get("data") or {}).get("previous_attributes"),
    ]
    sources += [r.get("previous_attributes") for r in _records(event.get("data"))]
    for prev in sources:
        if isinstance(prev, dict):
            names |= {k for k in prev if isinstance(k, str) and k in CHANGE_FIELDS}
    return sorted(names)


def event_identity(event: dict[str, Any]) -> str:
    """Dedupe key: what the event is about, not the delivery (retries add ``delivery_errors``)."""
    if not event.get("event_time"):
        trimmed = {k: v for k, v in event.items() if k != "delivery_errors"}
        raw = json.dumps(trimmed, sort_keys=True, default=str).encode()
        return "evb:" + hashlib.sha256(raw).hexdigest()
    records = _records(event.get("data"))
    identity = {
        "subscription": event.get("webhook_subscription_id"),
        "subdomain": event.get("subdomain"),
        "event": event.get("event_name"),
        "time": event.get("event_time"),
        "type": event.get("resource_type"),
        "ids": sorted(resource_ids(event.get("data"))),
        "updated": sorted(str(r.get("updated_at") or "") for r in records),
    }
    raw = json.dumps(identity, sort_keys=True, default=str).encode()
    return "evt:" + hashlib.sha256(raw).hexdigest()


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def nexhealth_signature(secret: str, timestamp: str, body: bytes) -> str:
    """NexHealth's scheme: hex HMAC-SHA256(secret_key, timestamp + "." + base64(raw body))."""
    message = f"{timestamp}.{base64.b64encode(body).decode()}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_nexhealth(
    secret: str | None,
    headers: dict[str, str],
    body: bytes,
    now: datetime | None = None,
    tolerance: timedelta = SIGNATURE_TOLERANCE,
) -> bool:
    timestamp, signature = headers.get("timestamp"), headers.get("signature")
    if not (secret and timestamp and signature):
        return False
    sent = _parse_timestamp(timestamp)
    if sent is None or abs((now or utcnow()) - sent) > tolerance:
        return False  # stale (or far-future) deliveries can't be replayed
    expected = nexhealth_signature(secret, timestamp, body)
    given = signature.strip()
    digest = bytes.fromhex(expected)
    return hmac.compare_digest(given.lower().encode(), expected.encode()) or hmac.compare_digest(
        given.encode(), base64.b64encode(digest)
    )


async def _record(session: AsyncSession, **fields: Any) -> bool:
    exists = await session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == fields["provider"],
            WebhookEvent.delivery_key == fields["delivery_key"],
        )
    )
    if exists:
        return False
    session.add(WebhookEvent(**fields))
    await session.commit()
    return True


@router.post("/nexhealth/{token}")
async def nexhealth(
    token: str,
    request: Request,
    session: AsyncSession = Depends(_session),
    settings: Settings = Depends(get_settings),
):
    digest = token_hash(token)
    conn = next(
        (
            c
            for c in await session.scalars(
                select(PayerConnection).where(PayerConnection.kind == EHR)
            )
            if secrets.compare_digest(
                ((c.auth_ref or {}).get("webhook") or {}).get("token_hash", ""), digest
            )
        ),
        None,
    )
    if conn is None:
        raise ApiError(404, "not_found", "Unknown webhook endpoint.")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    sealed = ((conn.auth_ref or {}).get("webhook") or {}).get("secret_sealed")
    secret = PhiBox(settings).open(sealed).get("secret") if sealed else None
    if not verify_nexhealth(secret, headers, body):
        log.warning(
            "nexhealth webhook rejected",
            extra={"reason": "no secret stored" if not secret else "signature or timestamp"},
        )
        raise ApiError(401, "invalid_signature", "Signature check failed.")
    try:
        event = json.loads(body)
    except ValueError:
        raise ApiError(400, "invalid_body", "Expected JSON.") from None
    if not isinstance(event, dict):
        raise ApiError(400, "invalid_body", "Expected a JSON object.")
    retries = event.get("delivery_errors")
    detail = {
        "changed": changed_fields(event),
        "delivery_errors": len(retries) if isinstance(retries, list) else 0,
    }
    new = await _record(
        session,
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        provider="nexhealth",
        delivery_key=event_identity(event),
        event_name=str(event.get("event_name") or "unknown")[:80],
        resource_type=str(event.get("resource_type") or "")[:40] or None,
        resource_ids=resource_ids(event.get("data")),
        detail=detail,
    )
    return {"ok": True, "duplicate": not new}


@router.post("/stedi")
async def stedi_events(
    request: Request,
    session: AsyncSession = Depends(_session),
    settings: Settings = Depends(get_settings),
):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not stedi.verify_webhook(settings.stedi_webhook_secret.get_secret_value(), headers, body):
        raise ApiError(401, "invalid_signature", "Signature check failed.")
    event = json.loads(body)
    resource = event.get("resource") or {}
    conn = await session.scalar(select(PayerConnection).where(PayerConnection.kind == "stedi"))
    new = await _record(
        session,
        workspace_id=conn.workspace_id if conn else None,
        connection_id=conn.id if conn else None,
        provider="stedi",
        delivery_key=headers["webhook-id"][:80],
        event_name=str(event.get("type") or "unknown")[:80],
        resource_type=str(resource.get("type") or "")[:40] or None,
        resource_ids=[str(resource["id"])] if resource.get("id") else [],
    )
    return {"ok": True, "duplicate": not new}


async def register(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Create (or replace) this connection's NexHealth webhook endpoint and subscriptions."""
    if not settings.webhook_public_url:
        raise ApiError(
            409,
            "webhooks_unavailable",
            "Set WEBHOOK_PUBLIC_URL to the careplix-webhooks service before registering.",
        )
    token = secrets.token_urlsafe(32)
    client = nexhealth_client(conn, settings, transport)
    skipped: list[str] = []
    try:
        previous = ((conn.auth_ref or {}).get("webhook") or {}).get("endpoint_id")
        if previous:
            try:
                await client.request("DELETE", f"/webhook_endpoints/{previous}", scoped=False)
            except Exception:  # noqa: BLE001 - a stale endpoint must not block re-registration
                log.warning("could not delete previous webhook endpoint")
        target = f"{settings.webhook_public_url.rstrip('/')}/webhooks/nexhealth/{token}"
        endpoint = (
            await client.request(
                "POST", "/webhook_endpoints", json={"target_url": target}, scoped=False
            )
        ).get("data") or {}
        if not endpoint.get("id") or not endpoint.get("secret_key"):
            raise ApiError(502, "webhook_unsigned", "NexHealth returned no endpoint secret.")
        subscribed = []
        for resource_type, event in SUBSCRIPTIONS + OPTIONAL_SUBSCRIPTIONS:
            try:
                # subdomain goes in the query string (the client adds it), never in the body.
                await client.request(
                    "POST",
                    f"/webhook_endpoints/{endpoint['id']}/webhook_subscriptions",
                    json={"resource_type": resource_type, "event": event, "active": True},
                )
            except NexHealthError as err:
                optional = (resource_type, event) in OPTIONAL_SUBSCRIPTIONS
                if not optional or err.http_status is None or err.http_status >= 500:
                    raise
                skipped.append(event)
                continue
            subscribed.append(event)
    finally:
        await client.aclose()
    conn.auth_ref = {
        **(conn.auth_ref or {}),
        "webhook": {
            "endpoint_id": endpoint.get("id"),
            "token_hash": token_hash(token),
            "secret_sealed": PhiBox(settings).seal({"secret": endpoint.get("secret_key")}),
            "events": subscribed,
            "registered_at": utcnow().isoformat(),
        },
    }
    caps = dict(conn.capability or {})
    caps["webhooks"] = {
        "status": "ok",
        "checked_at": utcnow().isoformat(),
        "endpoint_active": True,
        "inactive_subscriptions": [],
        "missing_subscriptions": [],
        "skipped_subscriptions": skipped,
    }
    conn.capability = caps
    return {"endpoint_id": endpoint.get("id"), "events": subscribed, "skipped": skipped}


HEALTH_MESSAGES = {
    "ok": "Deliveries are flowing.",
    "not_registered": "Webhooks aren't registered: changes arrive only with the next sync.",
    "missing": "NexHealth no longer has this endpoint. Register webhooks again.",
    "deactivated": "NexHealth deactivated the endpoint after repeated failed deliveries. "
    "Register webhooks again (that also reactivates every subscription).",
    "subscriptions_inactive": "Some subscriptions are inactive: those events aren't sent. "
    "Register webhooks again.",
    "retrying": "NexHealth had to retry recent deliveries. After more than 10 failures in a row "
    "it pauses the endpoint for an hour and skips events due then; the sync pass fills gaps.",
}


async def check_health(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Read the endpoint and its subscriptions from NexHealth and surface anything off.

    NexHealth exposes ``active`` on endpoints and subscriptions. A pause has no API field: it shows
    up here as deliveries arriving with ``delivery_errors`` (NexHealth retried them).
    """
    webhook = (conn.auth_ref or {}).get("webhook") or {}
    endpoint_id = webhook.get("endpoint_id")
    health: dict[str, Any] = {"checked_at": utcnow().isoformat()}
    if not endpoint_id:
        health["status"] = "not_registered"
    else:
        client = nexhealth_client(conn, settings, transport)
        try:
            endpoints = (await client.request("GET", "/webhook_endpoints", scoped=False)).get(
                "data"
            ) or []
            mine = next((e for e in endpoints if str(e.get("id")) == str(endpoint_id)), None)
            subs: list[dict[str, Any]] = []
            if mine is not None:
                subs = (
                    await client.request(
                        "GET", f"/webhook_endpoints/{endpoint_id}/webhook_subscriptions"
                    )
                ).get("data") or []
        finally:
            await client.aclose()
        present = {s.get("event") for s in subs}
        inactive = sorted(str(s.get("event")) for s in subs if s.get("active") is False)
        missing = sorted(e for _, e in SUBSCRIPTIONS if e not in present) if mine else []
        since = utcnow() - timedelta(hours=24)
        retried = [
            e
            for e in await session.scalars(
                select(WebhookEvent).where(
                    WebhookEvent.connection_id == conn.id, WebhookEvent.provider == "nexhealth"
                )
            )
            if (as_utc(e.received_at) or since) >= since and (e.detail or {}).get("delivery_errors")
        ]
        health.update(
            endpoint_active=bool(mine and mine.get("active") is not False),
            inactive_subscriptions=inactive,
            missing_subscriptions=missing,
            retried_deliveries_24h=len(retried),
        )
        if mine is None:
            health["status"] = "missing"
        elif mine.get("active") is False:
            health["status"] = "deactivated"
        elif inactive or missing:
            health["status"] = "subscriptions_inactive"
        elif retried:
            health["status"] = "retrying"
        else:
            health["status"] = "ok"
    health["message"] = HEALTH_MESSAGES[health["status"]]
    conn.capability = {**(conn.capability or {}), "webhooks": health}
    if health["status"] not in ("ok", "not_registered"):
        log.warning("nexhealth webhook endpoint unhealthy", extra={"status": health["status"]})
    return health


def health_due(conn: PayerConnection, now: datetime | None = None) -> bool:
    if not ((conn.auth_ref or {}).get("webhook") or {}).get("endpoint_id"):
        return False
    checked = ((conn.capability or {}).get("webhooks") or {}).get("checked_at")
    if not checked:
        return True
    try:
        at = as_utc(datetime.fromisoformat(checked))
    except ValueError:
        return True
    return (now or utcnow()) - at >= HEALTH_EVERY


def create_webhook_app(settings: Settings | None = None) -> FastAPI:
    """Standalone ingress for Cloud Run (public, no IAP): ``careplix_api.practice.webhooks:app``."""
    from contextlib import asynccontextmanager

    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_url)
        await app.state.db.create_all()
        yield
        await app.state.db.dispose()

    app = FastAPI(
        title="CarePlix webhooks",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code}})

    app.include_router(router)
    return app


def __getattr__(name: str):  # lazily build ``app`` so importing the module has no side effects
    if name == "app":
        return create_webhook_app()
    raise AttributeError(name)
