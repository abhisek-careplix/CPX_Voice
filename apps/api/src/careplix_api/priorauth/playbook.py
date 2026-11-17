"""The prior authorization playbook: detect → packet → submit → track → write back → tell.

**Detect** (trigger ``auth_required``): an upcoming visit whose eligibility check (271) flags a
service as needing authorization, or whose appointment type a payer rule names
(:mod:`careplix_api.priorauth.rules`), opens one ``prior_authorization`` case per visit, with the
patient, coverage, codes, ordering provider and dates the request needs. The 271 or the rule is
the case's first evidence.

**Steps** (registered into :mod:`careplix_api.cases.playbooks`):

- ``prepare_packet``: the checklist from :mod:`careplix_api.priorauth.packet`. Gaps block the case
  with the list as its next step: a staff task in the review inbox.
- ``submit_auth``: the first route that works, in order: the payer's Da Vinci PAS API
  (``Claim/$submit``), its portal (the ``browser`` channel), a payer call by the auth-status agent,
  fax (the ``fax`` channel). Portal and fax are hooks: they report unavailable until a browser
  playbook and a fax vendor register a channel (:func:`careplix_api.cases.attempts.register`).
  Stedi has no 278, so there is no clearinghouse route yet (docs/solution/PRIOR_AUTH.md).
- ``track_auth``: ``Claim/$inquire`` on a schedule, or the call's reviewed findings. Approved →
  the auth number and dates go on the case. Pended → check again; stale (no decision after
  ``stale_after_h``) or "contact payer" → a follow-up call. Asked for more documents, or denied →
  hand-off with the payer's reason. A visit too close for a pending auth → hand-off too.
- ``record_auth``: the auth number and dates go back to the EHR as a chart note write intent when
  the connector takes one (EHR writes wait for an administrator in the review inbox); otherwise
  the payer's number is the record and staff own entering it.
- ``await_record``: the EHR's acknowledgement (or a person's decision on the note).
- ``tell_patient``: when the visit is affected (denied, still pending close to the visit, or the
  approval doesn't cover the visit date), a one-way text through the channel hook asks the patient
  to call the practice. No clinical or insurance detail goes in the text.

Every payer answer is evidence (``fhir`` links to the audited integration call, or the reviewed
call task). The completion contract (§2) holds only with that evidence, the system-of-record ack,
staff told and an owned next action.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import environment
from careplix_api.cases import attempts, contract, playbooks, policy, types
from careplix_api.cases.models import ContactAttempt
from careplix_api.cases.playbooks import POLL, Outcome, RunContext, Step, _hand_off, _next, _wait
from careplix_api.db import as_utc, utcnow
from careplix_api.integrations import pas
from careplix_api.integrations import service as gateway
from careplix_api.integrations.fhir import FhirError
from careplix_api.models import (
    Case,
    EhrRecord,
    EligibilityCheck,
    Finding,
    PayerConnection,
    WriteIntent,
)
from careplix_api.phi import PhiBox, initials
from careplix_api.priorauth import packet, rules
from careplix_api.settings import Settings

PLAYBOOK = "prior_authorization"
SOURCE = "prior_auth"
HORIZON = timedelta(days=30)  # visits this far ahead are checked for auth
ROUTE_CHANNEL = {"portal": "browser", "call": "call", "fax": "fax"}
APPROVED = "approved"
DECIDED = ("approved", "denied")
UPCOMING = ("booked", "confirmed")


def tracking_id(case: Case) -> str:
    """Our PAS tracking identifier: stable per case, unique across workspaces."""
    return f"pa:{case.id}"


def _at(value: Any) -> datetime | None:
    return as_utc(datetime.fromisoformat(value)) if value else None


def _hours(step: Step, key: str, default: float) -> timedelta:
    return timedelta(hours=float(step.params.get(key, default)))


def _note(ctx: RunContext, text: str) -> None:
    notes = [*(ctx.data.get("route_notes") or []), text[:160]]
    ctx.put(route_notes=notes[-6:])


def _set_case(case: Case, path: str, value: Any) -> None:
    data = dict(case.data or {})
    node = data
    *parents, leaf = path.split(".")
    for part in parents:
        node[part] = dict(node.get(part) or {})
        node = node[part]
    node[leaf] = value
    case.data = data  # reassign: JSON columns don't track in-place mutation
    case.updated_at = utcnow()


# --- prepare --------------------------------------------------------------------------------------


async def prepare_packet(ctx: RunContext, step: Step) -> Outcome:
    items = await packet.checklist(ctx.session, ctx.case, ctx.data.get("packet_provided") or {})
    ctx.put(packet=items, packet_checked_at=utcnow().isoformat())
    gaps = packet.missing(items)
    if gaps:
        names = ", ".join(packet.in_sentence(i["label"]) for i in gaps)
        return Outcome(
            "block",
            f"The prior auth packet is missing: {names}."[:300],
            next_step=f"Complete the packet ({names}), then resume the playbook."[:300],
        )
    return _next(f"Packet complete: {sum(i['present'] for i in items)} items")


# --- submit ---------------------------------------------------------------------------------------


def _request_defaults(case: Case) -> None:
    data = case.data or {}
    if not types.value(data, "request.submitted_on"):
        _set_case(case, "request.submitted_on", utcnow().date().isoformat())
    if not types.value(data, "request.urgency"):
        _set_case(case, "request.urgency", "standard")
    if not types.value(data, "service.units"):
        _set_case(case, "service.units", 1)


async def _pas(ctx: RunContext) -> PayerConnection | None:
    payer = types.value(ctx.case.data or {}, "payer.name") or ""
    return await gateway.find_connection(ctx.session, ctx.case.workspace_id, payer)


async def _pas_call(
    ctx: RunContext, conn: PayerConnection, operation: str, bundle: dict[str, Any], phase: str
) -> pas.PasStatus | None:
    """One PAS operation, audited (outcome codes only) and linked as evidence."""
    client = gateway.client_for(conn, ctx.engine.transport_getter())
    try:
        response = await client.operation(f"Claim/${operation}", bundle)
    except FhirError as err:
        await gateway._audit_call(
            ctx.session,
            conn,
            f"pas.{operation}",
            phase,
            None,
            ok=False,
            status=err.status,
            http_status=err.http_status,
            duration_ms=0,
            error=err.message[:200],
        )
        _note(ctx, f"PAS {operation}: {err.message}")
        return None
    finally:
        await client.aclose()
    status = pas.parse_inquiry_response(response.body)
    call = await gateway._audit_call(
        ctx.session,
        conn,
        f"pas.{operation}",
        phase,
        None,
        ok=True,
        status="success" if status.found else "not_found",
        http_status=response.http_status,
        duration_ms=response.duration_ms,
        request_hash=response.request_hash,
        summary={
            k: v
            for k, v in status.as_dict().items()
            if k in {"found", "outcome", "review_action", "reported_status", "final", "synthetic"}
        },
    )
    if status.found and status.reported_status != ctx.data.get("last_status"):
        # Each change of the payer's answer is evidence (not every unchanged re-check).
        verb = "Submitted" if operation == "submit" else "Payer answered"
        contract.add_link(
            ctx.session,
            ctx.case,
            part="evidence",
            kind="fhir",
            ref=call.id,
            label=f"{verb} by PAS: {_status_label(status.reported_status)}"
            + (f" ({status.review_action})" if status.review_action else ""),
            actor="playbook",
        )
    return status


def _status_label(status: str | None) -> str:
    return (status or "unknown").replace("_", " ")


async def _submit_pas(ctx: RunContext) -> bool:
    conn = await _pas(ctx)
    if conn is None:
        _note(ctx, "PAS: this payer has no prior authorization API connected")
        return False
    _request_defaults(ctx.case)
    bundle = pas.build_submit_bundle(
        ctx.case.data, tracking_id(ctx.case), packet.documents(ctx.data.get("packet") or [])
    )
    status = await _pas_call(ctx, conn, "submit", bundle, "submit")
    if status is None or not status.found:
        if status is not None:
            _note(ctx, "PAS submit: the payer answered without a decision record")
        return False
    now = utcnow()
    ctx.put(
        route="pas",
        connection_id=conn.id,
        submitted_at=now.isoformat(),
        stale_from=now.isoformat(),
        checks=0,
        last_status=status.reported_status,
        last_reason=status.reason,
    )
    _record_status(ctx, status.reported_status, status.authorization_reference)
    return True


async def _payer_agent(session: AsyncSession, case: Case) -> str | None:
    return await playbooks._agent_for(session, case)


async def _start_attempt(ctx: RunContext, route: str, purpose: str) -> bool:
    """Start a payer attempt on a channel (call, portal, fax). False with a note if it can't."""
    name = ROUTE_CHANNEL[route]
    if not attempts.available(name):
        _note(ctx, f"{route.title()}: not available yet")
        return False
    case = ctx.case
    address = None
    if route == "call":
        address = types.value(case.data or {}, "payer.phone")
        if not address:
            _note(ctx, "Call: no payer phone number on the case")
            return False
        _request_defaults(case)  # the agent tells the payer when the practice asked
    case.winner_attempt_id = None  # a new round with the payer
    round_ = int(ctx.data.get("round", 0)) + 1
    attempt = attempts.new_attempt(
        ctx.session,
        PhiBox(ctx.settings),
        case,
        channel_name=name,
        person_label="Payer",
        address=str(address) if address else None,
        audience="payer",
        round_=round_,
    )
    await ctx.session.flush()
    result = await attempts.dispatch(
        ctx.session,
        ctx.settings,
        case,
        attempt,
        objective=None,  # the auth-status template carries its own objective
        services={**ctx.engine.services, "agent_id": await _payer_agent(ctx.session, case)},
    )
    if result.state != "sent":
        _note(ctx, f"{route.title()}: {result.detail or 'could not start'}")
        return False
    now = utcnow().isoformat()
    ctx.put(route=route, round=round_, attempt_id=attempt.id, attempt_started_at=now)
    if purpose == "submit":
        ctx.put(submitted_at=now, stale_from=now)
    return True


