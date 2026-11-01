"""Webhook ingress (the ``careplix-webhooks`` service): verify, dedupe, record IDs, return fast.

This is the only public, non-IAP surface. It holds no business logic and stores no payloads:
each event records the resource IDs it names, and the practice worker re-reads those records
from the source. A forged NexHealth delivery can therefore only cause a re-read.

- NexHealth: deliveries go to an unguessable per-connection URL token (only its hash is stored).
  NexHealth's signing scheme is not documented; header *names* are logged to confirm it later.
- Stedi: Standard Webhooks signature with the event destination secret.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database
from careplix_api.errors import ApiError
from careplix_api.integrations import stedi
from careplix_api.models import PayerConnection, WebhookEvent
from careplix_api.phi import PhiBox
from careplix_api.practice.connections import EHR, nexhealth_client
from careplix_api.settings import Settings, get_settings

log = logging.getLogger("careplix.webhooks")
router = APIRouter(prefix="/webhooks")

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
async def nexhealth(token: str, request: Request, session: AsyncSession = Depends(_session)):
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
    try:
        event = json.loads(body)
    except ValueError:
        raise ApiError(400, "invalid_body", "Expected JSON.") from None
    log.info(
        "nexhealth webhook",
        extra={"event": event.get("event_name"), "header_names": sorted(request.headers.keys())},
    )
    new = await _record(
        session,
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        provider="nexhealth",
        delivery_key="sha256:" + hashlib.sha256(body).hexdigest(),
        event_name=str(event.get("event_name") or "unknown")[:80],
        resource_type=str(event.get("resource_type") or "")[:40] or None,
        resource_ids=resource_ids(event.get("data")),
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
    try:
        previous = ((conn.auth_ref or {}).get("webhook") or {}).get("endpoint_id")
        if previous:
            try:
                await client.request("DELETE", f"/webhook_endpoints/{previous}")
            except Exception:  # noqa: BLE001 - a stale endpoint must not block re-registration
                log.warning("could not delete previous webhook endpoint")
        target = f"{settings.webhook_public_url.rstrip('/')}/webhooks/nexhealth/{token}"
        endpoint = (
            await client.request("POST", "/webhook_endpoints", json={"target_url": target})
        ).get("data") or {}
        subscribed = []
        for resource_type, event in SUBSCRIPTIONS:
            await client.request(
                "POST",
                f"/webhook_endpoints/{endpoint['id']}/webhook_subscriptions",
                json={"resource_type": resource_type, "event": event, "active": True},
            )
            subscribed.append(event)
    finally:
        await client.aclose()
    conn.auth_ref = {
        **(conn.auth_ref or {}),
        "webhook": {
            "endpoint_id": endpoint.get("id"),
            "token_hash": token_hash(token),
            # Kept for signature verification once NexHealth documents the scheme.
            "secret_sealed": PhiBox(settings).seal({"secret": endpoint.get("secret_key")}),
            "events": subscribed,
        },
    }
    return {"endpoint_id": endpoint.get("id"), "events": subscribed}


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
