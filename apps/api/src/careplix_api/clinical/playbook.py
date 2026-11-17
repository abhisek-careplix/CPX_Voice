"""Clinical playbooks: refill requests, discharge follow-up and pre-visit preparation.

Registered into :mod:`careplix_api.cases.playbooks` (like the prior-auth playbook). The rule that
doesn't bend: **the agent never makes a clinical decision.** Each playbook gathers (answers,
protocol facts, the chart), prepares (summary, brief), routes to a clinician, and delivers only
clinician-approved words, word for word. A decision outside the approved library goes to a person.

- ``refill_request``: capture (voice, text or staff) → protocol facts from the EHR mirror → the
  clinician queue → the decision in approved words (text, else a call) → recorded → staff told.
- ``discharge_followup``: a discharge notice (staff entry, import, or a discharge fact) → a call
  within two business days with the scripted check → red flags to a clinician at once → the
  clinician reviews the answers → recorded → staff told.
- ``previsit_prep``: questionnaire by secure text link (else a call) → answers summarised → a
  chart-prep brief → the clinician reviews it → recorded → staff told.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import attempts, contract, playbooks, policy, types
from careplix_api.cases.playbooks import (
    POLL,
    Outcome,
    Playbook,
    RunContext,
    Step,
    _hand_off,
    _next,
    _wait,
)
from careplix_api.clinical import answers, brief, library, protocol, scripts
from careplix_api.db import as_utc, utcnow
from careplix_api.models import Case, EhrRecord, Workspace
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

DECISION_WAIT = timedelta(minutes=30)
CLINICAL_TYPES = ("refill_request", "discharge_followup", "previsit_prep")
DISCHARGE_DAYS = 2  # business days to make the call
DISCHARGE_LOOKBACK = timedelta(days=3)
PREVISIT_AHEAD = (timedelta(days=2), timedelta(days=5))

PLAYBOOK_DEFS = [
    Playbook(
        id="refill_request",
        name="Refill request",
        case_type="refill_request",
        triggers=("inbound_call", "inbound_text", "webhook", "import"),
        steps=(
            Step("clinical_prepare", "Set up the request and its questions"),
            Step("check_facts", "Check the request names the medicine, pharmacy and a number"),
            Step("protocol_check", "Read the protocol facts from the EHR mirror"),
            Step("tell_received", "Tell the patient the care team has the request"),
            Step("await_decision", "Wait for a clinician's decision (the clinician queue)"),
            Step("deliver_decision", "Tell the patient in approved words (text, else a call)"),
            Step("record_decision", "Record the decision"),
            Step("inform_staff", "Tell the front desk"),
            Step("close", "Check the completion contract"),
        ),
        due=timedelta(days=3),
        completion="A clinician decided, the patient heard the decision in approved words, the "
        "decision is recorded and the front desk is told.",
        hand_off="The request is missing facts, the decision isn't in the approved library, or "
        "the patient couldn't be reached: a person owns telling them.",
    ),
    Playbook(
        id="discharge_followup",
        name="Discharge follow-up",
        case_type="discharge_followup",
        triggers=("discharge_notice", "webhook", "import"),
        steps=(
            Step("clinical_prepare", "Set the two-business-day deadline and the script"),
            Step("check_facts", "Check there's a number to call"),
            Step(
                "contact",
                "Call the patient with the scripted check",
                {"channels": ("call",), "mode": "one"},
            ),
            Step(
                "await_answers",
                "Wait for the answers (red flags go to a clinician at once)",
                {"timeout_h": 6, "retries": 1, "retry_after_h": 3, "on_timeout": "hand_off"},
            ),
            Step("await_clinician", "A clinician reviews the answers"),
            Step("record_review", "Record the review"),
            Step("inform_staff", "Tell the care team"),
            Step("close", "Check the completion contract"),
        ),
        due=timedelta(days=2),
        completion="The patient answered the scripted check within two business days, any red "
        "flag reached a clinician at once, a clinician reviewed the answers and the care team "
        "is told.",
        hand_off="No number, no answer after two tries, or the deadline is near: a nurse calls "
        "the patient with the script from the Discharge follow-up page.",
    ),
    Playbook(
        id="previsit_prep",
        name="Pre-visit preparation",
        case_type="previsit_prep",
        triggers=("previsit", "webhook", "import"),
        steps=(
            Step("clinical_prepare", "Set up the questionnaire"),
            Step("check_facts", "Check there's a number to text"),
            Step(
                "send_form",
                "Send the questionnaire by secure link (else call)",
                {"form": "questionnaire"},
            ),
            Step(
                "await_answers",
                "Wait for the answers",
                {"timeout_h": 48, "retries": 0, "on_timeout": "continue"},
            ),
            Step("prepare_brief", "Build the chart-prep brief"),
            Step("await_clinician", "The clinician reviews the brief"),
            Step("record_review", "Record the review"),
            Step("inform_staff", "Tell the care team"),
            Step("close", "Check the completion contract"),
        ),
        due=timedelta(days=7),
        completion="The clinician reviewed a brief with the patient's problems, medicines, open "
        "care gaps, outside records and questionnaire answers before the visit.",
        hand_off="The questionnaire couldn't be sent: the brief still goes to the clinician; a "
        "person may call the patient for the answers.",
    ),
]


# --- business days ------------------------------------------------------------------------------


def add_business_days(start: date, days: int) -> date:
    """``days`` working days after ``start`` (weekends skipped; practice holidays come later)."""
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _day_end(d: date, tz: str | None) -> datetime:
    from careplix_api.practice.backfill import zone

    return as_utc(datetime.combine(d, time(17, 0), tzinfo=zone(tz)))


# --- helpers --------------------------------------------------------------------------------------


async def _contact_address(ctx: RunContext) -> str | None:
    contacts = ctx.data.get("contacts") or []
    return await playbooks._address(ctx, contacts[0]) if contacts else None


async def notify_patient(
    ctx: RunContext, text: str, *, label: str, purpose: str = "notice"
) -> attempts.SendResult:
    """One-way text to the case's patient, recorded as an attempt and (when sent) as informed."""
    address = await _contact_address(ctx)
    if not address:
        return attempts.SendResult("failed", detail="No number on file.")
    if not attempts.available("text"):
        return attempts.SendResult("unavailable", detail="Texting isn't available.")
    contacts = ctx.data.get("contacts") or [{}]
    attempt = attempts.new_attempt(
        ctx.session,
        PhiBox(ctx.settings),
        ctx.case,
        channel_name="text",
        person_label=contacts[0].get("label") or "Patient",
        person_ref=contacts[0].get("person_ref"),
        address=address,
        audience="patient",
        round_=int(ctx.data.get("round", 0)) or 1,
    )
    await ctx.session.flush()
    sctx = attempts.SendContext(ctx.session, ctx.settings, ctx.case, attempt, address, None)
    result = await attempts.channel("text").notify(sctx, text)
    attempt.message_id = result.message_id
    attempt.state = "notified" if result.state == "sent" else "failed"
    attempt.detail = (result.detail or None) and result.detail[:200]
    if result.state == "sent":
        contract.add_link(
            ctx.session,
            ctx.case,
            part="informed",
            kind="message",
            ref=result.message_id or attempt.id,
            label=label,
            audience="patient",
            channel="text",
            delivery="queued",
            actor="playbook",
        )
    return result


