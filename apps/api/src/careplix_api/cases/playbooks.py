"""Playbooks (operating plan D1): trigger → case → steps → completion check → hand-off.

A playbook is server-owned, like case types and guardrail packs. Each one names its case type,
the triggers that start it, its steps and its due date. The engine works every running playbook
in the background (see :class:`PlaybookEngine`), so work starts without anyone pressing Start:

- ``care_opportunity``: a clinician approves a care opportunity → a ``care_outreach`` case.
- ``backfill_opening``: a cancelled slot opens with ranked patients → a ``slot_backfill`` case
  that offers it to all of them at once; the first confirmed yes is booked.
- ``schedule``: a booked appointment comes inside the reminder window of the EHR schedule → an
  ``appointment_reminder`` case.
- ``auth_required``: an upcoming visit's eligibility check (271) or a payer rule says it needs
  prior authorization → a ``prior_authorization`` case (:mod:`careplix_api.priorauth.playbook`).
- ``webhook`` / ``import`` / ``inbound_call``: an EHR event, a worklist row or a call hands the
  engine a case directly (``POST /api/playbooks/{id}/trigger``).

Steps:

- ``check_facts``: the case has the facts and someone to contact; otherwise it's ``blocked`` with
  the reason, owned by a person, until they fill the gap and resume it.
- ``contact``: one attempt per person on the first channel that works (D2); ``mode="many"``
  contacts everyone at once.
- ``await_replies``: wait for the first yes (or a no, for one-person playbooks); retry, or hand off.
- ``settle``: update the system of record (care opportunity outcome, EHR confirmation, booking).
- ``await_ack``: wait for the EHR to acknowledge a write intent.
- ``inform_staff``: tell the staff owner what happened.
- ``close``: check the completion contract; ``done`` if it holds, otherwise hand off what's left.

Any step can hand off to a person: the case becomes ``handed_off`` with an owner, a due date and
the next step, and it escalates if that date passes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api import agents_service
from careplix_api.cases import attempts, contract, policy, types
from careplix_api.cases.models import ContactAttempt, PlaybookRun
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Agent, Case, EhrRecord, Membership, PayerConnection, Workspace
from careplix_api.phi import PhiBox, initials
from careplix_api.settings import Settings

log = logging.getLogger("careplix.playbooks")

TRIGGERS: dict[str, str] = {
    "care_opportunity": "A clinician approves a care opportunity",
    "backfill_opening": "A cancelled slot opens with patients waiting for one",
    "schedule": "A booked appointment enters the reminder window of the EHR schedule",
    "auth_required": "An eligibility check (271) or a payer rule says a visit needs prior auth",
    "webhook": "An EHR event or webhook",
    "import": "A worklist import",
    "inbound_call": "An inbound call",
}
LEASE = timedelta(minutes=2)
POLL = timedelta(minutes=1)
# Reminders start 7 days out; the cadence (careplix_api.practice.reminders) reminds again at
# 72 and 24 hours unless the patient has confirmed, cancelled or asked to reschedule.
REMINDER_WINDOW = timedelta(days=7)
REMINDER_LEAD = timedelta(hours=2)
MAX_STEPS_PER_PASS = 20


@dataclass(frozen=True)
class Step:
    kind: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Playbook:
    id: str
    name: str
    case_type: str
    triggers: tuple[str, ...]
    steps: tuple[Step, ...]
    due: timedelta
    completion: str
    hand_off: str

    def index(self, kind: str) -> int:
        return next(i for i, s in enumerate(self.steps) if s.kind == kind)


PLAYBOOKS: dict[str, Playbook] = {
    p.id: p
    for p in [
        Playbook(
            id="care_outreach",
            name="Care program outreach",
            case_type="care_outreach",
            triggers=("care_opportunity", "webhook", "import"),
            steps=(
                Step("check_facts", "Check the case has a number to call"),
                Step(
                    "contact",
                    "Invite the patient (text, then call)",
                    {"channels": ("text", "call"), "mode": "one"},
                ),
                Step(
                    "await_replies",
                    "Wait for their answer",
                    {"timeout_h": 48, "retries": 1, "retry_after_h": 24, "on_no": "settle"},
                ),
                Step("settle", "Record the outcome on the care opportunity", {"action": "care"}),
                Step("inform_staff", "Tell the care team"),
                Step(
                    "close",
                    "Check the completion contract",
                    {"yes_next_step": "Enrol the patient in the program"},
                ),
            ),
            due=timedelta(days=7),
            completion="The patient's answer is recorded on the care opportunity, the patient "
            "and the care team are told, and a yes has an owner to enrol them.",
            hand_off="No answer after two tries, or no number on file: the approving clinician "
            "owns the next step.",
        ),
        Playbook(
            id="appointment_reminder",
            name="Appointment reminder",
            case_type="appointment_reminder",
            triggers=("schedule", "webhook", "import"),
            steps=(
                Step("check_facts", "Check the case has a number to call"),
                Step(
                    "contact",
                    "Remind the patient (text, then call)",
                    {"channels": ("text", "call"), "mode": "one"},
                ),
                Step(
                    "await_replies",
                    "Wait for a confirmation",
                    {"timeout_h": 24, "retries": 1, "retry_after_h": 12, "on_no": "hand_off"},
                ),
                Step("settle", "Confirm the appointment in the EHR", {"action": "confirm"}),
                Step("await_ack", "Wait for the EHR to accept the confirmation"),
                Step("inform_staff", "Tell the front desk"),
                Step("close", "Check the completion contract"),
            ),
            due=timedelta(days=2),
            completion="The patient confirmed, the EHR accepted the confirmation, and the front "
            "desk is told.",
            hand_off="The patient wants to reschedule or cancel, or never answered: the front "
            "desk owns the call-back.",
        ),
        Playbook(
            id="slot_backfill",
            name="Fill a cancelled slot",
            case_type="slot_backfill",
            triggers=("backfill_opening", "webhook"),
            steps=(
                Step("check_facts", "Check there are patients to offer it to"),
                Step(
                    "contact",
                    "Offer the slot to everyone waiting at once",
                    {"channels": ("text", "call"), "mode": "many", "limit": 5},
                ),
                Step(
                    "await_replies",
                    "First confirmed yes wins",
                    {"timeout_h": 2, "retries": 0, "on_no": "wait_all"},
                ),
                Step("settle", "Book the winner in the EHR", {"action": "book_backfill"}),
                Step("inform_staff", "Tell the front desk"),
                Step("close", "Check the completion contract"),
            ),
            due=timedelta(hours=12),
            completion="The slot is booked in the EHR for the first patient who said yes, they "
            "are told, everyone else is told it's filled, and the front desk is told.",
            hand_off="Nobody said yes in time, or the booking was refused: the front desk works "
            "the slot from the Backfill page.",
        ),
        # Its own steps (prepare_packet … tell_patient) live in careplix_api.priorauth.playbook.
        Playbook(
            id="prior_authorization",
            name="Prior authorization",
            case_type="prior_authorization",
            triggers=("auth_required", "webhook", "import"),
            steps=(
                Step("prepare_packet", "Build the packet from the EHR: order, notes, documents"),
                Step(
                    "submit_auth",
                    "Submit: the payer's PAS API, else its portal, else a call, else fax",
                    {"routes": ("pas", "portal", "call", "fax")},
                ),
                Step(
                    "track_auth",
                    "Track the payer's decision; chase it when it goes stale",
                    {
                        "first_check_m": 1,
                        "check_every_h": 24,
                        "stale_after_h": 72,
                        "call_timeout_h": 48,
                        "follow_up_calls": 2,
                        "visit_guard_h": 24,
                    },
                ),
                Step("record_auth", "Write the auth number and dates back to the EHR"),
                Step("await_record", "Wait for the EHR (or staff) to take it"),
                Step("tell_patient", "Tell the patient if the visit is affected"),
                Step("inform_staff", "Tell the front desk"),
                Step("close", "Check the completion contract"),
            ),
            due=timedelta(days=10),
            completion="The payer's decision is recorded with its auth number and dates, the EHR "
            "has the number (or staff own entering it), the front desk is told, and the patient "
            "is told when the visit is affected.",
            hand_off="The packet is incomplete, the payer asks for more or denies it, or no route "
            "reaches the payer: the front desk owns the next step, with the payer's reason.",
        ),
    ]
}
BY_CASE_TYPE = {p.case_type: p.id for p in PLAYBOOKS.values()}


def view() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "case_type": p.case_type,
            "case_type_name": types.get(p.case_type)["name"],
            "triggers": [{"kind": t, "label": TRIGGERS[t]} for t in p.triggers],
            "steps": [{"kind": s.kind, "label": s.label} for s in p.steps],
            "due_hours": int(p.due.total_seconds() // 3600),
            "completion": p.completion,
            "hand_off": p.hand_off,
        }
        for p in PLAYBOOKS.values()
    ]


# --- outcomes of a step ---------------------------------------------------------------------------


@dataclass
class Outcome:
    kind: str  # next | wait | goto | hand_off | block | fail
    detail: str | None = None
    until: datetime | None = None
    step: int | None = None
    next_step: str | None = None


def _next(detail: str | None = None) -> Outcome:
    return Outcome("next", detail)


def _wait(until: datetime, detail: str | None = None) -> Outcome:
    return Outcome("wait", detail, until=until)


def _hand_off(reason: str, next_step: str) -> Outcome:
    return Outcome("hand_off", reason, next_step=next_step)


@dataclass
class RunContext:
    session: AsyncSession
    settings: Settings
    engine: PlaybookEngine
    case: Case
    run: PlaybookRun
    playbook: Playbook

    @property
    def data(self) -> dict[str, Any]:
        return self.run.data or {}

    def put(self, **values: Any) -> None:
        self.run.data = {**self.data, **values}


# --- step handlers --------------------------------------------------------------------------------


async def _address(ctx: RunContext, contact: dict[str, Any]) -> str | None:
    source = contact.get("address_from", "case")
    if source == "case":
        t = types.get(cases.type_id(ctx.case))
        raw = types.value(ctx.case.data, t["counterparty"]["phone"])
        return str(raw) if raw else None
    if source == "sealed":
        return PhiBox(ctx.settings).open(contact.get("address")).get("address")
    if source == "ehr":
        rec = await ctx.session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == contact.get("connection_id"),
                EhrRecord.resource == "patient",
                EhrRecord.source_id == contact.get("person_ref"),
            )
        )
        return PhiBox(ctx.settings).open(rec.pii).get("phone") if rec else None
    return None


async def check_facts(ctx: RunContext, step: Step) -> Outcome:
    tid = cases.type_id(ctx.case)
    gaps = types.missing(tid, ctx.case.data)
    if gaps:
        labels = {f["key"]: f["label"] for f in types.get(tid)["fields"]}
        missing = ", ".join(labels[k].lower() for k in gaps)
        return Outcome(
            "block",
            f"Missing facts: {missing}.",
            next_step=f"Fill in {missing}, then resume the playbook.",
        )
    contacts = ctx.data.get("contacts") or []
    reachable = [c for c in contacts if await _address(ctx, c)]
    if not reachable:
        return Outcome(
            "block",
            "No one on this case has a phone number on file.",
            next_step="Add a phone number, then resume the playbook.",
        )
    return _next(f"{len(reachable)} to contact")


def _within_hours(ctx: RunContext) -> bool:
    if not ctx.settings.playbook_contact_hours:
        return True
    from careplix_api.practice.backfill import within_contact_hours

    return within_contact_hours(ctx.settings, (ctx.case.data or {}).get("timezone"))


async def _agent_for(session: AsyncSession, case: Case) -> str | None:
    templates = types.get(cases.type_id(case))["agents"]
    for agent in await session.scalars(
        select(Agent)
        .where(Agent.workspace_id == case.workspace_id, Agent.template_id.in_(templates))
        .order_by(Agent.created_at)
    ):
        if await agents_service.current_version(session, agent) is not None:
            return agent.id
    return None


async def contact(ctx: RunContext, step: Step) -> Outcome:
    if not policy.agent_may("playbook.contact", ctx.case.workspace_id):
        return _hand_off("Policy: a person starts contact for this case.", "Contact the patient.")
    if not _within_hours(ctx):
        return _wait(utcnow() + timedelta(minutes=30), "Outside contact hours")
    round_ = int(ctx.data.get("round", 0)) + 1
    ctx.put(round=round_, round_started_at=utcnow().isoformat())
    contacts = list(ctx.data.get("contacts") or [])
    if step.params.get("mode") != "many":
        contacts = contacts[:1]
    contacts = contacts[: int(step.params.get("limit", 10))]
    services = {**ctx.engine.services, "agent_id": await _agent_for(ctx.session, ctx.case)}
    objective = cases.objective(ctx.case)
    box = PhiBox(ctx.settings)
    sent, problems = 0, []
    for person in contacts:
        address = await _address(ctx, person)
        if not address:
            problems.append(f"{person.get('label', 'Someone')}: no number")
            continue
        reached = False
        for name in step.params.get("channels", ("call",)):
            if not attempts.available(name):
                continue  # declared but not built yet (text: workstream C)
            attempt = attempts.new_attempt(
                ctx.session,
                box,
                ctx.case,
                channel_name=name,
                person_label=person.get("label") or "Patient",
                person_ref=person.get("person_ref"),
                address=address,
                audience=person.get("audience", "patient"),
                round_=round_,
            )
            await ctx.session.flush()
            result = await attempts.dispatch(
                ctx.session,
                ctx.settings,
                ctx.case,
                attempt,
                objective=objective,
                services=services,
            )
            if result.state == "sent":
                reached = True
                break
            problems.append(f"{attempt.person_label} by {name}: {result.detail}")
        sent += reached
    if not sent:
        reason = "; ".join(problems) or "No channel could reach anyone."
        return _hand_off(f"Couldn't reach anyone: {reason}"[:300], "Reach the patient another way.")
    return _next(f"Contacted {sent} of {len(contacts)}")


async def _round_attempts(ctx: RunContext) -> list[ContactAttempt]:
    round_ = int(ctx.data.get("round", 1))
    return [a for a in await attempts.for_case(ctx.session, ctx.case) if a.round == round_]


async def await_replies(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    await ctx.session.refresh(case, ["winner_attempt_id"])
    if case.winner_attempt_id:
        ctx.put(answer="yes", deciding_attempt=case.winner_attempt_id)
        return _next("Confirmed yes")
    if ctx.playbook.id == "slot_backfill":
        gone = await _opening_gone(ctx)
        if gone:
            await attempts.cancel_waiting(ctx.session, ctx.settings, case)
            return Outcome("fail", gone)
    round_attempts = await _round_attempts(ctx)
    waiting = [a for a in round_attempts if a.state in attempts.WAITING]
    declined = [a for a in round_attempts if a.state == "declined"]
    on_no = step.params.get("on_no", "hand_off")
    if declined and on_no != "wait_all":
        if on_no == "settle":
            ctx.put(answer="no", deciding_attempt=declined[0].id)
            return _next("Answered no")
        return _hand_off(
            "The patient asked to reschedule or cancel.",
            "Call the patient back to reschedule or cancel.",
        )
    started = datetime.fromisoformat(ctx.data.get("round_started_at") or utcnow().isoformat())
    deadline = started + timedelta(hours=float(step.params.get("timeout_h", 24)))
    if waiting and utcnow() < deadline:
        return _wait(min(utcnow() + POLL, deadline), f"{len(waiting)} waiting")
    for a in waiting:
        a.state, a.detail = "no_answer", "No answer in time"
    if int(ctx.data.get("round", 1)) <= int(step.params.get("retries", 0)):
        retry = utcnow() + timedelta(hours=float(step.params.get("retry_after_h", 1)))
        return Outcome(
            "goto", "No answer: trying again later", until=retry, step=ctx.playbook.index("contact")
        )
    if round_attempts and len(declined) == len(round_attempts):
        return _hand_off("Everyone said no.", "Offer the slot by hand or let it go.")
    return _hand_off(
        f"No answer after {ctx.data.get('round', 1)} tries.",
        "Reach the patient another way.",
    )


async def _opening_gone(ctx: RunContext) -> str | None:
    from careplix_api.models import BackfillOpening

    opening = await ctx.session.get(BackfillOpening, ctx.case.source_ref or "")
    if opening is None or opening.state in ("open", "booking"):
        return None
    return f"The slot is no longer open ({opening.state})."


async def _deciding(ctx: RunContext) -> ContactAttempt | None:
    ref = ctx.data.get("deciding_attempt")
    return await ctx.session.get(ContactAttempt, ref) if ref else None


async def settle(ctx: RunContext, step: Step) -> Outcome:
    action = step.params.get("action")
    answer = ctx.data.get("answer")
    deciding = await _deciding(ctx)
    if answer == "no" and deciding is not None:
        # A recorded no is evidence too, and the patient heard it was noted.
        contract.add_link(
            ctx.session,
            ctx.case,
            part="evidence",
            kind="task" if deciding.task_id else "attempt",
            ref=deciding.task_id or deciding.message_id or deciding.id,
            label=f"{deciding.person_label} said no by {deciding.channel}",
            actor="playbook",
        )
        contract.add_link(
            ctx.session,
            ctx.case,
            part="informed",
            kind="message",
            ref=deciding.message_id or deciding.task_id or deciding.id,
            label=f"{deciding.person_label} told their answer was noted",
            audience=deciding.audience,
            channel=deciding.channel,
            delivery="delivered",
            actor="playbook",
        )
    if action == "care":
        return await _settle_care(ctx, answer)
    if action == "confirm":
        return await _settle_confirm(ctx)
    if action == "book_backfill":
        return await _settle_backfill(ctx, deciding)
    return Outcome("fail", f"Unknown settle action {action}")


async def _settle_care(ctx: RunContext, answer: str | None) -> Outcome:
    from careplix_api.opportunities.models import CareOpportunity

    row = await ctx.session.get(CareOpportunity, ctx.case.source_ref or "")
    status = "enrolled" if answer == "yes" else "not_interested"
    if row is None:
        return _hand_off(
            "This case isn't linked to a care opportunity.",
            f"Record the patient's answer ({status.replace('_', ' ')}) by hand.",
        )
    if row.status in ("approved", "contacted"):
        row.status, row.updated_at = status, utcnow()
    contract.acknowledge(ctx.case, "care", f"care_opportunity:{row.id}:{row.status}")
    ctx.put(summary=f"Patient answer recorded: {row.status.replace('_', ' ')}.")
    return _next(row.status)


async def _settle_confirm(ctx: RunContext) -> Outcome:
    from careplix_api.practice import intents

    ehr = (ctx.case.data or {}).get("ehr") or {}
    conn = await ctx.session.get(PayerConnection, ehr.get("connection_id") or "")
    if conn is None or not ehr.get("appointment_id"):
        return _hand_off(
            "The patient confirmed, but this case has no EHR appointment to confirm.",
            "Mark the appointment confirmed in the EHR and record the acknowledgement.",
        )
    intent = await intents.propose(
        ctx.session,
        conn,
        ctx.settings,
        kind="appointment_confirm",
        target_source_id=str(ehr["appointment_id"]),
        payload={"confirmed": True},
        reason="Reminder: the patient confirmed the appointment",
        source="playbook",
        requested_by="playbook",
    )
    if intent.state == "proposed" and policy.agent_may(
        "playbook.ehr_write.appointment_confirm", ctx.case.workspace_id
    ):
        await intents.decide(ctx.session, intent, "playbook", approve=True)
    ctx.put(intent_id=intent.id, summary="Patient confirmed the appointment.")
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="write_intent",
        ref=intent.id,
        label="EHR confirmation proposed",
        actor="playbook",
    )
    return _next(f"Write intent {intent.state}")


async def await_ack(ctx: RunContext, step: Step) -> Outcome:
    from careplix_api.models import WriteIntent

    intent = await ctx.session.get(WriteIntent, ctx.data.get("intent_id") or "")
    if intent is None:
        return Outcome("fail", "The EHR write is missing.")
    if intent.state in ("accepted", "written"):
        contract.acknowledge(
            ctx.case, "nexhealth", f"write_intent:{intent.id}:{intent.remote_id or intent.state}"
        )
        return _next(intent.state)
    if intent.state in ("rejected", "failed", "needs_review"):
        return _hand_off(
            f"The EHR didn't take the confirmation ({intent.state}).",
            "Confirm the appointment in the EHR by hand.",
        )
    return _wait(utcnow() + POLL, f"Write {intent.state}")


async def _settle_backfill(ctx: RunContext, winner: ContactAttempt | None) -> Outcome:
    from careplix_api.models import BackfillOffer
    from careplix_api.practice import backfill

    if not policy.agent_may("playbook.ehr_write.appointment_book", ctx.case.workspace_id):
        return _hand_off("Policy: a person books backfill slots.", "Book from the Backfill page.")
    person = next(
        (
            c
            for c in ctx.data.get("contacts") or []
            if winner and c.get("person_ref") == winner.person_ref
        ),
        None,
    )
    offer = await ctx.session.get(BackfillOffer, (person or {}).get("offer_id") or "")
    if offer is None:
        return _hand_off(
            "The winner isn't on the opening's offer list.", "Book the slot from Backfill."
        )
    try:
        opening = await backfill.respond(
            ctx.engine.db,
            ctx.session,
            offer,
            "accepted",
            "playbook",
            ctx.settings,
            ctx.engine.transport_getter(),
        )
    except ApiError as err:
        return Outcome("fail", f"Booking stopped: {err.message}")
    if opening.state != "filled":
        return _hand_off(
            f"The booking didn't go through ({opening.state}): {opening.state_reason or ''}"[:300],
            "Check the slot on the Backfill page.",
        )
    contract.acknowledge(
        ctx.case,
        "nexhealth",
        f"write_intent:{opening.book_intent_id}:{opening.filled_appointment_ref or 'accepted'}",
    )
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="write_intent",
        ref=opening.book_intent_id,
        label="Slot booked in the EHR",
        actor="playbook",
    )
    ctx.put(summary="Slot booked for the first patient who said yes.")
    return _next("Booked")


async def inform_staff(ctx: RunContext, step: Step) -> Outcome:
    summary = ctx.data.get("summary") or "The playbook finished its steps."
    await attempts.notify_staff(
        ctx.session, ctx.case, f"{ctx.case.case_ref}: {summary}", settings=ctx.settings
    )
    return _next()


async def close(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    follow_up = step.params.get("yes_next_step") if ctx.data.get("answer") == "yes" else None
    follow_up = follow_up or ctx.data.get("follow_up")  # a step left work for a person
    if follow_up:
        contract.set_next_action(
            case,
            owner_id=case.owner_id or await ctx.engine.default_owner(ctx.session, case),
            due_at=utcnow() + timedelta(days=3),
            next_step=follow_up,
            nothing_left=False,
        )
    else:
        contract.set_next_action(case, nothing_left=True)
    verdict = contract.evaluate(case, await contract.links(ctx.session, case))
    if verdict["holds"]:
        await contract.transition(ctx.session, case, "done", "playbook")
        return Outcome("done", "Contract holds")
    missing = ", ".join(contract.PART_LABELS[k].lower() for k in verdict["missing"])
    case.nothing_left = False
    return _hand_off(f"Finished, but the contract is missing: {missing}.", f"Complete: {missing}.")


HANDLERS = {
    "check_facts": check_facts,
    "contact": contact,
    "await_replies": await_replies,
    "settle": settle,
    "await_ack": await_ack,
    "inform_staff": inform_staff,
    "close": close,
}


# --- starting playbooks ---------------------------------------------------------------------------


async def start(
    session: AsyncSession,
    case: Case,
    playbook_id: str | None = None,
    *,
    contacts: list[dict[str, Any]] | None = None,
    owner_id: str | None = None,
) -> PlaybookRun:
    """Attach a playbook to a case. Its first pass runs on the next engine tick (or ``advance``)."""
    pid = playbook_id or BY_CASE_TYPE.get(cases.type_id(case))
    playbook = PLAYBOOKS.get(pid or "")
    if playbook is None or playbook.case_type != cases.type_id(case):
        raise ApiError(422, "no_playbook", "No playbook works this case type.")
    existing = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    if existing is not None:
        raise ApiError(409, "playbook_running", "A playbook already works this case.")
    if contacts is None:
        t = types.get(cases.type_id(case))
        name = types.value(case.data, t["counterparty"]["name"]) or t["counterparty"]["label"]
        contacts = [
            {
                "label": _label(str(name)),
                "audience": t["counterparty"]["role"],
                "address_from": "case",
            }
        ]
    run = PlaybookRun(
        workspace_id=case.workspace_id,
        case_id=case.id,
        playbook_id=playbook.id,
        step=0,
        state="running",
        next_run_at=utcnow(),
        data={"contacts": contacts},
        log=[],
    )
    session.add(run)
    case.playbook = playbook.id
    case.owner_id = case.owner_id or owner_id
    if case.due_at is None:
        case.due_at = utcnow() + playbook.due
    return run


def _label(name: str) -> str:
    parts = name.split()
    return (
        initials({"first_name": parts[0], "last_name": parts[-1]}) if len(parts) > 1 else "Patient"
    )


async def resume(session: AsyncSession, case: Case, actor: str) -> PlaybookRun:
    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    if run is None:
        raise ApiError(404, "no_playbook", "No playbook works this case.")
    if run.state not in ("blocked", "handed_off", "failed"):
        raise ApiError(409, "playbook_running", "The playbook is still working this case.")
    playbook = PLAYBOOKS[run.playbook_id]
    resume_at = (run.data or {}).get("resume_at")
    if run.state == "blocked":
        run.step = 0  # check the facts again
    elif resume_at:
        # The step that handed off says where to pick up (prior auth: check with the payer again).
        run.step = playbook.index(resume_at)
        run.data = {**(run.data or {}), "resume_at": None, "resumed_at": utcnow().isoformat()}
    else:
        # A hand-off resumes by contacting again, as a new round with no winner yet.
        run.step = playbook.index("contact")
        run.data = {**(run.data or {}), "round": 0, "answer": None, "deciding_attempt": None}
        case.winner_attempt_id = None
    run.state, run.next_run_at, run.leased_until = "running", utcnow(), None
    await contract.transition(session, case, "in_progress", actor)
    return run


def run_view(run: PlaybookRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    playbook = PLAYBOOKS.get(run.playbook_id)
    return {
        "id": run.id,
        "playbook_id": run.playbook_id,
        "name": playbook.name if playbook else run.playbook_id,
        "state": run.state,
        "step": run.step,
        "steps": [
            {
                "label": s.label,
                "kind": s.kind,
                "state": "done" if i < run.step else ("current" if i == run.step else "todo"),
            }
            for i, s in enumerate(playbook.steps if playbook else ())
        ],
        "next_run_at": run.next_run_at,
        "log": (run.log or [])[-30:],
    }


# --- triggers -------------------------------------------------------------------------------------


async def _case_exists(session: AsyncSession, workspace_id: str, source: str, ref: str) -> bool:
    return (
        await session.scalar(
            select(Case.id).where(
                Case.workspace_id == workspace_id, Case.source == source, Case.source_ref == ref
            )
        )
    ) is not None


async def _create(
    session: AsyncSession,
    workspace_id: str,
    type_id: str,
    values: dict[str, Any],
    *,
    source: str,
    source_ref: str,
    extra: dict[str, Any] | None = None,
) -> Case | None:
    """A trigger's case, or None when the workspace's product line doesn't offer the type."""
    from careplix_api import packs
    from careplix_api.cases import service

    ws = await session.get(Workspace, workspace_id)
    if not packs.allows_case_type(ws.product_line if ws else None, type_id):
        return None

    case = await service.create(
        session,
        attempts.system_principal(workspace_id),
        type_id,
        values,
        source=source,
        source_ref=source_ref,
        allow_incomplete=True,
    )
    if extra:
        case.data = {**case.data, **extra}
    return case


