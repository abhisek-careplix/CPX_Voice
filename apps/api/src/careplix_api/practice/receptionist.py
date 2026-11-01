"""FO-3 AI receptionist on the EHR (FRONT_OFFICE_AUTOMATION_PLAN §2A: A1, A5).

The voice agent's front-desk tools. The model proposes; these handlers decide, against the
practice mirror and the live EHR (NexHealth or OpenEMR):

- ``find_patient`` binds the call to one patient by name and date of birth. Until it passes, no
  appointment is read or changed. The answer never says whether a person exists.
- ``find_slots`` asks the EHR for open times and remembers what it offered (``S1``, ``S2``…).
  ``book_appointment`` and ``reschedule_appointment`` take only those ids, so the model can never
  book a time the schedule didn't offer or a patient other than the verified one.
- Every EHR change is a write intent (EMR design §2.4). A patient-requested booking or reschedule
  is L2 and runs at once; a plain cancellation is L3 and, by default, waits for one click in the
  Practice write queue (``receptionist_cancel_mode``). The slot is re-checked with the EHR right
  before booking, and a reschedule releases the old visit only after the new one is accepted.
- A browser test can write only to a synthetic practice (OpenEMR local, NexHealth sandbox).
- Messages for the front desk are sealed; the call log keeps ids and states only.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import events
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import Rejected
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import (
    CallAttempt,
    EhrRecord,
    FrontDeskMessage,
    PayerConnection,
    ToolExecution,
    WriteIntent,
)
from careplix_api.phi import PhiBox
from careplix_api.practice import intents
from careplix_api.practice.backfill import free_operatory
from careplix_api.practice.connections import EHR_KINDS, audit, ehr_client, location_id
from careplix_api.settings import Settings

SCHEDULED = ("booked", "confirmed")
MAX_VERIFY = 2
MAX_WRITES = 3
MAX_MESSAGES = 3
MAX_SLOTS_SHOWN = 6
MAX_SLOTS_KEPT = 40
WRITE_TOOLS = ("book_appointment", "reschedule_appointment", "cancel_appointment")


@dataclass(frozen=True)
class ToolEnv:
    settings: Settings
    db: Database
    transport: httpx.AsyncBaseTransport | None = None


# --- call state ----------------------------------------------------------------------------------


def _state(attempt: CallAttempt) -> dict[str, Any]:
    return dict(attempt.front_desk or {})


def _save(attempt: CallAttempt, state: dict[str, Any]) -> None:
    attempt.front_desk = dict(state)  # reassign so the JSON column is marked changed


async def _connection(session: AsyncSession, attempt: CallAttempt) -> PayerConnection:
    """The practice this call works on: pinned on first use (OpenEMR first, then NexHealth)."""
    state = _state(attempt)
    if state.get("connection_id"):
        conn = await session.get(PayerConnection, state["connection_id"])
        if conn is not None and conn.workspace_id == attempt.workspace_id:
            return conn
    rows = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == attempt.workspace_id,
                PayerConnection.kind.in_(EHR_KINDS),
            )
        )
    )
    if not rows:
        raise Rejected("no_practice", "The practice schedule isn't connected yet.")
    rows.sort(key=lambda c: EHR_KINDS.index(c.kind))
    state["connection_id"] = rows[0].id
    _save(attempt, state)
    return rows[0]


def _verified(attempt: CallAttempt) -> str:
    patient = _state(attempt).get("patient_ref")
    if not patient:
        raise Rejected("not_verified", "Verify the caller with find_patient first.")
    return patient


async def _records(
    session: AsyncSession, conn: PayerConnection, resource: str, **where: Any
) -> list[EhrRecord]:
    query = select(EhrRecord).where(
        EhrRecord.connection_id == conn.id, EhrRecord.resource == resource
    )
    for column, value in where.items():
        query = query.where(getattr(EhrRecord, column) == value)
    return list(await session.scalars(query))


async def practice_zone(session: AsyncSession, conn: PayerConnection) -> ZoneInfo:
    names = [(conn.auth_ref or {}).get("timezone")] if conn.kind == "openemr" else []
    if not names:
        appts = await _records(session, conn, "appointment")
        names = [n for n, _ in Counter(a.attrs.get("timezone") for a in appts).most_common()]
    for name in names:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            continue
    return ZoneInfo("UTC")


def spoken(moment: datetime, tz: ZoneInfo) -> str:
    """'Tuesday, November 3 at 9:30 AM' in practice time: what the agent reads to the caller."""
    local = as_utc(moment).astimezone(tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%A, %B')} {local.day} at {clock}"


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def _dob(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


async def _count(session: AsyncSession, attempt: CallAttempt, tools: tuple[str, ...]) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(ToolExecution)
        .where(
            ToolExecution.attempt_id == attempt.id,
            ToolExecution.tool.in_(tools),
            ToolExecution.state == "Succeeded",
        )
    )


async def _guard_write(
    session: AsyncSession, attempt: CallAttempt, conn: PayerConnection, env: ToolEnv
) -> None:
    if attempt.sandbox and not conn.synthetic:
        raise Rejected(
            "test_call_no_writes",
            "This is a test call, so I can't change a real practice's schedule.",
        )
    if await _count(session, attempt, WRITE_TOOLS) >= MAX_WRITES:
        raise Rejected("write_limit", "That's as many changes as I can make on one call.")


async def _patient_appointment(
    session: AsyncSession, conn: PayerConnection, patient: str, appointment_id: str
) -> EhrRecord:
    [appt] = await _records(session, conn, "appointment", source_id=appointment_id) or [None]
    if (
        appt is None
        or appt.patient_ref != patient
        or appt.status not in SCHEDULED
        or not appt.starts_at
        or as_utc(appt.starts_at) <= utcnow()
    ):
        # Same answer for "not theirs" and "doesn't exist": nothing about other patients leaks.
        raise Rejected("unknown_appointment", "I couldn't find that upcoming appointment.")
    return appt


def _labels(records: list[EhrRecord]) -> dict[str, Any]:
    return {r.source_id: r.attrs.get("name") for r in records}


# --- read tools ----------------------------------------------------------------------------------


async def find_patient(session, attempt, _version, args, _evidence, env: ToolEnv):
    conn = await _connection(session, attempt)
    state = _state(attempt)
    tries = int(state.get("verify_tries", 0))
    if tries >= MAX_VERIFY:
        raise Rejected("verification_limit", "Verification attempts are used up for this call.")
    state["verify_tries"] = tries + 1
    want = (_norm(args["first_name"]), _norm(args["last_name"]), _dob(args["date_of_birth"]))
    box = PhiBox(env.settings)

    def identity(record: EhrRecord) -> tuple[str, str, str | None]:
        pii = box.open(record.pii)
        return (
            _norm(pii.get("first_name")),
            _norm(pii.get("last_name")),
            _dob(pii.get("date_of_birth")),
        )

    matches = [
        p.source_id
        for p in await _records(session, conn, "patient")
        if p.status != "inactive" and want[2] and identity(p) == want
    ]
    # Two charts with the same name and birth date can't be told apart by voice: not verified.
    if len(matches) != 1:
        _save(attempt, state)
        await events.append(session, attempt, "patient.verification_failed", "api", {})
        return {"verified": False}
    state["patient_ref"] = matches[0]
    _save(attempt, state)
    await events.append(session, attempt, "patient.verified", "api", {"patient_ref": matches[0]})
    return {"verified": True}


async def list_appointments(session, attempt, _version, _args, _evidence, env: ToolEnv):
    patient = _verified(attempt)
    conn = await _connection(session, attempt)
    tz = await practice_zone(session, conn)
    providers = _labels(await _records(session, conn, "provider"))
    types = _labels(await _records(session, conn, "appointment_type"))
    upcoming = sorted(
        (
            a
            for a in await _records(session, conn, "appointment", patient_ref=patient)
            if a.status in SCHEDULED and a.starts_at and as_utc(a.starts_at) > utcnow()
        ),
        key=lambda a: as_utc(a.starts_at),
    )
    return {
        "appointments": [
            {
                "appointment_id": a.source_id,
                "when": spoken(a.starts_at, tz),
                "provider": a.attrs.get("provider_name")
                or providers.get(a.attrs.get("provider_id"))
                or "the practice",
                "visit_type": types.get(a.attrs.get("appointment_type_id")) or "Appointment",
            }
            for a in upcoming[:5]
        ]
    }


def _pick_type(types: list[EhrRecord], wanted: str | None) -> tuple[EhrRecord | None, list[str]]:
    names = sorted({t.attrs.get("name") for t in types if t.attrs.get("name")})
    if wanted:
        key = _norm(wanted)
        exact = [t for t in types if _norm(t.attrs.get("name")) == key]
        loose = [t for t in types if key and key in _norm(t.attrs.get("name"))]
        hits = exact or loose
        return (hits[0] if len(hits) == 1 or exact else None), names
    return (types[0] if len(types) == 1 else None), names


async def _available(
    conn: PayerConnection,
    env: ToolEnv,
    start: datetime,
    end: datetime,
    minutes: int,
    providers: list[str],
    tz: ZoneInfo,
) -> list[tuple[datetime, str, str]]:
    client = ehr_client(conn, env.settings, env.transport)
    try:
        if getattr(client, "is_openemr", False):
            return await client.free_slots(start, end, minutes, providers)
        first, last = start.astimezone(tz).date(), end.astimezone(tz).date()
        params: dict[str, Any] = {
            "start_date": first.isoformat(),  # NexHealth reads dates in the location's time
            "days": max(1, min((last - first).days + 1, 30)),
            "lids[]": location_id(conn),
            "pids[]": providers,
            "slot_length": minutes,
            "slot_interval": 15,
        }
        body = await client.request("GET", "/available_slots", params)
        out: list[tuple[datetime, str, str]] = []
        for entry in body.get("data") or []:
            for slot in entry.get("slots") or []:
                at = _parse(slot.get("time"))
                if at is None or not (start <= at <= end):
                    continue
                pid = str(slot.get("provider_id") or entry.get("pid") or "")
                if providers and pid not in providers:
                    continue
                out.append((at, pid, str(slot.get("operatory_id") or "")))
        return sorted(out)
    finally:
        await client.aclose()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _spread(
    slots: list[tuple[datetime, str, str]], tz: ZoneInfo, limit: int
) -> list[tuple[datetime, str, str]]:
    """The earliest times, at most two per day, one per start time: easy to offer by voice."""
    seen_starts: set[datetime] = set()
    per_day: Counter = Counter()
    out = []
    for slot in slots:
        day = slot[0].astimezone(tz).date()
        if slot[0] in seen_starts or per_day[day] >= 2:
            continue
        seen_starts.add(slot[0])
        per_day[day] += 1
        out.append(slot)
        if len(out) >= limit:
            break
    return out


async def find_slots(session, attempt, _version, args, _evidence, env: ToolEnv):
    conn = await _connection(session, attempt)
    state = _state(attempt)
    if int(state.get("searches", 0)) >= 8:
        raise Rejected("search_limit", "I've searched the schedule enough on this call.")
    state["searches"] = int(state.get("searches", 0)) + 1
    tz = await practice_zone(session, conn)
    types = await _records(session, conn, "appointment_type")
    providers = [p for p in await _records(session, conn, "provider") if p.status != "inactive"]

    kind: EhrRecord | None = None
    provider_refs = [p.source_id for p in providers]
    exclude = None
    if args.get("appointment_id"):
        appt = await _patient_appointment(session, conn, _verified(attempt), args["appointment_id"])
        kind = next(
            (t for t in types if t.source_id == appt.attrs.get("appointment_type_id")), None
        )
        if appt.attrs.get("provider_id"):
            provider_refs = [appt.attrs["provider_id"]]
        exclude = appt.source_id
        names = sorted({t.attrs.get("name") for t in types if t.attrs.get("name")})
    else:
        kind, names = _pick_type(types, args.get("visit_type"))
        if kind is None and len(types) > 1:
            _save(attempt, state)
            return {
                "slots": [],
                "visit_types": names,
                "message": "Ask which of these visit types the caller needs.",
            }
    if args.get("provider") and not args.get("appointment_id"):
        key = _norm(args["provider"])
        chosen = [
            p.source_id
            for p in providers
            if key and (key in _norm(p.attrs.get("name")) or _norm(p.attrs.get("name")) in key)
        ]
        if not chosen:
            _save(attempt, state)
            return {
                "slots": [],
                "providers": sorted(p.attrs.get("name") for p in providers if p.attrs.get("name")),
                "message": "That provider wasn't found. Offer these, or any provider.",
            }
        provider_refs = chosen
    if not provider_refs:
        raise Rejected("no_providers", "The practice schedule has no providers to book with.")

    minutes = int((kind.attrs.get("minutes") if kind else None) or 30)
    start = utcnow() + timedelta(minutes=env.settings.receptionist_lead_minutes)
    if args.get("earliest_date"):
        day = date.fromisoformat(args["earliest_date"])
        start = max(start, datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC))
    end = start + timedelta(days=env.settings.receptionist_search_days)
    try:
        found = await _available(conn, env, start, end, minutes, provider_refs, tz)
    except NexHealthError as err:
        await audit(
            session,
            conn,
            "receptionist.find_slots",
            "receptionist",
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
        )
        raise Rejected("schedule_unavailable", "I can't reach the schedule right now.") from None
    part = args.get("part_of_day") or "any"
    if part != "any":
        found = [s for s in found if (s[0].astimezone(tz).hour < 12) == (part == "morning")]
    chosen_slots = _spread(found, tz, MAX_SLOTS_SHOWN)
    names_by_ref = _labels(providers)
    offered = dict(state.get("slots") or {})
    counter = int(state.get("slot_counter", 0))
    shown = []
    for at, pid, operatory in chosen_slots:
        counter += 1
        slot_id = f"S{counter}"
        offered[slot_id] = {
            "start": at.isoformat(),
            "minutes": minutes,
            "provider_ref": pid,
            "operatory_ref": operatory or None,
            "type_ref": kind.source_id if kind else None,
            "exclude": exclude,
        }
        shown.append(
            {"slot_id": slot_id, "when": spoken(at, tz), "provider": names_by_ref.get(pid)}
        )
    # Keep only the most recent offers: old ids expire rather than piling up.
    state["slots"] = dict(list(offered.items())[-MAX_SLOTS_KEPT:])
    state["slot_counter"] = counter
    _save(attempt, state)
    await audit(
        session,
        conn,
        "receptionist.find_slots",
        "receptionist",
        ok=True,
        status="success",
        summary={"found": len(found), "offered": len(shown)},
    )
    result: dict[str, Any] = {
        "visit_type": kind.attrs.get("name") if kind else None,
        "minutes": minutes,
        "slots": shown,
    }
    if not shown:
        result["message"] = "No open times in that range. Offer a different day or a message."
    return result


# --- writes --------------------------------------------------------------------------------------


async def _run(
    session: AsyncSession, env: ToolEnv, conn: PayerConnection, intent: WriteIntent
) -> WriteIntent:
    """Approve (policy), commit, send through the EHR writer and reload the outcome."""
    await intents.decide(session, intent, "policy:receptionist-L2", approve=True)
    await session.commit()
    await intents.execute_approved(env.db, env.settings, env.transport, only=[intent.id])
    await session.refresh(intent)
    return intent


async def _book(
    session: AsyncSession,
    attempt: CallAttempt,
    env: ToolEnv,
    conn: PayerConnection,
    patient: str,
    slot_id: str,
    reason: str,
) -> tuple[str, WriteIntent | None, dict[str, Any]]:
    state = _state(attempt)
    slot = (state.get("slots") or {}).get(slot_id)
    if slot is None:
        raise Rejected("unknown_slot", "Use a time from find_slots on this call.")
    start = datetime.fromisoformat(slot["start"])
    if start <= utcnow():
        raise Rejected("slot_in_past", "That time has passed. Search again.")
    tz = await practice_zone(session, conn)
    probe = SimpleNamespace(
        starts_at=start,
        minutes=slot["minutes"],
        timezone=str(tz),
        provider_ref=slot["provider_ref"],
        operatory_ref=slot.get("operatory_ref"),
        source_appointment_ref=slot.get("exclude"),
    )
    client = ehr_client(conn, env.settings, env.transport)
    try:
        operatory = await free_operatory(client, conn, probe)
    except NexHealthError:
        raise Rejected("schedule_unavailable", "I can't reach the schedule right now.") from None
    finally:
        await client.aclose()
    if operatory is None:
        state["slots"] = {k: v for k, v in state["slots"].items() if k != slot_id}
        _save(attempt, state)
        return "slot_taken", None, slot
    intent = await intents.propose(
        session,
        conn,
        env.settings,
        kind="appointment_book",
        target_source_id=patient,
        payload={
            "location_id": location_id(conn),
            "patient_id": int(patient),
            "provider_id": int(slot["provider_ref"]) if slot["provider_ref"] else None,
            "operatory_id": int(operatory) if operatory else None,
            "appointment_type_id": int(slot["type_ref"]) if slot.get("type_ref") else None,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=slot["minutes"])).isoformat(),
            "note": "Booked by the CarePlix receptionist at the patient's request",
        },
        reason=reason,
        source="receptionist",
        requested_by=f"call:{attempt.id}"[:36],
    )
    if intent.state != "proposed":
        # The identical booking was already made on this call (a retried tool call).
        return _outcome(intent), intent, slot
    intent = await _run(session, env, conn, intent)
    outcome = _outcome(intent)
    if outcome == "booked":
        await _mirror_booking(session, conn, intent, patient, slot, start)
        state = _state(attempt)
        state["slots"] = {k: v for k, v in (state.get("slots") or {}).items() if k != slot_id}
        _save(attempt, state)
    return outcome, intent, slot


def _outcome(intent: WriteIntent) -> str:
    if intent.state in ("accepted", "written"):
        return "booked"
    if intent.state in ("approved", "proposed"):
        return "pending"  # e.g. held while EHR sync is offline: the front desk will see it
    if intent.state == "failed":
        return "failed"
    return "needs_staff"


async def _mirror_booking(
    session: AsyncSession,
    conn: PayerConnection,
    intent: WriteIntent,
    patient: str,
    slot: dict[str, Any],
    start: datetime,
) -> None:
    """Show the new visit in the mirror now; the next sync replaces it with the EHR's copy."""
    if not intent.remote_id:
        return
    [existing] = await _records(session, conn, "appointment", source_id=intent.remote_id) or [None]
    if existing is not None:
        return
    tz = await practice_zone(session, conn)
    providers = _labels(await _records(session, conn, "provider"))
    session.add(
        EhrRecord(
            workspace_id=conn.workspace_id,
            connection_id=conn.id,
            resource="appointment",
            source_id=intent.remote_id,
            patient_ref=patient,
            starts_at=start,
            status="booked",
            attrs={
                "provider_id": slot["provider_ref"],
                "provider_name": providers.get(slot["provider_ref"]),
                "appointment_type_id": slot.get("type_ref"),
                "operatory_id": slot.get("operatory_ref"),
                "location_id": location_id(conn),
                "end_time": (start + timedelta(minutes=slot["minutes"])).isoformat(),
                "timezone": str(tz),
                "sooner_if_possible": False,
                "foreign_id": intent.foreign_id,
                "booked_by": "receptionist",
            },
            synced_at=utcnow(),
        )
    )