async def submit_auth(ctx: RunContext, step: Step) -> Outcome:
    # Production (real patients, real payers): a person submits until the owner decides the
    # agent may (G4). Labs submits on its own to the synthetic payer sandbox.
    if environment.is_production(ctx.settings) or not policy.agent_may(
        "playbook.prior_auth.submit", ctx.case.workspace_id
    ):
        ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
        return _hand_off(
            "Policy: a person submits prior authorizations.",
            "Submit the request to the payer, then resume the playbook to track it.",
        )
    ctx.put(route_notes=[])
    for route in step.params.get("routes", ("pas", "portal", "call", "fax")):
        if route == "pas":
            if await _submit_pas(ctx):
                return _next("Submitted through the payer's PAS API")
        elif await _start_attempt(ctx, route, "submit"):
            return _next(f"Started by {route}")
    notes = "; ".join(ctx.data.get("route_notes") or []) or "no route"
    return _hand_off(
        f"No route reaches this payer: {notes}."[:300],
        "Submit the prior auth on the payer's portal or by fax, then resume the playbook.",
    )


# --- track ----------------------------------------------------------------------------------------


def _record_status(ctx: RunContext, status: str, reference: str | None) -> None:
    _set_case(ctx.case, "auth.status", status)
    if reference:
        _set_case(ctx.case, "request.known_auth_reference", reference)


