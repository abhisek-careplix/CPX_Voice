"""FO-1 eligibility autopilot: verify coverage for every upcoming visit before the patient arrives.

Schedule (FRONT_OFFICE_AUTOMATION_PLAN B1): once when the appointment first appears ("booking"),
again inside 72 h and inside 24 h. A result for the same coverage from the last 24 h is reused,
so a patient with several visits costs one payer transaction.

Test mode: Stedi test keys only answer for Stedi's published mock member, so each request is
built from the real EHR coverage (plan, payer, relationship) but sent as the mock member, and
the result is labelled ``mock``. No patient identifier leaves CarePlix in test mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, as_utc, utcnow
from careplix_api.integrations import stedi
from careplix_api.models import EhrRecord, EligibilityCheck, PayerConnection
from careplix_api.phi import PhiBox
from careplix_api.practice import intents
from careplix_api.practice.connections import CLEARINGHOUSE, audit, is_test, stedi_client
from careplix_api.settings import Settings

HORIZON = timedelta(days=30)
REUSE_WINDOW = timedelta(hours=24)
SCHEDULED = ("booked", "confirmed")
SELF = {"self", "subscriber", "18", "insured"}


def due_trigger(lead: timedelta, done: set[str]) -> str | None:
    if not done:
        return (
            "t24"
            if lead <= timedelta(hours=24)
            else "t72"
            if lead <= timedelta(hours=72)
            else "booking"
        )
    if lead <= timedelta(hours=24) and "t24" not in done:
        return "t24"
    if lead <= timedelta(hours=72) and not done & {"t72", "t24"}:
        return "t72"
    return None


async def schedule(
    session: AsyncSession, conn: PayerConnection, now: datetime | None = None, limit: int = 500
) -> int:
    """Queue the checks that are due for this location's upcoming appointments."""
    now = now or utcnow()
    appointments = list(
        await session.scalars(
            select(EhrRecord)
            .where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "appointment",
                EhrRecord.status.in_(SCHEDULED),
                EhrRecord.starts_at >= now,
                EhrRecord.starts_at <= now + HORIZON,
            )
            .order_by(EhrRecord.starts_at)
            .limit(limit)
        )
    )
    if not appointments:
        return 0
    triggers: dict[str, set[str]] = {}
    for check in await session.scalars(
        select(EligibilityCheck).where(
            EligibilityCheck.ehr_connection_id == conn.id,
            EligibilityCheck.appointment_ref.in_([a.source_id for a in appointments]),
        )
    ):
        triggers.setdefault(check.appointment_ref, set()).add(check.trigger)
    queued = 0
    for appt in appointments:
        trigger = due_trigger(as_utc(appt.starts_at) - now, triggers.get(appt.source_id, set()))
        if trigger:
            session.add(
                EligibilityCheck(
                    workspace_id=conn.workspace_id,
                    ehr_connection_id=conn.id,
                    appointment_ref=appt.source_id,
                    patient_ref=appt.patient_ref,
                    trigger=trigger,
                )
            )
            queued += 1
    await session.flush()
    return queued


async def _record(
    session: AsyncSession, conn_id: str, resource: str, source_id: str | None
) -> EhrRecord | None:
    if not source_id:
        return None
    return await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn_id,
            EhrRecord.resource == resource,
            EhrRecord.source_id == source_id,
        )
    )


async def primary_coverage(
    session: AsyncSession, conn_id: str, patient_ref: str | None
) -> EhrRecord | None:
    if not patient_ref:
        return None
    coverages = list(
        await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn_id,
                EhrRecord.resource == "coverage",
                EhrRecord.patient_ref == patient_ref,
                EhrRecord.status == "active",
            )
        )
    )

    def rank(c: EhrRecord) -> tuple[int, int]:
        medical = 0 if (c.attrs.get("insurance_type") or "medical") == "medical" else 1
        priority = c.attrs.get("priority")
        return medical, priority if isinstance(priority, int) else 99

    return min(coverages, key=rank, default=None)


def _finish(
    check: EligibilityCheck,
    state: str,
    reason: str | None = None,
    summary: dict[str, Any] | None = None,
) -> EligibilityCheck:
    check.state, check.reason, check.completed_at = state, reason, utcnow()
    if summary is not None:
        check.summary = summary
    return check