async def call_patient(ctx: RunContext, objective: str) -> attempts.SendResult:
    """One call attempt by the case type's agent, with ``objective`` as its brief."""
    address = await _contact_address(ctx)
    if not address:
        return attempts.SendResult("failed", detail="No number on file.")
    contacts = ctx.data.get("contacts") or [{}]
    round_ = int(ctx.data.get("round", 0)) + 1
    ctx.put(round=round_, round_started_at=utcnow().isoformat())
    attempt = attempts.new_attempt(
        ctx.session,
        PhiBox(ctx.settings),
        ctx.case,
        channel_name="call",
        person_label=contacts[0].get("label") or "Patient",
        person_ref=contacts[0].get("person_ref"),
        address=address,
        audience="patient",
        round_=round_,
    )
    await ctx.session.flush()
    services = {
        **ctx.engine.services,
        "agent_id": await playbooks._agent_for(ctx.session, ctx.case),
    }
    return await attempts.dispatch(
        ctx.session, ctx.settings, ctx.case, attempt, objective=objective, services=services
    )


def ready_for_review(case: Case) -> bool:
    return bool(answers.clinical(case).get("ready"))


# --- steps ----------------------------------------------------------------------------------------


async def clinical_prepare(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    tid = cases.type_id(case)
    data = dict(case.data or {})
    if scripts.script(tid) and not types.value(data, "check.questions"):
        data["check"] = {**(data.get("check") or {}), "questions": scripts.spoken(tid)}
    if tid == "discharge_followup":
        out = (data.get("discharge") or {}).get("date")
        try:
            out_day = date.fromisoformat(str(out)[:10]) if out else utcnow().date()
        except ValueError:
            out_day = utcnow().date()
        call_by = add_business_days(out_day, DISCHARGE_DAYS)
        data["discharge"] = {**(data.get("discharge") or {}), "call_by": call_by.isoformat()}
        case.due_at = _day_end(call_by, data.get("timezone"))
        case.escalated_at = None
    case.data = data
    return _next()


async def protocol_check(ctx: RunContext, step: Step) -> Outcome:
    found = await protocol.facts(ctx.session, ctx.settings, ctx.case)
    answers.save(ctx.case, {"protocol": found, "ready": True})
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="ehr_record",
        ref=f"protocol:{ctx.case.id}",
        label=f"Refill protocol facts read from the EHR mirror ({len(found)})",
        actor="playbook",
    )
    unmet = [f["label"] for f in found if f["meets"] is False]
    return _next(f"{len(unmet)} protocol facts to look at" if unmet else "Protocol facts read")


