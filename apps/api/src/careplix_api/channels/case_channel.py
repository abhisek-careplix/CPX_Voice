"""The text channel for case attempts (workstream B's ``cases.attempts`` hook).

- ``send``: one text per attempt, worded from the case's own facts (minimum necessary: a day and
  time, the practice, never a name or anything clinical), expecting a YES or NO. Every gate in
  :func:`careplix_api.channels.service.send` applies; a refusal fails the attempt with the reason so
  the playbook falls back to its next channel.
- ``notify``: a one-way notice ("that time has been filled").
- A YES, NO or STOP to the text answers the attempt through ``attempts.record_reply`` (first
  confirmed yes wins the case); free text is left for a person in the inbox.
- Delivery receipts update the case's "people informed" links (``attempts.mark_delivery``).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import attempts
from careplix_api.cases.models import ContactAttempt
from careplix_api.channels import service
from careplix_api.errors import ApiError
from careplix_api.settings import get_settings

log = logging.getLogger("careplix.channels")

REPLY_PREFIX = "attempt:"
REASONS = {
    "no_consent": "No consent to text on file",
    "opted_out": "Opted out of texts (STOP)",
    "not_approved": "Not an approved test number",
    "not_configured": "Texting isn't configured",
    "emergency_stop": "Emergency stop is on",
    "disabled": "Texting is off for this workspace",
    "invalid_number": "Not a textable number",
    "not_textable": "Not a textable number",
}
# Twilio status → the case contract's delivery values
DELIVERY = {
    "queued": "queued",
    "accepted": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}


def wording(case, brand: str) -> str | None:
    """The offer text for a case, or None when this case type isn't worked by text."""
    facts = cases.context(case)
    kind = cases.type_id(case)
    if kind == "slot_backfill":
        who = f" with {facts['clinician_name']}" if facts.get("clinician_name") else ""
        return (
            f"{brand}: An earlier appointment opened on {facts.get('slot_at')}{who}. Reply YES to "
            "take it or NO to pass. First to reply gets it."
        )
    if kind == "appointment_reminder":
        return (
            f"{brand}: A reminder of your appointment on {facts.get('appointment_at')}. Reply YES "
            "to confirm or NO if you need to change it."
        )
    if kind == "care_outreach":
        return (
            f"{brand}: Your care team has a program they'd like to tell you about. Reply YES and "
            "we'll call you, or NO if you're not interested."
        )
    return None


async def _brand(ctx: attempts.SendContext) -> str:
    facts = cases.context(ctx.case)
    cs = await service.workspace_settings(ctx.session, ctx.case.workspace_id)
    brand = (cs.brand_name if cs else None) or facts.get("practice_name")
    return brand or ctx.settings.practice_name


def _outcome(result: service.SendResult) -> attempts.SendResult:
    if result.status in ("blocked", "failed", "undelivered"):
        reason = REASONS.get(result.reason or "", f"Text refused: {result.reason}")
        return attempts.SendResult("failed", message_id=result.message_id, detail=reason)
    detail = (
        f"Held for quiet hours until {result.send_after:%H:%M} UTC"
        if result.status == "held" and result.send_after
        else None
    )
    return attempts.SendResult("sent", message_id=result.message_id, detail=detail)


class TextChannel:
    name = "text"

    def __init__(self, provider=None):
        self.provider = provider  # tests inject a fake; None sends through Twilio

    async def send(self, ctx: attempts.SendContext) -> attempts.SendResult:
        body = wording(ctx.case, await _brand(ctx))
        if body is None:
            return attempts.SendResult("failed", detail="This case type isn't worked by text.")
        if not ctx.address:
            return attempts.SendResult("failed", detail="No number to text.")
        result = await service.send(
            ctx.session,
            ctx.settings,
            ctx.case.workspace_id,
            "sms",
            ctx.address,
            body,
            case_id=ctx.case.id,
            purpose=f"{cases.type_id(ctx.case)}_offer"[:30],
            reply_ref=f"{REPLY_PREFIX}{ctx.attempt.id}",
            expects_reply=True,
            idempotency_key=f"{REPLY_PREFIX}{ctx.attempt.id}",
            actor="playbook",
            provider=self.provider,
            commit=False,  # the playbook engine owns this transaction
        )
        return _outcome(result)

    async def notify(self, ctx: attempts.SendContext, text: str) -> attempts.SendResult:
        if not ctx.address:
            return attempts.SendResult("unavailable", detail="No number to text.")
        result = await service.send(
            ctx.session,
            ctx.settings,
            ctx.case.workspace_id,
            "sms",
            ctx.address,
            f"{await _brand(ctx)}: {text}",
            case_id=ctx.case.id,
            purpose="notice",
            idempotency_key=f"notice:{ctx.attempt.id}",
            actor="playbook",
            provider=self.provider,
            commit=False,
        )
        return _outcome(result)


async def answer_attempt(session: AsyncSession, reply: service.InboundReply) -> bool:
    """A YES, NO or STOP to an attempt's text answers that attempt."""
    if not (reply.reply_ref or "").startswith(REPLY_PREFIX):
        return False
    if reply.intent == "other":
        return False  # a sentence needs a person; it stays in the inbox, the offer stays open
    attempt = await session.get(ContactAttempt, reply.reply_ref[len(REPLY_PREFIX) :])
    if attempt is None or attempt.workspace_id != reply.workspace_id:
        return False
    answer = "yes" if reply.intent == "yes" else "no"
    try:
        await attempts.record_reply(
            session,
            reply.settings or get_settings(),
            attempt,
            answer,
            "text_reply",
            message_id=reply.offer_message_id,
        )
    except ApiError as err:
        log.info("reply to a settled attempt", extra={"code": err.code})
    return True


async def mark_delivery(session: AsyncSession, message_id: str, status: str) -> None:
    delivery = DELIVERY.get(status.lower())
    if delivery:
        await attempts.mark_delivery(session, message_id, delivery)


def install(provider=None) -> None:
    """Register the text channel and its reply handler. A channel already registered (a test's
    fake) is left in place."""
    if provider is not None or not attempts.available("text"):
        attempts.register(TextChannel(provider))
    service.on_reply(answer_attempt)