async def _decide(
    ctx: RunContext, step: Step, found: dict[str, Any], source: str
) -> Outcome | None:
    """A final or actionable answer → the outcome; still pending → None."""
    status = found.get("reported_status") or "unknown"
    reason = found.get("reason")
    _record_status(ctx, status, found.get("authorization_reference"))
    ctx.put(last_status=status, last_reason=reason, decided_by=source)
    if status == APPROVED:
        auth = {
            "status": status,
            "number": found.get("authorization_reference"),
            "valid_from": found.get("valid_from"),
            "valid_to": found.get("valid_to"),
            "source": source,
        }
        for key in ("valid_from", "valid_to"):
            if auth[key]:
                _set_case(ctx.case, f"auth.{key}", auth[key])
        ctx.put(auth=auth)
        number = auth["number"] or "no number given"
        return _next(f"Approved: {number}")
    if status == "denied":
        told = await _tell_patient(ctx)
        ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
        why = f"The payer denied the prior auth: {reason or 'no reason given'}."
        next_step = "Decide on a peer-to-peer review or an appeal with the ordering provider"
        next_step += "." if told else ", and call the patient about their visit."
        return _hand_off(why[:300], next_step[:300])
    if status == "pending_additional_information":
        ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
        return _hand_off(
            f"The payer asked for more: {reason or 'additional information'}."[:300],
            "Send the payer what it asked for, then resume the playbook to check again.",
        )
    return None


async def _visit_guard(ctx: RunContext, step: Step) -> Outcome | None:
    """A visit too close for a still-pending auth goes to a person, once."""
    visit = _at(((ctx.case.data or {}).get("visit") or {}).get("starts_at"))
    if visit is None or ctx.data.get("visit_guarded"):
        return None
    if visit - utcnow() > _hours(step, "visit_guard_h", 24):
        return None
    ctx.put(visit_guarded=True)
    told = await _tell_patient(ctx)
    ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
    return _hand_off(
        "The visit is close and the payer hasn't decided the prior auth.",
        "Decide with the patient whether to reschedule"
        + ("." if told else ", and call them about the visit."),
    )