async def _workspace_name(session: AsyncSession, workspace_id: str) -> str | None:
    ws = await session.get(Workspace, workspace_id)
    return ws.name if ws else None


async def trigger_care_opportunities(session: AsyncSession, settings: Settings) -> int:
    from careplix_api.models import User
    from careplix_api.opportunities import rules
    from careplix_api.opportunities.models import CareOpportunity, ClinicalFact

    box = PhiBox(settings)
    made = 0
    for row in await session.scalars(
        select(CareOpportunity).where(CareOpportunity.status == "approved").limit(50)
    ):
        if await _case_exists(session, row.workspace_id, "care_opportunity", row.id):
            continue
        who = {}
        fact = await session.scalar(
            select(ClinicalFact).where(
                ClinicalFact.workspace_id == row.workspace_id,
                ClinicalFact.patient_ref == row.patient_ref,
                ClinicalFact.kind == "patient",
            )
        )
        if fact is not None:
            who = box.open(fact.payload)
        program = rules.PROGRAMS.get(row.program)
        clinician = await session.get(User, row.decided_by) if row.decided_by else None
        case = await _create(
            session,
            row.workspace_id,
            "care_outreach",
            {
                "patient_name": who.get("name") or None,
                "patient_phone": who.get("phone") or None,
                "date_of_birth": who.get("birth_date") or None,
                "practice_name": await _workspace_name(session, row.workspace_id),
                "practice_phone": settings.sandbox_number or None,
                "program_name": program.title if program else row.program,
                "program_description": program.outreach if program else None,
                "approved_by": clinician.display_name if clinician else None,
            },
            source="care_opportunity",
            source_ref=row.id,
        )
        if case is None:
            continue
        # The clinician's approval is the fifth contract part, and evidence of why we call.
        case.clinician_by, case.clinician_at = row.decided_by, row.decided_at
        contract.add_link(
            session,
            case,
            part="evidence",
            kind="approval",
            ref=row.id,
            label="Clinician approved the care opportunity",
            actor="playbook",
        )
        await start(session, case, "care_outreach", owner_id=row.decided_by)
        made += 1
    return made