async def _cancel(
    session: AsyncSession,
    attempt: CallAttempt,
    env: ToolEnv,
    conn: PayerConnection,
    appt: EhrRecord,
    reason: str,
    *,
    run: bool,
) -> WriteIntent:
    intent = await intents.propose(
        session,
        conn,
        env.settings,
        kind="appointment_cancel",
        target_source_id=appt.source_id,
        payload={"cancelled": True, "patient_id": int(appt.patient_ref)},
        reason=reason,
        source="receptionist",
        requested_by=f"call:{attempt.id}"[:36],
    )
    if run and intent.state == "proposed":
        intent = await _run(session, env, conn, intent)
    if intent.state in ("accepted", "written"):
        appt.status = "cancelled"  # backfill sees the freed slot on its next pass
    return intent


async def book_appointment(session, attempt, _version, args, _evidence, env: ToolEnv):
    patient = _verified(attempt)
    conn = await _connection(session, attempt)
    await _guard_write(session, attempt, conn, env)
    outcome, intent, slot = await _book(
        session, attempt, env, conn, patient, args["slot_id"], "Receptionist: patient booked"
    )
    tz = await practice_zone(session, conn)
    await events.append(
        session,
        attempt,
        "appointment.booking",
        "api",
        {"outcome": outcome, "intent_id": intent.id if intent else None},
    )
    return _write_result(outcome, datetime.fromisoformat(slot["start"]), tz)