async def _follow_up_call(ctx: RunContext, step: Step, reason: str) -> Outcome:
    made = int(ctx.data.get("follow_up_calls", 0))
    if made >= int(step.params.get("follow_up_calls", 2)):
        ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
        return _hand_off(
            f"{reason} No decision after {made} follow-up call(s).".strip()[:300],
            "Call the payer about the request, then resume the playbook.",
        )
    if not await _start_attempt(ctx, "call", "follow_up"):
        notes = "; ".join((ctx.data.get("route_notes") or [])[-2:])
        ctx.put(resume_at="track_auth", next_check_at=utcnow().isoformat())
        return _hand_off(
            f"{reason} A follow-up call couldn't start: {notes}."[:300],
            "Call the payer about the request, then resume the playbook.",
        )
    ctx.put(follow_up_calls=made + 1, follow_up_reason=reason[:160], next_call_at=None)
    return _wait(utcnow() + POLL, "Calling the payer")


async def _track_pas(ctx: RunContext, step: Step) -> Outcome:
    now = utcnow()
    due = _at(ctx.data.get("next_check_at"))
    if due is None:
        due = _at(ctx.data.get("submitted_at") or now.isoformat()) + timedelta(
            minutes=float(step.params.get("first_check_m", 1))
        )
    if now < due:
        return await _visit_guard(ctx, step) or _wait(due, "Next check with the payer")
    conn = await ctx.session.get(PayerConnection, ctx.data.get("connection_id") or "")
    if conn is None or not conn.enabled:
        return await _follow_up_call(ctx, step, "The payer's PAS connection is off.")
    _request_defaults(ctx.case)
    bundle = pas.build_inquiry_bundle(ctx.case.data, tracking_id(ctx.case))
    status = await _pas_call(ctx, conn, "inquire", bundle, "track")
    ctx.put(checks=int(ctx.data.get("checks", 0)) + 1, last_checked_at=now.isoformat())
    if status is None:
        errors = int(ctx.data.get("check_errors", 0)) + 1
        ctx.put(check_errors=errors)
        if errors >= 3:
            return await _follow_up_call(ctx, step, "The payer's PAS API keeps failing.")
        ctx.put(next_check_at=(now + timedelta(hours=1)).isoformat())
        return _wait(now + timedelta(hours=1), "PAS check failed; trying again")
    ctx.put(check_errors=0)
    decided = await _decide(ctx, step, status.as_dict(), "pas")
    if decided is not None:
        return decided
    stale_from = _at(ctx.data.get("stale_from")) or now
    if status.review_action == "CT":
        return await _follow_up_call(ctx, step, "The payer asked us to contact them.")
    if now - stale_from >= _hours(step, "stale_after_h", 72):
        return await _follow_up_call(
            ctx, step, f"No decision since {stale_from.date().isoformat()}."
        )
    following = now + _hours(step, "check_every_h", 24)
    ctx.put(next_check_at=following.isoformat())
    return await _visit_guard(ctx, step) or _wait(
        following, f"Pended: {status.reason or _status_label(status.reported_status)}"[:160]
    )


async def _findings(session: AsyncSession, task_id: str | None) -> dict[str, Any]:
    """The reviewed call's findings by field (latest value wins)."""
    if not task_id:
        return {}
    out: dict[str, Any] = {}
    for f in await session.scalars(
        select(Finding)
        .where(Finding.task_id == task_id, (Finding.source.is_(None)) | (Finding.source == "call"))
        .order_by(Finding.created_at)
    ):
        out[f.field] = f.value
    return out


