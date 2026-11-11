"""Studio API for the practice layer: status, sync, capability probe, huddle and EHR writes."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.connect.catalog import OPERATIONS, PROFILES
from careplix_api.connect.connectors import connector_for
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import (
    AuditEvent,
    BackfillOffer,
    BackfillOpening,
    EhrRecord,
    EligibilityCheck,
    FrontDeskMessage,
    PayerConnection,
    SyncCursor,
    WebhookEvent,
    WriteIntent,
)
from careplix_api.phi import PhiBox, initials
from careplix_api.practice import backfill, eligibility, intents, receptionist, sync, webhooks
from careplix_api.practice.connections import CLEARINGHOUSE, EHR_KINDS, OPENEMR, verify
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter(prefix="/practices")


def _transport(request: Request):
    return getattr(request.app.state, "integration_transport", None)


async def _conn(
    session: AsyncSession,
    principal: Principal,
    kind: str | None = None,
    practice: str | None = None,
) -> PayerConnection:
    """The clearinghouse, or the selected practice (default: OpenEMR, then NexHealth)."""
    kinds = (kind,) if kind else EHR_KINDS
    rows = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == principal.workspace_id,
                PayerConnection.kind.in_(kinds),
            )
        )
    )
    if practice:
        rows = [c for c in rows if c.id == practice]
    rows.sort(key=lambda c: kinds.index(c.kind) if c.kind in kinds else 99)
    if not rows:
        if kind == CLEARINGHOUSE:
            message = "Stedi is not connected. Run make connect-stedi, then restart the API."
        else:
            message = (
                "No practice EHR is connected. Run make openemr-setup or make connect-nexhealth, "
                "then restart the API."
            )
        raise ApiError(404, "not_connected", message)
    return rows[0]


def _conn_view(conn: PayerConnection | None) -> dict[str, Any] | None:
    if conn is None:
        return None
    ref = conn.auth_ref or {}
    webhook = ref.get("webhook") or {}
    return {
        "id": conn.id,
        "kind": conn.kind,
        "name": conn.name,
        "env": ref.get("env"),
        "subdomain": ref.get("subdomain"),
        "location_id": ref.get("location_id"),
        "enabled": conn.enabled,
        "verified_at": conn.verified_at,
        "last_error": conn.last_error,
        "capability": conn.capability,
        "webhook": {
            "registered": bool(webhook.get("endpoint_id")),
            "events": webhook.get("events", []),
            "signed": bool(webhook.get("secret_sealed")),
            # Endpoint health from NexHealth (deactivated, inactive subscriptions, retries).
            "health": (conn.capability or {}).get("webhooks"),
        },
    }


@router.get("")
async def overview(
    practice: str | None = None,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    ws = principal.workspace_id
    practices = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == ws, PayerConnection.kind.in_(EHR_KINDS)
            )
        )
    )
    clearinghouse = await session.scalar(
        select(PayerConnection).where(
            PayerConnection.workspace_id == ws, PayerConnection.kind == CLEARINGHOUSE
        )
    )
    ehr = await _conn(session, principal, practice=practice) if practices else None
    mirror: dict[str, int] = {}
    cursors: list[dict[str, Any]] = []
    location = None
    if ehr is not None:
        mirror = dict(
            (
                await session.execute(
                    select(EhrRecord.resource, func.count())
                    .where(EhrRecord.connection_id == ehr.id)
                    .group_by(EhrRecord.resource)
                )
            ).all()
        )
        cursors = [
            {
                "resource": c.resource,
                "rows": c.rows,
                "api_calls": c.api_calls,
                "duration_ms": c.duration_ms,
                "last_run_at": c.last_run_at,
                "last_full_at": c.last_full_at,
                "last_error": c.last_error,
            }
            for c in await session.scalars(
                select(SyncCursor)
                .where(SyncCursor.connection_id == ehr.id)
                .order_by(SyncCursor.resource)
            )
        ]
        loc = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == ehr.id, EhrRecord.resource == "location"
            )
        )
        location = loc.attrs if loc else None
    checks = Counter(
        dict(
            (
                await session.execute(
                    select(EligibilityCheck.state, func.count())
                    .where(EligibilityCheck.ehr_connection_id == (ehr.id if ehr else "-"))
                    .group_by(EligibilityCheck.state)
                )
            ).all()
        )
    )
    coverage = Counter(
        (c.summary or {}).get("coverage")
        for c in await session.scalars(
            select(EligibilityCheck).where(
                EligibilityCheck.ehr_connection_id == (ehr.id if ehr else "-"),
                EligibilityCheck.state == "done",
            )
        )
    )
    webhook_rows = list(
        await session.scalars(
            select(WebhookEvent)
            .where(WebhookEvent.workspace_id == ws)
            .order_by(WebhookEvent.received_at.desc())
            .limit(200)
        )
    )
    recent_intents = list(
        await session.scalars(
            select(WriteIntent)
            .where(WriteIntent.connection_id == (ehr.id if ehr else "-"))
            .order_by(WriteIntent.created_at.desc())
            .limit(30)
        )
    )
    return {
        "ehr": _conn_view(ehr),
        "practices": [
            {"id": c.id, "kind": c.kind, "name": c.name, "env": (c.auth_ref or {}).get("env")}
            for c in practices
        ],
        "links": connector_for(ehr, settings).links() if ehr is not None else None,
        "hub": connector_for(ehr, settings).view() if ehr is not None else None,
        "clearinghouse": _conn_view(clearinghouse),
        "location": location,
        "mirror": mirror,
        "cursors": cursors,
        "api_calls_total": sum(c["api_calls"] for c in cursors),
        "webhooks": {
            "public_url_set": bool(settings.webhook_public_url),
            "received": len(webhook_rows),
            "pending": sum(1 for w in webhook_rows if w.processed_at is None),
            "last_at": webhook_rows[0].received_at if webhook_rows else None,
        },
        "eligibility": {
            "autopilot": settings.eligibility_autopilot,
            "states": dict(checks),
            "coverage": {k: v for k, v in coverage.items() if k},
        },
        "intents": [intents.view(i) for i in recent_intents],
    }


@router.post("/verify")
async def verify_connection(
    request: Request,
    kind: Literal["nexhealth", "openemr", "stedi"] | None = None,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, kind, practice)
    result = await verify(session, conn, settings, _transport(request))
    await session.commit()
    return result


@router.post("/sync")
async def sync_now(
    request: Request,
    full: bool = False,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    report = await sync.sync_practice(session, conn, settings, _transport(request), full=full)
    queued = await eligibility.schedule(session, conn) if settings.eligibility_autopilot else 0
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="practice.sync",
            target=conn.id,
            detail={"full": full, "api_calls": report["api_calls"]},
        )
    )
    await session.commit()
    return {**report, "eligibility_queued": queued}


@router.post("/webhooks/register")
async def register_webhooks(
    request: Request,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    result = await webhooks.register(session, conn, settings, _transport(request))
    await session.commit()
    return result


@router.post("/webhooks/check")
async def check_webhooks(
    request: Request,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Read the endpoint's state from NexHealth now (the worker also checks hourly)."""
    conn = await _conn(session, principal, practice=practice)
    if conn.kind != "nexhealth":
        raise ApiError(409, "not_nexhealth", "Webhook health applies to NexHealth practices.")
    health = await webhooks.check_health(session, conn, settings, _transport(request))
    await session.commit()
    return health