async def run_check(
    session: AsyncSession,
    check: EligibilityCheck,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EligibilityCheck:
    conn_id = check.ehr_connection_id
    appt = await _record(session, conn_id, "appointment", check.appointment_ref)
    if appt is None or appt.status not in SCHEDULED:
        return _finish(check, "skipped", "Appointment is no longer scheduled.")
    coverage = await primary_coverage(session, conn_id, appt.patient_ref)
    if coverage is None:
        return _finish(
            check, "skipped", "No active insurance on file.", {"coverage": "none_on_file"}
        )
    check.coverage_ref = coverage.source_id
    plan = await _record(session, conn_id, "insurance_plan", coverage.attrs.get("plan_id"))
    plan_name = (plan.attrs.get("name") if plan else None) or "Unknown plan"
    check.payer_id = (plan.attrs.get("payer_id") if plan else None) or None

    since = utcnow() - REUSE_WINDOW
    recent = await session.scalar(
        select(EligibilityCheck)
        .where(
            EligibilityCheck.ehr_connection_id == conn_id,
            EligibilityCheck.coverage_ref == coverage.source_id,
            EligibilityCheck.state == "done",
            EligibilityCheck.reused_from.is_(None),
            EligibilityCheck.completed_at >= since,
        )
        .order_by(EligibilityCheck.completed_at.desc())
        .limit(1)
    )
    if recent is not None and recent.id != check.id:
        check.reused_from, check.mock, check.stedi_ref = recent.id, recent.mock, recent.stedi_ref
        return _finish(check, "done", "Reused a result from the last 24 hours.", recent.summary)

    stedi_conn = await session.scalar(
        select(PayerConnection).where(
            PayerConnection.workspace_id == check.workspace_id,
            PayerConnection.kind == CLEARINGHOUSE,
            PayerConnection.enabled.is_(True),
        )
    )
    if stedi_conn is None:
        return _finish(check, "skipped", "No clearinghouse connected.")
    check.clearinghouse_connection_id = stedi_conn.id
    box = PhiBox(settings)

    if is_test(stedi_conn):
        check.mock = True
        request = stedi.eligibility_request(
            payer_id=stedi.MOCK_PAYER_ID,
            provider_npi=stedi.MOCK_PROVIDER_NPI,
            provider_name=settings.practice_name,
            subscriber=stedi.MOCK_SUBSCRIBER,
        )
    else:
        relation = str(coverage.attrs.get("subscription_relation") or "self").lower()
        if relation not in SELF:
            return _finish(
                check, "skipped", "Dependent coverage: the subscriber's details are not in the EHR."
            )
        patient = await _record(session, conn_id, "patient", appt.patient_ref)
        pii = box.open(patient.pii if patient else None)
        member = box.open(coverage.pii).get("member_id")
        provider = await _record(session, conn_id, "provider", appt.attrs.get("provider_id"))
        npi = settings.practice_npi or (provider.attrs.get("npi") if provider else None)
        missing = [
            name
            for name, value in (
                ("payer ID", check.payer_id),
                ("member ID", member),
                ("first name", pii.get("first_name")),
                ("last name", pii.get("last_name")),
                ("date of birth", pii.get("date_of_birth")),
                ("provider NPI", npi),
            )
            if not value
        ]
        if missing:
            return _finish(check, "skipped", "Missing " + ", ".join(missing) + ".")
        request = stedi.eligibility_request(
            payer_id=check.payer_id,
            provider_npi=npi,
            provider_name=settings.practice_name,
            subscriber={
                "first_name": pii["first_name"],
                "last_name": pii["last_name"],
                "date_of_birth": pii["date_of_birth"],
                "member_id": member,
            },
        )

    client = stedi_client(stedi_conn, settings, transport)
    try:
        response, ms = await client.eligibility(request)
    except stedi.StediError as err:
        call = await audit(
            session,
            stedi_conn,
            "stedi.eligibility",
            check.trigger,
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
        )
        check.integration_call_id = call.id
        return _finish(check, "failed", err.message[:200])
    finally:
        await client.aclose()
    summary = {**stedi.summarize_eligibility(response), "ehr_plan": plan_name, "mock": check.mock}
    call = await audit(
        session,
        stedi_conn,
        "stedi.eligibility",
        check.trigger,
        ok=True,
        status="success",
        duration_ms=ms,
        summary={"coverage": summary["coverage"], "mock": check.mock},
    )
    check.integration_call_id, check.stedi_ref = call.id, response.get("id")
    _finish(check, "done", None, summary)

    if summary["coverage"] == "inactive" and not check.mock:
        ehr = await session.get(PayerConnection, conn_id)
        await intents.propose(
            session,
            ehr,
            settings,
            kind="patient_alert",
            target_source_id=appt.patient_ref,
            payload={
                "note": f"CarePlix: the payer reported {plan_name} coverage inactive on "
                f"{utcnow():%Y-%m-%d}. Please confirm insurance with the patient before the visit."
            },
            reason="Eligibility: coverage reported inactive",
            source="eligibility",
            requested_by="system:eligibility",
        )
    return check


async def run_pending(
    db: Database,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    limit: int = 10,
) -> int:
    async with db.sessions() as session:
        ids = list(
            await session.scalars(
                select(EligibilityCheck.id)
                .where(EligibilityCheck.state == "pending")
                .order_by(EligibilityCheck.created_at)
                .limit(limit)
            )
        )
    for check_id in ids:
        async with db.sessions() as session:
            check = await session.get(EligibilityCheck, check_id)
            if check is not None and check.state == "pending":
                await run_check(session, check, settings, transport)
                await session.commit()
    return len(ids)


def view(check: EligibilityCheck | None) -> dict[str, Any] | None:
    if check is None:
        return None
    return {
        "id": check.id,
        "trigger": check.trigger,
        "state": check.state,
        "mock": check.mock,
        "summary": check.summary,
        "reason": check.reason,
        "reused": bool(check.reused_from),
        "payer_id": check.payer_id,
        "created_at": check.created_at,
        "completed_at": check.completed_at,
    }