async def tell_received(ctx: RunContext, step: Step) -> Outcome:
    if (types.value(ctx.case.data or {}, "refill.channel") or "") == "call":
        return _next("Told on the call")
    from careplix_api.channels import content

    if "refill_received" not in content.TEMPLATES:
        return _next()
    result = await notify_patient(
        ctx,
        content.TEMPLATES["refill_received"],
        label="Patient told the care team has the refill request",
    )
    return _next("Told" if result.state == "sent" else f"Not told: {result.detail}")


async def await_decision(ctx: RunContext, step: Step) -> Outcome:
    decision = answers.clinical(ctx.case).get("decision")
    if decision:
        return _next(f"Decided: {decision['outcome']}")
    return _wait(utcnow() + DECISION_WAIT, "With the clinician")


async def deliver_decision(ctx: RunContext, step: Step) -> Outcome:
    decision = answers.clinical(ctx.case).get("decision") or {}
    key = decision.get("instruction_key")
    if not key:
        return _hand_off(
            "The clinician's decision isn't in the approved instructions library.",
            "Tell the patient the clinician's decision yourself, then record who told them.",
        )
    if not policy.agent_may("playbook.clinical.deliver_approved", ctx.case.workspace_id):
        return _hand_off("Policy: a person delivers clinical decisions.", "Tell the patient.")
    row = await library.approved(ctx.session, ctx.case.workspace_id, key)
    if row is None or row.version != decision.get("version"):
        return _hand_off(
            "The approved wording changed or was withdrawn after the decision.",
            "Ask the clinician to choose approved wording again, or tell the patient yourself.",
        )
    facts = cases.context(ctx.case)
    first = str(facts.get("patient_name") or "").split(" ")[0]
    words = library.render(row.body, first_name=first, practice_phone=facts.get("practice_phone"))
    sent = None
    if row.channel in ("text", "any"):
        result = await notify_patient(
            ctx,
            words,
            label=f"Patient told the decision in approved words ({row.key} v{row.version})",
        )
        if result.state == "sent":
            sent = "text"
    if sent is None and row.channel in ("call", "any"):
        result = await call_patient(
            ctx,
            f"Once you have confirmed you are speaking with {facts.get('patient_name')}, read "
            f'this message from their care team word for word, and add nothing: "{words}" '
            "Answer no questions about it; offer the practice's call-back number "
            f"({facts.get('practice_phone')}).",
        )
        if result.state == "sent":
            sent = "call"
            contract.add_link(
                ctx.session,
                ctx.case,
                part="informed",
                kind="message",
                ref=result.task_id or result.message_id,
                label=f"Patient called with the decision in approved words ({row.key})",
                audience="patient",
                channel="call",
                delivery="sent",
                actor="playbook",
            )
    if sent is None:
        return _hand_off(
            "Couldn't reach the patient by text or call to tell them the decision.",
            f"Tell the patient the decision using the approved words '{row.title}'.",
        )
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="note",
        ref=f"instruction:{row.id}:{row.version}",
        label=f"Approved words '{row.title}' v{row.version} delivered word for word by {sent}",
        actor="playbook",
    )
    answers.save(ctx.case, {"delivered": {"by": sent, "key": row.key, "version": row.version}})
    return _next(f"Delivered by {sent}")