def _local(at: datetime, tz: str | None) -> str:
    from careplix_api.practice.backfill import zone

    return as_utc(at).astimezone(zone(tz)).strftime("%Y-%m-%d %H:%M")


async def _ehr_labels(session: AsyncSession, conn_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {"provider": {}, "location": {}}
    for rec in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn_id, EhrRecord.resource.in_(("provider", "location"))
        )
    ):
        out[rec.resource][rec.source_id] = rec.attrs
    return out


async def trigger_backfill_openings(session: AsyncSession, settings: Settings) -> int:
    from careplix_api.models import BackfillOffer, BackfillOpening

    box = PhiBox(settings)
    made = 0
    for opening in await session.scalars(
        select(BackfillOpening).where(BackfillOpening.state == "open").limit(50)
    ):
        if await _case_exists(session, opening.workspace_id, "backfill", opening.id):
            continue
        offers = list(
            await session.scalars(
                select(BackfillOffer)
                .where(BackfillOffer.opening_id == opening.id, BackfillOffer.state == "queued")
                .order_by(BackfillOffer.rank)
            )
        )
        if not offers:
            continue
        labels = await _ehr_labels(session, opening.connection_id)
        location = next(iter(labels["location"].values()), {})
        contacts = []
        for offer in offers:
            patient = await session.scalar(
                select(EhrRecord).where(
                    EhrRecord.connection_id == opening.connection_id,
                    EhrRecord.resource == "patient",
                    EhrRecord.source_id == offer.patient_ref,
                )
            )
            contacts.append(
                {
                    "label": initials(box.open(patient.pii)) if patient else "Patient",
                    "person_ref": offer.patient_ref,
                    "offer_id": offer.id,
                    "audience": "patient",
                    "address_from": "ehr",
                    "connection_id": opening.connection_id,
                }
            )
        provider = labels["provider"].get(opening.provider_ref or "", {})
        tz = opening.timezone or location.get("tz")
        case = await _create(
            session,
            opening.workspace_id,
            "slot_backfill",
            {
                "practice_name": location.get("institution_name")
                or await _workspace_name(session, opening.workspace_id),
                "practice_phone": settings.sandbox_number or None,
                "clinician_name": provider.get("name") or "the clinician",
                "slot_at": _local(opening.starts_at, tz),
                "minutes": opening.minutes,
                "location": location.get("name"),
            },
            source="backfill",
            source_ref=opening.id,
            extra={"timezone": tz, "ehr": {"connection_id": opening.connection_id}},
        )
        if case is None:
            continue
        case.due_at = min(
            utcnow() + PLAYBOOKS["slot_backfill"].due,
            as_utc(opening.starts_at) - timedelta(minutes=settings.backfill_lead_minutes),
        )
        await start(session, case, "slot_backfill", contacts=contacts)
        made += 1
    return made


