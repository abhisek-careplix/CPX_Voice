"""Appointment reminders: 7 days, 72 hours and 24 hours before the visit, with confirm, cancel
and reschedule replies (OPERATING_PLAN §5.1).

The ``appointment_reminder`` playbook's wait step is the cadence here:

- The schedule trigger opens one case per booked visit 7 days out; the first reminder goes at
  once (text, then a call). Visits booked later start at the stage they're in (72 h or 24 h).
- No answer: the next stage sends again (a new round), until 24 hours out; then the front desk
  owns a call-back.
- ``YES`` confirms (the EHR confirmation is auto-approved by policy). ``R`` (or ``NO``) asks to
  reschedule: the patient is told the front desk will call, and the front desk owns it. ``C``
  asks to cancel: a cancellation write intent goes to a person to approve (``playbook.ehr_write``)
  and the patient is told it's with the front desk. ``CANCEL`` alone is a carrier opt-out
  keyword (CTIA) and stays one.

Cases from a webhook or import without the visit's UTC time keep the plain wait (24 hours, one
retry).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import attempts, contract, playbooks, policy
from careplix_api.cases.models import ContactAttempt, PlaybookRun
from careplix_api.cases.playbooks import POLL, Outcome, Playbook, RunContext, Step, _hand_off, _next
from careplix_api.db import as_utc, utcnow
from careplix_api.models import AuditEvent, Case, PayerConnection

STAGES = (
    ("7d", timedelta(days=7)),
    ("72h", timedelta(hours=72)),
    ("24h", timedelta(hours=24)),
)
LAST_CALL = timedelta(hours=2)
MAX_WAIT = timedelta(hours=1)
RESCHEDULE_WORDS = {"R", "RESCHEDULE", "CHANGE", "MOVE", "NEW TIME"}
CANCEL_WORDS = {"C", "CANCEL APPT", "CANCEL APPOINTMENT", "CANCEL MY APPOINTMENT", "CANCEL VISIT"}
RESCHEDULE_NOTICE = "Thanks. The front desk will call you to find a new time."
CANCEL_NOTICE = (
    "Thanks. Your cancellation request is with the front desk, who will confirm it. If you'd "
    "rather keep the visit, call us."
)


def schedule(starts_at: datetime) -> list[dict[str, Any]]:
    return [{"stage": key, "at": starts_at - before} for key, before in STAGES]


def _starts_at(case: Case) -> datetime | None:
    raw = (case.data or {}).get("appointment_starts_at")
    try:
        return as_utc(datetime.fromisoformat(raw)) if raw else None
    except ValueError:
        return None


def reply_kind(text: str) -> str | None:
    whole = " ".join(text.strip().upper().replace(".", " ").replace("!", " ").split())
    if whole in RESCHEDULE_WORDS:
        return "reschedule"
    if whole in CANCEL_WORDS:
        return "cancel"
    return None


async def _tell(ctx: RunContext, text: str, label: str) -> None:
    from careplix_api.clinical.playbook import notify_patient

    await notify_patient(ctx, text, label=label)


async def _cancel(ctx: RunContext) -> Outcome:
    from careplix_api.practice import intents

    ehr = (ctx.case.data or {}).get("ehr") or {}
    conn = await ctx.session.get(PayerConnection, ehr.get("connection_id") or "")
    await _tell(ctx, CANCEL_NOTICE, "Patient told the cancellation is with the front desk")
    if conn is None or not ehr.get("appointment_id"):
        return _hand_off(
            "The patient asked to cancel (replied C).",
            "Cancel the appointment in the EHR and record the acknowledgement.",
        )
    intent = await intents.propose(
        ctx.session,
        conn,
        ctx.settings,
        kind="appointment_cancel",
        target_source_id=str(ehr["appointment_id"]),
        payload={"cancelled": True},
        reason="Reminder: the patient replied C to cancel",
        source="playbook",
        requested_by="playbook",
    )
    if intent.state == "proposed" and policy.agent_may(
        "playbook.ehr_write.appointment_cancel", ctx.case.workspace_id
    ):
        await intents.decide(ctx.session, intent, "playbook", approve=True)
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="write_intent",
        ref=intent.id,
        label="EHR cancellation proposed (the patient replied C)",
        actor="playbook",
    )
    ctx.put(intent_id=intent.id, summary="Patient asked to cancel; the cancellation is proposed.")
    ctx.put(follow_up="Offer the patient a new visit if they want one.")
    return Outcome("goto", "Cancellation proposed", step=ctx.playbook.index("await_ack"))


async def reminder_cadence(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    await ctx.session.refresh(case, ["winner_attempt_id"])
    if case.winner_attempt_id:
        ctx.put(answer="yes", deciding_attempt=case.winner_attempt_id)
        return _next("Confirmed")
    reply = ctx.data.get("reply")
    round_attempts = [
        a
        for a in await attempts.for_case(ctx.session, case)
        if a.round == int(ctx.data.get("round", 1)) and a.channel != "staff"
    ]
    declined = [a for a in round_attempts if a.state == "declined"]
    if reply == "cancel":
        return await _cancel(ctx)
    if reply == "reschedule" or declined:
        await _tell(ctx, RESCHEDULE_NOTICE, "Patient told the front desk will call to reschedule")
        return _hand_off("The patient asked to reschedule.", "Call the patient to find a new time.")
    starts = _starts_at(case)
    if starts is None:
        return await playbooks.await_replies(ctx, Step("await_replies", step.label, step.params))
    now = utcnow()
    plan = schedule(starts)
    sent = list(ctx.data.get("stages_sent") or [])
    round_ = int(ctx.data.get("round", 0))
    if round_ > int(ctx.data.get("cadence_round", 0)):
        # The round that just went out covers the latest stage that was due.
        due = [s for s in plan if s["at"] <= now]
        sent.append((due[-1] if due else plan[0])["stage"])
        ctx.put(stages_sent=sent, cadence_round=round_)
    last = max((i for i, s in enumerate(plan) if s["stage"] in sent), default=-1)
    later = plan[last + 1 :]
    if any(s["at"] <= now for s in later):
        return Outcome("goto", "Next reminder is due", step=ctx.playbook.index("contact"))
    if later:
        until = min(later[0]["at"], now + MAX_WAIT)
        return Outcome("wait", f"Next reminder {later[0]['stage']}", until=until)
    if now < starts - LAST_CALL:
        return Outcome("wait", "Waiting for a reply", until=min(starts - LAST_CALL, now + MAX_WAIT))
    for a in round_attempts:
        if a.state in attempts.WAITING:
            a.state, a.detail = "no_answer", "No answer before the visit"
    return _hand_off(
        f"No confirmation after {len(sent) or 1} reminders.", "Call the patient before the visit."
    )


async def take_reply(session: AsyncSession, reply) -> bool:
    """A reminder text answered R (reschedule) or C (cancel)."""
    from careplix_api.channels.case_channel import REPLY_PREFIX

    kind = reply_kind(reply.text or "")
    if kind is None or not (reply.reply_ref or "").startswith(REPLY_PREFIX):
        return False
    attempt = await session.get(ContactAttempt, reply.reply_ref[len(REPLY_PREFIX) :])
    if attempt is None or attempt.workspace_id != reply.workspace_id:
        return False
    case = await session.get(Case, attempt.case_id)
    if case is None or cases.type_id(case) != "appointment_reminder":
        return False
    if attempt.state in attempts.WAITING:
        attempt.state, attempt.answered_at = "declined", utcnow()
        attempt.detail = f"Replied to {kind}"
    from sqlalchemy import select

    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    if run is not None and run.state == "waiting":
        run.data = {**(run.data or {}), "reply": kind}
        await session.execute(
            update(PlaybookRun)
            .where(PlaybookRun.id == run.id)
            .values(next_run_at=utcnow())
            .execution_options(synchronize_session=False)
        )
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor="text_reply",
            action=f"case.reminder_{kind}",
            target=case.id,
            detail={"attempt": attempt.id},
        )
    )
    return True


def view() -> dict[str, Any]:
    return {
        "stages": [{"stage": k, "hours_before": int(d.total_seconds() // 3600)} for k, d in STAGES],
        "replies": [
            {"reply": "YES", "does": "Confirms; the EHR confirmation is written automatically"},
            {"reply": "R (or NO)", "does": "Asks to reschedule; the front desk calls them"},
            {"reply": "C", "does": "Asks to cancel; staff approve the EHR cancellation"},
            {"reply": "STOP", "does": "Opts out of texts (carrier rule); CANCEL alone does too"},
        ],
        "fallback": "A call when a text can't be sent; the front desk owns any visit still "
        "unconfirmed 2 hours before it starts.",
    }


def register() -> None:
    from careplix_api.channels import service as channels_service

    old = playbooks.PLAYBOOKS["appointment_reminder"]
    steps = tuple(
        Step("reminder_cadence", "Wait for a reply; remind again at 72 and 24 hours", s.params)
        if s.kind == "await_replies"
        else s
        for s in old.steps
    )
    playbooks.PLAYBOOKS["appointment_reminder"] = Playbook(
        id=old.id,
        name=old.name,
        case_type=old.case_type,
        triggers=old.triggers,
        steps=steps,
        due=timedelta(days=8),
        completion="The patient confirmed and the EHR accepted it, or asked to cancel and a "
        "person approved the cancellation; the patient and the front desk are told.",
        hand_off="The patient wants to reschedule, or never answered the 7-day, 72-hour and "
        "24-hour reminders: the front desk owns the call-back.",
    )
    playbooks.HANDLERS["reminder_cadence"] = reminder_cadence
    channels_service.on_reply(take_reply)


register()
_ = (POLL, policy)