async def reschedule_appointment(session, attempt, _version, args, _evidence, env: ToolEnv):
    patient = _verified(attempt)
    conn = await _connection(session, attempt)
    await _guard_write(session, attempt, conn, env)
    old = await _patient_appointment(session, conn, patient, args["appointment_id"])
    old_id = old.source_id
    outcome, book, slot = await _book(
        session, attempt, env, conn, patient, args["slot_id"], "Receptionist: patient rescheduled"
    )
    tz = await practice_zone(session, conn)
    when = datetime.fromisoformat(slot["start"])
    payload: dict[str, Any] = {"outcome": outcome, "intent_id": book.id if book else None}
    if outcome != "booked":
        await events.append(session, attempt, "appointment.reschedule", "api", payload)
        return _write_result(outcome, when, tz)
    # The patient agreed to the move on the call: release the old visit (L2, patient-requested).
    old = await _patient_appointment(session, conn, patient, old_id)
    release = await _cancel(
        session, attempt, env, conn, old, "Receptionist: patient moved this visit", run=True
    )
    payload["release_intent_id"] = release.id
    if release.state not in ("accepted", "written"):
        payload["outcome"] = "needs_staff"
        await events.append(session, attempt, "appointment.reschedule", "api", payload)
        return {
            "state": "needs_staff",
            "when": spoken(when, tz),
            "message": "The new time is booked, but the old appointment still needs the front "
            "desk to cancel it. Tell the caller the front desk will take care of it.",
        }
    await events.append(session, attempt, "appointment.reschedule", "api", payload)
    return _write_result("rescheduled", when, tz)


