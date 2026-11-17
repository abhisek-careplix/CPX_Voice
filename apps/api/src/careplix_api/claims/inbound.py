"""What the clearinghouse sends back: 277CA acknowledgements and 835 remittances.

The webhook service only records the event (``practice/webhooks.py``: the transaction ID and its
X12 type, no payload). :func:`process_events` then fetches each report through the gateway and
applies it (:mod:`.acks` for 277, :mod:`.era` for 835). Without webhooks (or to catch up),
:func:`poll` lists the clearinghouse's recent inbound transactions and :func:`fetch` applies one
transaction by ID: the same path, idempotent per claim and transaction.

The clearinghouse account is CarePlix's and serves every workspace; a report's claims are found
by control number, so the background loop runs without a tenant. Inside a request, the tenant
wall limits it to the caller's workspace.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import acks
from careplix_api.db import Database, utcnow
from careplix_api.integrations import clearinghouse
from careplix_api.models import PayerConnection, WebhookEvent
from careplix_api.practice.connections import CLEARINGHOUSE, is_test
from careplix_api.settings import Settings

log = logging.getLogger("careplix.claims")
KIND = {"transaction.x12.277": "277", "transaction.x12.835": "835"}


async def connection_for(
    session: AsyncSession, mode: str | None, workspace_id: str | None = None
) -> PayerConnection | None:
    """A clearinghouse connection in ``mode`` (test or production); any when mode is None."""
    query = select(PayerConnection).where(
        PayerConnection.kind == CLEARINGHOUSE, PayerConnection.enabled.is_(True)
    )
    if workspace_id:
        query = query.where(PayerConnection.workspace_id == workspace_id)
    for conn in await session.scalars(query.order_by(PayerConnection.created_at)):
        conn_mode = clearinghouse.TEST if is_test(conn) else clearinghouse.PRODUCTION
        if mode is None or conn_mode == mode:
            return conn
    return None


async def apply_report(
    session: AsyncSession,
    settings: Settings,
    gateway: clearinghouse.Clearinghouse,
    transaction_id: str,
    kind: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    report, _ = await gateway.report(transaction_id, kind)
    return await ingest(session, settings, report, transaction_id, kind, gateway.mode, now=now)


async def ingest(
    session: AsyncSession,
    settings: Settings,
    report: dict[str, Any],
    transaction_id: str,
    kind: str,
    mode: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if kind == "277":
        counts = await acks.ingest_277(
            session, settings, report, transaction_id=transaction_id, mode=mode, now=now
        )
    else:
        from careplix_api.claims import era  # local: era is optional until RC7 lands

        counts = await era.ingest_835(
            session, settings, report, transaction_id=transaction_id, mode=mode, now=now
        )
    return {"transaction_id": transaction_id, "kind": kind, **counts}


async def fetch(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    transaction_id: str,
    kind: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    conn = await connection_for(session, None, workspace_id)
    if conn is None:
        raise clearinghouse.ClearinghouseError(
            "not_connected", "Connect the clearinghouse first.", "correct"
        )
    gateway = clearinghouse.for_connection(conn, settings, transport)
    try:
        return await apply_report(session, settings, gateway, transaction_id, kind)
    finally:
        await gateway.aclose()


async def poll(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    days: int = 3,
) -> list[dict[str, Any]]:
    """Apply every 277 and 835 the clearinghouse processed in the last ``days`` days."""
    conn = await connection_for(session, None, workspace_id)
    if conn is None:
        raise clearinghouse.ClearinghouseError(
            "not_connected", "Connect the clearinghouse first.", "correct"
        )
    gateway = clearinghouse.for_connection(conn, settings, transport)
    start = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    try:
        for item in await gateway.recent_reports(start):
            try:
                out.append(
                    await apply_report(
                        session, settings, gateway, item["transaction_id"], item["kind"]
                    )
                )
            except clearinghouse.ClearinghouseError as err:
                out.append({**item, "error": err.message})
    finally:
        await gateway.aclose()
    return out


async def process_events(
    db: Database,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    limit: int = 50,
) -> dict[str, int]:
    """Webhook events for 277 and 835 transactions not processed yet, oldest first."""
    report = {"processed": 0, "failed": 0}
    async with db.sessions() as session:
        ids = list(
            await session.scalars(
                select(WebhookEvent.id)
                .where(
                    WebhookEvent.provider == "stedi",
                    WebhookEvent.processed_at.is_(None),
                    WebhookEvent.resource_type.in_(tuple(KIND)),
                )
                .order_by(WebhookEvent.received_at)
                .limit(limit)
            )
        )
    for event_id in ids:
        async with db.sessions() as session:
            event = await session.get(WebhookEvent, event_id)
            if event is None or event.processed_at is not None:
                continue
            mode = (event.detail or {}).get("mode")
            conn = await connection_for(session, mode)
            kind = KIND[event.resource_type or ""]
            if conn is None or not event.resource_ids:
                event.processed_at, event.outcome = utcnow(), "skipped"
                event.error = "No clearinghouse connection in this mode."[:200]
                await session.commit()
                continue
            gateway = clearinghouse.for_connection(conn, settings, transport)
            try:
                counts = await apply_report(session, settings, gateway, event.resource_ids[0], kind)
                event.processed_at, event.outcome = utcnow(), f"{kind}:{counts.get('claims', 0)}"
                report["processed"] += 1
            except clearinghouse.ClearinghouseError as err:
                await session.rollback()
                event = await session.get(WebhookEvent, event_id)
                if not err.retryable:
                    event.processed_at, event.outcome = utcnow(), "failed"
                event.error = err.message[:200]
                report["failed"] += 1
            except Exception:  # noqa: BLE001 - one bad report never blocks the rest
                log.exception("clearinghouse report failed")
                await session.rollback()
                event = await session.get(WebhookEvent, event_id)
                event.processed_at, event.outcome, event.error = utcnow(), "error", "See logs."
                report["failed"] += 1
            finally:
                await gateway.aclose()
            await session.commit()
    return report
