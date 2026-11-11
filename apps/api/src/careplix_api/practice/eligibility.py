"""FO-1 eligibility autopilot: verify coverage for every upcoming visit before the patient arrives.

Schedule (FRONT_OFFICE_AUTOMATION_PLAN B1): once when the appointment first appears ("booking"),
again inside 72 h and inside 24 h. An active or inactive answer for the same coverage from the
last 24 h is reused, so a patient with several visits costs one payer transaction; errors are
never reused.

Each check goes to the Stedi payer the EHR plan maps to (:mod:`careplix_api.practice.payers`):
Medicare fee-for-service to CMS with the patient's MBI, Medicare Advantage to the plan itself.
Payer connectivity errors (AAA 42/80, HTTP 429, timeouts) put the check back in the queue with a
growing wait (1 minute up to 30, for 8 hours); other errors need a person to correct the data.

Test mode: Stedi test keys only answer for Stedi's published mock members, so each request is
built from the real EHR coverage (plan, payer, relationship) but sent as a mock member: the
workspace's chosen scenario, or (``auto``) the one matching the plan. The result is labelled
``mock``. No patient identifier leaves CarePlix in test mode; the client refuses it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, as_utc, utcnow
from careplix_api.integrations import stedi
from careplix_api.models import (
    EhrRecord,
    EligibilityCheck,
    PayerConnection,
    PayerMapping,
    Workspace,
)
from careplix_api.phi import PhiBox
from careplix_api.practice import intents, payers
from careplix_api.practice.connections import CLEARINGHOUSE, audit, is_test, stedi_client
from careplix_api.settings import Settings

HORIZON = timedelta(days=30)
REUSE_WINDOW = timedelta(hours=24)
RETRY_WINDOW = timedelta(hours=8)  # Stedi: keep retrying connectivity errors for 8 hours
FIRST_RETRY, MAX_RETRY_WAIT = timedelta(minutes=1), timedelta(minutes=30)
SCHEDULED = ("booked", "confirmed")
SELF = {"self", "subscriber", "18", "insured"}
REUSABLE = ("active", "inactive")
RELATION = {
    "spouse": "SPOUSE",
    "01": "SPOUSE",
    "child": "CHILD",
    "19": "CHILD",
    "son": "CHILD",
    "daughter": "CHILD",
    "dependent": "CHILD",
}
MOCK_BY_KIND = {
    payers.MEDICARE: "cms_active",
    payers.MEDICARE_ADVANTAGE: "humana_medicare_advantage",
}


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


# --- shared by the autopilot, the Studio's Check button and the voice tool ---------------------


async def clearinghouse(session: AsyncSession, workspace_id: str) -> PayerConnection | None:
    return await session.scalar(
        select(PayerConnection).where(
            PayerConnection.workspace_id == workspace_id,
            PayerConnection.kind == CLEARINGHOUSE,
            PayerConnection.enabled.is_(True),
        )
    )


async def billing_npi(session: AsyncSession, workspace_id: str, settings: Settings) -> str | None:
    """The workspace's billing NPI, else PRACTICE_NPI. Only a valid NPI is ever sent."""
    workspace = await session.get(Workspace, workspace_id)
    npi = (workspace.billing_npi if workspace else None) or settings.practice_npi
    return npi if stedi.valid_npi(npi) else None


async def mock_scenario(session: AsyncSession, workspace_id: str, kind: str) -> str:
    workspace = await session.get(Workspace, workspace_id)
    chosen = (workspace.stedi_test_scenario if workspace else None) or stedi.AUTO
    if chosen in stedi.MOCKS:
        return chosen
    return MOCK_BY_KIND.get(kind, "aetna_active")


def provider_pin(mapping: PayerMapping | None, box: PhiBox) -> str | None:
    return box.open(mapping.pin).get("pin") if mapping and mapping.pin else None


def _pick(values: dict[str, Any], *names: str) -> Any:
    for name in names:
        if values.get(name):
            return values[name]
    return None


def subscriber_identity(coverage_pii: dict[str, Any]) -> dict[str, Any]:
    """The policyholder's details on a coverage, under whatever names the EHR mirror uses
    (``subscriber_first_name`` or a nested ``subscriber`` object). Empty when the mirror has
    none: the EHR sync that adds them is separate work, so this reads them defensively."""
    nested = coverage_pii.get("subscriber")
    nested = nested if isinstance(nested, dict) else {}
    return {
        k: v
        for k, v in {
            "first_name": _pick(nested, "first_name", "firstName")
            or _pick(coverage_pii, "subscriber_first_name"),
            "last_name": _pick(nested, "last_name", "lastName")
            or _pick(coverage_pii, "subscriber_last_name"),
            "date_of_birth": _pick(nested, "date_of_birth", "dob", "dateOfBirth")
            or _pick(coverage_pii, "subscriber_date_of_birth", "subscriber_dob"),
            "member_id": _pick(nested, "member_id", "subscriber_num", "memberId")
            or _pick(coverage_pii, "subscriber_member_id", "subscriber_num"),
        }.items()
        if v
    }


