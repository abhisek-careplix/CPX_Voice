"""Warm transfer: hand a live call to staff with a spoken whisper and a written summary.

1. The agent calls ``transfer_to_staff`` with a reason and a short administrative summary. The API
   decides whether a transfer can happen (a staff number is set, it's an approved destination, an
   outbound trunk exists, there is a real caller to hand over) and records a :class:`CallHandoff`
   with the summary sealed. It returns the number and trunk to the worker only, never the model.
2. The worker puts the caller on hold, dials staff into a side room, speaks the whisper, and moves
   staff into the caller's room once they agree (LiveKit's warm transfer workflow). It reports the
   outcome to ``POST /internal/attempts/{id}/handoffs/{handoff_id}``.
3. No staff number, not approved, no answer, declined or failed: the hand-off becomes a call-back
   promise. A call already working a case keeps that case; otherwise a ``call_back`` case is
   opened with the summary, and the agent tells the caller who will call back and when.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import connections, events
from careplix_api.cases import types as case_types
from careplix_api.channels.models import CallHandoff
from careplix_api.channels.service import normalize, workspace_settings
from careplix_api.db import utcnow
from careplix_api.errors import Rejected
from careplix_api.models import AuditEvent, CallAttempt, Case, Workspace
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

DEFAULT_LABEL = "a member of staff"
DEFAULT_PROMISE = "as soon as they can during office hours"
RINGING_TIMEOUT_S = 30
CALL_BACK_TYPE = "call_back"


def _whisper(label: str, reason: str, summary: str, workspace_name: str) -> str:
    """What the agent says to staff before connecting them (and nothing it wasn't told)."""
    why = reason.replace("_", " ")
    return (
        f"You are an automated assistant for {workspace_name}, briefing {label} about a caller "
        "who is on hold. When the staff member answers, say who you are, then read them this "
        f"summary in your own words, briefly: reason: {why}. Summary: {summary}. Then ask whether "
        "they can take the call now. If they say yes, call connect_to_caller. If they say no or "
        "can't, call decline_transfer with their reason. If you reach voicemail, call "
        "voicemail_detected. Don't add details, give advice, or speak to anyone else."
    )


async def _call_back_case(
    session: AsyncSession, attempt: CallAttempt, summary: str, practice: str, promise: str
) -> Case:
    """Open a call-back case for the caller (the promise the agent just made)."""
    from careplix_api.cases import service as cases_service  # local: heavy import graph

    ref = await cases_service._next_ref(session, attempt.workspace_id, CALL_BACK_TYPE)
    data = case_types.build(
        CALL_BACK_TYPE,
        ref,
        {
            "caller_phone": attempt.caller_e164 or attempt.destination_e164,
            "practice_name": practice,
            "summary": summary,
            "promise": promise,
        },
    )
    case = Case(
        workspace_id=attempt.workspace_id,
        case_ref=ref,
        synthetic=True,
        data=data,
        case_type=CALL_BACK_TYPE,
        status="open",
        source="inbound" if attempt.direction == "inbound" else "api",
        source_ref=attempt.id,
        created_by="agent_runtime",
    )
    session.add(case)
    await session.flush()
    return case


async def promise_call_back(
    session: AsyncSession, settings: Settings, handoff: CallHandoff, attempt: CallAttempt, why: str
) -> dict[str, Any]:
    """Turn a hand-off that couldn't connect into a call-back promise on a case."""
    cs = await workspace_settings(session, attempt.workspace_id)
    label = (cs.staff_label if cs else None) or DEFAULT_LABEL
    promise = (cs.callback_promise if cs else None) or DEFAULT_PROMISE
    if handoff.case_id is None:
        workspace = await session.get(Workspace, attempt.workspace_id)
        summary = PhiBox(settings).open(handoff.summary).get("summary", "")
        case = await _call_back_case(
            session, attempt, summary, workspace.name if workspace else "", promise
        )
        handoff.case_id = case.id
    handoff.state, handoff.detail, handoff.resolved_at = "callback_promised", why[:80], utcnow()
    await events.append(
        session,
        attempt,
        "handoff.callback_promised",
        "api",
        {"handoff_id": handoff.id, "case_id": handoff.case_id, "why": why[:80]},
    )
    return {
        "state": "callback_promised",
        "case_id": handoff.case_id,
        "message": f"Nobody can take the call right now. Tell the caller {label} will call them "
        f"back {promise}, confirm the number they want to be called on, then finish the call.",
    }


async def transfer_to_staff(session, attempt: CallAttempt, _version, args, _evidence, env=None):
    settings: Settings = env.settings
    box = PhiBox(settings)
    open_ = await session.scalar(
        select(CallHandoff).where(
            CallHandoff.attempt_id == attempt.id, CallHandoff.state == "dialing"
        )
    )
    if open_ is not None:
        raise Rejected("transfer_in_progress", "A transfer is already being connected.")
    handoff = CallHandoff(
        workspace_id=attempt.workspace_id,
        attempt_id=attempt.id,
        case_id=attempt.case_id,
        reason=args["reason"],
        summary=box.seal({"summary": args["summary"].strip()}),
        state="dialing",
    )
    session.add(handoff)
    await session.flush()
    cs = await workspace_settings(session, attempt.workspace_id)
    handoff.staff_label = (cs.staff_label if cs else None) or DEFAULT_LABEL
    session.add(
        AuditEvent(
            workspace_id=attempt.workspace_id,
            actor="agent_runtime",
            action="call.handoff",
            target=attempt.id,
            detail={"handoff_id": handoff.id, "reason": handoff.reason},
        )
    )
    workspace = await session.get(Workspace, attempt.workspace_id)
    eff = connections.effective(workspace, settings)
    staff = normalize(cs.staff_number) if cs and cs.staff_number else None
    why = None
    if attempt.pins.get("simulated"):
        why = "simulated_call"
    elif staff is None:
        why = "no_staff_number"
    elif staff not in eff.approved_destinations:
        why = "staff_number_not_approved"
    elif not eff.outbound_trunk_id:
        why = "no_outbound_trunk"
    elif workspace is not None and workspace.emergency_stop:
        why = "emergency_stop"
    if why is not None:
        return await promise_call_back(session, settings, handoff, attempt, why)
    await events.append(session, attempt, "handoff.dialing", "api", {"handoff_id": handoff.id})
    summary = args["summary"].strip()
    return {
        "state": "transferring",
        "handoff_id": handoff.id,
        "message": "Connecting. The caller hears hold music while staff are briefed.",
        # Worker only: stripped before the model sees the result.
        "dial": {
            "number": staff,
            "trunk_id": eff.outbound_trunk_id,
            "ringing_timeout_s": RINGING_TIMEOUT_S,
            "whisper": _whisper(
                handoff.staff_label, handoff.reason, summary, workspace.name if workspace else ""
            ),
        },
    }


async def settle(
    session: AsyncSession,
    settings: Settings,
    attempt: CallAttempt,
    handoff_id: str,
    outcome: str,
    detail: str | None,
) -> dict[str, Any]:
    """The worker's report: ``connected``, or why it didn't (then the call-back promise)."""
    handoff = await session.get(CallHandoff, handoff_id)
    if handoff is None or handoff.attempt_id != attempt.id:
        raise Rejected("handoff_not_found", "No such transfer on this call.")
    if handoff.state != "dialing":
        return {"state": handoff.state, "case_id": handoff.case_id}  # repeated report
    if outcome == "connected":
        handoff.state, handoff.resolved_at = "connected", utcnow()
        attempt.substate = "Transferred"
        await events.append(
            session, attempt, "handoff.connected", "worker", {"handoff_id": handoff.id}
        )
        return {"state": "connected", "case_id": handoff.case_id}
    return await promise_call_back(
        session, settings, handoff, attempt, f"staff_{outcome}" + (f":{detail}" if detail else "")
    )


def row(handoff: CallHandoff, box: PhiBox) -> dict[str, Any]:
    return {
        "id": handoff.id,
        "attempt_id": handoff.attempt_id,
        "case_id": handoff.case_id,
        "reason": handoff.reason,
        "summary": box.open(handoff.summary).get("summary"),
        "staff_label": handoff.staff_label,
        "state": handoff.state,
        "detail": handoff.detail,
        "created_at": handoff.created_at.isoformat() if handoff.created_at else None,
        "resolved_at": handoff.resolved_at.isoformat() if handoff.resolved_at else None,
    }


HANDLERS = {"transfer_to_staff": transfer_to_staff}