class AppointmentWriteIn(BaseModel):
    action: Literal["confirm", "check_in"]


@router.post("/appointments/{appointment_id}/write")
async def propose_appointment_write(
    appointment_id: str,
    body: AppointmentWriteIn,
    practice: str | None = None,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Propose confirming or checking in a visit (``PATCH confirmed`` / ``checkin_at``).

    It joins the write queue like every EHR write; ``appointment_updated`` confirms it.
    """
    conn = await _conn(session, principal, practice=practice)
    appt = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "appointment",
            EhrRecord.source_id == appointment_id,
        )
    )
    if appt is None or appt.status not in ("booked", "confirmed"):
        raise ApiError(404, "not_found", "No open appointment with that id in the mirror.")
    if body.action == "confirm":
        kind, payload, reason = "appointment_confirm", {"confirmed": True}, "Confirmed from Studio"
    else:
        now = utcnow().replace(microsecond=0)
        tz = await receptionist.practice_zone(session, conn)
        if appt.starts_at is None or as_utc(appt.starts_at).astimezone(tz).date() != (
            now.astimezone(tz).date()
        ):
            raise ApiError(409, "not_today", "Only today's visits can be checked in.")
        # NexHealth refuses a check-in time in the future, so it is the moment of the request.
        kind, payload = "appointment_check_in", {"checkin_at": now.isoformat()}
        reason = "Checked in from Studio"
    intent = await intents.propose(
        session,
        conn,
        settings,
        kind=kind,
        target_source_id=appointment_id,
        payload={**payload, "patient_id": appt.patient_ref},
        reason=reason,
        source="manual",
        requested_by=principal.user_id,
    )
    await session.commit()
    return intents.view(intent)


def _practice_tz(appointments: list[EhrRecord]) -> ZoneInfo:
    names = Counter(a.attrs.get("timezone") for a in appointments if a.attrs.get("timezone"))
    for name, _ in names.most_common():
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def _flags(
    appt: EhrRecord, coverage: EhrRecord | None, check: EligibilityCheck | None
) -> list[str]:
    flags: list[str] = []
    if appt.attrs.get("sooner_if_possible"):
        flags.append("asap_list")
    if coverage is None:
        flags.append("no_insurance_on_file")
    if check is None:
        flags.append("not_checked")
    elif check.state == "pending":
        flags.append("check_pending")
    elif check.state == "failed":
        flags.append("check_failed")
    elif check.state == "done":
        summary = check.summary or {}
        if summary.get("coverage") == "inactive":
            flags.append("coverage_inactive")
        if summary.get("auth_required_services"):
            flags.append("auth_required")
    return flags


@router.get("/huddle")
async def huddle(
    day: date | None = Query(default=None, alias="date"),
    days: int = Query(default=1, ge=1, le=14),
    practice: str | None = None,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The front desk's day: each visit with its insurance, eligibility result and flags."""
    conn = await _conn(session, principal, practice=practice)
    sample = list(
        await session.scalars(
            select(EhrRecord)
            .where(EhrRecord.connection_id == conn.id, EhrRecord.resource == "appointment")
            .limit(200)
        )
    )
    tz = _practice_tz(sample)
    first = day or utcnow().astimezone(tz).date()
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    appts = list(
        await session.scalars(
            select(EhrRecord)
            .where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "appointment",
                EhrRecord.starts_at >= start,
                EhrRecord.starts_at < end,
            )
            .order_by(EhrRecord.starts_at)
        )
    )

    async def lookup(resource: str, ids: set[str]) -> dict[str, EhrRecord]:
        if not ids:
            return {}
        rows = await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == resource,
                EhrRecord.source_id.in_(ids),
            )
        )
        return {r.source_id: r for r in rows}

    patients = await lookup("patient", {a.patient_ref for a in appts if a.patient_ref})
    types = await lookup(
        "appointment_type", {a.attrs.get("appointment_type_id") for a in appts} - {None}
    )
    latest: dict[str, EligibilityCheck] = {}
    for check in await session.scalars(
        select(EligibilityCheck)
        .where(
            EligibilityCheck.ehr_connection_id == conn.id,
            EligibilityCheck.appointment_ref.in_([a.source_id for a in appts] or ["-"]),
        )
        .order_by(EligibilityCheck.created_at)
    ):
        latest[check.appointment_ref] = check
    coverages = {
        a.patient_ref: await eligibility.primary_coverage(session, conn.id, a.patient_ref)
        for a in appts
        if a.patient_ref
    }
    plans = await lookup(
        "insurance_plan", {c.attrs.get("plan_id") for c in coverages.values() if c} - {None}
    )
    box = PhiBox(settings)
    rows = []
    for a in appts:
        coverage = coverages.get(a.patient_ref)
        plan = plans.get(coverage.attrs.get("plan_id")) if coverage else None
        check = latest.get(a.source_id)
        kind = types.get(a.attrs.get("appointment_type_id"))
        rows.append(
            {
                "id": a.source_id,
                "start": as_utc(a.starts_at),
                "end": a.attrs.get("end_time"),
                "status": a.status,
                "provider": a.attrs.get("provider_name"),
                "type": kind.attrs.get("name") if kind else None,
                "patient_id": a.patient_ref,
                "patient": initials(box.open(patients[a.patient_ref].pii))
                if a.patient_ref in patients
                else "Patient",
                "coverage": {
                    "plan": plan.attrs.get("name") if plan else None,
                    "payer_id": plan.attrs.get("payer_id") if plan else None,
                    "priority": coverage.attrs.get("priority"),
                    "insurance_type": coverage.attrs.get("insurance_type"),
                }
                if coverage
                else None,
                "eligibility": eligibility.view(check),
                "flags": _flags(a, coverage, check) if a.status in eligibility.SCHEDULED else [],
            }
        )
    scheduled = [r for r in rows if r["status"] in eligibility.SCHEDULED]
    return {
        "date": first.isoformat(),
        "days": days,
        "timezone": str(tz),
        "appointments": rows,
        "counts": {
            "scheduled": len(scheduled),
            "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
            "verified_active": sum(
                1
                for r in scheduled
                if ((r["eligibility"] or {}).get("summary") or {}).get("coverage") == "active"
            ),
            "needs_attention": sum(
                1 for r in scheduled if set(r["flags"]) - {"asap_list", "check_pending"}
            ),
        },
    }


