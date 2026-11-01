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
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import (
    AuditEvent,
    EhrRecord,
    EligibilityCheck,
    PayerConnection,
    SyncCursor,
    WebhookEvent,
    WriteIntent,
)
from careplix_api.phi import PhiBox, initials
from careplix_api.practice import eligibility, intents, sync, webhooks
from careplix_api.practice.connections import CLEARINGHOUSE, EHR, verify
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter(prefix="/practices")


def _transport(request: Request):
    return getattr(request.app.state, "integration_transport", None)


async def _conn(session: AsyncSession, principal: Principal, kind: str = EHR) -> PayerConnection:
    conn = await session.scalar(
        select(PayerConnection).where(
            PayerConnection.workspace_id == principal.workspace_id, PayerConnection.kind == kind
        )
    )
    if conn is None:
        what = "NexHealth" if kind == EHR else "Stedi"
        script = "connect-nexhealth" if kind == EHR else "connect-stedi"
        raise ApiError(
            404,
            "not_connected",
            f"{what} is not connected. Run make {script}, then restart the API.",
        )
    return conn


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
        },
    }


@router.get("")
async def overview(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    ws = principal.workspace_id
    conns = {
        c.kind: c
        for c in await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == ws, PayerConnection.kind.in_((EHR, CLEARINGHOUSE))
            )
        )
    }
    ehr = conns.get(EHR)
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
                    .where(EligibilityCheck.workspace_id == ws)
                    .group_by(EligibilityCheck.state)
                )
            ).all()
        )
    )
    coverage = Counter(
        (c.summary or {}).get("coverage")
        for c in await session.scalars(
            select(EligibilityCheck).where(
                EligibilityCheck.workspace_id == ws, EligibilityCheck.state == "done"
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
            .where(WriteIntent.workspace_id == ws)
            .order_by(WriteIntent.created_at.desc())
            .limit(30)
        )
    )
    return {
        "ehr": _conn_view(ehr),
        "clearinghouse": _conn_view(conns.get(CLEARINGHOUSE)),
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
    kind: Literal["nexhealth", "stedi"] = "nexhealth",
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal, kind)
    result = await verify(session, conn, settings, _transport(request))
    await session.commit()
    return result


@router.post("/sync")
async def sync_now(
    request: Request,
    full: bool = False,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal)
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
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal)
    result = await webhooks.register(session, conn, settings, _transport(request))
    await session.commit()
    return result


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
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The front desk's day: each visit with its insurance, eligibility result and flags."""
    conn = await _conn(session, principal)
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
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal)
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
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _conn(session, principal)
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
