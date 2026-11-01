"""Server-side tool authorization and execution (doc 03 restricted tools, SEC06, FR09).

The model proposes; this module disposes. A tool runs only if it is pinned in the attempt's
published configuration, its arguments match the registry schema, the attempt is live, and the
tool's own limits hold. Rejections are recorded and audited (T08). Executions are keyed by the
model's tool-call ID so a retried request never repeats a side effect (FR11).
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_contracts as contracts
from careplix_api import events, templates
from careplix_api.calls_service import ACTIVE_STATES
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import Rejected
from careplix_api.models import (
    AgentVersion,
    AuditEvent,
    CallAttempt,
    Case,
    Finding,
    Task,
    ToolExecution,
    TranscriptSegment,
)
from careplix_api.practice import receptionist
from careplix_api.practice.receptionist import ToolEnv

SUBSTATES = {"ivr": "IVR", "hold": "Hold", "conversation": "Conversation"}
DTMF_COOLDOWN_S = 1.5
DTMF_MAX_PER_CALL = 40

__all__ = ["Rejected", "ToolEnv", "execute"]


async def execute(
    session: AsyncSession,
    attempt: CallAttempt,
    name: str,
    call_id: str,
    arguments: dict[str, Any],
    evidence_segment_ids: list[str] | None = None,
    env: ToolEnv | None = None,
) -> dict[str, Any]:
    existing = await session.scalar(
        select(ToolExecution).where(
            ToolExecution.attempt_id == attempt.id, ToolExecution.call_id == call_id
        )
    )
    if existing is not None:
        return _response(existing)

    if name == "record_finding" and isinstance(arguments, dict):
        # Evidence comes from the worker's transcript window, never from the model.
        arguments = {**arguments, "evidence_segment_ids": list(evidence_segment_ids or [])}
    version = await session.get(AgentVersion, attempt.agent_version_id)
    pinned = {t["name"]: t["version"] for t in version.config["tools"]}
    tool = contracts.tool_registry().get(name)
    execution = ToolExecution(
        attempt_id=attempt.id,
        call_id=call_id[:80],
        tool=name[:40],
        tool_version=pinned.get(name, "-"),
        actor="agent_runtime",
        arguments=arguments if tool else {},
        state="Authorized",
    )
    try:
        if tool is None or name not in pinned or pinned[name] != tool["version"]:
            raise Rejected("tool_not_granted", "This tool is not available on this call.")
        if "agent_runtime" not in tool["allowed_roles"]:
            raise Rejected("tool_not_granted", "This tool is not available to the agent.")
        errors = list(
            Draft202012Validator(
                tool["arguments_schema"], format_checker=FormatChecker()
            ).iter_errors(arguments)
        )
        if errors:
            raise Rejected("invalid_arguments", "The tool arguments were not valid.")
        if attempt.state not in ACTIVE_STATES:
            raise Rejected("attempt_not_active", "The call is no longer active.")
        handler = _HANDLERS[name]
        claimed = tool["classification"] == "write" and name in receptionist.HANDLERS
        if claimed:
            # EHR writes take seconds. Claim the call id first, so a retry that arrives while
            # this one is still running finds it instead of writing a second time (FR11).
            session.add(execution)
            await session.commit()
        try:
            execution.result = await handler(
                session, attempt, version, arguments, evidence_segment_ids or [], env
            )
        except Rejected:
            raise
        except Exception:
            if not claimed:
                raise
            # Never leave a claimed write "running" forever: say the outcome is unknown, and
            # the write queue (needs_review) is where a person settles it.
            await session.rollback()
            execution.state = "Unknown"
            execution.result = {
                "state": "needs_staff",
                "message": "The outcome isn't confirmed. Tell the caller the front desk will "
                "check and call back.",
            }
            session.add(execution)
            await events.append(
                session, attempt, "tool.unknown", "api", {"tool": name[:40], "call_id": call_id}
            )
            return _response(execution)
        execution.state = "Succeeded"
    except Rejected as rejection:
        execution.state = "Rejected"
        execution.rejection_code = rejection.code
        execution.result = {"error": rejection.code, "message": rejection.message}
        session.add(
            AuditEvent(
                workspace_id=attempt.workspace_id,
                actor="agent_runtime",
                action="tool.rejected",
                target=attempt.id,
                detail={"tool": name[:40], "code": rejection.code},
            )
        )
    session.add(execution)
    await events.append(
        session,
        attempt,
        "tool.succeeded" if execution.state == "Succeeded" else "tool.rejected",
        "api",
        {
            "tool": execution.tool,
            "call_id": execution.call_id,
            "rejection_code": execution.rejection_code,
        },
    )
    return _response(execution)


def _response(execution: ToolExecution) -> dict[str, Any]:
    if execution.state == "Authorized":
        return {
            "state": "running",
            "result": {"message": "Still working on that request. Wait a moment, then check."},
        }
    return {"state": execution.state.lower(), "result": execution.result or {}}


# --- Handlers ------------------------------------------------------------------------------------


async def _get_assigned_context(session, attempt, version, args, _evidence, _env=None):
    if attempt.case_id is None:
        raise Rejected("no_case_bound", "No case is linked to this call yet.")
    case = await session.get(Case, attempt.case_id)
    context = templates.case_context(case.data)
    allowed = set(version.config["guardrails"].get("allowed_disclosures", []))
    values = {f: context[f] for f in args["fields"] if f in allowed and f in context}
    return {"values": values, "refused": [f for f in args["fields"] if f not in values]}


async def _verify_caller(session, attempt, _version, args, _evidence, _env=None):
    tries = await session.scalar(
        select(func.count())
        .select_from(ToolExecution)
        .where(ToolExecution.attempt_id == attempt.id, ToolExecution.tool == "verify_caller")
    )
    if tries >= 2:
        raise Rejected("verification_limit", "Verification attempts are exhausted for this call.")
    candidates = await session.scalars(
        select(Case).where(Case.workspace_id == attempt.workspace_id)
    )
    match = next(
        (
            c
            for c in candidates
            if c.data["patient"]["member_id"] == args["member_id"]
            and c.data["patient"]["date_of_birth"] == args["date_of_birth"]
        ),
        None,
    )
    # The result never distinguishes "no such case" from "details did not match" (doc 05/07).
    if match is None:
        return {"verified": False}
    attempt.case_id = match.id
    if attempt.task_id:
        task = await session.get(Task, attempt.task_id)
        task.case_id = match.id
    await events.append(session, attempt, "case.linked", "api", {"case_ref": match.case_ref})
    return {"verified": True}


async def _record_finding(session, attempt, version, args, evidence, _env=None):
    template = templates.get_template(version.config["task"]["template_id"])
    field = args["field"]
    if field not in template["output_fields"]:
        raise Rejected("unknown_field", f"'{field}' is not a finding for this task.")
    if args["value"] is None and not args.get("null_reason"):
        raise Rejected("null_reason_required", "Give a reason when a value is missing.")
    known = set(
        await session.scalars(
            select(TranscriptSegment.id).where(
                TranscriptSegment.attempt_id == attempt.id,
                TranscriptSegment.id.in_(evidence or ["-"]),
            )
        )
    )
    status = (
        "confirmed_by_counterpart"
        if args["confirmation"] == "read_back_confirmed"
        else "provisional"
    )
    prior = list(
        await session.scalars(
            select(Finding).where(Finding.attempt_id == attempt.id, Finding.field == field)
        )
    )
    if any(p.value != args["value"] and p.status != "reviewed" for p in prior):
        status = "conflicting"
        for p in prior:
            p.status = "conflicting"
    finding = Finding(
        workspace_id=attempt.workspace_id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        field=field,
        value=args["value"],
        status=status,
        evidence_segment_ids=[e for e in evidence if e in known],
        original_phrasing=args.get("original_phrasing"),
        null_reason=args.get("null_reason"),
    )
    session.add(finding)
    await session.flush()
    await events.append(
        session,
        attempt,
        "finding.recorded",
        "api",
        {"finding_id": finding.id, "field": field, "status": status},
    )
    return {"finding_id": finding.id, "status": status}


async def _set_call_state(session, attempt, _version, args, _evidence, _env=None):
    substate = SUBSTATES[args["substate"]]
    if attempt.substate != substate:
        previous, attempt.substate = attempt.substate, substate
        await events.append(
            session, attempt, "attempt.substate_changed", "api", {"from": previous, "to": substate}
        )
    return {"substate": substate}


async def _send_dtmf(session, attempt, _version, args, _evidence, _env=None):
    previous = list(
        await session.scalars(
            select(ToolExecution)
            .where(
                ToolExecution.attempt_id == attempt.id,
                ToolExecution.tool == "send_dtmf",
                ToolExecution.state == "Succeeded",
            )
            .order_by(ToolExecution.created_at.desc())
        )
    )
    if len(previous) >= DTMF_MAX_PER_CALL:
        raise Rejected("dtmf_limit", "Keypad limit reached for this call; menu loop suspected.")
    if previous and (utcnow() - as_utc(previous[0].created_at)).total_seconds() < DTMF_COOLDOWN_S:
        raise Rejected("dtmf_cooldown", "Wait for the menu before pressing again.")
    return {"digits": args["digits"], "execute": True}


async def _end_call(session, attempt, _version, args, _evidence, _env=None):
    if attempt.state != "Ending":
        await events.set_state(session, attempt, "Ending", "api", reason=args["reason"])
    return {"state": "ending"}


_HANDLERS = {
    "get_assigned_context": _get_assigned_context,
    "verify_caller": _verify_caller,
    "record_finding": _record_finding,
    "set_call_state": _set_call_state,
    "send_dtmf": _send_dtmf,
    "end_call": _end_call,
    **receptionist.HANDLERS,
}