@router.post("/appointments/{appointment_id}/eligibility")
async def check_now(
    appointment_id: str,
    request: Request,
    practice: str | None = None,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    appt = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "appointment",
            EhrRecord.source_id == appointment_id,
        )
    )
    if appt is None:
        raise ApiError(404, "not_found", "Appointment not found in the practice mirror.")
    check = EligibilityCheck(
        workspace_id=conn.workspace_id,
        ehr_connection_id=conn.id,
        appointment_ref=appt.source_id,
        patient_ref=appt.patient_ref,
        trigger="manual",
    )
    session.add(check)
    await session.flush()
    await eligibility.run_check(session, check, settings, _transport(request))
    await session.commit()
    return eligibility.view(check)


class IntentIn(BaseModel):
    kind: Literal["patient_alert"] = "patient_alert"
    patient_id: str = Field(min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=500)


@router.get("/intents")
async def list_intents(
    principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    rows = await session.scalars(
        select(WriteIntent)
        .where(WriteIntent.workspace_id == principal.workspace_id)
        .order_by(WriteIntent.created_at.desc())
        .limit(100)
    )
    return [intents.view(i) for i in rows]


@router.post("/intents")
async def propose_intent(
    body: IntentIn,
    practice: str | None = None,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    patient = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == body.patient_id,
        )
    )
    if patient is None:
        raise ApiError(404, "not_found", "Patient not found in the practice mirror.")
    intent = await intents.propose(
        session,
        conn,
        settings,
        kind=body.kind,
        target_source_id=body.patient_id,
        payload={"note": body.note},
        reason="Chart alert added from the Studio",
        source="manual",
        requested_by=principal.user_id,
    )
    await session.commit()
    return intents.view(intent)