async def trigger_reminders(session: AsyncSession, settings: Settings) -> int:
    now = utcnow()
    box = PhiBox(settings)
    made = 0
    conns = {
        c.id: c
        for c in await session.scalars(
            select(PayerConnection).where(PayerConnection.enabled.is_(True))
        )
    }
    for appt in await session.scalars(
        select(EhrRecord)
        .where(
            EhrRecord.resource == "appointment",
            EhrRecord.status == "booked",
            EhrRecord.starts_at > now + REMINDER_LEAD,
            EhrRecord.starts_at <= now + REMINDER_WINDOW,
        )
        .order_by(EhrRecord.starts_at)
        .limit(100)
    ):
        conn = conns.get(appt.connection_id)
        ref = f"appt:{appt.connection_id}:{appt.source_id}"
        if conn is None or await _case_exists(session, appt.workspace_id, "schedule", ref):
            continue
        patient = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == appt.connection_id,
                EhrRecord.resource == "patient",
                EhrRecord.source_id == appt.patient_ref,
            )
        )
        pii = box.open(patient.pii) if patient else {}
        labels = await _ehr_labels(session, appt.connection_id)
        location = labels["location"].get(appt.attrs.get("location_id") or "") or next(
            iter(labels["location"].values()), {}
        )
        tz = appt.attrs.get("timezone") or location.get("tz")
        name = " ".join(p for p in (pii.get("first_name"), pii.get("last_name")) if p)
        case = await _create(
            session,
            appt.workspace_id,
            "appointment_reminder",
            {
                "patient_name": name or None,
                "patient_phone": pii.get("phone"),
                "date_of_birth": pii.get("date_of_birth"),
                "practice_name": location.get("institution_name")
                or await _workspace_name(session, appt.workspace_id),
                "practice_phone": settings.sandbox_number or None,
                "clinician_name": labels["provider"]
                .get(appt.attrs.get("provider_id") or "", {})
                .get("name"),
                "appointment_at": _local(appt.starts_at, tz),
                "location": location.get("name"),
            },
            source="schedule",
            source_ref=ref,
            extra={
                "timezone": tz,
                "appointment_starts_at": as_utc(appt.starts_at).isoformat(),
                "ehr": {
                    "connection_id": appt.connection_id,
                    "appointment_id": appt.source_id,
                    "patient_id": appt.patient_ref,
                },
            },
        )
        if case is None:
            continue
        case.due_at = as_utc(appt.starts_at) - REMINDER_LEAD
        await start(session, case, "appointment_reminder")
        made += 1
    return made


