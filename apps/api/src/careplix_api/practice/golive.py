"""Go-live checklist for a practice: what must be true before automations touch its EHR.

A production NexHealth practice (a real clinic) runs no automation (receptionist writes,
cancellation backfill, eligibility autopilot) until every item passes:

1. Verified: the key reaches the subdomain and the location exists.
2. Appointment insertion is on (``insert_appt_client`` on the location): without it NexHealth
   keeps API bookings to itself and they never reach the EHR.
3. Read sync and write sync are green (``GET /sync_status``).
4. Webhooks are registered and healthy: they are how writes are confirmed.
5. The first full sync finished.
6. Test writes: book → confirm → cancel on the practice's own test patient, each confirmed by
   the EHR (insertion / ``appointment_updated`` webhooks). A step the EHR can't take (e.g.
   cancel on OrthoTrac) becomes a staff task and is shown as such.

Passing is recorded once (``auth_ref.golive.passed_at``). Sandbox practices and the local OpenEMR
clinic show the checklist but don't need it: they hold synthetic data only.
Manual Studio actions (write queue approvals, Check now) are never blocked: a person decided.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.connect.catalog import nexhealth_profile_for
from careplix_api.connect.connectors import connector_for
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import AuditEvent, EhrRecord, PayerConnection, SyncCursor, WriteIntent
from careplix_api.phi import PhiBox
from careplix_api.practice import intents
from careplix_api.practice.connections import EHR, location_id, nexhealth_env
from careplix_api.settings import Settings

STEPS = ("book", "confirm", "cancel")
STEP_KIND = {
    "book": "appointment_book",
    "confirm": "appointment_confirm",
    "cancel": "appointment_cancel",
}
SLOT_SEARCH = timedelta(days=14)
SLOT_LEAD = timedelta(hours=24)  # never in today's schedule, where the front desk works
TEST_WORD = "test"


def required(conn: PayerConnection) -> bool:
    """Real clinics only: a production NexHealth practice."""
    return conn.kind == EHR and nexhealth_env(conn) == "production"


def _state(conn: PayerConnection) -> dict[str, Any]:
    return dict((conn.auth_ref or {}).get("golive") or {})


def _save(conn: PayerConnection, state: dict[str, Any]) -> None:
    # A deep copy: the JSON column only notices a new value, never a mutated nested one (and an
    # autoflush may already hold this very dict as the committed value).
    conn.auth_ref = {**(conn.auth_ref or {}), "golive": copy.deepcopy(state)}


def automations_allowed(conn: PayerConnection) -> bool:
    """Whether automations may write to (or act for) this practice."""
    return not required(conn) or bool(_state(conn).get("passed_at"))


BLOCKED_MESSAGE = (
    "This practice hasn't passed its go-live checklist yet, so CarePlix doesn't change its "
    "schedule on its own. Offer to take a message for the front desk."
)


def _item(key: str, label: str, ok: bool | None, detail: str) -> dict[str, Any]:
    # ok: True passed, False failing, None waiting (in progress or not applicable yet)
    return {"key": key, "label": label, "ok": ok, "detail": detail}


async def _first_sync_done(session: AsyncSession, conn: PayerConnection) -> bool:
    cursor = await session.scalar(
        select(SyncCursor).where(
            SyncCursor.connection_id == conn.id, SyncCursor.resource == "appointment"
        )
    )
    return bool(cursor and cursor.last_full_at and not cursor.last_error)


def _syncs(conn: PayerConnection) -> list[dict[str, Any]]:
    return (conn.capability or {}).get("sync") or []


def _sync_item(conn: PayerConnection, field: str, label: str) -> dict[str, Any]:
    syncs = _syncs(conn)
    key = f"{field}_sync"
    if not syncs:
        if nexhealth_env(conn) != "production":
            # Demo practices: nothing to sync, so nothing to fail. A real clinic must have one.
            return _item(key, label, True, "Not applicable: the sandbox has no EHR behind it.")
        detail = (
            "NexHealth reports no sync for this location yet (is the Synchronizer or "
            "Chrome extension set up?)."
        )
        return _item(key, label, False, detail)
    red = [s.get("emr") or "EHR" for s in syncs if s.get(f"{field}_status") != "green"]
    if red:
        return _item(key, label, False, f"{', '.join(red)} {field} sync isn't green.")
    return _item(key, label, True, "Green.")


def _test_item(conn: PayerConnection) -> dict[str, Any]:
    test = _state(conn).get("test") or {}
    label = "Test writes: book, confirm, cancel on the test patient"
    if not test:
        return _item("test_writes", label, False, "Not run yet.")
    step = test.get("step")
    if step == "done":
        notes = "; ".join(test.get("notes") or [])
        return _item(
            "test_writes", label, True, "All confirmed by the EHR." + (f" {notes}" if notes else "")
        )
    if step == "failed":
        return _item("test_writes", label, False, test.get("error") or "A test write failed.")
    return _item("test_writes", label, None, f"Waiting for the EHR to confirm: {step}.")


async def checklist(session: AsyncSession, conn: PayerConnection) -> dict[str, Any]:
    cap = conn.capability or {}
    ref = conn.auth_ref or {}
    items: list[dict[str, Any]] = []
    location = cap.get("location")
    items.append(
        _item(
            "verified",
            "Connected: NexHealth reaches the practice's location",
            bool(conn.verified_at and location),
            (f"{location.get('name')} ({ref.get('subdomain')})" if location else conn.last_error)
            or "Not verified yet.",
        )
    )
    if conn.kind == EHR:
        detected = next(
            (p for p in map(nexhealth_profile_for, [s.get("emr") for s in _syncs(conn)]) if p),
            None,
        )
        expected = ref.get("expected_profile")
        if detected and expected and detected.id != expected:
            items.append(
                _item(
                    "ehr_match",
                    "The EHR is the one chosen",
                    False,
                    f"NexHealth reports {detected.name}. Check the subdomain and location, or "
                    "add the practice again with the right EHR.",
                )
            )
        insert = cap.get("insert_appt_client")
        items.append(
            _item(
                "insert_appt_client",
                "Appointment insertion is on in NexHealth",
                insert is True if insert is not None else None,
                "On."
                if insert
                else "Turn on “Insert appointments” for this location in "
                "NexHealth (Settings → Online booking), then sync."
                if insert is False
                else "Read on the first sync.",
            )
        )
        items.append(_sync_item(conn, "read", "Read sync is green"))
        items.append(_sync_item(conn, "write", "Write sync is green"))
        health = cap.get("webhooks") or {}
        registered = bool((ref.get("webhook") or {}).get("endpoint_id"))
        items.append(
            _item(
                "webhooks",
                "Webhooks are registered and delivering",
                registered and health.get("status") in ("ok", "retrying"),
                health.get("message") or ("Registered." if registered else "Not registered."),
            )
        )
    done = await _first_sync_done(session, conn)
    items.append(
        _item(
            "first_sync",
            "First full sync finished",
            done or None,
            "Done." if done else "Running in the background (large practices take a while).",
        )
    )
    items.append(_test_item(conn))
    state = _state(conn)
    return {
        "required": required(conn),
        "passed": all(i["ok"] is True for i in items),
        "passed_at": state.get("passed_at"),
        "automations_allowed": automations_allowed(conn),
        "items": items,
        "test": state.get("test"),
    }


async def _test_patient(
    session: AsyncSession, conn: PayerConnection, settings: Settings, patient_id: str
) -> EhrRecord:
    patient = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == patient_id,
        )
    )
    if patient is None or patient.status == "deleted":
        raise ApiError(
            404, "not_found", "No patient with that id in the practice's mirror. Sync, then retry."
        )
    pii = PhiBox(settings).open(patient.pii)
    names = f"{pii.get('first_name') or ''} {pii.get('last_name') or ''}".lower()
    if TEST_WORD not in names:
        raise ApiError(
            422,
            "not_a_test_patient",
            "Use the practice's test patient: a chart whose first or last name includes “Test”.",
        )
    return patient


async def _first_slot(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[datetime, str, str, int, str | None]:
    from careplix_api.practice.receptionist import practice_zone

    providers = [
        p.source_id
        for p in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id, EhrRecord.resource == "provider"
            )
        )
        if p.status != "inactive"
    ]
    if not providers:
        raise ApiError(409, "no_providers", "The practice has no providers to book with.")
    kind = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id, EhrRecord.resource == "appointment_type"
        )
    )
    minutes = int(((kind.attrs or {}).get("minutes") if kind else None) or 15)
    tz = await practice_zone(session, conn)
    start = utcnow() + SLOT_LEAD
    connector = connector_for(conn, settings, transport)
    try:
        slots = await connector.available_slots(
            start, start + SLOT_SEARCH, minutes, providers, tz, kind.source_id if kind else None
        )
        for at, pid, operatory in slots:
            probe = SimpleNamespace(
                starts_at=at,
                minutes=minutes,
                timezone=str(tz),
                provider_ref=pid,
                operatory_ref=operatory or None,
                source_appointment_ref=None,
            )
            free = await connector.slot_free(probe)
            if free is not None:
                return at, pid, free, minutes, kind.source_id if kind else None
    except NexHealthError as err:
        raise ApiError(502, "schedule_unavailable", f"NexHealth: {err.message}") from None
    finally:
        await connector.aclose()
    raise ApiError(409, "no_slots", "No open time in the next two weeks to book the test visit.")


async def start_test(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
    *,
    patient_id: str,
    actor: str,
) -> dict[str, Any]:
    """Book a visit for the test patient; the worker confirms and then cancels it."""
    state = _state(conn)
    running = (state.get("test") or {}).get("step") in STEPS
    if running:
        raise ApiError(409, "test_running", "A test is already running for this practice.")
    if required(conn):
        report = await checklist(session, conn)
        blocking = [
            i["label"]
            for i in report["items"]
            if i["key"] not in ("test_writes", "first_sync") and i["ok"] is not True
        ]
        if blocking:
            raise ApiError(409, "not_ready", "Finish these first: " + "; ".join(blocking) + ".")
    elif not conn.verified_at:
        raise ApiError(409, "not_ready", "Verify the practice first.")
    patient = await _test_patient(session, conn, settings, patient_id)
    at, provider, operatory, minutes, type_ref = await _first_slot(
        session, conn, settings, transport
    )
    intent = await intents.propose(
        session,
        conn,
        settings,
        kind="appointment_book",
        target_source_id=patient.source_id,
        payload={
            "location_id": location_id(conn),
            "patient_id": int(patient.source_id),
            "provider_id": int(provider) if provider else None,
            "operatory_id": int(operatory) if operatory else None,
            "appointment_type_id": int(type_ref) if type_ref else None,
            "start_time": at.isoformat(),
            "end_time": (at + timedelta(minutes=minutes)).isoformat(),
            "note": "CarePlix go-live test visit: it will be cancelled automatically",
        },
        reason="Go-live test: book",
        source="golive",
        requested_by=actor,
    )
    await intents.decide(session, intent, actor, approve=True)
    state["test"] = {
        "patient_id": patient.source_id,
        "started_at": utcnow().isoformat(),
        "started_by": actor,
        "step": "book",
        "slot": at.isoformat(),
        "intents": {"book": intent.id},
        "notes": [],
    }
    state.pop("passed_at", None)
    _save(conn, state)
    session.add(
        AuditEvent(
            workspace_id=conn.workspace_id,
            actor=actor,
            action="practice.golive_test_started",
            target=conn.id,
            detail={"intent_id": intent.id},
        )
    )
    return state["test"]


def _settled(intent: WriteIntent) -> str:
    """confirmed | waiting | staff | failed: where one test write stands."""
    if intent.state == "written":
        return "confirmed"
    if intent.state == "accepted" and intent.confirmation in ("no_sync", "not_reported"):
        return "confirmed"  # sandbox: no EHR behind it, NexHealth's acceptance is all there is
    if intent.state in ("approved", "sending", "accepted", "proposed"):
        return "waiting"
    if intent.state == "manual":
        return "staff"
    return "failed"


async def advance(session: AsyncSession, conn: PayerConnection, settings: Settings) -> str | None:
    """Move a running test one step when the EHR has confirmed the last one. Returns the step."""
    state = _state(conn)
    test = dict(state.get("test") or {})
    step = test.get("step")
    if step not in STEPS:
        return step
    ids = dict(test.get("intents") or {})
    intent = await session.get(WriteIntent, ids.get(step) or "-")
    if intent is None:
        test.update(step="failed", error=f"The {step} write is missing.")
        state["test"] = test
        _save(conn, state)
        return "failed"
    outcome = _settled(intent)
    if outcome == "waiting":
        return step
    notes = list(test.get("notes") or [])
    if outcome == "failed":
        test.update(
            step="failed",
            error=f"The test {step} didn't go through: {intent.last_error or intent.state}.",
        )
        state["test"] = test
        _save(conn, state)
        return "failed"
    if outcome == "staff":
        notes.append(f"{step.capitalize()} isn't supported on this EHR: staff do it in the EHR.")
        if step == "book":
            test.update(step="failed", error="This EHR doesn't take bookings from CarePlix.")
            state["test"] = test
            _save(conn, state)
            return "failed"
    if step == "book":
        test["appointment_id"] = intent.remote_id
    nxt = STEPS[STEPS.index(step) + 1] if step != STEPS[-1] else None
    if nxt is None:
        test.update(step="done", finished_at=utcnow().isoformat(), notes=notes)
        state["test"] = test
        report_ok = True
        if required(conn):
            _save(conn, state)
            report_ok = (await checklist(session, conn))["passed"]
        if report_ok:
            state["passed_at"] = utcnow().isoformat()
            session.add(
                AuditEvent(
                    workspace_id=conn.workspace_id,
                    actor="system:golive",
                    action="practice.golive_passed",
                    target=conn.id,
                    detail={"notes": notes},
                )
            )
        _save(conn, state)
        return "done"
    appointment = test.get("appointment_id")
    payload = {"confirmed": True} if nxt == "confirm" else {"cancelled": True}
    proposed = await intents.propose(
        session,
        conn,
        settings,
        kind=STEP_KIND[nxt],
        target_source_id=str(appointment),
        payload={**payload, "patient_id": int(test["patient_id"])},
        reason=f"Go-live test: {nxt}",
        source="golive",
        requested_by=test.get("started_by") or "system:golive",
    )
    if proposed.state == "proposed":
        await intents.decide(session, proposed, "system:golive", approve=True)
    ids[nxt] = proposed.id
    test.update(step=nxt, intents=ids, notes=notes)
    state["test"] = test
    _save(conn, state)
    return nxt


async def refresh(session: AsyncSession, conn: PayerConnection, settings: Settings) -> None:
    """Worker hook: advance a running test; record a pass once every item is green."""
    state = _state(conn)
    step = (state.get("test") or {}).get("step")
    if step in STEPS:
        step = await advance(session, conn, settings)
        state = _state(conn)
    finished = step == "done" and not state.get("passed_at") and required(conn)
    if finished and (await checklist(session, conn))["passed"]:
        state["passed_at"] = utcnow().isoformat()
        _save(conn, state)
