"""Practice worker: webhooks → re-reads, periodic sync, EHR writes, eligibility autopilot."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from careplix_api.connect.connectors import connector_for
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import EhrRecord, PayerConnection, WebhookEvent, WriteIntent
from careplix_api.practice import backfill, eligibility, intents, sync, webhooks
from careplix_api.practice.connections import EHR_KINDS
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
                        PayerConnection.kind.in_(EHR_KINDS), PayerConnection.enabled.is_(True)
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
                        await self._insertion(session, conn, event, outcome or "complete")
                        event.outcome = f"insertion_{outcome or 'complete'}"
                    elif base == "appointment_updated":
                        settled = await self._appointment_updated(
                            session, conn, event, outcome or "complete"
                        )
                        if outcome:
                            event.outcome = f"update_{outcome}"[:30]
                        else:
                            event.outcome = "update_confirmed" if settled else "refreshed"
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

    async def _insertion(
        self, session, conn: PayerConnection, event: WebhookEvent, outcome: str
    ) -> None:
        """``*_insertion.complete|failed|timed_out|incomplete`` for a record we created."""
        ids = event.resource_ids or []
        matched = list(
            await session.scalars(
                select(WriteIntent).where(
                    WriteIntent.connection_id == conn.id,
                    WriteIntent.remote_id.in_(ids or ["-"]),
                )
            )
        )
        foreign: dict[str, Any] = {}
        if event.resource_type == "Appointment" and ids:
            # Re-read the appointment: the mirror and the intent then carry the EHR's foreign_id.
            try:
                await sync.refresh_records(
                    session, conn, self.settings, "Appointment", ids, self.transport_getter()
                )
            except NexHealthError:
                log.warning("could not re-read inserted appointment")
            for rec in await session.scalars(
                select(EhrRecord).where(
                    EhrRecord.connection_id == conn.id,
                    EhrRecord.resource == "appointment",
                    EhrRecord.source_id.in_(ids),
                )
            ):
                foreign[rec.source_id] = rec.attrs.get("foreign_id")
        for intent in matched:
            intents.apply_insertion(intent, outcome, foreign.get(intent.remote_id or ""))
            if outcome == "complete" and intent.kind == "appointment_book":
                # A new patient reaches the EHR with their first booking: that confirms them too.
                for created in await session.scalars(
                    select(WriteIntent).where(
                        WriteIntent.connection_id == conn.id,
                        WriteIntent.kind == "patient_create",
                        WriteIntent.remote_id == intent.target_source_id,
                        WriteIntent.state == "accepted",
                    )
                ):
                    intents.apply_insertion(created, "complete", None)

    async def _appointment_updated(
        self, session, conn: PayerConnection, event: WebhookEvent, outcome: str
    ) -> int:
        """Confirm (or fail) PATCH intents on the appointment, then refresh the mirror.

        Success confirms only an intent whose field is in ``previous_attributes``; a failure
        without ``previous_attributes`` fails every unconfirmed edit of that appointment.
        """
        ids = event.resource_ids or []
        changed = (event.detail or {}).get("changed") or []
        received = as_utc(event.received_at) or utcnow()
        settled = 0
        for intent in await session.scalars(
            select(WriteIntent).where(
                WriteIntent.connection_id == conn.id,
                WriteIntent.target_source_id.in_(ids or ["-"]),
                WriteIntent.kind.in_(intents.UPDATE_KINDS),
                WriteIntent.state == "accepted",
            )
        ):
            if intent.sent_at is not None and as_utc(intent.sent_at) > received:
                continue  # an earlier change's event: not this write's answer
            if outcome == "complete":
                if intents.update_matches(intent, changed):
                    intents.apply_update(intent, "complete")
                    settled += 1
            elif not changed or intents.update_matches(intent, changed):
                intents.apply_update(intent, outcome)
                settled += 1
        if ids:
            await sync.refresh_records(
                session, conn, self.settings, "Appointment", ids, self.transport_getter()
            )
        return settled

    async def check_webhooks(self, conn_id: str) -> None:
        async with self.db.sessions() as session:
            conn = await session.get(PayerConnection, conn_id)
            if conn is None or conn.kind != "nexhealth" or not webhooks.health_due(conn):
                return
            try:
                await webhooks.check_health(session, conn, self.settings, self.transport_getter())
            except NexHealthError as err:
                caps = dict(conn.capability or {})
                caps["webhooks"] = {
                    **(caps.get("webhooks") or {}),
                    "checked_at": utcnow().isoformat(),
                    "check_error": err.message[:120],
                }
                conn.capability = caps
            await session.commit()

    def _sync_due(self, conn: PayerConnection) -> tuple[bool, bool]:
        """(due, full). The first sync is full; later ones follow the configured interval."""
        last = parse_dt(((conn.capability or {}).get("last_sync") or {}).get("at"))
        if last is None:
            return True, True
        interval = connector_for(conn, self.settings).sync_interval_s()
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
            await self.check_webhooks(conn_id)
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
