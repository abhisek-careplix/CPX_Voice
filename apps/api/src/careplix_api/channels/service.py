"""Text messages: the gates every outbound text passes, delivery, status, and inbound replies.

Outbound, in order: channel available → number valid → body passes the PHI check → workspace not
stopped and texting on → Twilio configured → number approved (labs: same list as calls) → consent
on file and no opt-out → inside the contact window (else held until it opens) → Twilio. A text
refused at a gate is still stored, ``blocked`` with the reason, so the case shows the attempt.

Inbound texts are recorded by the public webhook ingress (no business logic there); the Studio's
background loop then routes each YES or NO to the open offer it answers and hands it to the
registered reply handlers (workstream B's contact-many offers).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import connections
from careplix_api.channels import consent, content, quiet_hours
from careplix_api.channels.models import ChannelMessage, ChannelSettings
from careplix_api.channels.twilio import NOT_A_MOBILE, UNSUBSCRIBED, Sent, SmsError, TwilioSms
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Workspace
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

log = logging.getLogger("careplix.channels")

CHANNELS = ("sms",)  # email, fax and team chat come with their BAAs (P2)
FOOTER_EVERY = timedelta(days=30)
RETRY_AFTER = timedelta(minutes=1)
MAX_ATTEMPTS = 3
# Twilio statuses only move forward; callbacks can arrive out of order.
_RANK = {
    "held": 0,
    "sending": 1,
    "accepted": 1,
    "scheduled": 1,
    "queued": 2,
    "sent": 3,
    "failed": 4,
    "undelivered": 4,
    "canceled": 4,
    "delivered": 5,
    "read": 6,
}
FINAL = ("delivered", "read", "failed", "undelivered", "canceled", "blocked")
UNANSWERABLE = ("held", "blocked", "failed", "undelivered", "canceled")


class Provider(Protocol):
    async def send(self, to: str, body: str, sender: str, status_callback: str | None) -> Sent: ...


@dataclass(frozen=True)
class SendResult:
    message_id: str
    status: str  # held | queued | sent | … | failed | blocked
    reason: str | None = None  # why blocked or held: quiet_hours, no_consent, opted_out, …
    send_after: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status not in ("blocked", "failed", "undelivered")


@dataclass(frozen=True)
class InboundReply:
    """A text from a person, matched to the open offer it answers (if any)."""

    workspace_id: str
    channel: str
    intent: str  # yes | no | stop | other
    message_id: str  # the inbound text
    offer_message_id: str | None  # the outbound text it answers
    reply_ref: str | None  # the sender's own reference on that outbound text
    case_id: str | None
    received_at: datetime
    text: str


ReplyHandler = Callable[[AsyncSession, InboundReply], Awaitable[bool]]
_HANDLERS: list[tuple[str, ReplyHandler]] = []


def on_reply(handler: ReplyHandler) -> ReplyHandler:
    """Register a handler for routed replies. It returns True when it took the reply. Handlers
    run in the Studio's background loop, in registration order, inside one transaction."""
    name = f"{handler.__module__}.{handler.__qualname__}"[:60]
    if all(n != name for n, _ in _HANDLERS):
        _HANDLERS.append((name, handler))
    return handler


# --- settings ------------------------------------------------------------------------------------


async def workspace_settings(session: AsyncSession, workspace_id: str) -> ChannelSettings | None:
    return await session.scalar(
        select(ChannelSettings).where(ChannelSettings.workspace_id == workspace_id)
    )


def contact_window(cs: ChannelSettings | None, settings: Settings):
    start, end = quiet_hours.parse_window(settings.contact_hours)
    if cs and cs.contact_start and cs.contact_end:
        start, end = quiet_hours.parse_window(f"{cs.contact_start}-{cs.contact_end}")
    tz = quiet_hours.zone(cs.timezone if cs else None, settings.workspace_timezone)
    return tz, start, end


def sender_for(cs: ChannelSettings | None, settings: Settings) -> str:
    return (cs.sms_from if cs else None) or settings.sms_sender


def brand_for(cs: ChannelSettings | None, settings: Settings) -> str:
    return (cs.brand_name if cs else None) or settings.practice_name


