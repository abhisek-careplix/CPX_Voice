"""Billing API (RC3–RC8): charges, claims we build and submit, and what comes back.

Mounted before :mod:`.api` so its fixed paths (``/claims/charges``, ``/claims/work``, …) win over
``/claims/{claim_id}``.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.claims import (
    acks,
    appeals,
    billing,
    charges,
    denials,
    era,
    inbound,
    service,
    submit,
    x837,
)
from careplix_api.claims.models import (
    Appeal,
    Claim,
    ClaimCharge,
    ClaimTransaction,
    ClaimWork,
    EraPosting,
)
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import clearinghouse
from careplix_api.models import PayerConnection
from careplix_api.practice.connections import EHR
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
EDITABLE = ("draft", "ready", "blocked", "rejected")


def _transport(request: Request):
    return request.app.state.integration_transport


class VisitsIn(BaseModel):
    visits: list[str] | None = Field(default=None, max_length=20)


class BuildIn(BaseModel):
    charge_ids: list[str] | None = Field(default=None, max_length=200)


class ProfileIn(BaseModel):
    organization: str | None = Field(default=None, max_length=60)
    npi: str | None = Field(default=None, max_length=12)
    tax_id: str | None = Field(default=None, max_length=11)
    taxonomy: str | None = Field(default=None, max_length=10)
    address1: str | None = Field(default=None, max_length=55)
    city: str | None = Field(default=None, max_length=30)
    state: str | None = Field(default=None, max_length=2)
    postal_code: str | None = Field(default=None, max_length=10)
    submitter_name: str | None = Field(default=None, max_length=60)
    contact_phone: str | None = Field(default=None, max_length=14)


class LineEdit(BaseModel):
    line_id: str = Field(max_length=10)
    modifiers: list[str] | None = Field(default=None, max_length=4)
    units: str | None = Field(default=None, max_length=8)
    dx: list[int] | None = Field(default=None, max_length=4)


class SubmitIn(BaseModel):
    claim_ids: list[str] = Field(min_length=1, max_length=50)


class ReportIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["277", "835"]


class WorkDone(BaseModel):
    resolution: str | None = Field(default=None, max_length=200)
    dismiss: bool = False


class RouteIn(BaseModel):
    route: Literal["fix_resubmit", "appeal", "payer_call", "write_off"]


class AttachmentIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    content_type: Literal["application/pdf", "image/tiff", "image/jpeg", "image/png"]
    report_type: Literal["PROGRESS_REPORT", "OPERATIVE_NOTE"] = "PROGRESS_REPORT"
    data_base64: str = Field(min_length=4, max_length=8_400_000)


class LetterIn(BaseModel):
    text: str = Field(min_length=20, max_length=20_000)


class ApproveAppealIn(BaseModel):
    channel: Literal["fax", "portal", "mail"] = "fax"


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class DecidedIn(BaseModel):
    outcome: Literal["overturned", "upheld"]
    note: str | None = Field(default=None, max_length=200)


class ClaimEdit(BaseModel):
    diagnoses: list[str] | None = Field(default=None, max_length=12)
    place_of_service: str | None = Field(default=None, min_length=2, max_length=2)
    lines: list[LineEdit] | None = Field(default=None, max_length=50)
    test_outcome: Literal["paid", "partial", "denied", "none"] | None = None


# --- billing profile -----------------------------------------------------------------------------


@router.get("/clearinghouse/billing-profile")
async def get_profile(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    row = await billing.get(session, principal.workspace_id)
    await session.commit()
    return billing.view(row)


@router.put("/clearinghouse/billing-profile")
async def put_profile(
    body: ProfileIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    row = billing.update(
        await billing.get(session, principal.workspace_id), body.model_dump(exclude_none=True)
    )
    await session.commit()
    return billing.view(row)


# --- charges and draft claims ------------------------------------------------------------------


@router.get("/claims/charges")
async def list_charges(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    rows = await session.scalars(
        select(ClaimCharge)
        .where(ClaimCharge.workspace_id == principal.workspace_id)
        .order_by(ClaimCharge.created_at.desc(), ClaimCharge.source_ref)
        .limit(300)
    )
    return [charges.view(c) for c in rows]


@router.get("/claims/charges/visits")
async def visit_kinds(principal: Principal = Depends(viewer)):
    return [
        {"key": v.key, "label": v.label, "test_outcome": v.test_outcome} for v in charges.VISITS
    ]


@router.post("/claims/charges/samples")
async def add_charge_samples(
    body: VisitsIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    if set(body.visits or []) - set(charges.VISITS_BY_KEY):
        raise ApiError(422, "unknown_visit", "Pick listed sample visits.")
    made = await charges.add_samples(
        session, settings, principal.workspace_id, today=utcnow().date(), keys=body.visits
    )
    await session.commit()
    return [charges.view(c) for c in made]


@router.post("/claims/charges/pull")
async def pull_charges(
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Read recent charges from each connected NexHealth practice."""
    conns = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == principal.workspace_id,
                PayerConnection.kind == EHR,
                PayerConnection.enabled.is_(True),
            )
        )
    )
    if not conns:
        raise ApiError(409, "no_ehr", "Connect the practice's EHR (NexHealth) first.")
    out = []
    for conn in conns:
        result = await charges.pull_ehr(session, conn, settings, _transport(request))
        out.append({"connection_id": conn.id, "name": conn.name, **result})
    await session.commit()
    return out


