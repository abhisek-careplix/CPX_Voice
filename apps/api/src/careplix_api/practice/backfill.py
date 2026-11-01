"""FO-2 cancellation backfill (FRONT_OFFICE_AUTOMATION_PLAN §2C).

1. Detect: a cancelled or deleted appointment between the lead time and the horizon opens a slot,
   but only if NexHealth's available slots confirm the time is still free.
2. Rank: patients who asked to come sooner (``sooner_if_possible``), then same-type, same-provider
   move-ups, then recent cancellers of the same type with nothing booked. Inactive coverage and
   patients with no phone are left out.
3. Offer: until outbound calling and SMS exist, the front desk works the ranked list in Studio and
   records each answer. The same engine will drive the voice agent (FO-4).
4. Book safely: claim the opening atomically, re-check the slot with NexHealth, book through a
   write intent, and only then release a move-up's old appointment (cancel, never reschedule:
   medical EHRs don't accept time changes through NexHealth). A slot is never double-booked.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import (
    AuditEvent,
    BackfillOffer,
    BackfillOpening,
    EhrRecord,
    EligibilityCheck,
    PayerConnection,
)
from careplix_api.practice import intents
from careplix_api.practice.connections import audit, ehr_client, location_id
from careplix_api.practice.records import parse_dt
from careplix_api.settings import Settings

FREED = ("cancelled", "deleted")
SCHEDULED = ("booked", "confirmed")
ACTIVE = ("open", "no_candidates")
MAX_CANDIDATES = 10
TIER_SCORE = {"asap": 100, "move_up": 60, "recent_cancel": 30}


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def within_contact_hours(settings: Settings, tz: str | None, at: datetime | None = None) -> bool:
    start, end = (time.fromisoformat(t) for t in settings.contact_hours.split("-"))
    local = (at or utcnow()).astimezone(zone(tz)).time()
    return start <= local < end


def _log(opening: BackfillOpening, event: str, **detail: Any) -> None:
    # Identifiers and outcomes only: never names or phone numbers.
    opening.timeline = [
        *(opening.timeline or []),
        {"at": utcnow().isoformat(), "event": event, **detail},
    ]


async def free_operatory(
    client: NexHealthClient, conn: PayerConnection, opening: BackfillOpening
) -> str | None:
    """The operatory NexHealth reports free at exactly this start (``""`` if none are mapped).

    ``None`` means the time is no longer bookable: rebooked, blocked or outside working hours.
    """
    if getattr(client, "is_openemr", False):
        return await client.slot_free(opening)
    start = as_utc(opening.starts_at)
    local_day = start.astimezone(zone(opening.timezone)).date().isoformat()
    params: dict[str, Any] = {
        "start_date": local_day,
        "days": 1,
        "lids[]": location_id(conn),
        "slot_length": opening.minutes,
        "slot_interval": 15,
        "overlapping_operatory_slots": "true",
    }
    if opening.provider_ref:
        params["pids[]"] = opening.provider_ref
    body = await client.request("GET", "/available_slots", params)
    matches = [
        str(slot.get("operatory_id") or "")
        for day in body.get("data") or []
        for slot in day.get("slots") or []
        if parse_dt(slot.get("time")) == start
    ]
    if not matches:
        return None
    return opening.operatory_ref if opening.operatory_ref in matches else matches[0]


async def _records(session: AsyncSession, conn_id: str, resource: str) -> list[EhrRecord]:
    return list(
        await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn_id, EhrRecord.resource == resource
            )
        )
    )


async def rank_candidates(
    session: AsyncSession, conn: PayerConnection, opening: BackfillOpening
) -> list[dict[str, Any]]:
    appointments = await _records(session, conn.id, "appointment")
    patients = {p.source_id: p for p in await _records(session, conn.id, "patient")}
    minutes = {
        t.source_id: t.attrs.get("minutes")
        for t in await _records(session, conn.id, "appointment_type")
    }
    latest_coverage: dict[str, str] = {}
    for check in await session.scalars(
        select(EligibilityCheck)
        .where(EligibilityCheck.ehr_connection_id == conn.id, EligibilityCheck.state == "done")
        .order_by(EligibilityCheck.completed_at)
    ):
        if check.patient_ref:
            latest_coverage[check.patient_ref] = (check.summary or {}).get("coverage") or "unknown"
    start = as_utc(opening.starts_at)
    upcoming: dict[str, list[EhrRecord]] = {}
    for appt in appointments:
        if appt.status in SCHEDULED and appt.starts_at and as_utc(appt.starts_at) > utcnow():
            upcoming.setdefault(appt.patient_ref or "", []).append(appt)

    def fits(appt: EhrRecord) -> bool:
        kind = appt.attrs.get("appointment_type_id")
        if opening.appointment_type_ref and kind == opening.appointment_type_ref:
            return True
        length = minutes.get(kind)
        return isinstance(length, int) and length <= opening.minutes

    best: dict[str, dict[str, Any]] = {}

    def consider(
        patient_ref: str | None, tier: str, current: EhrRecord | None, same_type: bool
    ) -> None:
        patient = patients.get(patient_ref or "")
        if (
            patient is None
            or patient_ref == opening.source_patient_ref
            or patient.status == "inactive"
        ):
            return
        if not patient.attrs.get("has_phone"):
            return  # unreachable until SMS/email channels exist
        coverage = latest_coverage.get(patient_ref, "unchecked")
        if coverage == "inactive":
            return
        reasons = {
            "asap": ["Asked to come in sooner"],
            "move_up": ["Booked later for the same visit"],
            "recent_cancel": ["Cancelled recently and has nothing booked"],
        }[tier][:]
        score = TIER_SCORE[tier]
        if current is not None and current.attrs.get("provider_id") == opening.provider_ref:
            score += 20
            reasons.append("Same provider")
        if same_type:
            score += 10
        if current is not None and tier != "recent_cancel":
            days = (as_utc(current.starts_at) - start).days
            score += min(days, 30)
            reasons.append(f"Moves {days} day{'s' if days != 1 else ''} earlier")
        if coverage == "active":
            score += 10
            reasons.append("Coverage verified")
        candidate = {
            "patient_ref": patient_ref,
            "tier": tier,
            "score": score,
            "current_appointment_ref": current.source_id
            if current is not None and tier != "recent_cancel"
            else None,
            "reasons": reasons,
        }
        if patient_ref not in best or best[patient_ref]["score"] < score:
            best[patient_ref] = candidate

    for patient_ref, booked in upcoming.items():
        for appt in sorted(booked, key=lambda a: a.starts_at):
            if as_utc(appt.starts_at) <= start or not fits(appt):
                continue
            same_type = appt.attrs.get("appointment_type_id") == opening.appointment_type_ref
            if appt.attrs.get("sooner_if_possible"):
                consider(patient_ref, "asap", appt, same_type)
            elif same_type and appt.attrs.get("provider_id") == opening.provider_ref:
                consider(patient_ref, "move_up", appt, same_type)
    for appt in appointments:
        if (
            appt.status in FREED
            and appt.attrs.get("appointment_type_id") == opening.appointment_type_ref
            and appt.patient_ref not in upcoming
            and appt.source_id != opening.source_appointment_ref
        ):
            consider(appt.patient_ref, "recent_cancel", appt, True)
    ranked = sorted(best.values(), key=lambda c: (-c["score"], c["patient_ref"]))
    return ranked[:MAX_CANDIDATES]


async def _open(
    session: AsyncSession, conn: PayerConnection, appt: EhrRecord, client: NexHealthClient
) -> BackfillOpening:
    kind = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "appointment_type",
            EhrRecord.source_id == appt.attrs.get("appointment_type_id"),
        )
    )
    end = parse_dt(appt.attrs.get("end_time"))
    length = (kind.attrs.get("minutes") if kind else None) or (
        int((end - as_utc(appt.starts_at)).total_seconds() // 60) if end else 30
    )
    opening = BackfillOpening(
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        source_appointment_ref=appt.source_id,
        source_patient_ref=appt.patient_ref,
        provider_ref=appt.attrs.get("provider_id"),
        operatory_ref=appt.attrs.get("operatory_id"),
        appointment_type_ref=appt.attrs.get("appointment_type_id"),
        starts_at=as_utc(appt.starts_at),
        minutes=int(length),
        timezone=appt.attrs.get("timezone"),
        timeline=[],
    )
    operatory = await free_operatory(client, conn, opening)  # may raise: nothing added yet
    session.add(opening)
    _log(opening, "detected", appointment=appt.source_id, status=appt.status)
    if operatory is None:
        opening.state = "taken"
        opening.state_reason = (
            "NexHealth no longer shows this time as free (rebooked or outside working hours)."
        )
        _log(opening, "slot_not_free")
        return opening
    opening.operatory_ref = operatory or opening.operatory_ref
    _log(opening, "slot_confirmed_free", operatory=operatory or None)
    await session.flush()
    ranked = await rank_candidates(session, conn, opening)
    for rank, candidate in enumerate(ranked, 1):
        session.add(
            BackfillOffer(
                workspace_id=conn.workspace_id, opening_id=opening.id, rank=rank, **candidate
            )
        )
    opening.state = "open" if ranked else "no_candidates"
    _log(opening, "ranked", candidates=len(ranked))
    return opening


async def detect(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    limit: int = 20,
) -> int:
    """Open a backfill opening for each newly freed slot inside the backfill window."""
    now = utcnow()
    known = set(
        await session.scalars(
            select(BackfillOpening.source_appointment_ref).where(
                BackfillOpening.connection_id == conn.id
            )
        )
    )
    freed = [
        a
        for a in await session.scalars(
            select(EhrRecord)
            .where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "appointment",
                EhrRecord.status.in_(FREED),
                EhrRecord.starts_at >= now + timedelta(minutes=settings.backfill_lead_minutes),
                EhrRecord.starts_at <= now + timedelta(days=settings.backfill_horizon_days),
            )
            .order_by(EhrRecord.starts_at)
        )
        if a.source_id not in known
    ][:limit]
    if not freed:
        return 0
    client = ehr_client(conn, settings, transport)
    opened = 0
    try:
        for appt in freed:
            try:
                await _open(session, conn, appt, client)
                opened += 1
            except NexHealthError as err:
                # Leave this slot for the next pass; openings already made this pass stand.
                await audit(
                    session,
                    conn,
                    "nexhealth.available_slots",
                    "backfill",
                    ok=False,
                    status=err.status,
                    http_status=err.http_status,
                    error=err.message,
                )
                break
    finally:
        calls = client.calls
        await client.aclose()
    await audit(
        session,
        conn,
        "backfill.detect",
        "backfill",
        ok=True,
        status="success",
        summary={"opened": opened, "api_calls": calls},
    )
    return opened


async def expire(session: AsyncSession, conn: PayerConnection, settings: Settings) -> int:
    """Close openings that are too close to start, or whose appointment came back."""
    cutoff = utcnow() + timedelta(minutes=settings.backfill_lead_minutes)
    changed = 0
    for opening in await session.scalars(
        select(BackfillOpening).where(
            BackfillOpening.connection_id == conn.id, BackfillOpening.state.in_(ACTIVE)
        )
    ):
        source = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "appointment",
                EhrRecord.source_id == opening.source_appointment_ref,
            )
        )
        if source is not None and source.status not in FREED:
            opening.state, opening.state_reason = (
                "closed",
                "The original appointment was reinstated.",
            )
        elif as_utc(opening.starts_at) < cutoff:
            opening.state, opening.state_reason = "expired", "Too close to the start time to fill."
        else:
            continue
        _log(opening, opening.state)
        changed += 1
    return changed


async def respond(
    db: Database,
    session: AsyncSession,
    offer: BackfillOffer,
    outcome: str,
    actor: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BackfillOpening:
    """Record a patient's answer. "accepted" books the slot; the staff member approves the write."""
    opening = await session.get(BackfillOpening, offer.opening_id)
    if offer.state != "queued":
        raise ApiError(409, "invalid_state", f"This offer is already {offer.state}.")
    if outcome in ("declined", "no_answer"):
        offer.state, offer.responded_by, offer.responded_at = outcome, actor, utcnow()
        _log(opening, f"offer_{outcome}", rank=offer.rank)
        await session.commit()
        return opening

    # Claim the opening: only one acceptance can ever proceed to booking.
    claimed = await session.execute(
        update(BackfillOpening)
        .where(BackfillOpening.id == opening.id, BackfillOpening.state == "open")
        .values(state="booking")
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ApiError(409, "opening_unavailable", "This slot is no longer open.")
    await session.refresh(opening)
    offer.state, offer.responded_by, offer.responded_at = "accepted", actor, utcnow()
    _log(opening, "offer_accepted", rank=offer.rank, tier=offer.tier)
    conn = await session.get(PayerConnection, opening.connection_id)
    await session.commit()

    client = ehr_client(conn, settings, transport)
    try:
        operatory = await free_operatory(client, conn, opening)
    except NexHealthError as err:
        operatory, error = None, err.message
    else:
        error = None
    finally:
        await client.aclose()
    if operatory is None:
        opening.state = "taken" if error is None else "open"
        opening.state_reason = error or "Someone else took this time before it could be booked."
        offer.state = "failed"
        _log(opening, "slot_recheck_failed", error=bool(error))
        await session.commit()
        return opening

    start = as_utc(opening.starts_at)
    book = await intents.propose(
        session,
        conn,
        settings,
        kind="appointment_book",
        target_source_id=offer.patient_ref,
        payload={
            "location_id": location_id(conn),
            "patient_id": int(offer.patient_ref),
            "provider_id": int(opening.provider_ref) if opening.provider_ref else None,
            "operatory_id": int(operatory) if operatory else None,
            "appointment_type_id": int(opening.appointment_type_ref)
            if opening.appointment_type_ref
            else None,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=opening.minutes)).isoformat(),
            "note": "Booked by CarePlix backfill",
        },
        reason="Backfill: filling a cancelled slot",
        source="backfill",
        requested_by=actor,
    )
    await intents.decide(session, book, actor, approve=True)
    opening.book_intent_id = book.id
    await session.commit()
    await intents.execute_approved(db, settings, transport, only=[book.id])
    await session.refresh(book)

    if book.state in ("accepted", "written"):
        opening.state, opening.filled_at = "filled", utcnow()
        opening.filled_patient_ref, opening.filled_appointment_ref = (
            offer.patient_ref,
            book.remote_id,
        )
        offer.state = "booked"
        _log(opening, "booked", appointment=book.remote_id, confirmation=book.confirmation)
        # Close this slot's other offers, and this patient's offers for other slots.
        for other in await session.scalars(
            select(BackfillOffer).where(
                BackfillOffer.workspace_id == opening.workspace_id,
                BackfillOffer.state == "queued",
                (BackfillOffer.opening_id == opening.id)
                | (BackfillOffer.patient_ref == offer.patient_ref),
            )
        ):
            other.state = "superseded"
        if offer.current_appointment_ref:
            # The patient agreed to move up: release their later appointment (cancel, not move).
            release = await intents.propose(
                session,
                conn,
                settings,
                kind="appointment_cancel",
                target_source_id=offer.current_appointment_ref,
                payload={"cancelled": True, "patient_id": int(offer.patient_ref)},
                reason="Backfill: patient moved to an earlier slot",
                source="backfill",
                requested_by=actor,
            )
            await intents.decide(session, release, actor, approve=True)
            opening.release_intent_id = release.id
            await session.commit()
            await intents.execute_approved(db, settings, transport, only=[release.id])
            await session.refresh(release)
            _log(
                opening,
                "released_later_appointment",
                appointment=offer.current_appointment_ref,
                state=release.state,
            )
    elif book.state == "failed":
        opening.state, opening.state_reason = "open", f"Booking was refused: {book.last_error}"
        offer.state = "failed"
        _log(opening, "booking_refused")
    else:
        # Unknown outcome: never offer the slot to anyone else until a person checks the EHR.
        opening.state, opening.state_reason = "needs_review", book.last_error
        offer.state = "failed"
        _log(opening, "booking_unknown")
    session.add(
        AuditEvent(
            workspace_id=opening.workspace_id,
            actor=actor,
            action=f"backfill.{opening.state}",
            target=opening.id,
            detail={"offer": offer.id, "tier": offer.tier, "intent": book.id},
        )
    )
    await session.commit()
    return opening