def status_callback_url(settings: Settings, message_id: str) -> str | None:
    base = settings.webhook_public_url.rstrip("/")
    return f"{base}/webhooks/twilio/sms/status?m={message_id}" if base else None


def normalize(number: str) -> str | None:
    from careplix_api.calls_service import normalize_e164  # local: calls_service is heavy

    try:
        return normalize_e164(number)
    except ApiError:
        return None


# --- outbound ------------------------------------------------------------------------------------


def _note(msg: ChannelMessage, status: str, at: datetime, **extra: Any) -> None:
    msg.history = [*(msg.history or []), {"status": status, "at": at.isoformat(), **extra}][-20:]


async def _gate(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    cs: ChannelSettings | None,
    number: str,
) -> str | None:
    """Why this workspace may not text this number right now (``None``: it may)."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return "unknown_workspace"
    if workspace.emergency_stop:
        return "emergency_stop"
    if cs is not None and cs.sms_enabled is False:
        return "disabled"
    if not settings.sms_configured or not sender_for(cs, settings):
        return "not_configured"
    eff = connections.effective(workspace, settings)
    if number not in eff.approved_destinations:
        return "not_approved"  # labs sandbox gate: the same list calls use
    return await consent.can_text(session, settings, workspace_id, number)


async def _needs_footer(session: AsyncSession, msg: ChannelMessage, now: datetime) -> bool:
    recent = await session.scalar(
        select(ChannelMessage.id).where(
            ChannelMessage.workspace_id == msg.workspace_id,
            ChannelMessage.peer_hash == msg.peer_hash,
            ChannelMessage.direction == "outbound",
            ChannelMessage.id != msg.id,
            ChannelMessage.sent_at.is_not(None),
            ChannelMessage.sent_at >= now - FOOTER_EVERY,
        )
    )
    return recent is None


async def send(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    channel: str,
    to: str,
    body: str,
    *,
    case_id: str | None = None,
    purpose: str = "notice",
    reply_ref: str | None = None,
    expects_reply: bool = False,
    idempotency_key: str | None = None,
    actor: str = "system",
    provider: Provider | None = None,
    now: datetime | None = None,
) -> SendResult:
    """Send (or hold, or refuse) one text. Commits: the row exists before Twilio is called, so a
    retry with the same ``idempotency_key`` returns the first result instead of texting twice."""
    if channel not in CHANNELS:
        raise ApiError(422, "channel_unavailable", f"The {channel} channel isn't available yet.")
    now = now or utcnow()
    if idempotency_key:
        existing = await session.scalar(
            select(ChannelMessage).where(
                ChannelMessage.workspace_id == workspace_id,
                ChannelMessage.idempotency_key == idempotency_key[:100],
            )
        )
        if existing is not None:
            return _result(existing)
    number = normalize(to)
    box = PhiBox(settings)
    cs = await workspace_settings(session, workspace_id)
    msg = ChannelMessage(
        workspace_id=workspace_id,
        channel=channel,
        direction="outbound",
        status="sending",  # replaced below before anything is committed
        peer_hash=consent.peer_hash(settings, number or to.strip()),
        peer=box.seal({"number": number or to.strip()}),
        peer_last4=(number or "")[-4:] or None,
        sender=sender_for(cs, settings) or None,
        purpose=purpose[:30],
        case_id=case_id,
        reply_ref=(reply_ref or None) and reply_ref[:80],
        expects_reply=expects_reply,
        reply_deadline=now + timedelta(hours=settings.sms_reply_window_h)
        if expects_reply
        else None,
        idempotency_key=(idempotency_key or None) and idempotency_key[:100],
        created_by=actor[:36],
        created_at=now,
        history=[],
    )
    reason = "invalid_number" if number is None else content.check(body)
    if reason is None:
        msg.body = box.seal({"body": body.strip()})
        reason = await _gate(session, settings, workspace_id, cs, number)
    if reason is not None:
        msg.status, msg.status_reason = "blocked", reason
        _note(msg, "blocked", now, reason=reason)
        session.add(msg)
        await session.commit()
        log.info("text blocked", extra={"reason": reason, "purpose": msg.purpose})
        return _result(msg)
    session.add(msg)
    tz, start, end = contact_window(cs, settings)
    if quiet_hours.is_quiet(now, tz, start, end):
        msg.status, msg.status_reason = "held", "quiet_hours"
        msg.send_after = quiet_hours.next_open(now, tz, start, end)
        _note(msg, "held", now, reason="quiet_hours")
        await session.commit()
        return _result(msg)
    await _deliver(session, settings, msg, number, body.strip(), provider, now)
    return _result(msg)


def _result(msg: ChannelMessage) -> SendResult:
    return SendResult(msg.id, msg.status, msg.status_reason, as_utc(msg.send_after))


async def _deliver(
    session: AsyncSession,
    settings: Settings,
    msg: ChannelMessage,
    number: str,
    body: str,
    provider: Provider | None,
    now: datetime,
) -> None:
    if await _needs_footer(session, msg, now):
        body = content.with_opt_out(body)
    msg.status, msg.status_reason, msg.send_after = "sending", None, None
    msg.attempts = (msg.attempts or 0) + 1
    msg.segments = content.segments(body)
    await session.commit()  # claimed before the provider call
    provider = provider or TwilioSms(settings)
    try:
        sent = await provider.send(
            number, body, msg.sender or settings.sms_sender, status_callback_url(settings, msg.id)
        )
    except SmsError as err:
        if err.code == UNSUBSCRIBED:
            # Carrier says this person replied STOP: record it so nobody tries again.
            await consent.record(
                session, settings, msg.workspace_id, number, "opted_out", "carrier_opt_out"
            )
            msg.status, msg.status_reason = "blocked", "opted_out"
        elif err.code == "network" and msg.attempts < MAX_ATTEMPTS:
            msg.status, msg.status_reason, msg.send_after = "held", "retry", now + RETRY_AFTER
        else:
            msg.status = "failed"
            msg.status_reason = "not_textable" if err.code in NOT_A_MOBILE else "provider_error"
        msg.error_code = err.code[:10]
        _note(msg, msg.status, now, error_code=msg.error_code)
        await session.commit()
        log.warning("text not sent", extra={"error_code": err.code, "purpose": msg.purpose})
        return
    msg.provider_sid = sent.sid[:40]
    msg.sent_at = now
    apply_status(msg, sent.status, None, now, force=True)
    await session.commit()


def apply_status(
    msg: ChannelMessage,
    status: str,
    error_code: str | None,
    now: datetime,
    *,
    force: bool = False,
) -> bool:
    """Move an outbound text forward to Twilio's latest status. False when it was stale."""
    status = status.lower()
    if status not in _RANK:
        return False
    if not force and _RANK[status] < _RANK.get(msg.status, 0):
        return False
    msg.status = status
    if error_code:
        msg.error_code = error_code[:10]
    if status in ("delivered", "read"):
        msg.delivered_at = msg.delivered_at or now
    _note(msg, status, now, **({"error_code": error_code} if error_code else {}))
    return True