@router.post("/claims/build")
async def build_claims(
    body: BuildIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    made = await charges.build_claims(
        session, settings, principal.workspace_id, charge_ids=body.charge_ids
    )
    await session.commit()
    today = utcnow().date()
    return [service.view(c, today) for c in made]


@router.patch("/claims/{claim_id}")
async def edit_claim(
    claim_id: str,
    body: ClaimEdit,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """Fix a claim before (re)submission: diagnoses, place of service, line modifiers, units and
    diagnosis pointers. The scrub runs again before it can go out."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    if claim.stage not in EDITABLE:
        raise ApiError(409, "not_editable", "Only claims that haven't gone out can be edited.")
    if body.diagnoses is not None:
        claim.diagnoses = [x837.icd10(d) for d in body.diagnoses if x837.icd10(d)]
    if body.place_of_service is not None:
        claim.place_of_service = body.place_of_service
    if body.test_outcome is not None:
        claim.test_outcome = None if body.test_outcome == "none" else body.test_outcome
    if body.lines:
        edits = {e.line_id: e for e in body.lines}
        lines = []
        for line in claim.lines or []:
            edit = edits.get(line.get("line_id"))
            if edit is not None:
                line = dict(line)
                if edit.modifiers is not None:
                    line["modifiers"] = [m.strip().upper() for m in edit.modifiers if m.strip()]
                if edit.units is not None:
                    line["units"] = edit.units
                if edit.dx is not None:
                    line["dx"] = edit.dx
            lines.append(line)
        claim.lines = lines
    claim.billed_amount = x837.total(claim.lines or [])
    claim.scrub = None
    if claim.stage in ("ready", "blocked"):
        claim.stage = "draft"
    await session.commit()
    return service.view(claim, utcnow().date())


# --- scrub and submit (RC5) ---------------------------------------------------------------


@router.post("/claims/submit")
async def submit_many(
    body: SubmitIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Scrub and submit several claims; one result each (a refusal never stops the rest)."""
    out = []
    for claim_id in body.claim_ids:
        claim = await service.get(session, principal.workspace_id, claim_id)
        result = await submit.submit(
            session, settings, claim, actor=principal.user_id, transport=_transport(request)
        )
        await session.commit()
        out.append({"claim_id": claim.id, "claim_number": claim.claim_number, **result})
    return out


@router.post("/claims/{claim_id}/scrub")
async def scrub_claim(
    claim_id: str,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    claim = await service.get(session, principal.workspace_id, claim_id)
    await submit.scrub_claim(session, settings, claim, transport=_transport(request))
    await session.commit()
    return service.view(claim, utcnow().date())


@router.post("/claims/{claim_id}/submit")
async def submit_claim(
    claim_id: str,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    claim = await service.get(session, principal.workspace_id, claim_id)
    result = await submit.submit(
        session, settings, claim, actor=principal.user_id, transport=_transport(request)
    )
    await session.commit()
    return {**service.view(claim, utcnow().date()), "result": result}


@router.get("/claims/{claim_id}/transactions")
async def claim_transactions(
    claim_id: str, principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    claim = await service.get(session, principal.workspace_id, claim_id)
    rows = await session.scalars(
        select(ClaimTransaction)
        .where(
            ClaimTransaction.workspace_id == principal.workspace_id,
            ClaimTransaction.claim_id == claim.id,
        )
        .order_by(ClaimTransaction.created_at.desc())
    )
    return [submit.transaction_view(r) for r in rows]


@router.get("/claims/{claim_id}/transactions/{row_id}/raw")
async def claim_transaction_raw(
    claim_id: str,
    row_id: str,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The request and answer as sent and received (reviewers only: they name the patient)."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    row = await session.get(ClaimTransaction, row_id)
    if row is None or row.claim_id != claim.id or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "No transaction matches that.")
    opened = json.loads(clearinghouse.white_label(json.dumps(submit.evidence(settings, row))))
    return {"id": row.id, "kind": row.kind, "mode": row.mode, **opened}


@router.get("/claims/{claim_id}/x837")
async def preview_837(
    claim_id: str,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The claim as the clearinghouse will receive it, in the connected mode."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    conn = await service.clearinghouse_connection(session, principal.workspace_id)
    from careplix_api.practice.connections import is_test

    mode = clearinghouse.PRODUCTION if conn is not None and not is_test(conn) else "test"
    profile = billing.view(await billing.get(session, principal.workspace_id))
    body = x837.build(
        claim,
        service.patient(settings, claim),
        profile,
        mode=mode,
        **(await submit._original(session, claim)),
    )
    await session.commit()
    return {"mode": mode, "body": body}


# --- what comes back: 277CA, 835 ---------------------------------------------------------


@router.post("/claims/clearinghouse/fetch")
async def fetch_report(
    body: ReportIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Apply one acknowledgement (277) or remittance (835) by its transaction ID."""
    try:
        out = await inbound.fetch(
            session,
            settings,
            principal.workspace_id,
            body.transaction_id,
            body.kind,
            _transport(request),
        )
    except clearinghouse.ClearinghouseError as err:
        raise ApiError(502, "clearinghouse_error", err.message) from None
    await session.commit()
    return out


@router.post("/claims/clearinghouse/poll")
async def poll_reports(
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Check the clearinghouse for acknowledgements and remittances from the last 3 days."""
    try:
        out = await inbound.poll(session, settings, principal.workspace_id, _transport(request))
    except clearinghouse.ClearinghouseError as err:
        raise ApiError(502, "clearinghouse_error", err.message) from None
    await session.commit()
    return out


# --- the work queue: rejections and denials ------------------------------------------------


@router.get("/claims/work")
async def work_queue(
    state: Literal["open", "done", "all"] = "open",
    kind: Literal["rejection", "denial", "all"] = "all",
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
):
    query = (
        select(ClaimWork, Claim)
        .join(Claim, Claim.id == ClaimWork.claim_id)
        .where(ClaimWork.workspace_id == principal.workspace_id)
    )
    if state != "all":
        query = query.where(ClaimWork.state == state)
    if kind != "all":
        query = query.where(ClaimWork.kind == kind)
    rows = await session.execute(query.order_by(ClaimWork.created_at.desc()).limit(200))
    return [
        {
            **acks.work_view(work),
            "claim_number": claim.claim_number,
            "payer_name": claim.payer_name,
            "date_of_service": claim.date_of_service,
            "claim_stage": claim.stage,
        }
        for work, claim in rows.all()
    ]


@router.post("/claims/work/{work_id}/done")
async def work_done(
    work_id: str,
    body: WorkDone,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    row = await session.get(ClaimWork, work_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "No work item matches that.")
    if row.state == "open":
        row.state = "dismissed" if body.dismiss else "done"
        row.resolved_at, row.resolved_by = utcnow(), principal.user_id
        row.resolution = body.resolution or ("Dismissed." if body.dismiss else "Done.")
    await session.commit()
    return acks.work_view(row)


async def _work(session: AsyncSession, principal: Principal, work_id: str) -> ClaimWork:
    row = await session.get(ClaimWork, work_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "No work item matches that.")
    return row


@router.post("/claims/work/{work_id}/route")
async def reroute(
    work_id: str,
    body: RouteIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Send a denial down another route (a person's call); the new route starts its work."""
    row = await _work(session, principal, work_id)
    if row.kind != "denial" or row.state != "open":
        raise ApiError(409, "invalid_state", "Only open denials can be rerouted.")
    row.route = body.route
    row.due_on = (
        denials.deadline(
            (await session.get(Claim, row.claim_id)).plan_kind, body.route, None, utcnow().date()
        )
        if not row.due_on
        else row.due_on
    )
    claim = await service.get(session, principal.workspace_id, row.claim_id)
    await denials.act(session, settings, claim, row)
    await session.commit()
    return acks.work_view(row)


@router.post("/claims/work/{work_id}/correct")
async def correct(
    work_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """Draft the corrected claim for a denial: edit it, then scrub and submit as usual."""
    row = await _work(session, principal, work_id)
    claim = await service.get(session, principal.workspace_id, row.claim_id)
    if row.kind != "denial":
        raise ApiError(409, "invalid_state", "A rejection is fixed on the claim itself.")
    existing = await session.scalar(
        select(Claim).where(
            Claim.workspace_id == principal.workspace_id,
            Claim.replaces_claim_id == claim.id,
            Claim.stage != "void",
        )
    )
    if existing is not None:
        return service.view(existing, utcnow().date())
    try:
        new = await denials.corrected_claim(session, claim, row)
    except ValueError as err:
        raise ApiError(409, "no_payer_claim_number", str(err)) from None
    await session.commit()
    return service.view(new, utcnow().date())


# --- remittances: posting proposals (RC7) -----------------------------------------------------


@router.get("/claims/era")
async def era_postings(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    rows = list(
        await session.scalars(
            select(EraPosting)
            .where(EraPosting.workspace_id == principal.workspace_id)
            .order_by(EraPosting.created_at.desc())
            .limit(200)
        )
    )
    for row in rows:
        await era.refresh_state(session, row)
    await session.commit()
    return [era.view(r) for r in rows]


async def _posting(session: AsyncSession, principal: Principal, posting_id: str) -> EraPosting:
    row = await session.get(EraPosting, posting_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "No remittance posting matches that.")
    return row


@router.post("/claims/era/{posting_id}/approve")
async def approve_posting(
    posting_id: str,
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Approve every proposed write of one posting and send them (the batch approval)."""
    from careplix_api.models import WriteIntent
    from careplix_api.practice import intents

    row = await _posting(session, principal, posting_id)
    approved = []
    for intent_id in row.intent_ids or []:
        intent = await session.get(WriteIntent, intent_id)
        if intent is not None and intent.state == "proposed":
            await intents.decide(session, intent, principal.user_id, True)
            approved.append(intent.id)
    row.state = "posting" if approved else row.state
    await session.commit()
    if approved:
        await intents.execute_approved(
            request.app.state.db, settings, _transport(request), only=approved
        )
    await session.refresh(row)
    await era.refresh_state(session, row)
    await session.commit()
    return era.view(row)


@router.post("/claims/era/{posting_id}/posted")
async def mark_posted(
    posting_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """Staff posted it in the EHR by hand."""
    row = await _posting(session, principal, posting_id)
    if row.state not in ("manual", "needs_review", "review"):
        raise ApiError(409, "invalid_state", f"This posting is {row.state}.")
    row.state, row.note = "posted", f"Posted by hand ({principal.user_id})."[:200]
    await session.commit()
    return era.view(row)


# --- appeals and attachments (RC8) ----------------------------------------------------------------


@router.post("/claims/{claim_id}/attachments")
async def attach_to_claim(
    claim_id: str,
    body: AttachmentIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Records for a claim before it goes out (a 275 the claim then references)."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    entry = await appeals.attach(
        session, settings, claim, transport=_transport(request), **body.model_dump()
    )
    await session.commit()
    return entry


@router.get("/claims/appeals")
async def list_appeals(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    """Appeals by deadline, the closest first."""
    rows = await session.execute(
        select(Appeal, Claim)
        .join(Claim, Claim.id == Appeal.claim_id)
        .where(Appeal.workspace_id == principal.workspace_id)
        .order_by(Appeal.due_on)
    )
    today = utcnow().date()
    return [
        {
            **appeals.view(a, today),
            "claim_number": c.claim_number,
            "payer_name": c.payer_name,
        }
        for a, c in rows.all()
    ]


async def _appeal(session: AsyncSession, principal: Principal, appeal_id: str) -> Appeal:
    row = await session.get(Appeal, appeal_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "No appeal matches that.")
    return row


@router.get("/claims/appeals/{appeal_id}")
async def get_appeal(
    appeal_id: str,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The appeal with its letter (reviewers only: the letter names the patient)."""
    row = await _appeal(session, principal, appeal_id)
    return {**appeals.view(row), "letter": appeals.letter(settings, row)}


@router.put("/claims/appeals/{appeal_id}/letter")
async def edit_letter(
    appeal_id: str,
    body: LetterIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    row = appeals.edit(settings, await _appeal(session, principal, appeal_id), body.text)
    await session.commit()
    return {**appeals.view(row), "letter": body.text}


@router.post("/claims/appeals/{appeal_id}/approve")
async def approve_appeal(
    appeal_id: str,
    body: ApproveAppealIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    row = await appeals.approve(
        settings, await _appeal(session, principal, appeal_id), principal.user_id, body.channel
    )
    await session.commit()
    return appeals.view(row)


@router.post("/claims/appeals/{appeal_id}/sent")
async def appeal_sent(
    appeal_id: str,
    body: NoteIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    row = appeals.mark_sent(await _appeal(session, principal, appeal_id), body.note)
    await session.commit()
    return appeals.view(row)


@router.post("/claims/appeals/{appeal_id}/decided")
async def appeal_decided(
    appeal_id: str,
    body: DecidedIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """The payer's decision. Overturned: the payment arrives on a new remittance. Upheld: the
    denial stays open for the next step (a second-level appeal or a write-off)."""
    row = await _appeal(session, principal, appeal_id)
    if row.state != "sent":
        raise ApiError(409, "invalid_state", "Only a sent appeal gets a decision.")
    row.state = "decided"
    row.send_note = f"{body.outcome.capitalize()}. {body.note or ''}".strip()[:200]
    work = await session.get(ClaimWork, row.work_id) if row.work_id else None
    if work is not None and body.outcome == "overturned" and work.state == "open":
        work.state, work.resolved_at, work.resolved_by = "done", utcnow(), principal.user_id
        work.resolution = "Appeal overturned the denial."
    await session.commit()
    return appeals.view(row)


@router.post("/claims/appeals/{appeal_id}/attachments")
async def attach_to_appeal(
    appeal_id: str,
    body: AttachmentIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    row = await _appeal(session, principal, appeal_id)
    claim = await service.get(session, principal.workspace_id, row.claim_id)
    entry = await appeals.attach(
        session,
        settings,
        claim,
        appeal=row,
        transport=_transport(request),
        **body.model_dump(),
    )
    await session.commit()
    return entry
