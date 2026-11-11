"""FO-3 AI receptionist on the EHR (FRONT_OFFICE_AUTOMATION_PLAN §2A: A1, A5).

The voice agent's front-desk tools. The model proposes; these handlers decide, against the
practice mirror and the live EHR (NexHealth or OpenEMR):

- ``find_patient`` binds the call to one patient by name and date of birth. Until it passes, no
  appointment is read or changed. The answer never says whether a person exists. When the mirror
  has no match, the EHR is searched live (this location only, exact name and birth date).
- ``find_slots`` asks the EHR for open times and remembers what it offered (``S1``, ``S2``…).
  ``book_appointment``, ``book_new_patient`` and ``reschedule_appointment`` take only those ids,
  so the model can never book a time the schedule didn't offer or a patient other than the
  verified one. No times are offered while the EHR's sync is down.
- ``book_new_patient`` registers a caller ``find_patient`` didn't find and books them, where the
  EHR takes new patients from the API (per the CarePlix Connect catalog).
- Every EHR change is a write intent (EMR design §2.4). A patient-requested booking or reschedule
  is L2 and runs at once; a plain cancellation is L3 and, by default, waits for one click in the
  Practice write queue (``receptionist_cancel_mode``). The slot is re-checked with the EHR right
  before booking. Where the EHR takes time changes, a reschedule moves the visit in place;
  elsewhere it books the new time and releases the old visit only after that is accepted.
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
from careplix_api.connect.connectors import connector_for
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
from careplix_api.practice import intents, records
from careplix_api.practice.connections import EHR_KINDS, audit, location_id
from careplix_api.settings import Settings

SCHEDULED = ("booked", "confirmed")
MAX_VERIFY = 2
MAX_WRITES = 3
MAX_MESSAGES = 3
MAX_SLOTS_SHOWN = 6
MAX_SLOTS_KEPT = 40
WRITE_TOOLS = (
    "book_appointment",
    "book_new_patient",
    "reschedule_appointment",
    "cancel_appointment",
)


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
    """The practice's time zone: OpenEMR's configured one, NexHealth's location ``tz``.

    Only when the source gives none is it inferred from the appointments' own time zones.
    """
    if conn.kind == "openemr":
        names = [(conn.auth_ref or {}).get("timezone")]
    else:
        names = [loc.attrs.get("tz") for loc in await _records(session, conn, "location")]
        names.append((conn.capability or {}).get("location_tz"))
    names = [n for n in names if n]
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


def _identity(pii: dict[str, Any]) -> tuple[str, str, str | None]:
    return _norm(pii.get("first_name")), _norm(pii.get("last_name")), _dob(pii.get("date_of_birth"))


async def _live_lookup(
    session: AsyncSession,
    conn: PayerConnection,
    env: ToolEnv,
    args: dict[str, Any],
    want: tuple[str, str, str | None],
    box: PhiBox,
) -> list[str]:
    """Search the EHR by name and birth date (this location only) when the mirror has no match.

    NexHealth's name search is fuzzy, so only an exact name and birth date counts; a hit is
    added to the mirror so the rest of the call works from it.
    """
    connector = connector_for(conn, env.settings, env.transport)
    try:
        found = await connector.find_patients(
            str(args["first_name"]).strip(), str(args["last_name"]).strip(), str(want[2])
        )
    except NexHealthError as err:
        await audit(
            session,
            conn,
            "receptionist.find_patient.live",
            "receptionist",
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
        )
        return []
    finally:
        await connector.aclose()
    exact = [
        m
        for m in map(records.map_patient, found)
        if not m.attrs.get("inactive") and _identity(m.pii) == want
    ]
    if exact:
        await records.upsert(session, conn, "patient", exact, box)
    await audit(
        session,
        conn,
        "receptionist.find_patient.live",
        "receptionist",
        ok=True,
        status="success",
        summary={"searched": len(found), "matched": len(exact)},
    )
    return [m.source_id for m in exact]


async def find_patient(session, attempt, _version, args, _evidence, env: ToolEnv):
    conn = await _connection(session, attempt)
    state = _state(attempt)
    tries = int(state.get("verify_tries", 0))
    if tries >= MAX_VERIFY:
        raise Rejected("verification_limit", "Verification attempts are used up for this call.")
    state["verify_tries"] = tries + 1
    want = (_norm(args["first_name"]), _norm(args["last_name"]), _dob(args["date_of_birth"]))
    box = PhiBox(env.settings)

    matches = [
        p.source_id
        for p in await _records(session, conn, "patient")
        if p.status not in ("inactive", "deleted")
        and want[2]
        and _identity(box.open(p.pii)) == want
    ]
    if not matches and all(want):
        # The mirror can lag (a chart created since the last sync): ask the EHR directly.
        matches = await _live_lookup(session, conn, env, args, want, box)
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
    type_ref: str | None = None,
) -> list[tuple[datetime, str, str]]:
    connector = connector_for(conn, env.settings, env.transport)
    try:
        return await connector.available_slots(start, end, minutes, providers, tz, type_ref)
    finally:
        await connector.aclose()


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
    if not connector_for(conn, env.settings, env.transport).schedule_online():
        # NexHealth: check sync status before offering times. While the EHR's sync is down the
        # schedule may be stale and a booking would wait for the office to come back.
        _save(attempt, state)
        await audit(
            session,
            conn,
            "receptionist.find_slots",
            "receptionist",
            ok=False,
            status="sync_offline",
            summary={"offered": 0},
        )
        return {
            "slots": [],
            "message": "The practice's scheduling system is offline right now, so I can't offer "
            "times. Offer to take a message so the front desk can book it.",
        }
    try:
        found = await _available(
            conn, env, start, end, minutes, provider_refs, tz, kind.source_id if kind else None
        )
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


def _offered(attempt: CallAttempt, slot_id: str) -> tuple[dict[str, Any], datetime]:
    slot = (_state(attempt).get("slots") or {}).get(slot_id)
    if slot is None:
        raise Rejected("unknown_slot", "Use a time from find_slots on this call.")
    start = datetime.fromisoformat(slot["start"])
    if start <= utcnow():
        raise Rejected("slot_in_past", "That time has passed. Search again.")
    return slot, start


def _drop_slot(attempt: CallAttempt, slot_id: str) -> None:
    state = _state(attempt)
    state["slots"] = {k: v for k, v in (state.get("slots") or {}).items() if k != slot_id}
    _save(attempt, state)


async def _slot_operatory(
    session: AsyncSession,
    attempt: CallAttempt,
    env: ToolEnv,
    conn: PayerConnection,
    slot_id: str,
) -> tuple[dict[str, Any], datetime, str | None]:
    """Re-check an offered time with the EHR: its free operatory (``""`` if none are mapped),
    or ``None`` when the time was taken since it was offered (the offer is then withdrawn)."""
    slot, start = _offered(attempt, slot_id)
    tz = await practice_zone(session, conn)
    probe = SimpleNamespace(
        starts_at=start,
        minutes=slot["minutes"],
        timezone=str(tz),
        provider_ref=slot["provider_ref"],
        operatory_ref=slot.get("operatory_ref"),
        source_appointment_ref=slot.get("exclude"),
    )
    connector = connector_for(conn, env.settings, env.transport)
    try:
        operatory = await connector.slot_free(probe)
    except NexHealthError:
        raise Rejected("schedule_unavailable", "I can't reach the schedule right now.") from None
    finally:
        await connector.aclose()
    if operatory is None:
        _drop_slot(attempt, slot_id)
    return slot, start, operatory


async def _move(
    session: AsyncSession,
    attempt: CallAttempt,
    env: ToolEnv,
    conn: PayerConnection,
    old: EhrRecord,
    slot_id: str,
) -> tuple[str, WriteIntent | None, dict[str, Any]]:
    """Reschedule in place: ``PATCH start_time/end_time`` (EHRs that take time changes)."""
    slot, start, operatory = await _slot_operatory(session, attempt, env, conn, slot_id)
    if operatory is None:
        return "slot_taken", None, slot
    end = start + timedelta(minutes=slot["minutes"])
    payload: dict[str, Any] = {
        "patient_id": int(old.patient_ref),
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }
    if operatory:
        payload["operatory_id"] = int(operatory)
    intent = await intents.propose(
        session,
        conn,
        env.settings,
        kind="appointment_reschedule",
        target_source_id=old.source_id,
        payload=payload,
        reason="Receptionist: patient moved this visit",
        source="receptionist",
        requested_by=f"call:{attempt.id}"[:36],
    )
    if intent.state == "proposed":
        intent = await _run(session, env, conn, intent)
    outcome = _outcome(intent)
    if outcome == "booked":
        outcome = "rescheduled"
        old.starts_at = start
        old.attrs = {
            **old.attrs,
            "end_time": end.isoformat(),
            "operatory_id": operatory or old.attrs.get("operatory_id"),
        }
        _drop_slot(attempt, slot_id)
    return outcome, intent, slot


async def _book(
    session: AsyncSession,
    attempt: CallAttempt,
    env: ToolEnv,
    conn: PayerConnection,
    patient: str,
    slot_id: str,
    reason: str,
) -> tuple[str, WriteIntent | None, dict[str, Any]]:
    slot, start, operatory = await _slot_operatory(session, attempt, env, conn, slot_id)
    if operatory is None:
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
        _drop_slot(attempt, slot_id)
    return outcome, intent, slot


def _outcome(intent: WriteIntent) -> str:
    if intent.state in ("accepted", "written"):
        return "booked"
    if intent.state == "manual":
        return "front_desk"  # the practice's EHR doesn't take this write (CarePlix Connect catalog)
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
    if _support(conn, env, "appointment_reschedule") == "yes" and old.attrs.get("foreign_id"):
        # Denticon, Dentrix, Dentrix Enterprise, Eaglesoft and Open Dental take a time change on
        # the visit itself (once it is in the EHR): one write, nothing to release.
        outcome, move, slot = await _move(session, attempt, env, conn, old, args["slot_id"])
        tz = await practice_zone(session, conn)
        await events.append(
            session,
            attempt,
            "appointment.reschedule",
            "api",
            {"outcome": outcome, "intent_id": move.id if move else None, "in_place": True},
        )
        return _write_result(outcome, datetime.fromisoformat(slot["start"]), tz)
    if _support(conn, env, "appointment_cancel") == "no":
        # Moving a visit is "book the new time, cancel the old one". Where the EHR can't take
        # a cancel, booking first would leave the patient double-booked: don't start.
        raise Rejected(
            "not_supported_here",
            "This practice's system doesn't let me move visits. Offer to leave a message so "
            "the front desk can move it.",
        )
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
    # Where the EHR can't take a cancel, run it now: the hub turns it into a front-desk task.
    run = (
        env.settings.receptionist_cancel_mode == "auto"
        or _support(conn, env, "appointment_cancel") == "no"
    )
    reason = f"Receptionist: patient cancelled ({args['reason'].replace('_', ' ')})"
    intent = await _cancel(session, attempt, env, conn, appt, reason, run=run)
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


def _phone_digits(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # NANP number in E.164: NexHealth stores the ten digits
    return digits if len(digits) == 10 else None


async def book_new_patient(session, attempt, _version, args, _evidence, env: ToolEnv):
    """New-patient intake, then the booking, in one step.

    ``POST /patients`` (``return_existing_if_match``) creates the patient in NexHealth; the booking
    carries them into the EHR. Curve Hero, Dolphin and PracticeWorks can't take a new patient
    that way, and Dentrix and Eaglesoft need the patient's gender to create the chart.
    """
    conn = await _connection(session, attempt)
    state = _state(attempt)
    if state.get("patient_ref"):
        raise Rejected("already_verified", "The caller is verified: use book_appointment.")
    if not int(state.get("verify_tries", 0)):
        raise Rejected(
            "lookup_first",
            "Look the caller up with find_patient first: only a caller who "
            "isn't found is a new patient.",
        )
    connector = connector_for(conn, env.settings, env.transport)
    support, profile = connector.support("patient.create"), connector.profile
    if support == "no":
        raise Rejected(
            "new_patients_not_supported",
            "This practice's system doesn't let me register new patients. Offer to take a "
            "message so the front desk can register them and call back.",
        )
    if "gender" in (profile.new_patient_fields if profile else ()) and not args.get("gender"):
        raise Rejected(
            "gender_required",
            "This practice's system needs the patient's sex to open a chart. Ask whether it "
            "should be recorded as male, female or other, then try again.",
        )
    born = _dob(args["date_of_birth"])
    today = utcnow().date()
    if born is None or not (
        today.replace(year=today.year - 130) <= date.fromisoformat(born) <= today
    ):
        raise Rejected("invalid_date_of_birth", "Check the date of birth with the caller.")
    phone = _phone_digits(args.get("phone_number") or attempt.caller_e164)
    if phone is None:
        raise Rejected("phone_required", "Ask for a ten-digit phone number for the patient.")
    await _guard_write(session, attempt, conn, env)
    slot, start = _offered(attempt, args["slot_id"])
    if not slot.get("provider_ref"):
        raise Rejected("unknown_slot", "Use a time from find_slots on this call.")
    payload = {
        "location_id": location_id(conn),
        "provider_id": int(slot["provider_ref"]),
        "first_name": str(args["first_name"]).strip(),
        "last_name": str(args["last_name"]).strip(),
        "email": str(args["email"]).strip(),
        "date_of_birth": born,
        "phone_number": phone,
        "gender": args.get("gender"),
    }
    intent = await intents.propose(
        session,
        conn,
        env.settings,
        kind="patient_create",
        target_source_id="new",
        payload=payload,
        reason="Receptionist: new patient registered on a call",
        source="receptionist",
        requested_by=f"call:{attempt.id}"[:36],
    )
    if intent.state == "proposed":
        intent = await _run(session, env, conn, intent)
    tz = await practice_zone(session, conn)
    if intent.state not in ("accepted", "written") or not intent.remote_id:
        outcome = _outcome(intent)
        await events.append(session, attempt, "patient.registration", "api", {"outcome": outcome})
        result = _write_result(outcome, start, tz)
        result["message"] = (
            "I couldn't register the new patient. Offer to take a message so the front desk "
            "can register them and call back."
        )
        return result
    patient = intent.remote_id
    [known] = await _records(session, conn, "patient", source_id=patient) or [None]
    if known is None:
        # Show the patient in the mirror now; the next sync replaces this with NexHealth's copy.
        bio = {"date_of_birth": born, "phone_number": phone, "gender": payload["gender"]}
        mirrored = records.map_patient(
            {
                "id": patient,
                "first_name": payload["first_name"],
                "last_name": payload["last_name"],
                "email": payload["email"],
                "foreign_id": intent.foreign_id,
                "bio": bio,
            }
        )
        await records.upsert(session, conn, "patient", [mirrored], PhiBox(env.settings))
    state = _state(attempt)
    state["patient_ref"], state["new_patient"] = patient, True
    _save(attempt, state)
    existing = bool(known is not None or intent.foreign_id)
    await events.append(
        session,
        attempt,
        "patient.registered",
        "api",
        {"patient_ref": patient, "intent_id": intent.id, "existing": existing},
    )
    outcome, book, slot = await _book(
        session, attempt, env, conn, patient, args["slot_id"], "Receptionist: new patient booked"
    )
    await events.append(
        session,
        attempt,
        "appointment.booking",
        "api",
        {"outcome": outcome, "intent_id": book.id if book else None, "new_patient": True},
    )
    result = _write_result(outcome, start, tz)
    result["patient"] = "existing" if existing else "registered"
    if outcome == "booked":
        result["message"] = (
            "Booked. Tell the caller the front desk completes their details at the first visit."
        )
    return result


def _support(conn: PayerConnection, env: ToolEnv, kind: str) -> str:
    """What the CarePlix Connect catalog says this practice's EHR does with a write kind."""
    return connector_for(conn, env.settings, env.transport).intent_support(kind)


def _write_result(outcome: str, when: datetime, tz: ZoneInfo) -> dict[str, Any]:
    messages = {
        "front_desk": "This practice's system doesn't let CarePlix make this change, so it is "
        "with the front desk. Tell the caller it isn't done until the front desk confirms.",
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
    "book_new_patient": book_new_patient,
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