async def _track_attempt(ctx: RunContext, step: Step) -> Outcome:
    now = utcnow()
    next_call = _at(ctx.data.get("next_call_at"))
    if next_call is not None:
        if now < next_call:
            return await _visit_guard(ctx, step) or _wait(next_call, "Next call to the payer")
        return await _follow_up_call(ctx, step, "Still no decision.")
    attempt = await ctx.session.get(ContactAttempt, ctx.data.get("attempt_id") or "")
    if attempt is None:
        return Outcome("fail", "The payer attempt is missing.")
    if attempt.state in attempts.WAITING:
        started = _at(ctx.data.get("attempt_started_at")) or now
        if now - started < _hours(step, "call_timeout_h", 48):
            return await _visit_guard(ctx, step) or _wait(
                now + POLL, f"Waiting for the {attempt.channel} result"
            )
        attempt.state, attempt.detail = "no_answer", "No result in time"
    if attempt.state != "confirmed":
        # Unsuccessful call, failed send, nobody reviewed it in time: try again later.
        ctx.put(next_call_at=(now + timedelta(hours=2)).isoformat())
        _note(ctx, f"{attempt.channel.title()}: {attempt.detail or attempt.state}")
        return _wait(now + timedelta(hours=2), f"The {attempt.channel} didn't get an answer")
    found = await _findings(ctx.session, attempt.task_id)
    if not found.get("reported_status"):
        found["reported_status"] = "unknown"
    found["reason"] = found.get("missing_requirements") or found.get("next_steps")
    decided = await _decide(ctx, step, found, attempt.channel)
    if decided is not None:
        return decided
    if found["reported_status"] == "not_found" and not ctx.data.get("connection_id"):
        ctx.put(resume_at="track_auth", next_check_at=now.isoformat())
        return _hand_off(
            "The payer has no request on file, and a call can't file one.",
            "Submit the prior auth on the payer's portal or by fax, then resume the playbook.",
        )
    ctx.put(stale_from=now.isoformat())
    if ctx.data.get("connection_id"):
        # Back to checking the payer's API after the call.
        following = now + _hours(step, "check_every_h", 24)
        ctx.put(route="pas", next_check_at=following.isoformat())
        return _wait(following, f"Payer says: {_status_label(found['reported_status'])}")
    following = now + _hours(step, "check_every_h", 24)
    ctx.put(next_call_at=following.isoformat())
    return _wait(following, f"Payer says: {_status_label(found['reported_status'])}")


async def track_auth(ctx: RunContext, step: Step) -> Outcome:
    if ctx.data.get("route") == "pas":
        return await _track_pas(ctx, step)
    if ctx.data.get("route") in ROUTE_CHANNEL:
        return await _track_attempt(ctx, step)
    # Resumed after a person submitted it themselves: check by PAS if we can, else call.
    conn = await _pas(ctx)
    if conn is not None:
        ctx.put(route="pas", connection_id=conn.id, submitted_at=utcnow().isoformat())
        return _wait(utcnow(), "Checking with the payer")
    return await _follow_up_call(ctx, step, "Checking a request submitted by staff.")


# --- record the decision --------------------------------------------------------------------------


def _ack_payer(ctx: RunContext) -> None:
    auth = ctx.data.get("auth") or {}
    contract.acknowledge(ctx.case, "payer", f"auth:{auth.get('number') or auth.get('status')}")


def _auth_note(ctx: RunContext) -> str:
    auth = ctx.data.get("auth") or {}
    data = ctx.case.data or {}
    dates = (
        f", valid {auth['valid_from']} to {auth['valid_to']}"
        if auth.get("valid_from") and auth.get("valid_to")
        else ""
    )
    return (
        f"Prior authorization {auth.get('number') or '(no number given)'} approved by "
        f"{types.value(data, 'payer.name') or 'the payer'} for CPT "
        f"{types.value(data, 'service.cpt_code') or '?'}{dates}. Recorded by CarePlix "
        f"({ctx.case.case_ref})."
    )


async def record_auth(ctx: RunContext, step: Step) -> Outcome:
    from careplix_api.connect.connectors import connector_for
    from careplix_api.practice import intents

    auth = ctx.data.get("auth") or {}
    ctx.put(summary=f"Prior auth approved: {auth.get('number') or 'no number given'}.")
    ehr = (ctx.case.data or {}).get("ehr") or {}
    conn = await ctx.session.get(PayerConnection, ehr.get("connection_id") or "")
    if conn is None or not ehr.get("patient_id"):
        _ack_payer(ctx)
        return _next("No EHR on this case: the payer's number is the record")
    support = connector_for(conn, ctx.settings).intent_support("patient_alert")
    if support == "no":
        _ack_payer(ctx)
        ctx.put(follow_up=f"Enter auth {auth.get('number')} and its dates in the EHR.")
        return _next("This EHR takes no notes from us: staff enter the number")
    intent = await intents.propose(
        ctx.session,
        conn,
        ctx.settings,
        kind="patient_alert",
        target_source_id=str(ehr["patient_id"]),
        payload={"note": _auth_note(ctx)},
        reason=f"Record the approved prior auth number in the chart ({ctx.case.case_ref})",
        source="playbook",
        requested_by="playbook",
    )
    if intent.state == "proposed" and policy.agent_may(
        "playbook.ehr_write.patient_alert", ctx.case.workspace_id
    ):
        await intents.decide(ctx.session, intent, "playbook", approve=True)
    contract.add_link(
        ctx.session,
        ctx.case,
        part="evidence",
        kind="write_intent",
        ref=intent.id,
        label="Auth number proposed as a chart note",
        actor="playbook",
    )
    ctx.put(intent_id=intent.id)
    return _next(f"Chart note {intent.state}")


