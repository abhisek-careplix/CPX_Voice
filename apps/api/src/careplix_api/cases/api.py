"""Cases API: the work list, case types, the completion contract, playbooks, contact attempts and
the one review inbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.cases import attempts, contract, inbox, playbooks, policy, service, types
from careplix_api.cases.models import ContactAttempt
from careplix_api.errors import ApiError
from careplix_api.models import Case
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()


class CaseIn(BaseModel):
    case_type: str
    fields: dict[str, Any] = Field(default_factory=dict, max_length=40)


class SamplesIn(BaseModel):
    case_type: str
    count: int = Field(default=3, ge=1, le=10)


class StatusIn(BaseModel):
    status: Literal["open", "done", "handed_off", "blocked", "failed", "closed"]
    reason: str | None = Field(default=None, max_length=300)
    owner_id: str | None = Field(default=None, max_length=36)
    due_at: datetime | None = None
    next_step: str | None = Field(default=None, max_length=300)


class EvidenceIn(BaseModel):
    kind: str = Field(max_length=20)
    label: str = Field(min_length=2, max_length=200)
    ref: str | None = Field(default=None, max_length=120)


class InformedIn(BaseModel):
    audience: Literal["patient", "caregiver", "staff", "payer"]
    channel: Literal["call", "text", "email", "fax", "portal", "studio", "in_person"]
    label: str = Field(min_length=2, max_length=200)
    message_id: str | None = Field(default=None, max_length=120)
    delivery: Literal["queued", "sent", "delivered", "read", "failed"] = "sent"


class AckIn(BaseModel):
    system: str = Field(min_length=2, max_length=40)
    ack: str = Field(min_length=1, max_length=120)


class NextActionIn(BaseModel):
    nothing_left: bool = False
    owner_id: str | None = Field(default=None, max_length=36)
    due_at: datetime | None = None
    next_step: str | None = Field(default=None, max_length=300)


class FactsIn(BaseModel):
    fields: dict[str, Any] = Field(max_length=40)


class ContactIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    address: str = Field(min_length=7, max_length=40)
    person_ref: str | None = Field(default=None, max_length=60)
    audience: Literal["patient", "caregiver", "staff", "payer"] = "patient"


class TriggerIn(BaseModel):
    trigger: Literal["webhook", "import", "inbound_call"] = "webhook"
    fields: dict[str, Any] = Field(default_factory=dict, max_length=40)
    contacts: list[ContactIn] | None = Field(default=None, max_length=20)
    source_ref: str | None = Field(default=None, max_length=80)


class ReplyIn(BaseModel):
    answer: Literal["yes", "no", "no_answer"]
    message_id: str | None = Field(default=None, max_length=80)


class DecideIn(BaseModel):
    kind: str = Field(max_length=20)
    id: str = Field(max_length=36)
    decision: str = Field(max_length=20)


def _engine(request: Request) -> playbooks.PlaybookEngine | None:
    return getattr(request.app.state, "playbooks", None)


async def _detail(session, principal, settings, case):
    return await service.detail(session, principal, settings, case)


@router.get("/case-types")
async def list_case_types(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    """The case types this workspace's product line offers (careplix_api.packs)."""
    from careplix_api import packs
    from careplix_api.models import Workspace

    ws = await session.get(Workspace, principal.workspace_id)
    line = ws.product_line if ws else None
    return [t for t in types.view() if packs.allows_case_type(line, t["id"])]


@router.get("/cases")
async def list_cases(
    q: str = "",
    case_type: str | None = None,
    status: str | None = None,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
):
    return await service.list_cases(session, principal, q, case_type or None, status or None)


