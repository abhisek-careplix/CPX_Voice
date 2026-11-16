"""Twilio webhooks on the public ingress (``careplix-webhooks``): verify, record, answer fast.

- ``POST /webhooks/twilio/sms``: an inbound text. Stored sealed; STOP and START change consent at
  once; everything else is routed by the Studio's background loop.
- ``POST /webhooks/twilio/sms/status?m=<message id>``: a delivery status for a text we sent. The
  query carries our own message id, so a callback that beats our commit still finds its row.

Both reject anything without a valid ``X-Twilio-Signature`` (403), before reading the body's
meaning. Responses are TwiML: empty, or the keyword reply when this app sends those.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.channels import service, twilio
from careplix_api.channels.models import ChannelMessage
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.settings import Settings, get_settings

log = logging.getLogger("careplix.webhooks")
router = APIRouter(prefix="/webhooks/twilio")


async def _session(request: Request):
    async with request.app.state.db.sessions() as session:
        yield session


def twiml(reply: str | None = None) -> Response:
    inner = f"<Message>{escape(reply)}</Message>" if reply else ""
    return Response(
        f"<?xml version='1.0' encoding='UTF-8'?><Response>{inner}</Response>",
        media_type="application/xml",
    )


async def _verified_params(request: Request, settings: Settings) -> dict[str, str]:
    # Twilio posts application/x-www-form-urlencoded (no multipart parser needed).
    raw = (await request.body()).decode("utf-8", errors="replace")
    params = parse_qsl(raw, keep_blank_values=True)
    headers = {k.lower(): v for k, v in request.headers.items()}
    urls = twilio.candidate_urls(
        settings.webhook_public_url,
        headers.get("x-forwarded-proto", request.url.scheme),
        headers.get("x-forwarded-host") or headers.get("host", ""),
        request.url.path,
        request.url.query,
    )
    token = settings.twilio_auth_token.get_secret_value()
    if not twilio.verify(token, urls, params, headers.get("x-twilio-signature", "")):
        log.warning(
            "twilio webhook rejected",
            extra={"reason": "no auth token" if not token else "signature"},
        )
        raise ApiError(403, "invalid_signature", "Signature check failed.")
    if settings.twilio_account_sid and dict(params).get("AccountSid") not in (
        None,
        settings.twilio_account_sid,
    ):
        raise ApiError(403, "wrong_account", "Not this account's webhook.")
    return dict(params)


@router.post("/sms")
async def inbound_sms(
    request: Request,
    session: AsyncSession = Depends(_session),
    settings: Settings = Depends(get_settings),
):
    params = await _verified_params(request, settings)
    msg, reply = await service.record_inbound(session, settings, params)
    log.info(
        "inbound text",
        extra={"duplicate": msg is None, "intent": msg.intent if msg else None},
    )
    return twiml(reply)


@router.post("/sms/status")
async def sms_status(
    request: Request,
    m: str = "",
    session: AsyncSession = Depends(_session),
    settings: Settings = Depends(get_settings),
):
    params = await _verified_params(request, settings)
    msg = await session.get(ChannelMessage, m[:36]) if m else None
    sid = params.get("MessageSid") or params.get("SmsSid")
    if msg is None or (msg.provider_sid and sid and msg.provider_sid != sid):
        return twiml()  # not ours, or a stale id: nothing to update, nothing for Twilio to retry
    if not msg.provider_sid and sid:
        msg.provider_sid = sid[:40]
    status = params.get("MessageStatus") or params.get("SmsStatus") or ""
    if service.apply_status(msg, status, params.get("ErrorCode"), utcnow()):
        if msg.case_id:
            from careplix_api.channels import case_channel  # the case's "people informed" links

            await case_channel.mark_delivery(session, msg.id, msg.status)
        await session.commit()
    return twiml()