async def await_record(ctx: RunContext, step: Step) -> Outcome:
    intent = await ctx.session.get(WriteIntent, ctx.data.get("intent_id") or "")
    if intent is None:
        return _next("Nothing to wait for")
    number = (ctx.data.get("auth") or {}).get("number") or "auth"
    if intent.state in ("accepted", "written"):
        contract.acknowledge(ctx.case, "ehr", f"write_intent:{intent.id}:{number}")
        return _next(f"EHR {intent.state}")
    if intent.state in ("rejected", "manual"):
        # A person chose not to (or the EHR can't take it): the payer's number is the record and
        # staff own entering it.
        _ack_payer(ctx)
        ctx.put(follow_up=f"Enter auth {number} and its dates in the EHR.")
        return _next(f"Chart note {intent.state}: staff enter it")
    if intent.state in ("failed", "needs_review"):
        _ack_payer(ctx)
        ctx.put(resume_at="tell_patient")
        return _hand_off(
            f"The EHR didn't take the auth note ({intent.state}).",
            f"Enter auth {number} and its dates in the EHR, then resume the playbook.",
        )
    # Proposed (waiting in the review inbox for an administrator), approved or sending.
    wait = timedelta(minutes=15) if intent.state == "proposed" else POLL
    return _wait(utcnow() + wait, f"Chart note {intent.state}")


# --- the patient ----------------------------------------------------------------------------------


async def _patient_phone(ctx: RunContext) -> tuple[str | None, str]:
    ehr = (ctx.case.data or {}).get("ehr") or {}
    if ehr.get("connection_id") and ehr.get("patient_id"):
        rec = await ctx.session.scalar(
            select(EhrRecord).where(
                EhrRecord.connection_id == ehr["connection_id"],
                EhrRecord.resource == "patient",
                EhrRecord.source_id == str(ehr["patient_id"]),
            )
        )
        if rec is not None:
            pii = PhiBox(ctx.settings).open(rec.pii)
            return pii.get("phone"), initials(pii) or "Patient"
    return None, "Patient"


def _visit_text(ctx: RunContext) -> str:
    """The notice body. The text channel puts the practice's name in front of it."""
    data = ctx.case.data or {}
    when = ((data.get("visit") or {}).get("at") or "").split(" ")[0] or types.value(
        data, "service.date"
    )
    phone = ctx.settings.sandbox_number
    return (
        "Please call us about your upcoming visit"
        + (f" on {when}" if when else "")
        + " before you come in"
        + (f", at {phone}" if phone else "")
        + "."
    )


async def _tell_patient(ctx: RunContext) -> bool:
    """A one-way text through the channel hook. No clinical or insurance detail in it."""
    if ctx.data.get("patient_told"):
        return True
    if not attempts.available("text"):
        return False
    address, label = await _patient_phone(ctx)
    if not address:
        return False
    attempt = attempts.new_attempt(
        ctx.session,
        PhiBox(ctx.settings),
        ctx.case,
        channel_name="text",
        person_label=label,
        person_ref=str(((ctx.case.data or {}).get("ehr") or {}).get("patient_id") or "") or None,
        address=address,
        audience="patient",
        round_=int(ctx.data.get("round", 0)),
    )
    await ctx.session.flush()
    result = await attempts.channel("text").notify(
        attempts.SendContext(ctx.session, ctx.settings, ctx.case, attempt, address, None),
        _visit_text(ctx),
    )
    attempt.message_id = result.message_id
    attempt.detail = (result.detail or None) and result.detail[:200]
    attempt.state = "notified" if result.state == "sent" else "failed"
    if result.state != "sent":
        return False
    contract.add_link(
        ctx.session,
        ctx.case,
        part="informed",
        kind="message",
        ref=result.message_id or attempt.id,
        label=f"{label} asked by text to call about their visit",
        audience="patient",
        channel="text",
        delivery="sent",
        actor="playbook",
    )
    ctx.put(patient_told=True)
    return True