async def deliver_due(
    session: AsyncSession,
    settings: Settings,
    provider: Provider | None = None,
    now: datetime | None = None,
) -> int:
    """Send held texts whose time has come, re-checking every gate first."""
    now = now or utcnow()
    due = list(
        await session.scalars(
            select(ChannelMessage)
            .where(
                ChannelMessage.direction == "outbound",
                ChannelMessage.status == "held",
                ChannelMessage.send_after <= now,
            )
            .order_by(ChannelMessage.send_after)
            .limit(50)
        )
    )
    box = PhiBox(settings)
    for msg in due:
        number = box.open(msg.peer).get("number", "")
        body = box.open(msg.body).get("body", "")
        cs = await workspace_settings(session, msg.workspace_id)
        reason = await _gate(session, settings, msg.workspace_id, cs, number)
        if reason is not None:
            msg.status, msg.status_reason = "blocked", reason
            _note(msg, "blocked", now, reason=reason)
            await session.commit()
            continue
        tz, start, end = contact_window(cs, settings)
        if quiet_hours.is_quiet(now, tz, start, end):
            msg.send_after = quiet_hours.next_open(now, tz, start, end)
            await session.commit()
            continue
        await _deliver(session, settings, msg, number, body, provider, now)
    return len(due)


# --- inbound -------------------------------------------------------------------------------------