async def cancel_appointment(session, attempt, _version, args, _evidence, env: ToolEnv):
    patient = _verified(attempt)
    conn = await _connection(session, attempt)
    await _guard_write(session, attempt, conn, env)
    appt = await _patient_appointment(session, conn, patient, args["appointment_id"])
    auto = env.settings.receptionist_cancel_mode == "auto"
    reason = f"Receptionist: patient cancelled ({args['reason'].replace('_', ' ')})"
    intent = await _cancel(session, attempt, env, conn, appt, reason, run=auto)
    tz = await practice_zone(session, conn)
    if intent.state == "proposed":
        outcome = "requested"
    elif intent.state in ("accepted", "written"):
        outcome = "cancelled"
    else:
        outcome = _outcome(intent)
    await events.append(
        session, attempt, "appointment.cancel", "api", {"outcome": outcome, "intent_id": intent.id}
    )
    result = _write_result(outcome, appt.starts_at, tz)
    if outcome == "requested":
        result["message"] = (
            "The cancellation request is with the front desk, who will confirm it. "
            "Tell the caller it is not cancelled until they hear back."
        )
    return result


def _write_result(outcome: str, when: datetime, tz: ZoneInfo) -> dict[str, Any]:
    messages = {
        "slot_taken": "That time was just taken. Apologise and offer another time.",
        "pending": "The request is saved and the front desk will confirm it. It is not booked yet.",
        "failed": "The schedule refused that change. Offer another time or a message.",
        "needs_staff": "The outcome isn't confirmed. Tell the caller the front desk will "
        "check and call back.",
    }
    out: dict[str, Any] = {"state": outcome, "when": spoken(when, tz)}
    if outcome in messages:
        out["message"] = messages[outcome]
    return out