def _visit_outside_auth(ctx: RunContext) -> bool:
    auth = ctx.data.get("auth") or {}
    data = ctx.case.data or {}
    day = ((data.get("visit") or {}).get("at") or "").split(" ")[0] or types.value(
        data, "service.date"
    )
    if not day or not auth.get("valid_from") or not auth.get("valid_to"):
        return False
    return not (auth["valid_from"] <= day <= auth["valid_to"])


async def tell_patient(ctx: RunContext, step: Step) -> Outcome:
    if not _visit_outside_auth(ctx):
        return _next("The visit isn't affected")
    told = await _tell_patient(ctx)
    follow_up = "Move the visit inside the approved dates, or ask the payer to change them."
    ctx.put(follow_up=follow_up if told else f"{follow_up} Call the patient about it.")
    return _next("Patient told" if told else "Patient not reached: staff call")


HANDLERS = {
    "prepare_packet": prepare_packet,
    "submit_auth": submit_auth,
    "track_auth": track_auth,
    "record_auth": record_auth,
    "await_record": await_record,
    "tell_patient": tell_patient,
}


# --- detect ---------------------------------------------------------------------------------------


async def _latest_check(session: AsyncSession, appt: EhrRecord) -> EligibilityCheck | None:
    return await session.scalar(
        select(EligibilityCheck)
        .where(
            EligibilityCheck.ehr_connection_id == appt.connection_id,
            EligibilityCheck.appointment_ref == appt.source_id,
            EligibilityCheck.state == "done",
        )
        .order_by(EligibilityCheck.created_at.desc())
        .limit(1)
    )


async def _record(session: AsyncSession, conn_id: str, resource: str, source_id: Any):
    if not source_id:
        return None
    return await session.scalar(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn_id,
            EhrRecord.resource == resource,
            EhrRecord.source_id == str(source_id),
        )
    )


async def _coverage(
    session: AsyncSession, appt: EhrRecord, check: EligibilityCheck | None
) -> EhrRecord | None:
    if check is not None and check.coverage_ref:
        found = await _record(session, appt.connection_id, "coverage", check.coverage_ref)
        if found is not None:
            return found
    rows = list(
        await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == appt.connection_id,
                EhrRecord.resource == "coverage",
                EhrRecord.patient_ref == appt.patient_ref,
                EhrRecord.status == "active",
            )
        )
    )
    rows.sort(key=lambda r: (r.attrs.get("priority") is None, r.attrs.get("priority") or 0))
    return rows[0] if rows else None


async def trigger_auth_required(session: AsyncSession, settings: Settings) -> int:
    """Open one prior auth case per upcoming visit a 271 flag or a payer rule says needs one."""
    now = utcnow()
    box = PhiBox(settings)
    conns = {
        c.id: c
        for c in await session.scalars(
            select(PayerConnection).where(PayerConnection.enabled.is_(True))
        )
    }
    by_workspace: dict[str, list] = {}
    made = 0
    for appt in await session.scalars(
        select(EhrRecord)
        .where(
            EhrRecord.resource == "appointment",
            EhrRecord.status.in_(UPCOMING),
            EhrRecord.starts_at > now,
            EhrRecord.starts_at <= now + HORIZON,
        )
        .order_by(EhrRecord.starts_at)
        .limit(200)
    ):
        ref = f"pa:{appt.connection_id}:{appt.source_id}"
        if appt.connection_id not in conns or await playbooks._case_exists(
            session, appt.workspace_id, SOURCE, ref
        ):
            continue
        check = await _latest_check(session, appt)
        flagged = list(((check.summary or {}) if check else {}).get("auth_required_services") or [])
        coverage = await _coverage(session, appt, check)
        plan = (
            await _record(
                session, appt.connection_id, "insurance_plan", coverage.attrs.get("plan_id")
            )
            if coverage
            else None
        )
        summary_payer = (check.summary or {}).get("payer") if check else None
        payer = {
            "name": (plan.attrs.get("name") if plan else None)
            or (summary_payer if isinstance(summary_payer, str) else None),
            "payer_id": (plan.attrs.get("payer_id") if plan else None)
            or (check.payer_id if check else None),
        }
        appt_type = await _record(
            session, appt.connection_id, "appointment_type", appt.attrs.get("appointment_type_id")
        )
        if appt.workspace_id not in by_workspace:
            by_workspace[appt.workspace_id] = await rules.for_workspace(session, appt.workspace_id)
        rule = rules.first_match(
            by_workspace[appt.workspace_id],
            type_ref=appt.attrs.get("appointment_type_id"),
            type_name=appt_type.attrs.get("name") if appt_type else None,
            payer=payer,
        )
        if not flagged and (
            rule is None or as_utc(appt.starts_at) - now > timedelta(days=rule.lead_days)
        ):
            continue
        case = await _open_case(
            session, settings, box, appt, check, coverage, payer, appt_type, rule, flagged, ref
        )
        if case is not None:
            made += 1
    return made


