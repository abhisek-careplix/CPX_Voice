"""Scripted answers on a case, the summary a clinician reads, and red flags sent at once.

Answers arrive from three places, all through :func:`record`: the clinical-check agent on a call
(``record_check_answers``), the patient on a secure form, or staff typing what the patient said.
A red-flag answer (the script's rule, not the agent's judgement) escalates the case to a
clinician immediately: a ``red_flag`` item in the review inbox, the staff alert channel, and the
case due now. A clinician acknowledges it with what they did; that acknowledgement is evidence.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import attempts, contract
from careplix_api.clinical import library, scripts
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Case
from careplix_api.settings import Settings

ANSWERS = ("yes", "no", "unsure", "declined", "text")
SOURCES = ("call", "form", "staff", "text")
ACK_ACTIONS = ("called_patient", "sent_to_emergency", "booked_visit", "no_action_needed", "other")


def clinical(case: Case) -> dict[str, Any]:
    return dict((case.data or {}).get("clinical") or {})


def save(case: Case, values: dict[str, Any]) -> None:
    case.data = {**(case.data or {}), "clinical": {**clinical(case), **values}}
    case.updated_at = utcnow()


def answers(case: Case) -> dict[str, dict[str, Any]]:
    return dict(clinical(case).get("answers") or {})


def remaining(case: Case) -> list[str]:
    got = answers(case)
    return [
        q["key"]
        for q in scripts.questions(cases.type_id(case))
        if q["required"] and q["key"] not in got
    ]


def complete(case: Case) -> bool:
    return bool(answers(case)) and not remaining(case)


def red_flag_open(case: Case) -> bool:
    c = clinical(case)
    return bool(c.get("red_flags")) and not c.get("red_flag_ack")


async def record(
    session: AsyncSession,
    settings: Settings,
    case: Case,
    given: list[dict[str, Any]],
    *,
    source: str,
    actor: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Store answers by question key; escalate any new red flag. Returns what the caller says."""
    if source not in SOURCES:
        raise ApiError(422, "bad_source", "Answers come from a call, a form, a text or staff.")
    questions = scripts.by_key(cases.type_id(case))
    if not questions:
        raise ApiError(422, "no_script", "This case has no scripted questions.")
    have = answers(case)
    flags = []
    now = utcnow().isoformat()
    for item in given:
        key, answer = item.get("question"), item.get("answer")
        q = questions.get(key or "")
        if q is None:
            raise ApiError(422, "unknown_question", f"'{key}' isn't one of this case's questions.")
        if answer not in ANSWERS:
            raise ApiError(422, "bad_answer", f"Answer is one of {', '.join(ANSWERS)}.")
        detail = (item.get("detail") or "").strip()[:300] or None
        if q["kind"] == scripts.TEXT and answer == "text" and not detail:
            raise ApiError(422, "detail_required", f"Give the patient's words for '{key}'.")
        have[key] = {"answer": answer, "detail": detail, "source": source, "at": now}
        if scripts.is_red_flag(q, answer):
            flags.append({"question": key, "label": q["label"], "answer": answer, "at": now})
    save(case, {"answers": have})
    if ref is None or not await contract.has_link(session, case, "evidence", "note", ref):
        contract.add_link(
            session,
            case,
            part="evidence",
            kind="note",
            ref=ref,
            label=f"Scripted answers recorded ({source})",
            actor=actor,
        )
    if source in ("call", "staff") and not await contract.has_link(
        session, case, "informed", "message", f"check:{ref or source}"
    ):
        # The patient was on the phone for the check: they know what happens next.
        contract.add_link(
            session,
            case,
            part="informed",
            kind="message",
            ref=f"check:{ref or source}",
            label="Patient took the scripted check by phone",
            audience="patient",
            channel="call",
            delivery="delivered",
            actor=actor,
        )
    out: dict[str, Any] = {"state": "recorded", "remaining": remaining(case), "red_flag": False}
    if flags:
        await escalate(session, settings, case, flags, source=source, actor=actor)
        out["red_flag"] = True
        out["say"] = await red_flag_words(session, case)
        out["message"] = "Read the words in 'say' exactly, then end the call politely."
    elif not out["remaining"]:
        out["message"] = (
            "All questions are answered. Thank them, say the care team will review the answers, "
            "and end the call."
        )
    # Wake the playbook so it moves on now rather than at its next poll.
    await _wake(session, case)
    return out