async def record_decision(ctx: RunContext, step: Step) -> Outcome:
    decision = answers.clinical(ctx.case).get("decision") or {}
    contract.acknowledge(
        ctx.case,
        "careplix",
        f"clinician_decision:{decision.get('outcome')}:{decision.get('by', '')[:12]}",
    )
    ctx.put(
        summary=f"Refill decided ({str(decision.get('outcome', '')).replace('_', ' ')}) and the "
        "patient was told."
    )
    if decision.get("outcome") == "approved":
        ctx.put(follow_up="The clinician sends the prescription from the EHR if not already done.")
    elif decision.get("outcome") == "needs_visit":
        ctx.put(follow_up="Book the visit the clinician asked for when the patient calls.")
    return _next()


async def await_answers(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case  # loaded fresh for each pass; a refresh here would drop this pass's changes
    if answers.red_flag_open(case) or answers.complete(case):
        answers.save(case, {"ready": True})
        return _next("Answers in" + (" (red flag)" if answers.red_flag_open(case) else ""))
    started = datetime.fromisoformat(ctx.data.get("round_started_at") or utcnow().isoformat())
    deadline = started + timedelta(hours=float(step.params.get("timeout_h", 24)))
    if case.due_at and cases.type_id(case) == "discharge_followup":
        deadline = min(deadline, as_utc(case.due_at) - timedelta(hours=2))
    if utcnow() < deadline:
        return _wait(min(utcnow() + POLL * 5, deadline), "Waiting for answers")
    round_ = int(ctx.data.get("round", 1))
    retry_at = utcnow() + timedelta(hours=float(step.params.get("retry_after_h", 1)))
    near_deadline = case.due_at is not None and retry_at > as_utc(case.due_at) - timedelta(hours=2)
    if round_ <= int(step.params.get("retries", 0)) and not near_deadline:
        idx = next(
            i for i, s in enumerate(ctx.playbook.steps) if s.kind in ("contact", "send_form")
        )
        return Outcome("goto", "No answers yet: trying again later", until=retry_at, step=idx)
    if step.params.get("on_timeout") == "continue":
        answers.save(case, {"ready": True})
        return _next("No answers; the brief goes without them")
    by = types.value(case.data or {}, "discharge.call_by")
    return _hand_off(
        "The patient hasn't answered the scripted check.",
        "Call the patient with the script on the Discharge follow-up page"
        + (f" by {by}." if by else "."),
    )


async def send_form(ctx: RunContext, step: Step) -> Outcome:
    from careplix_api.forms import service as forms

    kind = step.params.get("form", "questionnaire")
    round_ = int(ctx.data.get("round", 0)) + 1
    ctx.put(round=round_, round_started_at=utcnow().isoformat())
    link, url = await forms.issue(ctx.session, ctx.settings, ctx.case, kind, "playbook")
    ctx.put(form_link=link.id)
    text = (
        f"Please answer a few questions for your care team before your visit: {url}"
        if kind == "questionnaire"
        else f"Please complete your registration before your visit: {url}"
    )
    result = await notify_patient(ctx, text, label=f"Secure {kind} link sent to the patient")
    if result.state == "sent":
        return _next("Link sent by text")
    if kind == "questionnaire":
        call = await call_patient(ctx, cases.objective(ctx.case) or "")
        if call.state == "sent":
            contract.add_link(
                ctx.session,
                ctx.case,
                part="informed",
                kind="message",
                ref=call.task_id or call.message_id,
                label="Patient called with the pre-visit questions",
                audience="patient",
                channel="call",
                delivery="sent",
                actor="playbook",
            )
            return _next("Text failed; calling instead")
        if step.params.get("on_fail") != "hand_off":
            ctx.put(round_started_at=(utcnow() - timedelta(days=30)).isoformat())
            return _next(f"Couldn't send the questionnaire: {result.detail}")
    return _hand_off(
        f"Couldn't send the {kind} link: {result.detail}",
        f"Give the patient the {kind} another way (copy a new link from the case).",
    )


async def prepare_brief(ctx: RunContext, step: Step) -> Outcome:
    built = await brief.build(ctx.session, ctx.settings, ctx.case)
    answers.save(ctx.case, {"brief": built, "brief_at": utcnow().isoformat(), "ready": True})
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="ehr_record",
        ref=f"brief:{ctx.case.id}",
        label="Chart-prep brief assembled from the EHR mirror and the questionnaire",
        actor="playbook",
    )
    return _next()


