"""Claim follow-up (RC6, workflow R4): re-check claims by age, and call the payer when the
electronic status is stale or unavailable.

The whole policy is two pure functions and one small entry point, so a playbook trigger can take
it over later (Cases v2 / playbooks): call :func:`follow_up` for one claim, or :func:`run_due` for
a workspace. Until then :func:`run_forever` runs it from the API's background loops.

Schedule (by claim age, from the day the payer received it):

- first electronic check at :data:`FIRST_CHECK_AGE`, then every week until the payer finalizes it
  (paid, denied, finalized, no payment); connectivity errors retry within the hour.

Claims we submitted (``stage`` set) follow their 277CA and 835 instead: a call when there's no
acknowledgement :data:`ACK_OVERDUE` after submission, or no remittance :data:`ERA_OVERDUE` after
it (test mode and traditional Medicare have no 276; elsewhere a 276 still runs on the schedule).
Their rejections and denials are worked from the claims work queue, not by a status call.

A payer call (a ``claim_status`` case) is opened once per claim when:

- the payer can't find the claim, rejected it, or asks for more information: at once;
- there's no electronic status (traditional Medicare, a payer without 276/277, repeated errors)
  and the claim is :data:`CALL_AFTER_AGE` old;
- the status is stale: still pending or accepted, unchanged for :data:`STALE_AFTER`, on a claim
  :data:`CALL_AFTER_AGE` old.

"Once" is kept by the claim's ``follow_up_case_id`` and the case's ``source_ref``
(``claim:<id>``): a closed case is a person's decision and isn't reopened or replaced.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.cases import service as cases_service
from careplix_api.claims import directory, service
from careplix_api.claims.models import Claim
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations.clearinghouse import (
    ACCEPTED,
    ERROR,
    FINAL,
    INFO_REQUESTED,
    NOT_FOUND,
    PENDING,
    REJECTED,
    STATUS_LABEL,
    UNAVAILABLE,
)
from careplix_api.models import Case
from careplix_api.settings import Settings

log = logging.getLogger("careplix.claims")

FIRST_CHECK_AGE = timedelta(days=14)  # clean electronic claims mostly adjudicate in 14–30 days
CALL_AFTER_AGE = timedelta(days=30)
STALE_AFTER = timedelta(days=21)
CALL_NOW = frozenset({NOT_FOUND, REJECTED, INFO_REQUESTED})
SOURCE = "claim_follow_up"
SYSTEM_ACTOR = "system:claims"
NOT_OUT = frozenset({"draft", "ready", "blocked", "void"})  # claims we haven't submitted
OUT_AT_PAYER = frozenset({"submitted", "accepted"})  # submitted, waiting for the payer
ACK_OVERDUE = timedelta(days=3)  # 277CAs come within a day or two
ERA_OVERDUE = timedelta(days=30)  # Medicare pays clean claims from day 14; most payers by 30


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def due(claim: Claim, now: datetime) -> bool:
    """Whether the schedule asks about this claim now."""
    if claim.status in FINAL or claim.no_route:
        return False
    if claim.stage is not None and claim.stage not in OUT_AT_PAYER:
        return False  # not out yet, settled by a remittance, or in the rejection queue
    submitted = _date(claim.submitted_on)
    if submitted is None or now.date() - submitted < FIRST_CHECK_AGE:
        return False
    return claim.next_check_at is None or as_utc(claim.next_check_at) <= now


def submitted_needs_call(claim: Claim, now: datetime) -> str | None:
    """Claims we submitted: acknowledgement and remittance age are the signal (all that test
    mode and traditional Medicare give), plus what a 276 said where the payer answers one.
    Rejections and denials have their own queue, so they never open a status call."""
    if claim.stage not in OUT_AT_PAYER:
        return None
    sent = as_utc(claim.submitted_at).date() if claim.submitted_at else _date(claim.submitted_on)
    if sent is None:
        return None
    age = (now.date() - sent).days
    if claim.stage == "submitted" and age >= ACK_OVERDUE.days:
        return f"No acknowledgement (277CA) {age} days after submission."
    if claim.status in (NOT_FOUND, INFO_REQUESTED):
        return f"Electronic status: {STATUS_LABEL[claim.status].lower()}."
    if claim.era_at is None and age >= ERA_OVERDUE.days:
        return f"Accepted, but no remittance (835) {age} days after submission."
    return None


def needs_call(claim: Claim, now: datetime) -> str | None:
    """Why a person (or the calling agent) should phone the payer about this claim, or None."""
    if claim.stage is not None:
        return submitted_needs_call(claim, now)
    status = claim.status
    if status is None or status in FINAL:
        return None
    if status in CALL_NOW:
        return f"Electronic status: {STATUS_LABEL[status].lower()}."
    submitted = _date(claim.submitted_on)
    if submitted is None or now.date() - submitted < CALL_AFTER_AGE:
        return None
    age = (now.date() - submitted).days
    if status in (UNAVAILABLE, ERROR):
        why = claim.no_route or "No electronic status."
        return f"{why.rstrip('.')}, and the claim is {age} days old."
    since = _date(claim.status_since)
    if status in (PENDING, ACCEPTED) and since and now.date() - since >= STALE_AFTER:
        days = (now.date() - since).days
        return f"Stale: {STATUS_LABEL[status].lower()} for {days} days on a {age}-day-old claim."
    return None


def _principal(workspace_id: str) -> Principal:
    return Principal(SYSTEM_ACTOR, workspace_id, "operator", "Claim follow-up")


def case_values(settings: Settings, claim: Claim) -> dict[str, Any]:
    p = service.patient(settings, claim)
    return {
        "patient_name": p.get("display_name")
        or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
        "date_of_birth": p.get("date_of_birth"),
        "member_id": p.get("member_id"),
        "provider_name": claim.provider_name,
        "provider_npi": claim.provider_npi,
        "payer_name": claim.payer_name,
        "payer_phone": claim.payer_phone,
        "claim_number": claim.payer_claim_number or claim.claim_number,
        "date_of_service": claim.date_of_service,
        "billed_amount": claim.billed_amount,
        "cpt_code": claim.cpt_code,
    }


async def ensure_case(
    session: AsyncSession, settings: Settings, claim: Claim, reason: str
) -> Case | None:
    """Open the claim's payer call, once. Returns the new case, or None when it already has one
    (open or closed) or the case can't be made yet (the reason is kept on the claim)."""
    if claim.follow_up_case_id:
        return None
    existing = await session.scalar(
        select(Case).where(
            Case.workspace_id == claim.workspace_id, Case.source_ref == f"claim:{claim.id}"
        )
    )
    if existing is not None:
        claim.follow_up_case_id = existing.id
        return None
    if not claim.payer_phone:
        row = await directory.find(session, claim.workspace_id, claim.payer_id)
        claim.payer_phone = row.phone if row and row.phone else None
    try:
        case = await cases_service.create(
            session,
            _principal(claim.workspace_id),
            "claim_status",
            {k: v for k, v in case_values(settings, claim).items() if v not in (None, "")},
            source=SOURCE,
            source_ref=f"claim:{claim.id}",
        )
    except ApiError as err:
        claim.follow_up_error = err.message[:200]
        return None
    claim.follow_up_case_id, claim.follow_up_reason = case.id, reason[:160]
    claim.follow_up_error = None
    return case