async def _open_case(
    session: AsyncSession,
    settings: Settings,
    box: PhiBox,
    appt: EhrRecord,
    check: EligibilityCheck | None,
    coverage: EhrRecord | None,
    payer: dict[str, Any],
    appt_type: EhrRecord | None,
    rule: Any,
    flagged: list[str],
    ref: str,
) -> Case | None:
    patient = await _record(session, appt.connection_id, "patient", appt.patient_ref)
    pii = box.open(patient.pii) if patient else {}
    cov_pii = box.open(coverage.pii) if coverage else {}
    labels = await playbooks._ehr_labels(session, appt.connection_id)
    location = labels["location"].get(appt.attrs.get("location_id") or "") or next(
        iter(labels["location"].values()), {}
    )
    provider = labels["provider"].get(appt.attrs.get("provider_id") or "", {})
    tz = appt.attrs.get("timezone") or location.get("tz")
    local = playbooks._local(appt.starts_at, tz)
    name = " ".join(p for p in (pii.get("first_name"), pii.get("last_name")) if p)
    service = (
        rule.service_description
        if rule
        else (f"{flagged[0]} ({appt_type.attrs.get('name')})" if appt_type else flagged[0])
    )
    values = {
        "patient_name": name or None,
        "date_of_birth": pii.get("date_of_birth"),
        "member_id": cov_pii.get("member_id") or cov_pii.get("subscriber_member_id"),
        "postal_code": (pii.get("address") or {}).get("zip_code"),
        "provider_name": location.get("institution_name")
        or await playbooks._workspace_name(session, appt.workspace_id),
        "provider_npi": provider.get("npi") or settings.practice_npi or None,
        "payer_name": payer.get("name"),
        "service_description": service,
        "cpt_code": rule.cpt_code if rule else None,
        "units": 1,
        "urgency": "standard",
        "icd_codes": ", ".join(rule.icd_codes or []) if rule and rule.icd_codes else None,
        "ordering_provider": provider.get("name") or appt.attrs.get("provider_name"),
        "service_date": local.split(" ")[0],
    }
    case = await playbooks._create(
        session,
        appt.workspace_id,
        "prior_authorization",
        values,
        source=SOURCE,
        source_ref=ref,
        extra={
            "timezone": tz,
            "ehr": {
                "connection_id": appt.connection_id,
                "appointment_id": appt.source_id,
                "patient_id": appt.patient_ref,
                "coverage_id": coverage.source_id if coverage else None,
            },
            "visit": {"starts_at": as_utc(appt.starts_at).isoformat(), "at": local},
            "auth_trigger": {
                "eligibility_check_id": check.id if check and flagged else None,
                "services": flagged,
                "rule_id": rule.id if rule else None,
            },
        },
    )
    if case is None:
        return None
    if flagged and check is not None:
        contract.add_link(
            session,
            case,
            part="evidence",
            kind="x12",
            ref=check.id,
            label=f"Eligibility (271) says prior auth is needed: {', '.join(flagged)}"[:200],
            actor="playbook",
        )
    if rule is not None:
        contract.add_link(
            session,
            case,
            part="evidence",
            kind="note",
            ref=rule.id,
            label=f"Payer rule: CPT {rule.cpt_code} needs prior auth ({rule.payer})"[:200],
            actor="playbook",
        )
    run_due = playbooks.PLAYBOOKS[PLAYBOOK].due
    case.due_at = max(
        utcnow() + timedelta(hours=1),
        min(utcnow() + run_due, as_utc(appt.starts_at) - timedelta(days=1)),
    )
    await playbooks.start(session, case, PLAYBOOK)
    return case


def install() -> None:
    playbooks.HANDLERS.update(HANDLERS)
    playbooks.TRIGGER_SCANS["auth_required"] = trigger_auth_required


install()