async def _workspaces_for_sender(
    session: AsyncSession, settings: Settings, our_number: str
) -> list[str]:
    rows = list(await session.scalars(select(ChannelSettings)))
    own = [r.workspace_id for r in rows if r.sms_from and r.sms_from == our_number]
    if own:
        return own
    if our_number != settings.sms_sender:
        return []
    overridden = {r.workspace_id for r in rows if r.sms_from}
    return [w for w in await session.scalars(select(Workspace.id)) if w not in overridden]


async def record_inbound(
    session: AsyncSession, settings: Settings, params: dict[str, str]
) -> tuple[ChannelMessage | None, str | None]:
    """Store an inbound text and apply STOP/START at once. Returns the row (``None`` when it's a
    duplicate delivery) and the reply to send back, if this app sends keyword replies."""
    sid = (params.get("MessageSid") or params.get("SmsSid") or "")[:40]
    if sid and await session.scalar(
        select(ChannelMessage.id).where(ChannelMessage.provider_sid == sid)
    ):
        return None, None
    number = normalize(params.get("From", "")) or params.get("From", "")[:20]
    our_number = normalize(params.get("To", "")) or ""
    body = (params.get("Body") or "")[:1600]
    now = utcnow()
    kind = content.intent(body)
    opt_out_type = (params.get("OptOutType") or "").upper()  # Twilio Advanced Opt-Out
    kind = {"STOP": "stop", "START": "start", "HELP": "help"}.get(opt_out_type, kind)
    digest = consent.peer_hash(settings, number)
    candidates = await _workspaces_for_sender(session, settings, our_number)
    # Whose text this answers: the workspace that last texted this person from this number.
    latest = await session.scalar(
        select(ChannelMessage)
        .where(
            ChannelMessage.peer_hash == digest,
            ChannelMessage.direction == "outbound",
            ChannelMessage.workspace_id.in_(candidates or ["-"]),
        )
        .order_by(ChannelMessage.created_at.desc())
        .limit(1)
    )
    workspace_id = latest.workspace_id if latest else None
    if workspace_id is None and len(candidates) == 1:
        workspace_id = candidates[0]
    box = PhiBox(settings)
    msg = ChannelMessage(
        workspace_id=workspace_id,
        channel="sms",
        direction="inbound",
        peer_hash=digest,
        peer=box.seal({"number": number}),
        peer_last4=number[-4:] or None,
        sender=our_number or None,
        body=box.seal({"body": body}),
        provider_sid=sid or None,
        status="received",
        intent=kind,
        created_at=now,
        history=[{"status": "received", "at": now.isoformat()}],
    )
    session.add(msg)
    if kind == "stop":
        for ws in candidates:
            await consent.record(session, settings, ws, number, "opted_out", "keyword_stop")
    elif kind == "start":
        targets = {workspace_id} if workspace_id else set()
        for ws in candidates:
            if await consent.state(session, settings, ws, number) == "opted_out":
                targets.add(ws)
        for ws in targets:
            await consent.record(session, settings, ws, number, "opted_in", "keyword_start")
    await session.commit()
    reply = None
    if settings.sms_keyword_replies == "app" and kind in ("stop", "start", "help"):
        cs = await workspace_settings(session, workspace_id) if workspace_id else None
        reply = keyword_reply(kind, cs, settings)
    return msg, reply


def keyword_reply(kind: str, cs: ChannelSettings | None, settings: Settings) -> str:
    brand = brand_for(cs, settings)
    if kind == "stop":
        return f"{brand}: You're unsubscribed and won't get more texts. Reply START to resubscribe."
    if kind == "start":
        return (
            f"{brand}: You're resubscribed to appointment and scheduling texts. Msg & data rates "
            "may apply. Reply HELP for help, STOP to opt out."
        )
    phone = (cs.help_phone if cs else None) or ""
    call = f" For help call {phone}." if phone else " For help, call the office."
    return (
        f"{brand}: Appointment and scheduling texts.{call} Msg & data rates may apply. "
        "Reply STOP to opt out."
    )