async def await_clinician(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    if case.clinician_by and not answers.red_flag_open(case):
        return _next("Reviewed")
    return _wait(utcnow() + DECISION_WAIT, "With the clinician")


async def record_review(ctx: RunContext, step: Step) -> Outcome:
    case = ctx.case
    tid = cases.type_id(case)
    contract.acknowledge(
        case, "careplix", f"clinician_review:{tid}:{(case.clinician_by or '')[:12]}"
    )
    flagged = answers.clinical(case).get("red_flags")
    ack = answers.clinical(case).get("red_flag_ack") or {}
    if tid == "discharge_followup":
        ctx.put(
            summary="Discharge check done"
            + (" with a red flag the clinician followed up." if flagged else "; nothing flagged.")
        )
        if ack.get("action") == "booked_visit" or (
            (answers.answers(case).get("follow_up_booked") or {}).get("answer") == "no"
        ):
            ctx.put(follow_up="Make sure the post-discharge visit is booked.")
        row = await library.approved(ctx.session, case.workspace_id, "discharge_check_done")
        if row is not None and row.channel in ("text", "any"):
            facts = cases.context(case)
            await notify_patient(
                ctx,
                library.render(row.body, practice_phone=facts.get("practice_phone")),
                label="Patient thanked in approved words",
            )
    else:
        ctx.put(summary="Pre-visit brief reviewed by the clinician.")
    return _next()


HANDLERS = {
    "clinical_prepare": clinical_prepare,
    "protocol_check": protocol_check,
    "tell_received": tell_received,
    "await_decision": await_decision,
    "deliver_decision": deliver_decision,
    "record_decision": record_decision,
    "await_answers": await_answers,
    "send_form": send_form,
    "prepare_brief": prepare_brief,
    "await_clinician": await_clinician,
    "record_review": record_review,
}


# --- triggers -------------------------------------------------------------------------------------


async def _patient_from_mirror(
    session: AsyncSession, settings: Settings, workspace_id: str, ref: str
) -> tuple[dict[str, Any], str | None]:
    rec = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.workspace_id == workspace_id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == ref,
        )
    )
    return (PhiBox(settings).open(rec.pii), rec.connection_id) if rec else ({}, None)


async def open_discharge(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    values: dict[str, Any],
    *,
    source: str,
    source_ref: str | None,
    patient_ref: str | None = None,
    owner_id: str | None = None,
) -> Case | None:
    ws = await session.get(Workspace, workspace_id)
    values = {
        "practice_name": ws.name if ws else None,
        "practice_phone": settings.sandbox_number or None,
        **{k: v for k, v in values.items() if v not in (None, "")},
    }
    extra = {"patient_ref": patient_ref} if patient_ref else None
    case = await playbooks._create(
        session,
        workspace_id,
        "discharge_followup",
        values,
        source=source,
        source_ref=source_ref or "",
        extra=extra,
    )
    if case is not None:
        await playbooks.start(session, case, "discharge_followup", owner_id=owner_id)
    return case


async def trigger_discharges(session: AsyncSession, settings: Settings) -> int:
    """A recent hospital discharge (imported clinical fact) opens a discharge follow-up."""
    from careplix_api.opportunities.models import ClinicalFact

    box = PhiBox(settings)
    made = 0
    today = utcnow().date()
    for fact in await session.scalars(
        select(ClinicalFact).where(ClinicalFact.kind == "discharge").limit(200)
    ):
        data = box.open(fact.payload)
        try:
            out = date.fromisoformat(str(data.get("date"))[:10])
        except ValueError:
            continue
        if not (today - DISCHARGE_LOOKBACK <= out <= today):
            continue
        ref = f"{fact.patient_ref}:{out.isoformat()}"
        if await playbooks._case_exists(session, fact.workspace_id, "discharge", ref):
            continue
        who = await session.scalar(
            select(ClinicalFact).where(
                ClinicalFact.workspace_id == fact.workspace_id,
                ClinicalFact.patient_ref == fact.patient_ref,
                ClinicalFact.kind == "patient",
            )
        )
        person = box.open(who.payload) if who else {}
        pii, _conn = await _patient_from_mirror(
            session, settings, fact.workspace_id, fact.patient_ref
        )
        name = person.get("name") or " ".join(
            p for p in (pii.get("first_name"), pii.get("last_name")) if p
        )
        case = await open_discharge(
            session,
            settings,
            fact.workspace_id,
            {
                "patient_name": name or None,
                "patient_phone": pii.get("phone"),
                "date_of_birth": person.get("birth_date") or pii.get("date_of_birth"),
                "facility": data.get("facility") or "Hospital",
                "discharged_on": out.isoformat(),
            },
            source="discharge",
            source_ref=ref,
            patient_ref=fact.patient_ref,
        )
        made += case is not None
    return made