async def red_flag_words(session: AsyncSession, case: Case) -> str:
    row = await library.approved(session, case.workspace_id, library.RED_FLAG_KEY)
    if row is None:
        return library.PLATFORM_RED_FLAG
    facts = cases.context(case)
    return library.render(row.body, practice_phone=facts.get("practice_phone") or "")


async def escalate(
    session: AsyncSession,
    settings: Settings,
    case: Case,
    flags: list[dict[str, Any]],
    *,
    source: str,
    actor: str,
) -> None:
    c = clinical(case)
    save(case, {"red_flags": [*(c.get("red_flags") or []), *flags], "red_flag_ack": None})
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor=actor,
            action="case.red_flag",
            target=case.id,
            detail={"questions": [f["question"] for f in flags], "source": source},
        )
    )
    # Minimum necessary: the case reference only; the clinician opens the case for the answers.
    await attempts.notify_staff(
        session,
        case,
        f"Red flag on {case.case_ref}: a clinician needs to review it now.",
        kind="escalation",
        settings=settings,
    )


async def acknowledge_red_flag(
    session: AsyncSession, principal, case: Case, action: str, note: str
) -> None:
    from careplix_api.cases import policy

    if not policy.can_decide(principal, "clinician"):
        raise ApiError(403, "forbidden", "A clinician reviews red flags.")
    if not clinical(case).get("red_flags"):
        raise ApiError(409, "no_red_flag", "This case has no red flag.")
    if action not in ACK_ACTIONS:
        raise ApiError(422, "bad_action", f"Say what you did: {', '.join(ACK_ACTIONS)}.")
    save(
        case,
        {
            "red_flag_ack": {
                "by": principal.user_id,
                "name": principal.display_name,
                "at": utcnow().isoformat(),
                "action": action,
                "note": (note or "").strip()[:300] or None,
            }
        },
    )
    contract.add_link(
        session,
        case,
        part="evidence",
        kind="approval",
        ref=f"red_flag:{principal.user_id}",
        label=f"Red flag reviewed by {principal.display_name}: {action.replace('_', ' ')}",
        actor=principal.user_id,
    )
    await _wake(session, case)


def summary(case: Case) -> list[dict[str, Any]]:
    """The answers in the order a clinician reads them: red flags, then attention items, then
    the rest. Each line is the question's label and the patient's answer, never an inference."""
    got = answers(case)
    lines = []
    for q in scripts.questions(cases.type_id(case)):
        a = got.get(q["key"])
        if a is None:
            continue
        flagged = scripts.is_red_flag(q, a["answer"])
        attention = bool(q.get("attention")) and a["answer"] in scripts.FLAG_ANSWERS
        lines.append(
            {
                "question": q["key"],
                "label": q["label"],
                "text": q["text"],
                "answer": a["answer"],
                "detail": a.get("detail"),
                "source": a.get("source"),
                "level": "red_flag" if flagged else ("attention" if attention else "info"),
            }
        )
    order = {"red_flag": 0, "attention": 1, "info": 2}
    return sorted(lines, key=lambda line: order[line["level"]])


async def _wake(session: AsyncSession, case: Case) -> None:
    from sqlalchemy import update

    from careplix_api.cases.models import PlaybookRun

    await session.execute(
        update(PlaybookRun)
        .where(PlaybookRun.case_id == case.id, PlaybookRun.state == "waiting")
        .values(next_run_at=utcnow())
        .execution_options(synchronize_session=False)
    )