async def route_replies(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> int:
    """Match new inbound texts to the open offer they answer and hand them to the handlers."""
    now = now or utcnow()
    pending = list(
        await session.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.direction == "inbound", ChannelMessage.processed_at.is_(None))
            .order_by(ChannelMessage.created_at)
            .limit(50)
        )
    )
    box = PhiBox(settings)
    for msg in pending:
        msg.processed_at = now
        if msg.workspace_id is None or msg.intent in ("help", "start"):
            msg.routed_to = "no_workspace" if msg.workspace_id is None else "keyword"
            await session.commit()
            continue
        received = as_utc(msg.created_at)
        offer = await session.scalar(
            select(ChannelMessage)
            .where(
                ChannelMessage.workspace_id == msg.workspace_id,
                ChannelMessage.peer_hash == msg.peer_hash,
                ChannelMessage.direction == "outbound",
                ChannelMessage.expects_reply.is_(True),
                ChannelMessage.answered_by.is_(None),
                ChannelMessage.reply_deadline >= received,
                ChannelMessage.created_at <= received,
                ChannelMessage.status.not_in(UNANSWERABLE),
            )
            .order_by(ChannelMessage.created_at.desc())
            .limit(1)
        )
        if offer is None:
            msg.routed_to = "no_open_offer"  # a person reads it in the inbox
            await session.commit()
            continue
        # A free-text answer leaves the offer open: only YES, NO or STOP settles it.
        if msg.intent in ("yes", "no", "stop"):
            offer.answered_by = msg.id
        msg.in_reply_to, msg.case_id = offer.id, offer.case_id
        reply = InboundReply(
            workspace_id=msg.workspace_id,
            channel=msg.channel,
            intent=msg.intent or "other",
            message_id=msg.id,
            offer_message_id=offer.id,
            reply_ref=offer.reply_ref,
            case_id=offer.case_id,
            received_at=received,
            text=box.open(msg.body).get("body", ""),
        )
        msg.routed_to = "unhandled"
        for name, handler in list(_HANDLERS):
            try:
                if await handler(session, reply):
                    msg.routed_to = name
                    break
            except Exception:
                log.exception("reply handler failed", extra={"handler": name})
                msg.routed_to = "handler_error"
                break
        await session.commit()
    return len(pending)


# --- evidence and the loop -----------------------------------------------------------------------


def evidence_row(msg: ChannelMessage) -> dict[str, Any]:
    """What a case records as "people informed": ids and delivery status, never the body."""
    return {
        "message_id": msg.id,
        "channel": msg.channel,
        "direction": msg.direction,
        "provider_id": msg.provider_sid,
        "to": f"•••• {msg.peer_last4}" if msg.peer_last4 else None,
        "purpose": msg.purpose,
        "status": msg.status,
        "reason": msg.status_reason,
        "error_code": msg.error_code,
        "sent_at": as_utc(msg.sent_at).isoformat() if msg.sent_at else None,
        "delivered_at": as_utc(msg.delivered_at).isoformat() if msg.delivered_at else None,
    }


async def delivery_evidence(
    session: AsyncSession, workspace_id: str, case_id: str
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(ChannelMessage)
        .where(ChannelMessage.workspace_id == workspace_id, ChannelMessage.case_id == case_id)
        .order_by(ChannelMessage.created_at)
    )
    return [evidence_row(m) for m in rows]


async def process_once(
    db: Database, settings: Settings, provider: Provider | None = None
) -> tuple[int, int]:
    async with db.sessions() as session:
        sent = await deliver_due(session, settings, provider)
    async with db.sessions() as session:
        routed = await route_replies(session, settings)
    return sent, routed


async def run_forever(db: Database, settings: Settings, provider_fn=None) -> None:
    while True:
        try:
            await process_once(db, settings, provider_fn() if provider_fn else None)
        except Exception:
            log.exception("channel pass failed")
        await asyncio.sleep(settings.sms_interval_s)
