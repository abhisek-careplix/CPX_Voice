"""Clinical support API: refills, discharge follow-up, pre-visit, the approved instructions
library, and the clinician's decisions. Every decision needs ``clinical_signoff``."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.auth import Principal, require
from careplix_api.cases import playbooks
from careplix_api.cases import service as case_service
from careplix_api.clinical import answers, library, scripts, service
from careplix_api.clinical import playbook as clinical_playbook
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import EhrRecord
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
TYPES = clinical_playbook.CLINICAL_TYPES


class InstructionIn(BaseModel):
    key: str = Field(min_length=3, max_length=40)
    title: str = Field(min_length=2, max_length=120)
    topic: Literal["refill", "discharge", "previsit", "general"]
    outcome: Literal["approved", "denied", "needs_visit"] | None = None
    channel: Literal["text", "call", "any"] = "any"
    body: str = Field(min_length=10, max_length=600)


class InstructionEditIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    topic: Literal["refill", "discharge", "previsit", "general"] | None = None
    outcome: Literal["approved", "denied", "needs_visit"] | None = None
    channel: Literal["text", "call", "any"] | None = None
    body: str | None = Field(default=None, min_length=10, max_length=600)


class DecideIn(BaseModel):
    outcome: Literal["approved", "denied", "needs_visit", "other"]
    instruction_id: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=300)


class AnswerIn(BaseModel):
    question: str = Field(max_length=40)
    answer: Literal["yes", "no", "unsure", "declined", "text"]
    detail: str | None = Field(default=None, max_length=300)


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(min_length=1, max_length=12)


class RedFlagIn(BaseModel):
    action: Literal[
        "called_patient", "sent_to_emergency", "booked_visit", "no_action_needed", "other"
    ]
    note: str | None = Field(default=None, max_length=300)


class NewCaseIn(BaseModel):
    case_type: Literal["refill_request", "discharge_followup", "previsit_prep"]
    fields: dict[str, Any] = Field(default_factory=dict, max_length=20)
    patient_ref: str | None = Field(default=None, max_length=60)


class ImportIn(BaseModel):
    csv: str = Field(max_length=200_000)


class PrevisitIn(BaseModel):
    appointment_id: str = Field(max_length=40)


async def _advance(request: Request, case_id: str) -> None:
    engine = getattr(request.app.state, "playbooks", None)
    if engine is not None:
        await engine.advance(case_id)


async def _case(session: AsyncSession, principal: Principal, case_id: str):
    case = await case_service.get(session, principal, case_id)
    if cases.type_id(case) not in TYPES:
        raise ApiError(404, "not_found", "Not a clinical support case.")
    return case


async def _detail(session, principal, settings, case_id: str):
    case = await _case(session, principal, case_id)
    await session.refresh(case)
    return await service.detail(session, principal, settings, case)


# --- scripts and the approved instructions library ---------------------------------------------


@router.get("/clinical/scripts")
async def get_scripts(principal: Principal = Depends(viewer)):
    return scripts.view()


@router.get("/clinical/instructions")
async def list_instructions(
    principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    rows = await library.listing(session, principal.workspace_id)
    await session.commit()
    return {
        "instructions": [library.view(r) for r in rows],
        "placeholders": list(library.PLACEHOLDERS),
        "can_approve": principal.can("clinical_signoff"),
    }


@router.post("/clinical/instructions")
async def create_instruction(
    body: InstructionIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    row = await library.create(session, principal, body.model_dump())
    await session.commit()
    return library.view(row)


@router.put("/clinical/instructions/{ref}")
async def edit_instruction(
    ref: str,
    body: InstructionEditIn,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
):
    if not (principal.can("start_task") or principal.can("clinical_signoff")):
        raise ApiError(403, "forbidden", "Your role doesn't edit instructions.")
    row = await library.get(session, principal.workspace_id, ref)
    await library.edit(session, principal, row, body.model_dump(exclude_unset=True))
    await session.commit()
    return library.view(row)


@router.post("/clinical/instructions/{ref}/approve")
async def approve_instruction(
    ref: str,
    principal: Principal = Depends(require("clinical_signoff")),
    session: AsyncSession = Session,
):
    row = await library.get(session, principal.workspace_id, ref)
    await library.approve(session, principal, row)
    await session.commit()
    return library.view(row)


@router.post("/clinical/instructions/{ref}/retire")
async def retire_instruction(
    ref: str,
    principal: Principal = Depends(require("clinical_signoff")),
    session: AsyncSession = Session,
):
    row = await library.get(session, principal.workspace_id, ref)
    await library.retire(session, principal, row)
    await session.commit()
    return library.view(row)


# --- clinical cases -----------------------------------------------------------------------------


@router.get("/clinical/cases")
async def list_clinical(
    type: Literal["refill_request", "discharge_followup", "previsit_prep"],
    status: Literal["open", "closed", "all"] = "open",
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
):
    rows = await service.cases_of(session, principal, type, None if status == "all" else status)
    out = [service.row(c) for c in rows]
    counts: dict[str, int] = {}
    for r in out:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    return {
        "cases": out,
        "counts": counts,
        "script": scripts.script(type),
        "can_decide": principal.can("clinical_signoff"),
    }


@router.post("/clinical/cases")
async def new_clinical_case(
    body: NewCaseIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Staff entry: a refill request taken by hand, a discharge notice, or a pre-visit case."""
    if body.case_type == "discharge_followup":
        case = await service.discharge_from_staff(
            session, principal, settings, {**body.fields, "patient_ref": body.patient_ref}
        )
    else:
        fields = dict(body.fields)
        if body.case_type == "refill_request":
            fields.setdefault("requested_by", "staff")
        case = await case_service.create(
            session, principal, body.case_type, fields, source="staff", allow_incomplete=True
        )
        if body.patient_ref:
            case.data = {**case.data, "patient_ref": body.patient_ref}
        await playbooks.start(session, case, body.case_type, owner_id=principal.user_id)
    await session.commit()
    await _advance(request, case.id)
    return await _detail(session, principal, settings, case.id)