async def open_previsit(
    session: AsyncSession,
    settings: Settings,
    appt: EhrRecord,
    *,
    owner_id: str | None = None,
) -> Case | None:
    pii, _ = await _patient_from_mirror(
        session, settings, appt.workspace_id, appt.patient_ref or ""
    )
    labels = await playbooks._ehr_labels(session, appt.connection_id)
    location = labels["location"].get(appt.attrs.get("location_id") or "") or next(
        iter(labels["location"].values()), {}
    )
    tz = appt.attrs.get("timezone") or location.get("tz")
    name = " ".join(p for p in (pii.get("first_name"), pii.get("last_name")) if p)
    case = await playbooks._create(
        session,
        appt.workspace_id,
        "previsit_prep",
        {
            "patient_name": name or None,
            "patient_phone": pii.get("phone"),
            "date_of_birth": pii.get("date_of_birth"),
            "practice_name": location.get("institution_name")
            or await playbooks._workspace_name(session, appt.workspace_id),
            "practice_phone": settings.sandbox_number or None,
            "clinician_name": labels["provider"]
            .get(appt.attrs.get("provider_id") or "", {})
            .get("name")
            or "your clinician",
            "appointment_at": playbooks._local(appt.starts_at, tz),
            "visit_type": appt.attrs.get("appointment_type_name"),
        },
        source="previsit",
        source_ref=f"appt:{appt.connection_id}:{appt.source_id}",
        extra={
            "timezone": tz,
            "patient_ref": appt.patient_ref,
            "ehr": {
                "connection_id": appt.connection_id,
                "appointment_id": appt.source_id,
                "patient_id": appt.patient_ref,
            },
        },
    )
    if case is None:
        return None
    case.due_at = as_utc(appt.starts_at) - timedelta(hours=2)
    await playbooks.start(session, case, "previsit_prep", owner_id=owner_id)
    return case


async def trigger_previsit(session: AsyncSession, settings: Settings) -> int:
    """Opt-in (``PLAYBOOK_TRIGGERS`` includes ``previsit``): booked visits 2–5 days ahead."""
    now = utcnow()
    made = 0
    for appt in await session.scalars(
        select(EhrRecord)
        .where(
            EhrRecord.resource == "appointment",
            EhrRecord.status == "booked",
            EhrRecord.starts_at > now + PREVISIT_AHEAD[0],
            EhrRecord.starts_at <= now + PREVISIT_AHEAD[1],
        )
        .order_by(EhrRecord.starts_at)
        .limit(100)
    ):
        ref = f"appt:{appt.connection_id}:{appt.source_id}"
        if await playbooks._case_exists(session, appt.workspace_id, "previsit", ref):
            continue
        made += (await open_previsit(session, settings, appt)) is not None
    return made


def register() -> None:
    for p in PLAYBOOK_DEFS:
        playbooks.PLAYBOOKS[p.id] = p
        playbooks.BY_CASE_TYPE[p.case_type] = p.id
    playbooks.HANDLERS.update(HANDLERS)
    playbooks.TRIGGERS.update(
        {
            "discharge_notice": "A hospital discharge notice (staff entry, import or a "
            "discharge fact; the ADT feed comes later)",
            "previsit": "A booked visit 2 to 5 days ahead (opt-in)",
            "inbound_text": "A text from a patient",
        }
    )
    playbooks.TRIGGER_SCANS["discharge_notice"] = trigger_discharges
    playbooks.TRIGGER_SCANS["previsit"] = trigger_previsit


register()