TRIGGER_SCANS = {
    "care_opportunity": trigger_care_opportunities,
    "backfill_opening": trigger_backfill_openings,
    "schedule": trigger_reminders,
}


# --- the engine -----------------------------------------------------------------------------------


class PlaybookEngine:
    """Runs triggers, due playbook steps and overdue escalation; one pass per tick."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        gateway: Any = None,
        runner: Any = None,
        transport_getter=lambda: None,
    ):
        self.db, self.settings = db, settings
        self.services = {"gateway": gateway, "runner": runner}
        self.transport_getter = transport_getter
        # Playbooks whose steps live in their own package register them on import.
        from careplix_api.clinical import playbook as _clinical  # noqa: F401
        from careplix_api.practice import intake as _intake  # noqa: F401
        from careplix_api.practice import reminders as _reminders  # noqa: F401
        from careplix_api.priorauth import playbook as _prior_auth  # noqa: F401

    async def default_owner(self, session: AsyncSession, case: Case) -> str | None:
        """Who owns a hand-off when nobody does yet: the case's creator, else an administrator."""
        if case.created_by and not case.created_by.startswith("playbook"):
            return case.created_by
        return await session.scalar(
            select(Membership.user_id)
            .where(Membership.workspace_id == case.workspace_id, Membership.role == "admin")
            .limit(1)
        )

    async def scan_triggers(self) -> dict[str, int]:
        enabled = {t.strip() for t in self.settings.playbook_triggers.split(",") if t.strip()}
        made: dict[str, int] = {}
        for name, scan in TRIGGER_SCANS.items():
            if name not in enabled:
                continue
            async with self.db.sessions() as session:
                try:
                    made[name] = await scan(session, self.settings)
                    await session.commit()
                except Exception:  # noqa: BLE001 - one trigger never blocks the others
                    log.exception("playbook trigger %s failed", name)
                    await session.rollback()
        return made

    async def _claim(self, session: AsyncSession, run_id: str) -> bool:
        now = utcnow()
        claimed = await session.execute(
            update(PlaybookRun)
            .where(
                PlaybookRun.id == run_id,
                PlaybookRun.state.in_(("running", "waiting")),
                (PlaybookRun.leased_until.is_(None)) | (PlaybookRun.leased_until < now),
            )
            .values(leased_until=now + LEASE)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return claimed.rowcount == 1

    async def work(self, run_id: str) -> PlaybookRun | None:
        """Run one case's playbook until it waits, hands off or finishes."""
        async with self.db.sessions() as session:
            if not await self._claim(session, run_id):
                return None
            run = await session.get(PlaybookRun, run_id)
            await session.refresh(run)
            try:
                await self._steps(session, run)
            except Exception as exc:  # noqa: BLE001 - one broken case never stops the loop
                log.exception("playbook step failed")
                await session.rollback()
                run = await session.get(PlaybookRun, run_id)
                run.tries = (run.tries or 0) + 1
                run.next_run_at = utcnow() + timedelta(minutes=min(60, 2**run.tries))
                _log(run, "error", "error", type(exc).__name__)
            run.leased_until = None
            await session.commit()
            return run

    async def _steps(self, session: AsyncSession, run: PlaybookRun) -> None:
        case = await session.get(Case, run.case_id)
        playbook = PLAYBOOKS.get(run.playbook_id)
        if case is None or playbook is None or (case.status or "open") in contract.TERMINAL:
            run.state = "done" if case is not None and case.status == "done" else "failed"
            return
        if (case.status or "open") == "open":
            await contract.transition(session, case, "in_progress", "playbook")
        ctx = RunContext(session, self.settings, self, case, run, playbook)
        for _ in range(MAX_STEPS_PER_PASS):
            if run.step >= len(playbook.steps):
                run.state = "done"
                return
            step = playbook.steps[run.step]
            outcome = await HANDLERS[step.kind](ctx, step)
            _log(run, step.kind, outcome.kind, outcome.detail)
            if outcome.kind == "next":
                run.step += 1
                continue
            if outcome.kind == "done":
                run.state, run.step = "done", len(playbook.steps)
                return
            if outcome.kind in ("wait", "goto"):
                if outcome.kind == "goto":
                    run.step = outcome.step or 0
                run.state, run.next_run_at = "waiting", outcome.until or utcnow()
                if run.next_run_at <= utcnow():
                    continue
                return
            owner = case.owner_id or await self.default_owner(session, case)
            reason = (outcome.detail or "")[:300]
            if outcome.kind == "hand_off":
                await attempts.cancel_waiting(session, self.settings, case)
                if owner:
                    await contract.transition(
                        session,
                        case,
                        "handed_off",
                        "playbook",
                        reason=reason,
                        owner_id=owner,
                        next_step=outcome.next_step or reason,
                    )
                else:
                    await contract.transition(session, case, "blocked", "playbook", reason=reason)
                run.state = "handed_off"
            elif outcome.kind == "block":
                contract.set_next_action(
                    case,
                    owner_id=owner,
                    due_at=utcnow() + timedelta(days=1),
                    next_step=outcome.next_step,
                    nothing_left=False,
                )
                await contract.transition(session, case, "blocked", "playbook", reason=reason)
                run.state = "blocked"
            else:
                await contract.transition(session, case, "failed", "playbook", reason=reason)
                run.state = "failed"
            return
        run.next_run_at = utcnow() + POLL  # a playbook looping without waiting: look again later

    async def advance(self, case_id: str) -> PlaybookRun | None:
        """Work a case's playbook now (after a reply, a resume or a trigger)."""
        async with self.db.sessions() as session:
            run_id = await session.scalar(
                select(PlaybookRun.id).where(PlaybookRun.case_id == case_id)
            )
        return await self.work(run_id) if run_id else None

    async def run_due(self, limit: int = 50) -> int:
        async with self.db.sessions() as session:
            due = list(
                await session.scalars(
                    select(PlaybookRun.id)
                    .where(
                        PlaybookRun.state.in_(("running", "waiting")),
                        PlaybookRun.next_run_at <= utcnow(),
                    )
                    .order_by(PlaybookRun.next_run_at)
                    .limit(limit)
                )
            )
        worked = 0
        for run_id in due:
            worked += (await self.work(run_id)) is not None
        return worked

    async def escalate(self) -> int:
        async with self.db.sessions() as session:
            found = await contract.escalate_overdue(session, self.settings)
            await session.commit()
            return len(found)

    async def run_once(self) -> dict[str, Any]:
        report: dict[str, Any] = {"created": await self.scan_triggers()}
        report["worked"] = await self.run_due()
        report["escalated"] = await self.escalate()
        return report

    async def run_forever(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("playbook pass failed")
            await asyncio.sleep(interval_s)


def _log(run: PlaybookRun, step: str, outcome: str, detail: str | None) -> None:
    # Steps, outcomes and counts only: never names or numbers.
    run.log = [
        *(run.log or [])[-99:],
        {"at": utcnow().isoformat(), "step": step, "outcome": outcome, "detail": detail},
    ]


async def answer_task(session: AsyncSession, settings: Settings, task) -> str | None:
    """The task-outcome hook: a reviewed call answers its attempt. Returns the case to advance."""
    await attempts.on_task_outcome(session, settings, task)
    return task.case_id