async def follow_up(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    *,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """One claim: check it if the schedule says so, then open the payer call if it needs one."""
    now = now or utcnow()
    if claim.stage in NOT_OUT:
        return {"checked": False, "case_id": None}  # not at the payer yet
    checked = None
    if due(claim, now):
        checked = await service.check(
            session, settings, claim, trigger="schedule", transport=transport, now=now
        )
    elif claim.status is None and claim.no_route is None:
        # Never checked and not due: a payer without electronic status still gets its reason.
        reason = await service.no_route(session, claim)
        if reason:
            checked = await service.check(
                session, settings, claim, trigger="schedule", transport=transport, now=now
            )
    reason = needs_call(claim, now)
    case = await ensure_case(session, settings, claim, reason) if reason else None
    return {"checked": checked is not None, "case_id": case.id if case else None}


async def run_due(
    db: Database,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    now: datetime | None = None,
    workspace_id: str | None = None,
) -> dict[str, int]:
    """Every claim that isn't finished, oldest first; one claim's failure never stops the rest."""
    now = now or utcnow()
    report = {"checked": 0, "cases": 0, "errors": 0}
    async with db.sessions() as session:
        query = select(Claim.id).where(or_(Claim.status.is_(None), Claim.status.not_in(FINAL)))
        if workspace_id:
            query = query.where(Claim.workspace_id == workspace_id)
        ids = list(await session.scalars(query.order_by(Claim.submitted_on)))
    for claim_id in ids:
        async with db.sessions() as session:
            claim = await session.get(Claim, claim_id)
            try:
                out = await follow_up(session, settings, claim, now=now, transport=transport)
                await session.commit()
            except Exception:  # noqa: BLE001 - one bad claim never blocks the queue
                await session.rollback()
                log.exception("claim follow-up failed")
                report["errors"] += 1
                continue
        report["checked"] += int(out["checked"])
        report["cases"] += int(out["case_id"] is not None)
    return report


async def run_forever(
    db: Database,
    settings: Settings,
    transport_getter=lambda: None,
    interval_s: float = 900.0,
    inbound_every_s: float = 60.0,
) -> None:
    """Clearinghouse reports (277CA, 835) every minute; the follow-up schedule every 15."""
    from careplix_api.claims import inbound  # local: inbound imports follow-up's neighbours

    last_sweep = -interval_s
    loop = asyncio.get_running_loop()
    while True:
        try:
            await inbound.process_events(db, settings, transport_getter())
        except Exception:
            log.exception("clearinghouse report pass failed")
        if loop.time() - last_sweep >= interval_s:
            last_sweep = loop.time()
            try:
                await run_due(db, settings, transport_getter())
            except Exception:
                log.exception("claim follow-up pass failed")
        await asyncio.sleep(inbound_every_s)