@router.post("/intents/{intent_id}/{decision}")
async def decide_intent(
    intent_id: str,
    decision: Literal["approve", "reject"],
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    intent = await session.get(WriteIntent, intent_id)
    if intent is None or intent.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Write not found.")
    await intents.decide(session, intent, principal.user_id, decision == "approve")
    await session.commit()
    if decision == "approve":
        # Send now rather than on the next worker pass, so the reviewer sees the outcome.
        await intents.execute_approved(request.app.state.db, settings, _transport(request))
        await session.refresh(intent)
    return intents.view(intent)


# --- FO-2 cancellation backfill ---------------------------------------------------------------


async def _labels(
    session: AsyncSession, conn: PayerConnection, settings: Settings
) -> dict[str, Any]:
    box = PhiBox(settings)
    labels: dict[str, dict[str, Any]] = {
        "providers": {},
        "types": {},
        "patients": {},
        "appointments": {},
    }
    for rec in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource.in_(("provider", "appointment_type", "patient", "appointment")),
        )
    ):
        if rec.resource == "provider":
            labels["providers"][rec.source_id] = rec.attrs.get("name")
        elif rec.resource == "appointment_type":
            labels["types"][rec.source_id] = rec.attrs.get("name")
        elif rec.resource == "patient":
            labels["patients"][rec.source_id] = initials(box.open(rec.pii))
        else:
            labels["appointments"][rec.source_id] = as_utc(rec.starts_at)
    return labels


