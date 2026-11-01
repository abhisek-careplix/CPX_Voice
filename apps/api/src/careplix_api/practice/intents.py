"""Write intents: the only path that writes to the practice's EHR (EMR design §2.4).

proposed → approved → sending → accepted → written
                           └→ failed (rejected by NexHealth) / needs_review (outcome unknown)

- NexHealth's 2xx means *accepted*; ``written`` needs an insertion webhook or a read-back showing
  the EHR's ``foreign_id``. Kinds with no insertion signal end at ``accepted`` with confirmation
  ``not_reported``. We never claim more than the source confirms.
- NexHealth never retries; neither do we automatically. An unknown outcome goes to a person.
- ``sending`` is committed before the HTTP call, so a crash can't cause a silent second write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import AuditEvent, PayerConnection, WriteIntent
from careplix_api.phi import PhiBox
from careplix_api.practice.connections import audit, nexhealth_client
from careplix_api.practice.sync import write_status
from careplix_api.settings import Settings

CONFIRM_SLA = timedelta(hours=2)
STUCK_SENDING = timedelta(minutes=10)


@dataclass(frozen=True)
class WriteKind:
    level: str
    target_resource: str
    insertion_event: str | None  # NexHealth event that proves the EHR has the record
    confirm_by_read: bool = False  # otherwise prove it by reading the record back

    @property
    def confirmable(self) -> bool:
        return bool(self.insertion_event or self.confirm_by_read)

    async def send(
        self, client: NexHealthClient, intent: WriteIntent, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def read_back(
        self, client: NexHealthClient, intent: WriteIntent
    ) -> dict[str, Any] | None:
        return None

    def in_ehr(self, record: dict[str, Any]) -> bool:
        return bool(record.get("foreign_id"))


class PatientAlert(WriteKind):
    async def send(self, client, intent, payload):
        body = await client.request(
            "POST",
            f"/patients/{intent.target_source_id}/alerts",
            json={"patient_alert": {"note": payload["note"]}},
        )
        return body.get("data") or {}

    async def read_back(self, client, intent):
        return await client.get(f"/patients/{intent.target_source_id}/alerts/{intent.remote_id}")


class BookAppointment(WriteKind):
    """POST /appointments. ``appointment_insertion.complete`` proves the EHR has it."""

    async def send(self, client, intent, payload):
        body = await client.request(
            "POST",
            "/appointments",
            params={"location_id": payload["location_id"], "notify_patient": "false"},
            json={
                "appt": {k: v for k, v in payload.items() if k != "location_id" and v is not None}
            },
        )
        return (body.get("data") or {}).get("appt") or {}

    async def read_back(self, client, intent):
        return await client.get(f"/appointments/{intent.remote_id}")


class CancelAppointment(WriteKind):
    """PATCH cancelled=true: the one appointment edit every NexHealth EHR supports."""

    async def send(self, client, intent, payload):
        body = await client.request(
            "PATCH", f"/appointments/{intent.target_source_id}", json={"appt": {"cancelled": True}}
        )
        appt = (body.get("data") or {}).get("appt") or {}
        # The appointment already exists in the EHR; its id is the one we changed.
        return {**appt, "id": appt.get("id") or intent.target_source_id, "foreign_id": None}

    async def read_back(self, client, intent):
        return await client.get(f"/appointments/{intent.target_source_id}")

    def in_ehr(self, record):
        return record.get("cancelled") is True and bool(record.get("foreign_id"))


KINDS: dict[str, WriteKind] = {
    # Alerts have no documented insertion event: confirmation stays "not_reported".
    "patient_alert": PatientAlert("L2", "patient", None),
    "appointment_book": BookAppointment("L2", "patient", "appointment_insertion"),
    "appointment_cancel": CancelAppointment("L2", "appointment", None, confirm_by_read=True),
}


def _hash(kind: str, target: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"k": kind, "t": target, "p": payload}, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def propose(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    *,
    kind: str,
    target_source_id: str,
    payload: dict[str, Any],
    reason: str,
    source: str,
    requested_by: str,
) -> WriteIntent:
    spec = KINDS[kind]
    digest = _hash(kind, target_source_id, payload)
    # The same write proposed twice (e.g. by two automation passes) stays one intent.
    duplicate = await session.scalar(
        select(WriteIntent).where(
            WriteIntent.connection_id == conn.id,
            WriteIntent.payload_hash == digest,
            WriteIntent.state.in_(("proposed", "approved", "sending", "accepted", "written")),
        )
    )
    if duplicate is not None:
        return duplicate
    intent = WriteIntent(
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        kind=kind,
        level=spec.level,
        target_resource=spec.target_resource,
        target_source_id=target_source_id,
        payload=PhiBox(settings).seal(payload),
        payload_hash=digest,
        reason=reason[:200],
        source=source,
        requested_by=requested_by,
    )
    session.add(intent)
    await session.flush()
    session.add(
        AuditEvent(
            workspace_id=conn.workspace_id,
            actor=requested_by,
            action="ehr_write.proposed",
            target=intent.id,
            detail={"kind": kind, "level": spec.level, "source": source},
        )
    )
    return intent


async def decide(
    session: AsyncSession, intent: WriteIntent, actor: str, approve: bool
) -> WriteIntent:
    if intent.state != "proposed":
        raise ApiError(409, "invalid_state", f"This write is already {intent.state}.")
    intent.state = "approved" if approve else "rejected"
    intent.approved_by, intent.approved_at = actor, utcnow()
    if not approve:
        intent.resolved_at = utcnow()
    session.add(
        AuditEvent(
            workspace_id=intent.workspace_id,
            actor=actor,
            action="ehr_write.approved" if approve else "ehr_write.rejected",
            target=intent.id,
            detail={"kind": intent.kind},
        )
    )
    return intent


async def execute_approved(
    db: Database,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    only: list[str] | None = None,
) -> int:
    """Send approved intents, one transaction per intent around the external call."""
    async with db.sessions() as session:
        query = select(WriteIntent.id).where(WriteIntent.state == "approved")
        if only is not None:
            query = query.where(WriteIntent.id.in_(only))
        ids = list(await session.scalars(query.order_by(WriteIntent.approved_at)))
    sent = 0
    for intent_id in ids:
        async with db.sessions() as session:
            intent = await session.get(WriteIntent, intent_id)
            conn = await session.get(PayerConnection, intent.connection_id) if intent else None
            if intent is None or conn is None or intent.state != "approved":
                continue
            if write_status(conn) == "red":
                intent.last_error = "EHR sync is offline; held until it is back."
                await session.commit()
                continue
            intent.state, intent.attempts, intent.sent_at = "sending", intent.attempts + 1, utcnow()
            await session.commit()  # claim before calling out (no silent duplicate writes)
            await _send(session, conn, intent, settings, transport)
            await session.commit()
            sent += 1
    return sent


async def _send(
    session: AsyncSession,
    conn: PayerConnection,
    intent: WriteIntent,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    spec = KINDS[intent.kind]
    client = nexhealth_client(conn, settings, transport)
    try:
        data = await spec.send(client, intent, PhiBox(settings).open(intent.payload))
        intent.remote_id = str(data.get("id")) if data.get("id") is not None else None
        intent.foreign_id = data.get("foreign_id")
        intent.state, intent.last_error = "accepted", None
        if not spec.confirmable:
            intent.confirmation = "not_reported"
        elif not (conn.capability or {}).get("writes_confirmable", True):
            # Sandbox demo location: no sync, so the EHR can never confirm. Say so; don't wait.
            intent.confirmation = "no_sync"
        else:
            intent.confirmation = "pending"
        if intent.foreign_id:
            intent.state, intent.confirmation, intent.resolved_at = (
                "written",
                "ehr_written",
                utcnow(),
            )
        ok, status, http_status, error = True, "success", None, None
    except NexHealthError as err:
        # A 4xx is a definite refusal; a timeout or network error leaves the outcome unknown.
        definite = err.http_status is not None and 400 <= err.http_status < 500
        intent.state = "failed" if definite else "needs_review"
        intent.last_error, intent.resolved_at = err.message[:200], utcnow()
        ok, status, http_status, error = False, err.status, err.http_status, err.message
    finally:
        calls = client.calls
        await client.aclose()
    await audit(
        session,
        conn,
        f"nexhealth.write.{intent.kind}",
        "write",
        ok=ok,
        status=status,
        http_status=http_status,
        error=error,
        summary={"intent_id": intent.id, "state": intent.state, "api_calls": calls},
    )
    session.add(
        AuditEvent(
            workspace_id=intent.workspace_id,
            actor="system:ehr_writer",
            action=f"ehr_write.{intent.state}",
            target=intent.id,
            detail={"kind": intent.kind, "remote_id": intent.remote_id},
        )
    )


def apply_insertion(intent: WriteIntent, outcome: str, foreign_id: str | None) -> None:
    """``*_insertion.complete|failed|timed_out`` from NexHealth."""
    if outcome == "complete":
        intent.state, intent.confirmation = "written", "ehr_written"
        intent.foreign_id = foreign_id or intent.foreign_id
    else:
        intent.state, intent.last_error = "needs_review", f"EHR insertion {outcome}"
    intent.resolved_at = utcnow()


async def reconcile(
    db: Database, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Stuck sends go to a person; overdue confirmations are read back once."""
    changed = 0
    now = utcnow()
    async with db.sessions() as session:
        pending = list(
            await session.scalars(
                select(WriteIntent).where(
                    (WriteIntent.state == "sending")
                    | ((WriteIntent.state == "accepted") & (WriteIntent.confirmation == "pending"))
                )
            )
        )
        for intent in pending:
            sent_at = as_utc(intent.sent_at) or now
            if intent.state == "sending" and now - sent_at > STUCK_SENDING:
                intent.state = "needs_review"
                intent.last_error = "Send was interrupted; check the chart before retrying."
                changed += 1
            elif intent.state == "accepted" and now - sent_at > CONFIRM_SLA:
                conn = await session.get(PayerConnection, intent.connection_id)
                client = nexhealth_client(conn, settings, transport)
                try:
                    record = await KINDS[intent.kind].read_back(client, intent)
                except NexHealthError:
                    record = None
                finally:
                    await client.aclose()
                if record and KINDS[intent.kind].in_ehr(record):
                    apply_insertion(intent, "complete", record.get("foreign_id"))
                else:
                    intent.state = "needs_review"
                    intent.last_error = "No EHR confirmation within the expected time."
                changed += 1
        await session.commit()
    return changed


def view(intent: WriteIntent, box: PhiBox | None = None) -> dict[str, Any]:
    out = {
        "id": intent.id,
        "kind": intent.kind,
        "level": intent.level,
        "target_resource": intent.target_resource,
        "target_id": intent.target_source_id,
        "reason": intent.reason,
        "source": intent.source,
        "state": intent.state,
        "confirmation": intent.confirmation,
        "remote_id": intent.remote_id,
        "foreign_id": intent.foreign_id,
        "attempts": intent.attempts,
        "last_error": intent.last_error,
        "created_at": intent.created_at,
        "approved_at": intent.approved_at,
        "sent_at": intent.sent_at,
        "resolved_at": intent.resolved_at,
    }
    if box is not None:
        out["payload"] = box.open(intent.payload)
    return out