@router.post("/clinical/discharges/import")
async def import_discharges(
    body: ImportIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    made = await service.discharge_import(session, principal, settings, body.csv)
    await session.commit()
    return made


@router.get("/clinical/previsit/upcoming")
async def upcoming_visits(
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Booked visits in the next 7 days, and whether each has a pre-visit case."""
    from datetime import timedelta

    from careplix_api.models import Case
    from careplix_api.phi import PhiBox, initials

    now = utcnow()
    have = set(
        await session.scalars(
            select(Case.source_ref).where(
                Case.workspace_id == principal.workspace_id, Case.source == "previsit"
            )
        )
    )
    box = PhiBox(settings)
    out = []
    for appt in await session.scalars(
        select(EhrRecord)
        .where(
            EhrRecord.workspace_id == principal.workspace_id,
            EhrRecord.resource == "appointment",
            EhrRecord.status.in_(("booked", "confirmed")),
            EhrRecord.starts_at > now,
            EhrRecord.starts_at <= now + timedelta(days=7),
        )
        .order_by(EhrRecord.starts_at)
        .limit(100)
    ):
        patient = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == appt.connection_id,
                EhrRecord.resource == "patient",
                EhrRecord.source_id == appt.patient_ref,
            )
        )
        out.append(
            {
                "id": appt.id,
                "starts_at": appt.starts_at,
                "patient": initials(box.open(patient.pii)) if patient else "Patient",
                "prepared": f"appt:{appt.connection_id}:{appt.source_id}" in have,
            }
        )
    return {"visits": out}


@router.post("/clinical/previsit")
async def start_previsit(
    body: PrevisitIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    appt = await session.get(EhrRecord, body.appointment_id)
    if (
        appt is None
        or appt.workspace_id != principal.workspace_id
        or appt.resource != "appointment"
    ):
        raise ApiError(404, "not_found", "Visit not found.")
    case = await clinical_playbook.open_previsit(
        session, settings, appt, owner_id=principal.user_id
    )
    if case is None:
        raise ApiError(422, "not_offered", "This workspace's product line doesn't offer this.")
    await session.commit()
    await _advance(request, case.id)
    return await _detail(session, principal, settings, case.id)


@router.get("/clinical/cases/{case_id}")
async def get_clinical_case(
    case_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await _detail(session, principal, settings, case_id)


@router.post("/clinical/cases/{case_id}/decide")
async def decide(
    case_id: str,
    body: DecideIn,
    request: Request,
    principal: Principal = Depends(require("clinical_signoff")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await _case(session, principal, case_id)
    await service.decide_refill(
        session, principal, case, body.outcome, body.instruction_id, body.note
    )
    await session.commit()
    await _advance(request, case.id)
    return await _detail(session, principal, settings, case_id)


@router.post("/clinical/cases/{case_id}/sign-off")
async def sign_off(
    case_id: str,
    request: Request,
    principal: Principal = Depends(require("clinical_signoff")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The clinician reviewed the discharge answers or the pre-visit brief."""
    from careplix_api.cases import inbox

    case = await _case(session, principal, case_id)
    if not answers.clinical(case).get("ready"):
        raise ApiError(409, "not_ready", "There's nothing to review yet.")
    if answers.red_flag_open(case):
        raise ApiError(409, "red_flag_open", "Acknowledge the red flag first.")
    await inbox.sign_off(session, principal, case)
    await session.commit()
    await _advance(request, case.id)
    return await _detail(session, principal, settings, case_id)


@router.post("/clinical/cases/{case_id}/answers")
async def staff_answers(
    case_id: str,
    body: AnswersIn,
    request: Request,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Staff record what the patient said (a nurse made the call by hand)."""
    case = await _case(session, principal, case_id)
    out = await service.record_staff_answers(
        session, principal, settings, case, [a.model_dump() for a in body.answers]
    )
    await session.commit()
    await _advance(request, case.id)
    return {**out, "case": await _detail(session, principal, settings, case_id)}


@router.post("/clinical/cases/{case_id}/red-flag")
async def acknowledge_red_flag(
    case_id: str,
    body: RedFlagIn,
    request: Request,
    principal: Principal = Depends(require("clinical_signoff")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await _case(session, principal, case_id)
    await answers.acknowledge_red_flag(session, principal, case, body.action, body.note or "")
    await session.commit()
    await _advance(request, case.id)
    return await _detail(session, principal, settings, case_id)


@router.post("/clinical/cases/{case_id}/form-link")
async def new_form_link(
    case_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """A fresh questionnaire link to give the patient another way (older links stop working)."""
    from careplix_api.forms import service as forms

    case = await _case(session, principal, case_id)
    if not scripts.script(cases.type_id(case)):
        raise ApiError(422, "no_form", "This case has no questionnaire.")
    _link, url = await forms.issue(session, settings, case, "questionnaire", principal.user_id)
    await session.commit()
    return {"url": url}