def opening_view(
    opening: BackfillOpening,
    offers: list[BackfillOffer],
    labels: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    queued = [o for o in offers if o.state == "queued"]
    return {
        "id": opening.id,
        "state": opening.state,
        "state_reason": opening.state_reason,
        "start": as_utc(opening.starts_at),
        "minutes": opening.minutes,
        "timezone": opening.timezone,
        "provider": labels["providers"].get(opening.provider_ref),
        "type": labels["types"].get(opening.appointment_type_ref),
        "source_appointment_id": opening.source_appointment_ref,
        "within_contact_hours": within_contact_hours(settings, opening.timezone),
        "next_offer_id": queued[0].id if queued and opening.state == "open" else None,
        "filled_appointment_id": opening.filled_appointment_ref,
        "detected_at": opening.detected_at,
        "filled_at": opening.filled_at,
        "timeline": opening.timeline or [],
        "offers": [
            {
                "id": o.id,
                "rank": o.rank,
                "tier": o.tier,
                "score": o.score,
                "patient_id": o.patient_ref,
                "patient": labels["patients"].get(o.patient_ref, "Patient"),
                "current_appointment_id": o.current_appointment_ref,
                "current_start": labels["appointments"].get(o.current_appointment_ref),
                "reasons": o.reasons,
                "state": o.state,
                "responded_at": o.responded_at,
            }
            for o in sorted(offers, key=lambda x: x.rank)
        ],
    }
