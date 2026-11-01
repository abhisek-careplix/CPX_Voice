"""Practice worker: webhooks → re-reads, periodic sync, EHR writes, eligibility autopilot."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from careplix_api.db import Database, utcnow
from careplix_api.models import PayerConnection, WebhookEvent, WriteIntent
from careplix_api.practice import backfill, eligibility, intents, sync
from careplix_api.practice.connections import EHR
from careplix_api.practice.records import parse_dt
from careplix_api.settings import Settings

log = logging.getLogger("careplix.practice")


class PracticeWorker:
    def __init__(self, db: Database, settings: Settings, transport_getter=lambda: None):
        self.db, self.settings, self.transport_getter = db, settings, transport_getter

    async def _connections(self) -> list[str]:
        async with self.db.sessions() as session:
            return list(
                await session.scalars(
                    select(PayerConnection.id).where(
                        PayerConnection.kind == EHR, PayerConnection.enabled.is_(True)
                    )
                )
            )

    async def process_webhooks(self, conn_id: str, limit: int = 100) -> int:
        async with self.db.sessions() as session:
            conn = await session.get(PayerConnection, conn_id)
            events = list(
                await session.scalars(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.connection_id == conn_id, WebhookEvent.processed_at.is_(None)
                    )
                    .order_by(WebhookEvent.received_at)
                    .limit(limit)
                )
            )
            for event in events:
                base, _, outcome = event.event_name.partition(".")
                try:
                    if base.endswith("_insertion"):
                        for intent in await session.scalars(
                            select(WriteIntent).where(
                                WriteIntent.connection_id == conn_id,
                                WriteIntent.remote_id.in_(event.resource_ids or ["-"]),
                            )
                        ):
                            intents.apply_insertion(intent, outcome or "complete", None)
                        event.outcome = f"insertion_{outcome or 'complete'}"
                    elif event.resource_type in sync.SINGLE:
                        await sync.refresh_records(
                            session,
                            conn,
                            self.settings,
                            event.resource_type,
                            event.resource_ids,
                            self.transport_getter(),
                        )
                        event.outcome = "refreshed"
                    elif event.resource_type == "SyncStatus":
                        await sync.refresh_sync_status(
                            session, conn, self.settings, self.transport_getter()
                        )
                        event.outcome = "sync_status"
                    else:
                        event.outcome = "ignored"
                except Exception as exc:  # noqa: BLE001 - one bad event never blocks the queue
                    event.outcome, event.error = "error", type(exc).__name__
                event.processed_at = utcnow()
            await session.commit()
            return len(events)

    def _sync_due(self, conn: PayerConnection) -> tuple[bool, bool]:
        """(due, full). The first sync is full; later ones follow the configured interval."""
        last = parse_dt(((conn.capability or {}).get("last_sync") or {}).get("at"))
        if last is None:
            return True, True
        interval = self.settings.practice_sync_interval_s
        return bool(interval) and utcnow() - last >= timedelta(seconds=interval), False

    async def run_once(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "webhooks": 0,
            "synced": 0,
            "writes": 0,
            "eligibility_queued": 0,
            "eligibility_run": 0,
        }
        transport = self.transport_getter()
        for conn_id in await self._connections():
            report["webhooks"] += await self.process_webhooks(conn_id)
            async with self.db.sessions() as session:
                conn = await session.get(PayerConnection, conn_id)
                due, full = self._sync_due(conn)
                if due:
                    await sync.sync_practice(session, conn, self.settings, transport, full=full)
                    report["synced"] += 1
                if self.settings.eligibility_autopilot:
                    report["eligibility_queued"] += await eligibility.schedule(session, conn)
                await session.commit()
        report["writes"] = await intents.execute_approved(self.db, self.settings, transport)
        await intents.reconcile(self.db, self.settings, transport)
        if self.settings.eligibility_autopilot:
            report["eligibility_run"] = await eligibility.run_pending(
                self.db, self.settings, transport
            )
        if self.settings.backfill_enabled:
            # After eligibility, so candidate ranking sees this pass's coverage results.
            report["backfill_opened"] = 0
            for conn_id in await self._connections():
                async with self.db.sessions() as session:
                    conn = await session.get(PayerConnection, conn_id)
                    await backfill.expire(session, conn, self.settings)
                    report["backfill_opened"] += await backfill.detect(
                        session, conn, self.settings, transport
                    )
                    await session.commit()
        return report

    async def run_forever(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("practice pass failed")
            await asyncio.sleep(interval_s)
