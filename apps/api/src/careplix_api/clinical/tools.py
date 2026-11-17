"""Voice tools for clinical support. Both write to a case; neither decides anything.

- ``request_refill`` (front-desk receptionist, inbound): a verified caller's refill request
  becomes a ``refill_request`` case worked by its playbook (protocol facts, clinician queue,
  approved words back to the patient).
- ``record_check_answers`` (clinical-check agent, outbound): the patient's answers to the case's
  scripted questions, by question key. A red-flag answer escalates to a clinician at once and
  the result gives the agent the approved words to read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from careplix_api import events
from careplix_api.cases import playbooks
from careplix_api.clinical import answers, library, scripts
from careplix_api.errors import ApiError, Rejected
from careplix_api.models import AuditEvent, Case, EhrRecord, Task, Workspace
from careplix_api.phi import PhiBox

MAX_REFILLS_PER_CALL = 3
MAX_ANSWER_CALLS = 40
REFILL_SAID = (
    "Your refill request is with your care team. They review requests, usually within two "
    "business days, and you'll get a text when it's decided. I can't say whether it will be "
    "approved."
)


async def request_refill(session, attempt, _version, args, _evidence, env):
    from careplix_api.practice.receptionist import _connection, _state

    state = _state(attempt)
    patient = state.get("patient_ref")
    if not patient:
        raise Rejected("not_verified", "Verify the caller with find_patient first.")
    made = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "clinical.refill_requested", AuditEvent.target == attempt.id)
    )
    if made >= MAX_REFILLS_PER_CALL:
        raise Rejected("refill_limit", "I've already sent the refill requests for this call.")
    conn = await _connection(session, attempt)
    rec = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == patient,
        )
    )
    pii = PhiBox(env.settings).open(rec.pii) if rec else {}
    ws = await session.get(Workspace, attempt.workspace_id)
    name = " ".join(p for p in (pii.get("first_name"), pii.get("last_name")) if p)
    case = await playbooks._create(
        session,
        attempt.workspace_id,
        "refill_request",
        {
            "patient_name": name or None,
            "patient_phone": pii.get("phone") or attempt.caller_e164,
            "date_of_birth": pii.get("date_of_birth"),
            "practice_name": ws.name if ws else None,
            "practice_phone": env.settings.sandbox_number or None,
            "medication": args["medication"],
            "dose": args.get("dose") or None,
            "pharmacy": args["pharmacy"],
            "requested_by": "call",
        },
        source="inbound",
        source_ref=f"call:{attempt.id}:{made + 1}",
        extra={
            "patient_ref": patient,
            "ehr": {"connection_id": conn.id, "patient_id": patient},
        },
    )
    if case is None:
        raise Rejected("not_offered", "Refill requests aren't handled here; take a message.")
    from careplix_api.cases import contract

    contract.add_link(
        session,
        case,
        part="evidence",
        kind="call",
        ref=attempt.id,
        label="Refill requested by the patient on a call",
        actor="agent_runtime",
    )
    await playbooks.start(session, case, "refill_request")
    session.add(
        AuditEvent(
            workspace_id=attempt.workspace_id,
            actor="agent_runtime",
            action="clinical.refill_requested",
            target=attempt.id,
            detail={"case": case.id},
        )
    )
    await events.append(session, attempt, "refill.requested", "api", {"case_id": case.id})
    return {"state": "requested", "reference": case.case_ref, "message": REFILL_SAID}


async def _call_case(session, attempt) -> Case:
    task = await session.get(Task, attempt.task_id) if attempt.task_id else None
    case = await session.get(Case, task.case_id) if task and task.case_id else None
    if case is None or case.workspace_id != attempt.workspace_id:
        raise Rejected("no_case", "This call has no case to record answers on.")
    return case


async def record_check_answers(session, attempt, _version, args, _evidence, env):
    rehearsal = await _rehearsal(session, attempt, args)
    if rehearsal is not None:
        return rehearsal
    case = await _call_case(session, attempt)
    used = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "clinical.answers_recorded", AuditEvent.target == attempt.id)
    )
    if used >= MAX_ANSWER_CALLS:
        raise Rejected("answer_limit", "Answers for this call are already recorded.")
    try:
        out = await answers.record(
            session,
            env.settings,
            case,
            args["answers"],
            source="call",
            actor="agent_runtime",
            ref=attempt.task_id,
        )
    except ApiError as err:
        raise Rejected(err.code, err.message) from err
    session.add(
        AuditEvent(
            workspace_id=attempt.workspace_id,
            actor="agent_runtime",
            action="clinical.answers_recorded",
            target=attempt.id,
            detail={
                "case": case.id,
                "questions": [a["question"] for a in args["answers"]],
                "red_flag": out["red_flag"],
            },
        )
    )
    await events.append(
        session,
        attempt,
        "clinical.answers",
        "api",
        {"count": len(args["answers"]), "red_flag": out["red_flag"]},
    )
    return out


async def _rehearsal(session, attempt, args) -> dict[str, Any] | None:
    """A Voice Studio run has a scenario and no case: nothing is stored on a case, but the red-flag
    rule still applies (by question key, across the scripts) and returns the words to read, so the
    judge can check the agent read them exactly."""
    task = await session.get(Task, attempt.task_id) if attempt.task_id else None
    if task is None or task.case_id or not task.scenario_id:
        return None
    flags = []
    for item in args["answers"]:
        for script in scripts.SCRIPTS.values():
            q = next((q for q in script["questions"] if q["key"] == item["question"]), None)
            if q is not None and scripts.is_red_flag(q, item["answer"]):
                flags.append(item["question"])
                break
    await events.append(
        session,
        attempt,
        "clinical.answers",
        "api",
        {"count": len(args["answers"]), "red_flag": bool(flags)},
    )
    if not flags:
        return {"state": "recorded", "remaining": [], "red_flag": False}
    row = await library.approved(session, attempt.workspace_id, library.RED_FLAG_KEY)
    return {
        "state": "recorded",
        "remaining": [],
        "red_flag": True,
        "say": library.render(row.body, practice_phone="") if row else library.PLATFORM_RED_FLAG,
        "message": "Read the words in 'say' exactly, then end the call politely.",
    }


HANDLERS = {"request_refill": request_refill, "record_check_answers": record_check_answers}