def _norm_id(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def who_to_send(
    relation: str,
    patient: dict[str, Any],
    member_id: str | None,
    coverage_pii: dict[str, Any],
    medicaid: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """(subscriber, dependent, reason-if-not-possible), following Stedi's dependent rules: a
    patient with their own member ID is the subscriber, even when they're someone's child (that
    is the norm in Medicaid); only a patient known by the policyholder's ID is a dependent."""
    me = {
        "first_name": patient.get("first_name"),
        "last_name": patient.get("last_name"),
        "date_of_birth": patient.get("date_of_birth"),
    }
    if relation in SELF or medicaid:
        return {**me, "member_id": member_id}, None, None
    holder = subscriber_identity(coverage_pii)
    own_id = _norm_id(coverage_pii.get("patient_member_id") or coverage_pii.get("own_member_id"))
    if own_id:
        return {**me, "member_id": own_id}, None, None
    holder_id = _norm_id(holder.get("member_id"))
    if holder_id and member_id and holder_id != _norm_id(member_id):
        return {**me, "member_id": member_id}, None, None  # the patient's own number
    if not (holder.get("first_name") and holder.get("last_name") and (holder_id or member_id)):
        return None, None, "Dependent coverage: the subscriber's details are not in the EHR."
    subscriber = {**holder, "member_id": holder.get("member_id") or member_id}
    dependent = {**me, "relation": RELATION.get(relation, "OTHER_ADULT")}
    return subscriber, dependent, None


def _visit_date(appt: EhrRecord) -> tuple[date, date]:
    """The visit's date and today's date, both in the practice's time zone."""
    try:
        tz = ZoneInfo(appt.attrs.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return as_utc(appt.starts_at).astimezone(tz).date(), utcnow().astimezone(tz).date()


def _finish(
    check: EligibilityCheck,
    state: str,
    reason: str | None = None,
    summary: dict[str, Any] | None = None,
) -> EligibilityCheck:
    check.state, check.reason, check.completed_at = state, reason, utcnow()
    check.next_attempt_at = None
    if summary is not None:
        check.summary = summary
    return check


def _requeue(check: EligibilityCheck, reason: str, summary: dict[str, Any]) -> EligibilityCheck:
    """Payer connectivity: try again later (1, 2, 4 … 30 minutes) for up to 8 hours."""
    attempts = (check.attempts or 0) + 1
    if utcnow() - as_utc(check.created_at or utcnow()) >= RETRY_WINDOW:
        return _finish(check, "failed", f"{reason} Gave up after {attempts} attempts.", summary)
    wait = min(MAX_RETRY_WAIT, FIRST_RETRY * 2 ** (attempts - 1))
    check.attempts, check.state, check.reason = attempts, "pending", reason
    check.next_attempt_at, check.summary = utcnow() + wait, summary
    return check


def error_summary(
    errors: list[dict[str, Any]], outcome: str, payer_id: str | None, fallback: str
) -> dict[str, Any]:
    return {
        "coverage": "error",
        "errors": [e["code"] for e in errors],
        "error_details": errors,
        "error_class": outcome,
        "message": stedi.error_message(errors, payer_id) or fallback,
    }


async def _resolve_redirect(
    session: AsyncSession,
    client: stedi.StediClient,
    workspace_id: str,
    redirect: dict[str, Any] | None,
) -> None:
    """Find the Stedi payer for a Medicare Advantage plan or Medicaid MCO the payer pointed to, so
    the next check goes there (never back to CMS)."""
    if not redirect or redirect.get("payer_id") or not redirect.get("name"):
        return
    kind = payers.MEDICARE_ADVANTAGE if redirect["kind"] == "medicare_advantage" else None
    mapping = await payers.resolve(
        session, client, workspace_id, ehr_payer_id=None, name=redirect["name"], kind=kind
    )
    if mapping.state in payers.SETTLED and mapping.stedi_payer_id != stedi.CMS:
        redirect["payer_id"] = mapping.stedi_payer_id


async def run_check(
    session: AsyncSession,
    check: EligibilityCheck,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    forwarded_for: str | None = None,
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
    ehr_payer_id = (plan.attrs.get("payer_id") if plan else None) or None
    check.payer_id = ehr_payer_id and str(ehr_payer_id)[:20]

    stedi_conn = await clearinghouse(session, check.workspace_id)
    if stedi_conn is None:
        return _finish(check, "skipped", "No clearinghouse connected.")
    check.clearinghouse_connection_id = stedi_conn.id
    test = is_test(stedi_conn)

    # Reuse a definite answer (active or inactive) for this coverage; never an error, and never
    # across test and production.
    for recent in await session.scalars(
        select(EligibilityCheck)
        .where(
            EligibilityCheck.ehr_connection_id == conn_id,
            EligibilityCheck.coverage_ref == coverage.source_id,
            EligibilityCheck.state == "done",
            EligibilityCheck.reused_from.is_(None),
            EligibilityCheck.mock.is_(test),
            EligibilityCheck.completed_at >= utcnow() - REUSE_WINDOW,
            EligibilityCheck.id != check.id,
        )
        .order_by(EligibilityCheck.completed_at.desc())
        .limit(5)
    ):
        if (recent.summary or {}).get("coverage") in REUSABLE:
            check.reused_from, check.mock, check.stedi_ref = (
                recent.id,
                recent.mock,
                recent.stedi_ref,
            )
            return _finish(check, "done", "Reused a result from the last 24 hours.", recent.summary)

    box = PhiBox(settings)
    client = stedi_client(stedi_conn, settings, transport)
    try:
        kind = payers.plan_kind(plan_name, ehr_payer_id)
        if test:
            check.mock = True
            # Payer mapping is shown in test mode too, but it never blocks a mock check.
            mapping = await payers.resolve(
                session, client, check.workspace_id, ehr_payer_id=ehr_payer_id, name=plan_name
            )
            scenario = await mock_scenario(session, check.workspace_id, mapping.plan_kind or kind)
            request, payer_id, npi = (
                stedi.mock_request(scenario),
                stedi.MOCKS[scenario].payer_id,
                None,
            )
        else:
            npi = await billing_npi(session, check.workspace_id, settings)
            if npi is None:
                return _finish(
                    check,
                    "skipped",
                    "Set the practice's billing NPI (Practice → Eligibility settings) first.",
                )
            mapping = await payers.resolve(
                session, client, check.workspace_id, ehr_payer_id=ehr_payer_id, name=plan_name
            )
            if mapping.state == "error":
                return _requeue(
                    check,
                    "Stedi's payer directory didn't answer.",
                    {"coverage": "error", "error_class": stedi.RETRY},
                )
            if mapping.state not in payers.SETTLED or not mapping.stedi_payer_id:
                reason = (
                    f"Confirm which payer “{plan_name}” is in Practice → Payers."
                    if mapping.state == "ambiguous"
                    else f"Stedi has no payer matching “{plan_name}”: set it in Practice → Payers."
                )
                return _finish(check, "skipped", reason, {"coverage": "payer_unmapped"})
            payer_id = mapping.stedi_payer_id
            relation = str(coverage.attrs.get("subscription_relation") or "self").lower()
            patient_rec = await _record(session, conn_id, "patient", appt.patient_ref)
            pii = box.open(patient_rec.pii if patient_rec else None)
            coverage_pii = box.open(coverage.pii)
            medicaid = mapping.plan_kind == payers.MEDICAID or "MEDICAID" in (
                mapping.programs or []
            )
            subscriber, dependent, why = who_to_send(
                relation, pii, coverage_pii.get("member_id"), coverage_pii, medicaid
            )
            if why:
                return _finish(check, "skipped", why)
            person = dependent or subscriber
            missing = [
                name
                for name, value in (
                    ("member ID", subscriber.get("member_id")),
                    ("first name", person.get("first_name")),
                    ("last name", person.get("last_name")),
                    ("date of birth", person.get("date_of_birth")),
                )
                if not value
            ]
            if missing:
                return _finish(check, "skipped", "Missing " + ", ".join(missing) + ".")
            visit, today = _visit_date(appt)
            try:
                request = stedi.patient_request(
                    payer_id=payer_id,
                    provider_npi=npi,
                    subscriber=subscriber,
                    dependent=dependent,
                    service_date=stedi.service_date(visit, today, payer_id),
                    pin=provider_pin(mapping, box),
                )
            except stedi.InvalidMbi as err:
                return _finish(check, "skipped", err.message, {"coverage": "invalid_mbi"})

        try:
            response, ms = await client.eligibility(
                request, forwarded_for=forwarded_for if payer_id == stedi.CMS else None
            )
        except stedi.StediError as err:
            if err.status == "busy":  # Stedi's pool was full: one immediate retry
                try:
                    response, ms = await client.eligibility(
                        request, forwarded_for=forwarded_for if payer_id == stedi.CMS else None
                    )
                    err = None
                except stedi.StediError as again:
                    err = again
            if err is not None:
                return await _failed(session, stedi_conn, check, err, payer_id, npi, test)
        summary = {
            **stedi.summarize_eligibility(response, payer_id=payer_id),
            "ehr_plan": plan_name,
            "mock": check.mock,
        }
        await _resolve_redirect(session, client, check.workspace_id, summary.get("redirect"))
    finally:
        await client.aclose()

    call = await audit(
        session,
        stedi_conn,
        "stedi.eligibility",
        check.trigger,
        ok=summary["coverage"] != "error",
        status="success" if summary["coverage"] != "error" else "error",
        duration_ms=ms,
        summary={
            "coverage": summary["coverage"],
            "mock": check.mock,
            "errors": summary["errors"],
        },
    )
    check.integration_call_id, check.stedi_ref = call.id, response.get("id")
    if npi:
        await payers.note_outcome(
            session,
            check.workspace_id,
            npi,
            payer_id,
            summary["error_details"],
            accepted=summary["coverage"] in REUSABLE,
        )
    if summary["coverage"] == "error":
        outcome = summary["error_class"] or stedi.CORRECT
        if outcome == stedi.RETRY:
            return _requeue(check, summary["message"] or "The payer didn't answer.", summary)
        return _finish(check, "failed", summary["message"], summary)
    _finish(check, "done", None, summary)

    note = None
    redirect = summary.get("redirect")
    if summary["coverage"] == "inactive":
        note = (
            f"CarePlix: the payer reported {plan_name} coverage inactive on {utcnow():%Y-%m-%d}. "
            "Please confirm insurance with the patient before the visit."
        )
    elif redirect:
        where = redirect.get("name") or "another plan"
        label = {
            "medicare_advantage": "a Medicare Advantage plan",
            "managed_care": "a Medicaid managed care plan",
        }.get(redirect["kind"], "another payer")
        note = (
            f"CarePlix: {summary.get('payer') or plan_name} reports {label} ({where}) on "
            f"{utcnow():%Y-%m-%d}. Re-check eligibility with that plan and update the insurance "
            "on file before the visit."
        )
    if note and not check.mock:
        ehr = await session.get(PayerConnection, conn_id)
        await intents.propose(
            session,
            ehr,
            settings,
            kind="patient_alert",
            target_source_id=appt.patient_ref,
            payload={"note": note},
            reason="Eligibility: coverage reported inactive"
            if summary["coverage"] == "inactive"
            else "Eligibility: patient covered by another plan",
            source="eligibility",
            requested_by="system:eligibility",
        )
    return check


async def _failed(
    session: AsyncSession,
    stedi_conn: PayerConnection,
    check: EligibilityCheck,
    err: stedi.StediError,
    payer_id: str,
    npi: str | None,
    test: bool,
) -> EligibilityCheck:
    call = await audit(
        session,
        stedi_conn,
        "stedi.eligibility",
        check.trigger,
        ok=False,
        status=err.status,
        http_status=err.http_status,
        error=err.message,
        summary={"errors": [e["code"] for e in err.errors], "mock": test},
    )
    check.integration_call_id = call.id
    if npi:
        await payers.note_outcome(
            session, check.workspace_id, npi, payer_id, err.errors, accepted=False
        )
    summary = error_summary(err.errors, err.outcome, payer_id, err.message[:200])
    if err.outcome == stedi.RETRY:
        return _requeue(check, summary["message"], summary)
    return _finish(check, "failed", summary["message"][:200], summary)


async def run_pending(
    db: Database,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    limit: int = 10,
) -> int:
    """Run due checks, several at once (Stedi can take up to two minutes per check). SQLite
    allows one writer, so local development runs them one at a time."""
    now = utcnow()
    async with db.sessions() as session:
        ids = list(
            await session.scalars(
                select(EligibilityCheck.id)
                .where(
                    EligibilityCheck.state == "pending",
                    or_(
                        EligibilityCheck.next_attempt_at.is_(None),
                        EligibilityCheck.next_attempt_at <= now,
                    ),
                )
                .order_by(EligibilityCheck.created_at)
                .limit(limit)
            )
        )
    sqlite = db.engine.dialect.name == "sqlite"
    gate = asyncio.Semaphore(1 if sqlite else max(1, settings.stedi_max_in_flight))

    async def one(check_id: str) -> None:
        async with gate, db.sessions() as session:
            check = await session.get(EligibilityCheck, check_id)
            due = check is not None and check.state == "pending"
            if due and (check.next_attempt_at is None or as_utc(check.next_attempt_at) <= now):
                await run_check(session, check, settings, transport)
                await session.commit()

    await asyncio.gather(*(one(i) for i in ids))
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
        "attempts": check.attempts or 0,
        "next_attempt_at": check.next_attempt_at,
        "created_at": check.created_at,
        "completed_at": check.completed_at,
    }
