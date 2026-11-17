"""Clinical decisions and case views: the clinician's refill decision, staff-entered answers,
discharge notices by hand or import, and what the Refills, Discharge and Pre-visit pages show."""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.auth import Principal
from careplix_api.cases import contract, playbooks, policy, service
from careplix_api.cases.models import PlaybookRun
from careplix_api.cases.playbooks import run_view
from careplix_api.clinical import answers, library, scripts
from careplix_api.clinical.models import ApprovedInstruction
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Case
from careplix_api.settings import Settings

DECISIONS = ("approved", "denied", "needs_visit", "other")
DISCHARGE_COLUMNS = (
    "practice_name",
    "practice_phone",
    "patient_name",
    "patient_phone",
    "date_of_birth",
    "facility",
    "discharged_on",
    "clinician_name",
    "patient_ref",
)
MAX_IMPORT_ROWS = 500


async def decide_refill(
    session: AsyncSession,
    principal: Principal,
    case: Case,
    outcome: str,
    instruction_id: str | None,
    note: str | None,
) -> None:
    """A clinician's refill decision, with the approved words the patient will hear.

    ``other`` (or no instruction) means the clinician's words aren't in the library: a person
    tells the patient. The decision and the sign-off are the contract's clinician part.
    """
    if cases.type_id(case) != "refill_request":
        raise ApiError(422, "not_refill", "Only refill requests are decided here.")
    who = policy.approver("clinical.refill_decision", case.workspace_id)
    if not policy.can_decide(principal, who):
        raise ApiError(403, "forbidden", "A clinician decides refill requests.")
    if outcome not in DECISIONS:
        raise ApiError(422, "bad_decision", f"Decision is one of {', '.join(DECISIONS)}.")
    if answers.clinical(case).get("decision"):
        raise ApiError(409, "already_decided", "This request is already decided.")
    if (case.status or "open") in contract.TERMINAL:
        raise ApiError(409, "closed", "This case is closed.")
    row: ApprovedInstruction | None = None
    if outcome != "other":
        if not instruction_id:
            raise ApiError(
                422,
                "instruction_required",
                "Choose the approved words the patient will hear, or decide 'other' and a person "
                "will tell them.",
            )
        row = await library.get(session, case.workspace_id, instruction_id)
        if row.state != "approved":
            raise ApiError(422, "not_approved", "Those words aren't approved yet.")
        if row.topic != "refill" or (row.outcome and row.outcome != outcome):
            raise ApiError(422, "wrong_instruction", "Those words don't match this decision.")
    now = utcnow()
    answers.save(
        case,
        {
            "decision": {
                "outcome": outcome,
                "instruction_key": row.key if row else None,
                "instruction_title": row.title if row else None,
                "version": row.version if row else None,
                "note": (note or "").strip()[:300] or None,
                "by": principal.user_id,
                "name": principal.display_name,
                "at": now.isoformat(),
            }
        },
    )
    case.clinician_by, case.clinician_at = principal.user_id, now
    contract.add_link(
        session,
        case,
        part="evidence",
        kind="approval",
        ref=principal.user_id,
        label=f"Clinician decision by {principal.display_name}: {outcome.replace('_', ' ')}",
        actor=principal.user_id,
    )
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor=principal.user_id,
            action="clinical.refill_decided",
            target=case.id,
            detail={"outcome": outcome, "instruction": row.key if row else None},
        )
    )
    await answers._wake(session, case)


async def cases_of(
    session: AsyncSession, principal: Principal, case_type: str, status: str | None = None
) -> list[Case]:
    query = select(Case).where(
        Case.workspace_id == principal.workspace_id, Case.case_type == case_type
    )
    rows = list(await session.scalars(query.order_by(Case.created_at.desc()).limit(200)))
    if status == "open":
        rows = [c for c in rows if (c.status or "open") not in contract.TERMINAL]
    elif status == "closed":
        rows = [c for c in rows if (c.status or "open") in contract.TERMINAL]
    return rows


def _stage(case: Case) -> str:
    c = answers.clinical(case)
    tid = cases.type_id(case)
    if (case.status or "open") in contract.TERMINAL:
        return case.status or "done"
    if c.get("red_flags") and not c.get("red_flag_ack"):
        return "red_flag"
    if case.status in ("handed_off", "blocked"):
        return case.status
    if tid == "refill_request":
        if c.get("delivered"):
            return "told"
        if c.get("decision"):
            return "decided"
        return "with_clinician" if c.get("ready") else "preparing"
    if case.clinician_by:
        return "reviewed"
    if c.get("ready"):
        return "with_clinician"
    return "waiting_on_patient" if case.playbook else "new"