@router.post("/cases")
async def create_case(
    body: CaseIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.create(session, principal, body.case_type, body.fields)
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.post("/cases/samples")
async def add_samples(
    body: SamplesIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    made = await service.add_samples(session, principal, body.case_type, body.count)
    await session.commit()
    return [service.row(c) for c in made]


@router.get("/cases/{case_id}")
async def get_case(
    case_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/status")
async def set_status(
    case_id: str,
    body: StatusIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    await service.set_status(
        session,
        principal,
        case,
        body.status,
        reason=body.reason,
        owner_id=body.owner_id,
        due_at=body.due_at,
        next_step=body.next_step,
    )
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/evidence")
async def add_evidence(
    case_id: str,
    body: EvidenceIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    contract.add_link(
        session,
        case,
        part="evidence",
        kind=body.kind,
        ref=body.ref,
        label=body.label,
        actor=principal.user_id,
    )
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/informed")
async def add_informed(
    case_id: str,
    body: InformedIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    contract.add_link(
        session,
        case,
        part="informed",
        kind="message",
        ref=body.message_id,
        label=body.label,
        audience=body.audience,
        channel=body.channel,
        delivery=body.delivery,
        actor=principal.user_id,
    )
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/ack")
async def acknowledge(
    case_id: str,
    body: AckIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    contract.acknowledge(case, body.system.strip(), body.ack.strip())
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.put("/cases/{case_id}/next-action")
async def next_action(
    case_id: str,
    body: NextActionIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    if not body.nothing_left and not (body.owner_id and body.due_at and body.next_step):
        raise ApiError(
            422, "owner_required", "Say there's nothing left, or give an owner, due date and step."
        )
    contract.set_next_action(
        case,
        owner_id=body.owner_id,
        due_at=body.due_at,
        next_step=body.next_step,
        nothing_left=body.nothing_left,
    )
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/clinician-signoff")
async def clinician_signoff(
    case_id: str,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    await inbox.sign_off(session, principal, case)
    await session.commit()
    return await _detail(session, principal, settings, case)


@router.put("/cases/{case_id}/facts")
async def update_facts(
    case_id: str,
    body: FactsIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Fill gaps a trigger couldn't (e.g. a phone number), so a blocked playbook can resume."""
    case = await service.get(session, principal, case_id)
    await service.update_facts(session, principal, case, body.fields)
    await session.commit()
    return await _detail(session, principal, settings, case)


# --- playbooks ------------------------------------------------------------------------------------


@router.get("/playbooks")
async def list_playbooks(principal: Principal = Depends(viewer)):
    return {"playbooks": playbooks.view(), "triggers": playbooks.TRIGGERS}


@router.post("/playbooks/{playbook_id}/trigger")
async def trigger(
    playbook_id: str,
    body: TriggerIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """An EHR event, worklist row or inbound call hands a playbook its work (synthetic only)."""
    playbook = playbooks.PLAYBOOKS.get(playbook_id)
    if playbook is None:
        raise ApiError(404, "not_found", "No such playbook.")
    source = (
        "webhook" if body.trigger == "webhook" else body.trigger.replace("inbound_call", "inbound")
    )
    case = await service.create(
        session,
        principal,
        playbook.case_type,
        body.fields,
        source=source,
        source_ref=body.source_ref,
        allow_incomplete=True,
    )
    box = PhiBox(settings)
    contacts = (
        [
            {
                "label": c.label,
                "person_ref": c.person_ref,
                "audience": c.audience,
                "address_from": "sealed",
                "address": box.seal({"address": c.address}),
            }
            for c in body.contacts
        ]
        if body.contacts
        else None
    )
    await playbooks.start(session, case, playbook.id, contacts=contacts, owner_id=principal.user_id)
    await session.commit()
    engine = _engine(request)
    if engine is not None:
        await engine.advance(case.id)
    await session.refresh(case)
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/playbook")
async def start_playbook(
    case_id: str,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    await playbooks.start(session, case, owner_id=principal.user_id)
    await session.commit()
    if (engine := _engine(request)) is not None:
        await engine.advance(case.id)
    await session.refresh(case)
    return await _detail(session, principal, settings, case)


@router.post("/cases/{case_id}/playbook/resume")
async def resume_playbook(
    case_id: str,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    await playbooks.resume(session, case, principal.user_id)
    await session.commit()
    if (engine := _engine(request)) is not None:
        await engine.advance(case.id)
    await session.refresh(case)
    return await _detail(session, principal, settings, case)


# --- contact attempts -----------------------------------------------------------------------------


@router.post("/attempts/{attempt_id}/reply")
async def reply(
    attempt_id: str,
    body: ReplyIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Record a person's answer (staff taking it by phone; channels call record_reply directly)."""
    attempt = await session.get(ContactAttempt, attempt_id)
    if attempt is None or attempt.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Attempt not found.")
    await attempts.record_reply(
        session, settings, attempt, body.answer, principal.user_id, message_id=body.message_id
    )
    await session.commit()
    if (engine := _engine(request)) is not None:
        await engine.advance(attempt.case_id)
    case = await session.get(Case, attempt.case_id)
    await session.refresh(case)
    return await _detail(session, principal, settings, case)


# --- the one review inbox -------------------------------------------------------------------------


@router.get("/inbox")
async def review_inbox(
    kind: str | None = None,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await inbox.items(session, principal, settings, kind or None)


@router.post("/inbox/decide")
async def inbox_decide(
    body: DecideIn,
    request: Request,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    result = await inbox.decide(session, principal, body.kind, body.id, body.decision)
    await session.commit()
    if result.get("send"):
        from careplix_api.practice import intents

        # Send now rather than on the next worker pass, so the reviewer sees the outcome.
        await intents.execute_approved(
            request.app.state.db,
            settings,
            getattr(request.app.state, "integration_transport", None),
            only=[result["send"]],
        )
    return await inbox.items(session, principal, settings)


@router.get("/approval-policy")
async def approval_policy(principal: Principal = Depends(viewer)):
    return {"rules": policy.view(principal.workspace_id), "approvers": list(policy.APPROVERS)}
