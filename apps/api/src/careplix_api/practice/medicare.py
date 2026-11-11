"""Medicare benefits planner: one detailed CMS (HETS) check for a patient the practice is seeing,
stored sealed and reused for the day.

HETS Rules of Behavior (CMS, v2.0) shape every guardrail here:

- Original Medicare fee-for-service only, for a specific patient being treated or scheduled: a
  check needs the patient plus an upcoming appointment, or an explicit "planning for an upcoming
  visit" reason. A Medicare Advantage plan is never sent to CMS.
- No blanket or batch sweeps: one detailed check per patient per day (HETS refreshes daily; the
  day's snapshot is reused), a few attempts a day when CMS errors, and a per-user hourly cap.
- Every check is audited (an :class:`AuditEvent` per request and an ``IntegrationCall`` per
  Stedi call) and carries the requester's IP to Stedi in ``X-Forwarded-For``.
- AAA 41 ("CMS attestation required") and 97 (suspended attestation) are shown as such.

Two requests per patient, without dates (CMS answers for today): the STC list, then the
preventive HCPCS list. A list CMS rejects as a request problem is retried in smaller groups.

Test mode: Stedi's CMS mock answers only its published request (STC 30), so that is all that goes
out; the result is labelled test data. A bundled synthetic 271 built from the HETS Companion
Guide's Appendix B can be loaded instead (``sample``), without calling Stedi.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.integrations import medicare_benefits as mb
from careplix_api.integrations import stedi
from careplix_api.models import (
    AuditEvent,
    EhrRecord,
    MedicareBenefitSnapshot,
    PayerConnection,
    PayerMapping,
)
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility, medicare_planner, payers
from careplix_api.practice.connections import audit, is_test, stedi_client
from careplix_api.settings import Settings

PURPOSES = ("appointment", "upcoming_visit")
SAMPLE = (
    Path(__file__).resolve().parent.parent / "integrations" / "samples" / "cms_hets_sample.json"
)
# Patient or provider problems: asking again with other services won't help.
STOP = {
    "41",
    "97",
    "43",
    "44",
    "50",
    "51",
    "58",
    "64",
    "65",
    "67",
    "68",
    "71",
    "72",
    "73",
    "75",
    "76",
}


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@cache
def sample_body() -> dict[str, Any]:
    return json.loads(SAMPLE.read_text())["body"]


def _zone(appt: EhrRecord | None) -> ZoneInfo:
    try:
        return ZoneInfo((appt.attrs.get("timezone") if appt else None) or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _record(
    session: AsyncSession, conn: PayerConnection, resource: str, source_id: str | None
) -> EhrRecord | None:
    if not source_id:
        return None
    return await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == resource,
            EhrRecord.source_id == source_id,
        )
    )


async def medicare_coverage(
    session: AsyncSession, conn: PayerConnection, patient_ref: str
) -> tuple[EhrRecord | None, str, str | None]:
    """(coverage, plan kind, plan name) for the patient's Medicare coverage: Original Medicare
    first; a Medicare Advantage plan is returned so the caller can refuse it."""
    found: list[tuple[int, EhrRecord, str, str | None]] = []
    for coverage in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "coverage",
            EhrRecord.patient_ref == patient_ref,
            EhrRecord.status == "active",
        )
    ):
        plan = await _record(session, conn, "insurance_plan", coverage.attrs.get("plan_id"))
        name = plan.attrs.get("name") if plan else None
        payer_id = plan.attrs.get("payer_id") if plan else None
        kind = payers.plan_kind(name, payer_id)
        mapping = await session.scalar(
            select(PayerMapping).where(
                PayerMapping.workspace_id == conn.workspace_id,
                PayerMapping.source_key == payers.source_key(payer_id, name),
            )
        )
        if mapping is not None and mapping.plan_kind == payers.MEDICARE_ADVANTAGE:
            kind = payers.MEDICARE_ADVANTAGE
        rank = {payers.MEDICARE: 0, payers.MEDICARE_ADVANTAGE: 1}.get(kind, 9)
        found.append((rank, coverage, kind, name))
    if not found:
        return None, "none", None
    rank, coverage, kind, name = min(found, key=lambda x: x[0])
    return coverage, kind, name


async def _purpose(
    session: AsyncSession,
    conn: PayerConnection,
    patient_ref: str,
    appointment_id: str | None,
    reason: str | None,
) -> tuple[str, EhrRecord | None]:
    if appointment_id:
        appt = await _record(session, conn, "appointment", appointment_id)
        if appt is None or appt.patient_ref != patient_ref:
            raise Refused(404, "not_found", "That appointment isn't this patient's.")
        if appt.status not in eligibility.SCHEDULED or (
            appt.starts_at and as_utc(appt.starts_at) < utcnow() - timedelta(days=1)
        ):
            raise Refused(
                422,
                "appointment_not_upcoming",
                "Medicare benefits are checked for upcoming visits.",
            )
        return "appointment", appt
    if reason == "upcoming_visit":
        return "upcoming_visit", None
    raise Refused(
        422,
        "purpose_required",
        "CMS allows checks only for a patient being treated or scheduled: pick the appointment, "
        "or confirm you're planning an upcoming visit.",
    )


async def latest(
    session: AsyncSession, conn: PayerConnection, patient_ref: str, mode: str | None = None
) -> MedicareBenefitSnapshot | None:
    query = select(MedicareBenefitSnapshot).where(
        MedicareBenefitSnapshot.ehr_connection_id == conn.id,
        MedicareBenefitSnapshot.patient_ref == patient_ref,
    )
    if mode:
        query = query.where(MedicareBenefitSnapshot.mode == mode)
    return await session.scalar(query.order_by(MedicareBenefitSnapshot.created_at.desc()).limit(1))


def _audit(
    session: AsyncSession, conn: PayerConnection, user: str, patient_ref: str, **detail: Any
) -> None:
    session.add(
        AuditEvent(
            workspace_id=conn.workspace_id,
            actor=user,
            action="medicare.benefits_check",
            target=f"patient:{patient_ref}"[:80],
            detail=detail,
        )
    )


async def _send(
    client: stedi.StediClient,
    request: dict[str, Any],
    forwarded_for: str | None,
) -> tuple[dict[str, Any] | None, int, stedi.StediError | None]:
    try:
        body, ms = await client.eligibility(request, forwarded_for=forwarded_for)
        return body, ms, None
    except stedi.StediError as err:
        if err.status != "busy":
            return None, 0, err
    try:  # Stedi's pool was full: one immediate retry
        body, ms = await client.eligibility(request, forwarded_for=forwarded_for)
        return body, ms, None
    except stedi.StediError as err:
        return None, 0, err


async def refresh(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
    *,
    user: str,
    patient_ref: str,
    appointment_id: str | None = None,
    reason: str | None = None,
    forwarded_for: str | None = None,
    sample: bool = False,
) -> tuple[MedicareBenefitSnapshot, bool]:
    """(snapshot, reused). Raises :class:`Refused` when a guardrail says no; refusals are
    audited too."""
    patient = await _record(session, conn, "patient", patient_ref)
    if patient is None:
        raise Refused(404, "not_found", "Patient not found in the practice mirror.")
    purpose, appt = await _purpose(session, conn, patient_ref, appointment_id, reason)
    clearinghouse = await eligibility.clearinghouse(session, conn.workspace_id)
    if clearinghouse is None:
        raise Refused(409, "no_clearinghouse", "Connect Stedi to check Medicare benefits.")
    test = is_test(clearinghouse)
    mode = stedi.TEST if test else stedi.PRODUCTION
    coverage, kind, plan_name = await medicare_coverage(session, conn, patient_ref)

    def refuse(status: int, code: str, message: str) -> Refused:
        _audit(session, conn, user, patient_ref, purpose=purpose, mode=mode, outcome=code)
        return Refused(status, code, message)

    if kind == payers.MEDICARE_ADVANTAGE:
        raise refuse(
            422,
            "medicare_advantage",
            f"Medicare Advantage: check benefits with {plan_name or 'the plan'}. CMS is never "
            "asked about Medicare Advantage patients.",
        )
    if sample and not test:
        raise refuse(422, "sample_test_only", "The synthetic sample is for test mode only.")
    if kind != payers.MEDICARE and not test:
        raise refuse(
            422,
            "not_medicare",
            "No Original Medicare coverage on file for this patient. The benefits planner is "
            "for Medicare fee-for-service patients.",
        )

    tz = _zone(appt)
    today = utcnow().astimezone(tz).date()
    visit = as_utc(appt.starts_at).astimezone(tz).date() if appt and appt.starts_at else None
    dos = visit if visit and visit > today else today

    if not sample:
        day = _day_start(utcnow())
        today_rows = list(
            await session.scalars(
                select(MedicareBenefitSnapshot).where(
                    MedicareBenefitSnapshot.ehr_connection_id == conn.id,
                    MedicareBenefitSnapshot.patient_ref == patient_ref,
                    MedicareBenefitSnapshot.mode == mode,
                    MedicareBenefitSnapshot.source == "stedi",
                    MedicareBenefitSnapshot.created_at >= day,
                )
            )
        )
        done = sorted((r for r in today_rows if r.state == "done"), key=lambda r: r.created_at)
        if done:
            _audit(
                session,
                conn,
                user,
                patient_ref,
                purpose=purpose,
                appointment=appointment_id,
                mode=mode,
                outcome="reused",
                snapshot=done[-1].id,
            )
            return done[-1], True
        if len(today_rows) >= settings.medicare_benefits_daily_attempts:
            raise refuse(
                429,
                "daily_limit",
                "Medicare was already asked about this patient several times today. HETS "
                "refreshes daily: try again tomorrow.",
            )
        hourly = await session.scalar(
            select(func.count())
            .select_from(MedicareBenefitSnapshot)
            .where(
                MedicareBenefitSnapshot.workspace_id == conn.workspace_id,
                MedicareBenefitSnapshot.requested_by == user,
                MedicareBenefitSnapshot.source == "stedi",
                MedicareBenefitSnapshot.created_at >= utcnow() - timedelta(hours=1),
            )
        )
        if hourly >= settings.medicare_benefits_hourly_per_user:
            raise refuse(
                429,
                "sweep_guard",
                "Too many Medicare checks this hour. CMS forbids batch sweeps: check patients "
                "as their visits come up.",
            )

    box = PhiBox(settings)
    snapshot = MedicareBenefitSnapshot(
        workspace_id=conn.workspace_id,
        ehr_connection_id=conn.id,
        clearinghouse_connection_id=clearinghouse.id,
        patient_ref=patient_ref,
        appointment_ref=appointment_id,
        purpose=purpose,
        mode=mode,
        source="sample" if sample else "stedi",
        state="done",
        date_of_service=dos.isoformat(),
        requested_by=user,
    )
    responses: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    raised: list[dict[str, Any]] = []  # AAA errors on 400/429 bodies, not in any response
    stop: str | None = None
    npi: str | None = None
    requested: list[str] = []

    if sample:
        body = sample_body()
        responses.append(body)
        groups.append(
            {"system": "SAMPLE", "services": [], "outcome": "ok", "label": "Synthetic sample"}
        )
        requested = list(mb.HCPCS_SERVICES)
    else:
        client = stedi_client(clearinghouse, settings, transport)
        try:
            if test:
                plan: list[tuple[dict[str, Any], dict[str, Any]]] = [
                    ({"system": "STC", "services": ["30"]}, stedi.mock_request("cms_active"))
                ]
            else:
                npi = await eligibility.billing_npi(session, conn.workspace_id, settings)
                if npi is None:
                    raise refuse(
                        422,
                        "billing_npi",
                        "Set the practice's billing NPI (Practice → Eligibility) first.",
                    )
                pii = box.open(patient.pii)
                member_id = box.open(coverage.pii if coverage else None).get("member_id")
                person = {k: pii.get(k) for k in ("first_name", "last_name", "date_of_birth")}
                missing = [k.replace("_", " ") for k, v in person.items() if not v]
                if not member_id:
                    missing.insert(0, "Medicare number (MBI)")
                if missing:
                    raise refuse(422, "missing_details", "Missing " + ", ".join(missing) + ".")
                try:
                    stedi.normalize_mbi(member_id)
                except stedi.InvalidMbi as err:
                    raise refuse(422, err.code, err.message) from None
                subscriber = {**person, "member_id": member_id}

                def build(group: dict[str, Any]) -> dict[str, Any]:
                    return stedi.patient_request(
                        payer_id=stedi.CMS,
                        provider_npi=npi,
                        subscriber=subscriber,
                        services=[
                            {"system": group["system"], "value": c} for c in group["services"]
                        ],
                    )

                plan = [
                    (g, build(g)) for g in mb.request_groups(settings.medicare_benefits_group_size)
                ]
            queue = list(plan)
            while queue and stop is None:
                group, request = queue.pop(0)
                body, ms, err = await _send(client, request, forwarded_for)
                found = stedi.parse_errors(body) if body is not None else err.errors
                codes = {e["code"] for e in found}
                outcome = (
                    "ok"
                    if body is not None and not found
                    else stedi.classify(
                        found, err.http_status if err else None, err.status if err else None
                    )
                    or stedi.CORRECT
                )
                call = await audit(
                    session,
                    clearinghouse,
                    "stedi.medicare_benefits",
                    "manual",
                    ok=outcome == "ok",
                    status="success" if outcome == "ok" else (err.status if err else "error"),
                    http_status=err.http_status if err else None,
                    duration_ms=ms,
                    error=err.message if err else None,
                    summary={
                        "group": mb.group_label(group),
                        "errors": sorted(codes),
                        "mock": test,
                    },
                )
                groups.append(
                    {
                        **group,
                        "label": mb.group_label(group),
                        "outcome": outcome,
                        "errors": sorted(codes),
                        "integration_call_id": call.id,
                    }
                )
                if body is not None:
                    responses.append(body)
                    if group["system"] == "HCPCS":
                        requested += group["services"]
                else:
                    raised += found
                errors += found
                if outcome == "ok":
                    continue
                if codes & STOP:
                    stop = "patient_or_provider"
                elif outcome == stedi.RETRY:
                    stop = "retry"
                elif not test and len(group["services"]) > 1:
                    # A request CMS couldn't take as a whole: retry it in smaller groups.
                    smaller = mb.split(group, settings.medicare_benefits_fallback_size)
                    queue[:0] = [(g, build(g)) for g in smaller]
        finally:
            await client.aclose()

    benefits = mb.parse(
        responses,
        date_of_service=dos,
        today=today,
        practice_npi=npi,
        requested_hcpcs=requested,
        x12=[r.get("x12") for r in responses if r.get("x12")],
    )
    if raised:
        benefits["error_details"] += raised
        benefits["errors"] += [e["code"] for e in raised]
        benefits["message"] = stedi.error_message(benefits["error_details"], stedi.CMS)
    got_benefits = any(g["outcome"] == "ok" for g in groups)
    if stop == "retry" and not got_benefits:
        benefits["message"] = "CMS isn't answering right now. Try again later."
    snapshot.state = "done" if got_benefits else "failed"
    snapshot.message = (benefits["message"] or "")[:200] or None
    snapshot.request_groups = groups
    snapshot.benefits = box.seal({"benefits": benefits})
    snapshot.raw = box.seal({"responses": responses}) if not sample else None
    blockers = [i["key"] for i in benefits["items"] if i.get("blocker")]
    snapshot.summary = {
        "coverage": benefits["coverage"],
        "part_a": (benefits["entitlement"].get("part_a") or {}).get("covered"),
        "part_b": (benefits["entitlement"].get("part_b") or {}).get("covered"),
        "medicare_advantage": bool(benefits["routing"]["medicare_advantage"]),
        "qmb": bool((benefits["routing"]["qmb"] or {}).get("active")),
        "blockers": len(blockers),
        "items": len(benefits["items"]),
        "errors": sorted(set(benefits["errors"])),
        "mock": test,
        "plan_kind": kind,
    }
    session.add(snapshot)
    await session.flush()
    if npi:
        await payers.note_outcome(
            session, conn.workspace_id, npi, stedi.CMS, errors, accepted=got_benefits
        )
    _audit(
        session,
        conn,
        user,
        patient_ref,
        purpose=purpose,
        appointment=appointment_id,
        mode=mode,
        source=snapshot.source,
        outcome=snapshot.state,
        snapshot=snapshot.id,
        groups=[g["label"] for g in groups],
        errors=snapshot.summary["errors"],
    )
    return snapshot, False


def view(
    snapshot: MedicareBenefitSnapshot | None,
    settings: Settings,
    *,
    reused: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "snapshot": None,
            "benefits": None,
            "plan": medicare_planner.suggestions(None, today=today or date.today()),
        }
    benefits = PhiBox(settings).open(snapshot.benefits).get("benefits")
    visit = date.fromisoformat(snapshot.date_of_service) if snapshot.date_of_service else None
    today = today or utcnow().date()
    return {
        "snapshot": {
            "id": snapshot.id,
            "state": snapshot.state,
            "mode": snapshot.mode,
            "source": snapshot.source,
            "test_data": snapshot.mode == stedi.TEST,
            "purpose": snapshot.purpose,
            "appointment_id": snapshot.appointment_ref,
            "date_of_service": snapshot.date_of_service,
            "fetched_at": snapshot.created_at,
            "message": snapshot.message,
            "groups": [
                {k: g.get(k) for k in ("label", "system", "outcome", "errors")}
                for g in snapshot.request_groups or []
            ],
            "summary": snapshot.summary,
            "reused": reused,
        },
        "benefits": benefits,
        "plan": medicare_planner.suggestions(benefits, today=today, visit=visit),
    }
