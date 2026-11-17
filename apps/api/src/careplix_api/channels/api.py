"""Channels in the Studio (settings, message log, consent, hand-offs) and the worker's hand-off
report. Numbers are shown masked; message bodies are minimum-necessary by construction."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import connections
from careplix_api.auth import AttemptGrant, Principal, require, require_attempt_token
from careplix_api.channels import consent, quiet_hours, service, transfer
from careplix_api.channels.models import CallHandoff, ChannelMessage, ChannelSettings
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError, Rejected
from careplix_api.models import CallAttempt, Case
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
internal_router = APIRouter(prefix="/internal")

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def _mask(number: str | None) -> str | None:
    return f"•••• {number[-4:]}" if number else None


async def _view(session: AsyncSession, settings: Settings, workspace_id: str) -> dict:
    cs = await service.workspace_settings(session, workspace_id)
    tz, start, end = service.contact_window(cs, settings)
    eff = await connections.load(session, workspace_id, settings)
    staff = service.normalize(cs.staff_number) if cs and cs.staff_number else None
    base = settings.webhook_public_url.rstrip("/")
    return {
        "configured": settings.sms_configured,
        "sender": service.sender_for(cs, settings) or None,
        "messaging_service": bool(settings.twilio_messaging_service_sid),
        "keyword_replies": settings.sms_keyword_replies,
        "inbound_webhook": f"{base}/webhooks/twilio/sms" if base else None,
        "sms_enabled": cs.sms_enabled is not False if cs else True,
        "timezone": str(tz),
        "contact_start": start.strftime("%H:%M"),
        "contact_end": end.strftime("%H:%M"),
        "quiet_now": quiet_hours.is_quiet(utcnow(), tz, start, end),
        "brand_name": service.brand_for(cs, settings),
        "help_phone": cs.help_phone if cs else None,
        "staff_number": _mask(staff),
        "staff_number_approved": bool(staff and staff in eff.approved_destinations),
        "staff_label": (cs.staff_label if cs else None) or transfer.DEFAULT_LABEL,
        "callback_promise": (cs.callback_promise if cs else None) or transfer.DEFAULT_PROMISE,
        "approved_destinations": [_mask(n) for n in sorted(eff.approved_destinations)],
    }


@router.get("/channels/settings")
async def get_settings_view(
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await _view(session, settings, principal.workspace_id)


class SettingsIn(BaseModel):
    timezone: str | None = Field(default=None, max_length=40)
    contact_start: str | None = Field(default=None, pattern=HHMM)
    contact_end: str | None = Field(default=None, pattern=HHMM)
    sms_enabled: bool | None = None
    sms_from: str | None = Field(default=None, max_length=20)
    brand_name: str | None = Field(default=None, max_length=80)
    help_phone: str | None = Field(default=None, max_length=20)
    staff_number: str | None = Field(default=None, max_length=20)
    staff_label: str | None = Field(default=None, max_length=60)
    callback_promise: str | None = Field(default=None, max_length=120)


def _phone(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    number = service.normalize(raw)
    if number is None:
        raise ApiError(422, "invalid_number", "Enter a phone number like +13125550142.")
    return number


@router.put("/channels/settings")
async def put_settings(
    body: SettingsIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    cs = await service.workspace_settings(session, principal.workspace_id)
    if cs is None:
        cs = ChannelSettings(workspace_id=principal.workspace_id)
        session.add(cs)
    given = body.model_dump(exclude_unset=True)
    if "sms_from" in given and not principal.platform:
        # Senders are shared telephony (numbers, 10DLC campaigns): CarePlix staff only.
        raise ApiError(403, "forbidden", "Only CarePlix staff can change the sending number.")
    tz_name = given.get("timezone")
    if tz_name and str(quiet_hours.zone(tz_name, "UTC")) != tz_name:
        raise ApiError(422, "invalid_timezone", "Use a time zone like America/Chicago.")
    for key in ("sms_from", "help_phone", "staff_number"):
        if key in given:
            given[key] = _phone(given[key])
    for key, value in given.items():
        setattr(cs, key, value.strip() if isinstance(value, str) else value)
    cs.updated_by = principal.user_id
    start, end = cs.contact_start, cs.contact_end
    if bool(start) != bool(end) or (start and start >= end):
        raise ApiError(422, "invalid_window", "Set both times, with the start before the end.")
    await session.commit()
    return await _view(session, settings, principal.workspace_id)


def _message_row(msg: ChannelMessage, box: PhiBox) -> dict:
    return {
        **service.evidence_row(msg),
        "body": box.open(msg.body).get("body"),
        "case_id": msg.case_id,
        "reply_ref": msg.reply_ref,
        "expects_reply": msg.expects_reply,
        "answered_by": msg.answered_by,
        "intent": msg.intent,
        "in_reply_to": msg.in_reply_to,
        "routed_to": msg.routed_to,
        "send_after": as_utc(msg.send_after).isoformat() if msg.send_after else None,
        "created_at": as_utc(msg.created_at).isoformat(),
        "history": msg.history or [],
    }


@router.get("/channels/messages")
async def list_messages(
    case_id: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    query = select(ChannelMessage).where(ChannelMessage.workspace_id == principal.workspace_id)
    if case_id:
        query = query.where(ChannelMessage.case_id == case_id)
    rows = await session.scalars(
        query.order_by(ChannelMessage.created_at.desc()).limit(max(1, min(limit, 200)))
    )
    box = PhiBox(settings)
    return [_message_row(m, box) for m in rows]


class MessageIn(BaseModel):
    to: str = Field(min_length=7, max_length=20)
    body: str = Field(min_length=1, max_length=480)
    case_id: str | None = Field(default=None, max_length=36)


@router.post("/channels/messages")
async def send_message(
    body: MessageIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """A one-off text from the Studio (testing, or staff following up on a case)."""
    if body.case_id:
        case = await session.get(Case, body.case_id)
        if case is None or case.workspace_id != principal.workspace_id:
            raise ApiError(404, "case_not_found", "Case not found.")
    result = await service.send(
        session,
        settings,
        principal.workspace_id,
        "sms",
        body.to,
        body.body,
        case_id=body.case_id,
        purpose="staff_message",
        actor=principal.user_id,
        provider=getattr(request.app.state, "sms_provider", None),
    )
    msg = await session.get(ChannelMessage, result.message_id)
    return _message_row(msg, PhiBox(settings))


class ConsentIn(BaseModel):
    number: str = Field(min_length=7, max_length=20)
    state: Literal["opted_in", "opted_out"]
    source: Literal["staff_entry", "intake_form", "verbal_on_call"]
    note: str = Field(min_length=3, max_length=200)  # where the consent is on file


@router.get("/channels/consents")
async def get_consent(
    number: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    e164 = _phone(number)
    return {
        "number": _mask(e164),
        "state": await consent.state(session, settings, principal.workspace_id, e164),
    }


@router.post("/channels/consents")
async def record_consent(
    body: ConsentIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    e164 = _phone(body.number)
    row = await consent.record(
        session,
        settings,
        principal.workspace_id,
        e164,
        body.state,
        body.source,
        actor=principal.user_id,
        note=body.note,
    )
    await session.commit()
    return {"number": _mask(e164), "state": row.state, "source": row.source}


@router.get("/channels/handoffs")
async def list_handoffs(
    case_id: str | None = None,
    attempt_id: str | None = None,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    query = select(CallHandoff).where(CallHandoff.workspace_id == principal.workspace_id)
    if case_id:
        query = query.where(CallHandoff.case_id == case_id)
    if attempt_id:
        query = query.where(CallHandoff.attempt_id == attempt_id)
    rows = await session.scalars(query.order_by(CallHandoff.created_at.desc()).limit(100))
    box = PhiBox(settings)
    return [transfer.row(h, box) for h in rows]


class HandoffOutcomeIn(BaseModel):
    outcome: Literal["connected", "no_answer", "declined", "voicemail", "failed"]
    detail: str | None = Field(default=None, max_length=60)


@internal_router.post("/attempts/{attempt_id}/handoffs/{handoff_id}")
async def handoff_outcome(
    handoff_id: str,
    body: HandoffOutcomeIn,
    request: Request,
    grant: AttemptGrant = Depends(require_attempt_token),
    settings: Settings = Depends(get_settings),
):
    """The worker reports how a warm transfer went; anything but connected becomes a call-back."""
    async with request.app.state.db.sessions() as session:
        attempt = await session.get(CallAttempt, grant.attempt_id)
        if attempt is None or attempt.workspace_id != grant.workspace_id:
            raise ApiError(404, "attempt_not_found", "Attempt not found.")
        try:
            result = await transfer.settle(
                session, settings, attempt, handoff_id, body.outcome, body.detail
            )
        except Rejected as rejection:
            raise ApiError(404, rejection.code, rejection.message) from None
        await session.commit()
        return result
