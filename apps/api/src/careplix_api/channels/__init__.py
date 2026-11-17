"""Channels (OPERATING_PLAN §7 P2): text messages today, with warm transfer on voice.

The interface other workstreams call (tasks and playbooks, contact-many offers):

    result = await channels.send(session, settings, workspace_id, "sms", to, body,
                                 case_id=case.id, purpose="opening_offer",
                                 reply_ref=offer.id, expects_reply=True,
                                 idempotency_key=f"offer:{offer.id}:{person}")
    # result.status: queued/sent… (in flight), held (quiet hours, result.send_after), or
    # blocked/failed with result.reason (opted_out, no_consent, not_approved, identifier, …)

    @channels.on_reply
    async def take_offer(session, reply: channels.InboundReply) -> bool:
        if reply.reply_ref is None or not reply.reply_ref.startswith("offer:"):
            return False          # not mine; the next handler gets it
        ...                        # reply.intent is "yes", "no", "stop" or "other"
        return True

    # §2 "people informed": message ids with delivery status
    evidence = await channels.delivery_evidence(session, workspace_id, case_id)

Consent lives behind :mod:`careplix_api.channels.consent` (``can_text``, ``record``) so person
memory can own it later. Approved wording is in :data:`careplix_api.channels.content.TEMPLATES`.
"""

from careplix_api.channels.content import TEMPLATES, render
from careplix_api.channels.service import (
    InboundReply,
    SendResult,
    delivery_evidence,
    on_reply,
    send,
)

__all__ = [
    "TEMPLATES",
    "InboundReply",
    "SendResult",
    "delivery_evidence",
    "on_reply",
    "render",
    "send",
]
