"""Claim status checks (276/277): ask, keep the answer as evidence, update the claim.

:func:`check` asks about one claim:

- payers with no electronic claim status (traditional Medicare, or the directory says
  ``NOT_SUPPORTED``) aren't asked: the claim is marked ``unavailable`` with the reason;
- sample claims are answered by their synthetic 277 (:mod:`.samples`), unless a person asks the
  clearinghouse;
- everything else goes through the clearinghouse gateway. In test mode that sends the published
  test claim, never this patient, and the answer is labelled ``mock``.

Connectivity errors (payer down, rate limit, timeout) leave the status alone and retry within the
hour; after :data:`MAX_FAILURES` in a row, or on an error a retry won't fix, the claim has no
electronic status (``unavailable``) and the follow-up schedule hands it to a payer call.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import directory, samples
from careplix_api.claims.models import Claim, ClaimStatusCheck
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import clearinghouse
from careplix_api.integrations.clearinghouse import FINAL, STATUS_LABEL, UNAVAILABLE
from careplix_api.models import PayerConnection
from careplix_api.phi import PhiBox
from careplix_api.practice.connections import CLEARINGHOUSE, audit
from careplix_api.settings import Settings

RECHECK_EVERY = timedelta(days=7)
RETRY_AFTER = timedelta(hours=1)
MAX_FAILURES = 3
SAMPLE, CLEARINGHOUSE_VIA, AUTO = "sample", "clearinghouse", "auto"


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def age_days(claim: Claim, today: date) -> int:
    submitted = _date(claim.submitted_on)
    return (today - submitted).days if submitted else 0


def view(claim: Claim, today: date) -> dict[str, Any]:
    """The claim for the Studio: outcomes and our numbers; the patient only as initials."""
    return {
        "id": claim.id,
        "claim_number": claim.claim_number,
        "payer_claim_number": claim.payer_claim_number,
        "source": claim.source,
        "synthetic": claim.synthetic,
        "sample_scenario": claim.sample_scenario,
        "payer_id": claim.payer_id,
        "payer_name": claim.payer_name,
        "plan_kind": claim.plan_kind,
        "provider_name": claim.provider_name,
        "date_of_service": claim.date_of_service,
        "billed_amount": claim.billed_amount,
        "cpt_code": claim.cpt_code,
        "submitted_on": claim.submitted_on,
        "age_days": age_days(claim, today),
        "status": claim.status,
        "status_label": claim.status_label or "Not checked yet",
        "status_since": claim.status_since,
        "status_mock": bool(claim.status_mock),
        "final": claim.status in FINAL,
        "amount_paid": claim.amount_paid,
        "no_route": claim.no_route,
        "last_checked_at": _iso(claim.last_checked_at),
        "next_check_at": _iso(claim.next_check_at),
        "failures": claim.failures or 0,
        "follow_up_case_id": claim.follow_up_case_id,
        "follow_up_reason": claim.follow_up_reason,
        "follow_up_error": claim.follow_up_error,
    }


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def check_view(row: ClaimStatusCheck) -> dict[str, Any]:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "source": row.source,
        "mode": row.mode,
        "mock": row.mock,
        "state": row.state,
        "status": row.status,
        "label": STATUS_LABEL.get(row.status, row.status),
        "summary": row.summary,
        "message": row.message,
        "duration_ms": row.duration_ms,
        "has_response": bool(row.raw),
        "created_at": _iso(row.created_at),
    }


async def get(session: AsyncSession, workspace_id: str, claim_id: str) -> Claim:
    claim = await session.get(Claim, claim_id)
    if claim is None or claim.workspace_id != workspace_id:
        raise ApiError(404, "claim_not_found", "No accessible claim matches that reference.")
    return claim


async def checks(session: AsyncSession, claim: Claim) -> list[ClaimStatusCheck]:
    return list(
        await session.scalars(
            select(ClaimStatusCheck)
            .where(
                ClaimStatusCheck.workspace_id == claim.workspace_id,
                ClaimStatusCheck.claim_id == claim.id,
            )
            .order_by(ClaimStatusCheck.created_at.desc())
        )
    )


async def clearinghouse_connection(
    session: AsyncSession, workspace_id: str
) -> PayerConnection | None:
    return await session.scalar(
        select(PayerConnection).where(
            PayerConnection.workspace_id == workspace_id,
            PayerConnection.kind == CLEARINGHOUSE,
            PayerConnection.enabled.is_(True),
        )
    )


def patient(settings: Settings, claim: Claim) -> dict[str, Any]:
    return PhiBox(settings).open(claim.patient)


async def no_route(session: AsyncSession, claim: Claim) -> str | None:
    """Why this claim's payer has no electronic claim status, if it hasn't."""
    if claim.plan_kind == "medicare":
        return clearinghouse.claim_status_route(clearinghouse.stedi.CMS, None)
    row = await directory.find(session, claim.workspace_id, claim.payer_id)
    support = (row.support or {}).get("claim_status") if row else None
    return clearinghouse.claim_status_route(claim.payer_id, support)


def apply(claim: Claim, summary: dict[str, Any], *, mock: bool, today: date) -> None:
    """Take the 277's status onto the claim. ``status_since`` is the payer's effective date, or
    today when the status changed and the payer gave none."""
    status = summary["status"]
    changed = status != claim.status
    claim.status, claim.status_label = status, STATUS_LABEL[status]
    claim.status_mock = mock
    claim.status_since = summary.get("effective_date") or (
        today.isoformat() if changed or not claim.status_since else claim.status_since
    )
    if summary.get("amount_paid") not in (None, ""):
        claim.amount_paid = str(summary["amount_paid"])
    if summary.get("payer_claim_number") and not mock:
        claim.payer_claim_number = str(summary["payer_claim_number"])[:60]
    claim.no_route, claim.failures = None, 0


def _unavailable(claim: Claim, reason: str) -> None:
    claim.status, claim.status_label = UNAVAILABLE, STATUS_LABEL[UNAVAILABLE]
    claim.no_route = reason[:120]
    claim.status_mock = False


def _query(settings: Settings, claim: Claim) -> clearinghouse.ClaimQuery:
    p = patient(settings, claim)
    return clearinghouse.ClaimQuery(
        payer_id=claim.payer_id or "",
        provider_npi=claim.provider_npi,
        subscriber={
            "first_name": p.get("first_name", ""),
            "last_name": p.get("last_name", ""),
            "member_id": p.get("member_id", ""),
            "date_of_birth": p.get("date_of_birth", ""),
            "gender": p.get("gender", ""),
        },
        service_from=_date(claim.date_of_service) or utcnow().date(),
        billed_amount=claim.billed_amount,
        patient_account_number=claim.claim_number,
        payer_claim_number=claim.payer_claim_number,
    )


async def check(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    *,
    trigger: str = "manual",
    via: str = AUTO,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> ClaimStatusCheck:
    """Ask about one claim and record the answer. Never raises for a payer or network problem."""
    now = now or utcnow()
    today = now.date()
    box = PhiBox(settings)
    record = ClaimStatusCheck(
        workspace_id=claim.workspace_id, claim_id=claim.id, trigger=trigger, summary={}
    )
    reason = await no_route(session, claim)
    use_sample = via == SAMPLE or (via == AUTO and claim.source == "sample")
    if use_sample and not claim.sample_scenario:
        raise ApiError(422, "no_sample", "Only synthetic sample claims have a sample answer.")
    if reason:
        record.source, record.mode, record.state = "none", "none", "unavailable"
        record.status, record.message = UNAVAILABLE, reason
        _unavailable(claim, reason)
        claim.next_check_at = None
    elif use_sample:
        body = samples.response(claim, today)
        summary = clearinghouse.summarize_claim_status(
            body, patient_account_number=claim.claim_number
        )
        record.source, record.mode, record.state = "sample", "sample", "done"
        record.status, record.summary = summary["status"], summary
        record.raw = box.seal({"response": body})
        apply(claim, summary, mock=False, today=today)
    else:
        await _ask(session, settings, claim, record, transport, now)
    claim.last_checked_at = now
    if claim.status in FINAL:
        claim.next_check_at = None
    elif record.state == "done":
        claim.next_check_at = now + RECHECK_EVERY
    session.add(record)
    await session.flush()
    claim.last_check_id = record.id
    return record


async def _ask(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    record: ClaimStatusCheck,
    transport: httpx.AsyncBaseTransport | None,
    now: datetime,
) -> None:
    record.source = "clearinghouse"
    conn = await clearinghouse_connection(session, claim.workspace_id)
    if conn is None:
        record.mode, record.state, record.status = "none", "unavailable", UNAVAILABLE
        record.message = "The clearinghouse isn't connected."
        _unavailable(claim, record.message)
        claim.next_check_at = now + RECHECK_EVERY
        return
    record.clearinghouse_connection_id = conn.id
    gateway = clearinghouse.for_connection(conn, settings, transport)
    record.mode = gateway.mode
    try:
        answer = await gateway.claim_status(_query(settings, claim), today=now.date())
    except clearinghouse.ClearinghouseError as err:
        record.status, record.message = clearinghouse.ERROR, err.message[:200]
        call = await audit(
            session,
            conn,
            "clearinghouse.claim_status",
            "manual" if record.trigger == "manual" else "schedule",
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
            summary={"mock": gateway.mode == clearinghouse.TEST},
        )
        record.integration_call_id, record.mock = call.id, gateway.mode == clearinghouse.TEST
        claim.failures = (claim.failures or 0) + 1
        if err.retryable and claim.failures < MAX_FAILURES:
            record.state = "failed"
            claim.next_check_at = now + RETRY_AFTER
        else:
            record.state = "unavailable"
            _unavailable(claim, f"No electronic answer: {err.message}")
            claim.next_check_at = now + RECHECK_EVERY
        return
    finally:
        await gateway.aclose()
    summary = answer.summary
    record.mock, record.duration_ms = answer.mock, answer.ms
    record.state, record.status, record.summary = "done", summary["status"], summary
    record.raw = PhiBox(settings).seal({"request": answer.request, "response": answer.raw})
    call = await audit(
        session,
        conn,
        "clearinghouse.claim_status",
        "manual" if record.trigger == "manual" else "schedule",
        ok=True,
        status="success",
        duration_ms=answer.ms,
        summary={
            "mock": answer.mock,
            "status": summary["status"],
            "category": summary.get("category_code"),
        },
    )
    record.integration_call_id = call.id
    apply(claim, summary, mock=answer.mock, today=now.date())


def evidence(settings: Settings, row: ClaimStatusCheck) -> dict[str, Any]:
    """The stored 277 (and, for live checks, the 276 that asked), opened for a person."""
    return PhiBox(settings).open(row.raw)


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
