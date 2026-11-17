"""Intake and registration (OPERATING_PLAN §5.1): forms by secure link, insurance card capture,
and the demographics and coverage proposed to the EHR as write intents.

Playbook ``patient_intake``: the registration link goes by text; the patient fills in their
details and photographs their insurance card; their answers (sealed) become two write intents,
a patient update and a coverage record, that a person approves in the review inbox (policy
``playbook.ehr_write``). EHRs that can't take those writes yet (NexHealth's v3.1 beta isn't on)
turn them into a staff task: the hub marks them ``manual`` and the front desk enters them, with
the card photos on the case.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.cases import contract, playbooks
from careplix_api.cases import service as case_service
from careplix_api.cases.models import PlaybookRun
from careplix_api.cases.playbooks import (
    POLL,
    Outcome,
    Playbook,
    RunContext,
    Step,
    _hand_off,
    _next,
    _wait,
    run_view,
)
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Case, PayerConnection, WriteIntent
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

DEMOGRAPHICS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "sex",
    "email",
    "phone",
    "address_line1",
    "city",
    "state",
    "postal_code",
    "preferred_language",
)
COVERAGE = ("payer_name", "member_id", "group_number", "subscriber_name", "relationship")
DONE_STATES = ("accepted", "written")
STOPPED = ("rejected", "failed", "needs_review")

PLAYBOOK = Playbook(
    id="patient_intake",
    name="Intake and registration",
    case_type="patient_intake",
    triggers=("webhook", "import", "inbound_call"),
    steps=(
        Step("check_facts", "Check there's a number to text"),
        Step("send_form", "Text the registration link", {"form": "intake", "on_fail": "hand_off"}),
        Step("await_form", "Wait for the form (one reminder)", {"timeout_h": 48, "retries": 1}),
        Step("propose_registration", "Propose demographics and coverage to the EHR"),
        Step("await_registration", "Wait for the EHR (or staff) to take them"),
        Step("inform_staff", "Tell the front desk"),
        Step("close", "Check the completion contract"),
    ),
    due=timedelta(days=7),
    completion="The patient registered by secure link, the EHR accepted their demographics and "
    "coverage (or staff entered them), and the patient and front desk are told.",
    hand_off="No form after a reminder, or the EHR can't take the write: the front desk "
    "completes registration from the case, with the card photos.",
)


async def await_form(ctx: RunContext, step: Step) -> Outcome:
    from careplix_api.forms import service as forms

    link = await forms.latest(ctx.session, ctx.case, "intake")
    if link is not None and link.submitted_at is not None:
        return _next("Form received")
    from datetime import datetime

    started = datetime.fromisoformat(ctx.data.get("round_started_at") or utcnow().isoformat())
    deadline = started + timedelta(hours=float(step.params.get("timeout_h", 48)))
    if utcnow() < deadline:
        return _wait(min(utcnow() + POLL * 10, deadline), "Waiting for the form")
    if int(ctx.data.get("round", 1)) <= int(step.params.get("retries", 0)):
        return Outcome("goto", "Sending the link again", step=ctx.playbook.index("send_form"))
    return _hand_off(
        "The patient hasn't completed registration.",
        "Call the patient to complete registration (send a new link from the case).",
    )


async def propose_registration(ctx: RunContext, step: Step) -> Outcome:
    from careplix_api.forms import service as forms
    from careplix_api.practice import intents

    link = await forms.latest(ctx.session, ctx.case, "intake")
    values = forms.intake_answers(ctx.settings, link)
    ehr = (ctx.case.data or {}).get("ehr") or {}
    conn = await ctx.session.get(PayerConnection, ehr.get("connection_id") or "")
    if conn is None or not ehr.get("patient_id"):
        return _hand_off(
            "This patient isn't linked to an EHR chart yet.",
            "Enter the registration in the EHR from the case (card photos are on it).",
        )
    proposed = []
    demo = {k: values.get(k) for k in DEMOGRAPHICS if values.get(k)}
    if demo:
        proposed.append(
            await intents.propose(
                ctx.session,
                conn,
                ctx.settings,
                kind="patient_update",
                target_source_id=str(ehr["patient_id"]),
                payload=demo,
                reason="Registration: demographics the patient entered by secure link",
                source="playbook",
                requested_by="playbook",
            )
        )
    if not values.get("no_insurance") and (values.get("member_id") or values.get("payer_name")):
        proposed.append(
            await intents.propose(
                ctx.session,
                conn,
                ctx.settings,
                kind="coverage_create",
                target_source_id=str(ehr["patient_id"]),
                payload={k: values.get(k) for k in COVERAGE if values.get(k)},
                reason="Registration: coverage from the patient's insurance card",
                source="playbook",
                requested_by="playbook",
            )
        )
    for intent in proposed:
        contract.add_link(
            ctx.session,
            ctx.case,
            part="evidence",
            kind="write_intent",
            ref=intent.id,
            label=f"EHR {intent.kind.replace('_', ' ')} proposed from the registration form",
            actor="playbook",
        )
    ctx.put(intent_ids=[i.id for i in proposed])
    if not proposed:
        return _hand_off("The form had nothing to write.", "Check the registration by hand.")
    return _next(f"{len(proposed)} write(s) proposed")


async def await_registration(ctx: RunContext, step: Step) -> Outcome:
    ids = ctx.data.get("intent_ids") or []
    rows = [await ctx.session.get(WriteIntent, i) for i in ids]
    rows = [r for r in rows if r is not None]
    if not rows:
        return Outcome("fail", "The EHR writes are missing.")
    if all(r.state in DONE_STATES for r in rows):
        contract.acknowledge(
            ctx.case,
            "ehr",
            ";".join(f"{r.kind}:{r.remote_id or r.state}" for r in rows)[:120],
        )
        ctx.put(summary="Registration done: demographics and coverage are in the EHR.")
        return _next("Written")
    manual = [r for r in rows if r.state == "manual"]
    if manual:
        return _hand_off(
            "This EHR doesn't take registration writes yet.",
            "Enter the demographics and coverage in the EHR from the case, then record the "
            "acknowledgement.",
        )
    stopped = [r for r in rows if r.state in STOPPED]
    if stopped:
        return _hand_off(
            f"The EHR didn't take the registration ({stopped[0].state}).",
            "Check the registration in the EHR and enter what's missing.",
        )
    return _wait(utcnow() + POLL * 5, "Waiting on approval or the EHR")


def register() -> None:
    playbooks.PLAYBOOKS[PLAYBOOK.id] = PLAYBOOK
    playbooks.BY_CASE_TYPE[PLAYBOOK.case_type] = PLAYBOOK.id
    playbooks.HANDLERS.update(
        {
            "await_form": await_form,
            "propose_registration": propose_registration,
            "await_registration": await_registration,
        }
    )


register()


# --- API ------------------------------------------------------------------------------------------

router = APIRouter()


class IntakeIn(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict, max_length=20)
    connection_id: str | None = Field(default=None, max_length=40)
    patient_id: str | None = Field(default=None, max_length=40)


def _row(case: Case) -> dict[str, Any]:
    intake = (case.data or {}).get("intake") or {}
    return {
        **case_service.row(case),
        "submitted_at": intake.get("submitted_at"),
        "has_card_photo": bool(intake.get("has_card_photo")),
        "no_insurance": bool(intake.get("no_insurance")),
        "linked": bool(((case.data or {}).get("ehr") or {}).get("patient_id")),
    }


@router.get("/intake")
async def list_intake(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    rows = list(
        await session.scalars(
            select(Case)
            .where(Case.workspace_id == principal.workspace_id, Case.case_type == "patient_intake")
            .order_by(Case.created_at.desc())
            .limit(200)
        )
    )
    return {"cases": [_row(c) for c in rows]}


@router.post("/intake")
async def start_intake(
    body: IntakeIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await case_service.create(
        session, principal, "patient_intake", body.fields, source="staff", allow_incomplete=True
    )
    if body.connection_id or body.patient_id:
        conn = await session.get(PayerConnection, body.connection_id or "")
        if conn is None or conn.workspace_id != principal.workspace_id:
            raise ApiError(404, "not_found", "EHR connection not found.")
        case.data = {
            **case.data,
            "patient_ref": body.patient_id,
            "ehr": {"connection_id": conn.id, "patient_id": body.patient_id},
        }
    await playbooks.start(session, case, "patient_intake", owner_id=principal.user_id)
    await session.commit()
    engine = getattr(request.app.state, "playbooks", None)
    if engine is not None:
        await engine.advance(case.id)
    return await intake_detail(case.id, principal, session, settings)


@router.get("/intake/{case_id}")
async def intake_detail(
    case_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    from careplix_api.forms import service as forms

    case = await case_service.get(session, principal, case_id)
    if case.case_type != "patient_intake":
        raise ApiError(404, "not_found", "Not an intake case.")
    await session.refresh(case)
    link = await forms.latest(session, case, "intake")
    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    values = forms.intake_answers(settings, link) if link and link.submitted_at else {}
    writes = []
    for intent_id in ((run.data or {}).get("intent_ids") or []) if run else []:
        intent = await session.get(WriteIntent, intent_id)
        if intent is not None:
            writes.append({"id": intent.id, "kind": intent.kind, "state": intent.state})
    return {
        **_row(case),
        "case": await case_service.detail(session, principal, settings, case),
        "form": forms.view(link),
        "answers": {k: v for k, v in values.items() if k not in ("card_front", "card_back")},
        "card": {"front": values.get("card_front"), "back": values.get("card_back")},
        "writes": writes,
        "run": run_view(run),
    }


@router.post("/intake/{case_id}/link")
async def new_intake_link(
    case_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """A fresh registration link to give the patient another way (older links stop working)."""
    from careplix_api.forms import service as forms

    case = await case_service.get(session, principal, case_id)
    if case.case_type != "patient_intake":
        raise ApiError(404, "not_found", "Not an intake case.")
    _link, url = await forms.issue(session, settings, case, "intake", principal.user_id)
    await session.commit()
    return {"url": url}


@router.get("/reminders/cadence")
async def reminder_cadence(principal: Principal = Depends(viewer)):
    from careplix_api.practice import reminders

    return reminders.view()