@router.get("/backfill")
async def backfill_board(
    practice: str | None = None,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    openings = list(
        await session.scalars(
            select(BackfillOpening)
            .where(BackfillOpening.connection_id == conn.id)
            .order_by(BackfillOpening.starts_at)
        )
    )
    offers: dict[str, list[BackfillOffer]] = {}
    for offer in await session.scalars(
        select(BackfillOffer).where(BackfillOffer.opening_id.in_([o.id for o in openings] or ["-"]))
    ):
        offers.setdefault(offer.opening_id, []).append(offer)
    labels = await _labels(session, conn, settings)
    states = Counter(o.state for o in openings)
    filled = [o for o in openings if o.state == "filled" and o.filled_at]
    waits = sorted(
        (as_utc(o.filled_at) - as_utc(o.detected_at)).total_seconds() / 60 for o in filled
    )
    resolved = states["filled"] + states["expired"]
    active = [
        o for o in openings if o.state in ("open", "booking", "needs_review", "no_candidates")
    ]
    recent = sorted(
        (o for o in openings if o not in active), key=lambda o: o.detected_at, reverse=True
    )[:20]
    return {
        "settings": {
            "lead_minutes": settings.backfill_lead_minutes,
            "horizon_days": settings.backfill_horizon_days,
            "contact_hours": settings.contact_hours,
            "enabled": settings.backfill_enabled,
        },
        "metrics": {
            "states": dict(states),
            "fill_rate": round(states["filled"] / resolved, 2) if resolved else None,
            "median_minutes_to_fill": round(waits[len(waits) // 2]) if waits else None,
            "visit_minutes_recovered": sum(o.minutes for o in filled),
        },
        "active": [
            backfill.opening_view(o, offers.get(o.id, []), labels, settings) for o in active
        ],
        "recent": [
            backfill.opening_view(o, offers.get(o.id, []), labels, settings) for o in recent
        ],
    }


@router.post("/backfill/scan")
async def backfill_scan(
    request: Request,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, practice=practice)
    expired = await backfill.expire(session, conn, settings)
    opened = await backfill.detect(session, conn, settings, _transport(request))
    await session.commit()
    return {"opened": opened, "expired": expired}


class OfferOutcome(BaseModel):
    outcome: Literal["accepted", "declined", "no_answer"]


@router.post("/backfill/offers/{offer_id}")
async def backfill_respond(
    offer_id: str,
    body: OfferOutcome,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    offer = await session.get(BackfillOffer, offer_id)
    if offer is None or offer.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Offer not found.")
    opening = await backfill.respond(
        request.app.state.db,
        session,
        offer,
        body.outcome,
        principal.user_id,
        settings,
        _transport(request),
    )
    conn = await session.get(PayerConnection, opening.connection_id)
    offers = list(
        await session.scalars(select(BackfillOffer).where(BackfillOffer.opening_id == opening.id))
    )
    return backfill.opening_view(opening, offers, await _labels(session, conn, settings), settings)


@router.get("/patients/{patient_id}/contact")
async def patient_contact(
    patient_id: str,
    practice: str | None = None,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Reveal the name and phone needed to call a patient. Every reveal is audited."""
    conn = await _conn(session, principal, practice=practice)
    patient = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == patient_id,
        )
    )
    if patient is None:
        raise ApiError(404, "not_found", "Patient not found in the practice mirror.")
    pii = PhiBox(settings).open(patient.pii)
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="phi.contact_viewed",
            target=f"patient:{patient_id}",
            detail={"purpose": "backfill_outreach"},
        )
    )
    await session.commit()
    name = " ".join(x for x in (pii.get("first_name"), pii.get("last_name")) if x)
    return {"name": name or None, "phone": pii.get("phone")}


# --- CarePlix Connect: catalog and EHR emulation ------------------------------------------------


@router.get("/catalog")
async def catalog(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Every EHR profile the hub supports, and how this workspace's connections map onto it."""
    conns = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == principal.workspace_id,
                PayerConnection.kind.in_(EHR_KINDS),
            )
        )
    )
    connections = []
    for conn in conns:
        connector = connector_for(conn, settings)
        cap = conn.capability or {}
        connections.append(
            {
                **connector.view(),
                "env": (conn.auth_ref or {}).get("env"),
                "verified_at": conn.verified_at,
                "writes_confirmable": cap.get("writes_confirmable"),
                "support": {op: connector.support(op) for op in OPERATIONS},
            }
        )
    return {
        "operations": [{"id": op, "label": label} for op, label in OPERATIONS.items()],
        "profiles": [p.view() for p in PROFILES.values()],
        "connections": connections,
    }


class EmulationIn(BaseModel):
    profile: str | None = Field(default=None, max_length=60)


@router.put("/emulation")
async def set_emulation(
    body: EmulationIn,
    practice: str | None = None,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Run the OpenEMR test clinic as another EHR (its operations and confirmation timing)."""
    conn = await _conn(session, principal, OPENEMR, practice)
    if body.profile is not None and (
        body.profile not in PROFILES or PROFILES[body.profile].path != "nexhealth"
    ):
        raise ApiError(422, "unknown_profile", "Choose one of the catalog's NexHealth EHRs.")
    ref = dict(conn.auth_ref or {})
    if body.profile:
        ref["emulate"] = body.profile
    else:
        ref.pop("emulate", None)
    conn.auth_ref = ref
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="connect.emulation_set",
            target=conn.id,
            detail={"profile": body.profile},
        )
    )
    await session.commit()
    connector = connector_for(conn, settings)
    return {
        **connector.view(),
        "support": {op: connector.support(op) for op in OPERATIONS},
    }


# --- FO-3 receptionist: messages for the front desk ---------------------------------------------


async def _message(
    session: AsyncSession, principal: Principal, message_id: str
) -> FrontDeskMessage:
    message = await session.get(FrontDeskMessage, message_id)
    if message is None or message.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Message not found.")
    return message


async def _patient_initials(
    session: AsyncSession, messages: list[FrontDeskMessage], settings: Settings
) -> dict[str, str]:
    refs = {(m.connection_id, m.patient_ref) for m in messages if m.patient_ref}
    if not refs:
        return {}
    box = PhiBox(settings)
    out: dict[str, str] = {}
    for rec in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.resource == "patient",
            EhrRecord.connection_id.in_({c for c, _ in refs}),
            EhrRecord.source_id.in_({p for _, p in refs}),
        )
    ):
        out[rec.source_id] = initials(box.open(rec.pii))
    return out


@router.get("/messages")
async def list_messages(
    state: Literal["open", "done", "all"] = "open",
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The queue shows topic, initials and whether a callback is wanted; text opens on demand."""
    query = select(FrontDeskMessage).where(FrontDeskMessage.workspace_id == principal.workspace_id)
    if state != "all":
        query = query.where(FrontDeskMessage.state == state)
    rows = list(
        await session.scalars(query.order_by(FrontDeskMessage.created_at.desc()).limit(100))
    )
    labels = await _patient_initials(session, rows, settings)
    box = PhiBox(settings)
    out = []
    for m in rows:
        view = receptionist.message_view(m, box, labels)
        for sealed in ("message", "caller_name", "caller_phone"):
            view[sealed] = None
        out.append(view)
    return out


@router.get("/messages/{message_id}")
async def open_message(
    message_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Reveal a message's text and callback details. Every reveal is audited."""
    message = await _message(session, principal, message_id)
    labels = await _patient_initials(session, [message], settings)
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="phi.message_viewed",
            target=f"front_desk_message:{message.id}",
            detail={"topic": message.topic},
        )
    )
    await session.commit()
    return receptionist.message_view(message, PhiBox(settings), labels)


@router.post("/messages/{message_id}/done")
async def message_done(
    message_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    message = await _message(session, principal, message_id)
    if message.state != "done":
        message.state, message.handled_by, message.handled_at = "done", principal.user_id, utcnow()
        session.add(
            AuditEvent(
                workspace_id=principal.workspace_id,
                actor=principal.user_id,
                action="front_desk_message.done",
                target=message.id,
                detail={"topic": message.topic},
            )
        )
        await session.commit()
    labels = await _patient_initials(session, [message], settings)
    view = receptionist.message_view(message, PhiBox(settings), labels)
    for sealed in ("message", "caller_name", "caller_phone"):
        view[sealed] = None
    return view