async def leave_message(session, attempt, _version, args, _evidence, env: ToolEnv):
    taken = await session.scalar(
        select(func.count())
        .select_from(FrontDeskMessage)
        .where(FrontDeskMessage.attempt_id == attempt.id)
    )
    if taken >= MAX_MESSAGES:
        raise Rejected("message_limit", "I've already taken the messages for this call.")
    state = _state(attempt)
    try:
        conn = await _connection(session, attempt)
    except Rejected:
        conn = None  # messages still reach the front desk without a connected EHR
    patient = state.get("patient_ref")
    message = FrontDeskMessage(
        workspace_id=attempt.workspace_id,
        connection_id=conn.id if conn else None,
        attempt_id=attempt.id,
        patient_ref=patient,
        topic=args["topic"],
        callback_requested=bool(args["callback_requested"]),
        body=PhiBox(env.settings).seal(
            {
                "message": args["message"],
                "caller_name": None if patient else args.get("caller_name"),
                "caller_phone": attempt.caller_e164,
            }
        ),
    )
    session.add(message)
    await session.flush()
    await events.append(
        session, attempt, "message.left", "api", {"message_id": message.id, "topic": message.topic}
    )
    return {"state": "recorded", "reference": message.id[:8].upper()}


HANDLERS = {
    "find_patient": find_patient,
    "list_appointments": list_appointments,
    "find_slots": find_slots,
    "book_appointment": book_appointment,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment": cancel_appointment,
    "leave_message": leave_message,
}


def message_view(message: FrontDeskMessage, box: PhiBox, labels: dict[str, str]) -> dict:
    body = box.open(message.body)
    return {
        "id": message.id,
        "reference": message.id[:8].upper(),
        "topic": message.topic,
        "callback_requested": message.callback_requested,
        "verified": message.patient_ref is not None,
        "patient_id": message.patient_ref,
        "patient": labels.get(message.patient_ref or "") if message.patient_ref else None,
        "caller_name": body.get("caller_name"),
        "caller_phone": body.get("caller_phone"),
        "message": body.get("message"),
        "attempt_id": message.attempt_id,
        "state": message.state,
        "created_at": message.created_at,
        "handled_at": message.handled_at,
    }