def row(case: Case) -> dict[str, Any]:
    c = answers.clinical(case)
    facts = cases.context(case)
    base = service.row(case)
    return {
        **base,
        "stage": _stage(case),
        "red_flag": bool(c.get("red_flags")) and not c.get("red_flag_ack"),
        "decision": (c.get("decision") or {}).get("outcome"),
        "answered": len(answers.answers(case)),
        "questions": len([q for q in scripts.questions(cases.type_id(case)) if q["required"]]),
        "medication": facts.get("medication"),
        "pharmacy": facts.get("pharmacy"),
        "facility": facts.get("facility"),
        "discharged_on": facts.get("discharged_on"),
        "call_by": facts.get("call_by"),
        "appointment_at": facts.get("appointment_at"),
        "clinician_name": facts.get("clinician_name"),
    }


async def detail(
    session: AsyncSession, principal: Principal, settings: Settings, case: Case
) -> dict[str, Any]:
    from careplix_api.forms import service as forms

    c = answers.clinical(case)
    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    tid = cases.type_id(case)
    out = {
        **row(case),
        "case": await service.detail(session, principal, settings, case),
        "script": scripts.script(tid),
        "answers": answers.summary(case),
        "remaining": answers.remaining(case),
        "protocol": c.get("protocol") or [],
        "decided": c.get("decision"),
        "delivered": c.get("delivered"),
        "red_flags": c.get("red_flags") or [],
        "red_flag_ack": c.get("red_flag_ack"),
        "brief": c.get("brief"),
        "run": run_view(run),
        "form": forms.view(await forms.latest(session, case, "questionnaire")),
        "can_decide": policy.can_decide(principal, "clinician"),
    }
    return out


async def record_staff_answers(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    case: Case,
    given: list[dict[str, Any]],
) -> dict[str, Any]:
    if not principal.can("start_task") and not principal.can("clinical_signoff"):
        raise ApiError(403, "forbidden", "Your role doesn't record answers.")
    out = await answers.record(
        session,
        settings,
        case,
        given,
        source="staff",
        actor=principal.user_id,
        ref=f"staff:{principal.user_id}",
    )
    # A nurse who made the call by hand picks the playbook up again from the answers.
    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    pb = playbooks.PLAYBOOKS.get(run.playbook_id) if run else None
    if (
        run is not None
        and pb is not None
        and run.state in ("handed_off", "blocked")
        and any(s.kind == "await_answers" for s in pb.steps)
    ):
        run.step = pb.index("await_answers")
        run.state, run.next_run_at, run.leased_until = "running", utcnow(), None
        run.data = {**(run.data or {}), "round_started_at": utcnow().isoformat()}
        await contract.transition(session, case, "in_progress", principal.user_id)
    return out


async def discharge_from_staff(
    session: AsyncSession, principal: Principal, settings: Settings, values: dict[str, Any]
) -> Case:
    from careplix_api.clinical import playbook

    case = await playbook.open_discharge(
        session,
        settings,
        principal.workspace_id,
        {k: values.get(k) for k in DISCHARGE_COLUMNS if k != "patient_ref"},
        source="staff",
        source_ref=None,
        patient_ref=values.get("patient_ref") or None,
        owner_id=principal.user_id,
    )
    if case is None:
        raise ApiError(422, "not_offered", "This workspace's product line doesn't offer this.")
    case.created_by = principal.user_id
    return case


async def discharge_import(
    session: AsyncSession, principal: Principal, settings: Settings, text: str
) -> dict[str, int]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not {"patient_name", "discharged_on"} <= {
        f.strip() for f in reader.fieldnames
    }:
        raise ApiError(
            422, "bad_columns", f"The file needs these columns: {', '.join(DISCHARGE_COLUMNS)}."
        )
    made = 0
    for n, raw in enumerate(reader, 2):
        if n > MAX_IMPORT_ROWS + 1:
            raise ApiError(422, "too_many_rows", f"Import at most {MAX_IMPORT_ROWS} rows.")
        values = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(values.values()):
            continue
        try:
            await discharge_from_staff(session, principal, settings, values)
        except ApiError as err:
            raise ApiError(422, err.code, f"Line {n}: {err.message}") from err
        made += 1
    return {"created": made}
