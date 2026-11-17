"""Channel records: text messages, contact consent, workspace channel settings, call hand-offs.

Phone numbers and message bodies are sealed with :class:`careplix_api.phi.PhiBox`; lookups use a
keyed hash of the number (``peer_hash``), never the number itself. Every table carries
``workspace_id`` and every query filters on it (inbound texts that match no workspace keep it
empty and are never shown in the Studio).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class ChannelSettings(IdMixin, Base):
    """Per-workspace channel choices. Empty values fall back to the environment's defaults."""

    __tablename__ = "channel_settings"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    timezone: Mapped[str | None] = mapped_column(String(40))  # IANA, e.g. America/Chicago
    contact_start: Mapped[str | None] = mapped_column(String(5))  # "08:00", workspace-local
    contact_end: Mapped[str | None] = mapped_column(String(5))  # "20:00"
    sms_enabled: Mapped[bool | None] = mapped_column(Boolean, default=True)
    sms_from: Mapped[str | None] = mapped_column(String(20))  # this workspace's own sender
    brand_name: Mapped[str | None] = mapped_column(String(80))  # who the texts say they're from
    help_phone: Mapped[str | None] = mapped_column(String(20))  # said in HELP replies
    # Warm transfer: who takes a live call, how the agent names them, and the fallback promise.
    staff_number: Mapped[str | None] = mapped_column(String(20))
    staff_label: Mapped[str | None] = mapped_column(String(60))
    callback_promise: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime | None] = mapped_column(default=utcnow, onupdate=utcnow)


class ChannelMessage(IdMixin, Base):
    """One text, either way. Outbound status follows Twilio's callbacks (the §2 "people informed"
    evidence). An outbound text that ``expects_reply`` is an open offer until a YES or NO from the
    same person answers it (``answered_by``) or ``reply_deadline`` passes."""

    __tablename__ = "channel_messages"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10), default="sms")
    direction: Mapped[str] = mapped_column(String(8))  # outbound | inbound
    peer_hash: Mapped[str] = mapped_column(String(64), index=True)
    peer: Mapped[str | None] = mapped_column(Text)  # sealed {"number"}
    peer_last4: Mapped[str | None] = mapped_column(String(4))
    sender: Mapped[str | None] = mapped_column(String(20))  # our number (not PHI)
    body: Mapped[str | None] = mapped_column(Text)  # sealed {"body"}
    segments: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str | None] = mapped_column(String(30))
    case_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # The caller's own reference (a contact-many offer, a task attempt): replies are routed by it.
    reply_ref: Mapped[str | None] = mapped_column(String(80), index=True)
    expects_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_deadline: Mapped[datetime | None] = mapped_column()
    answered_by: Mapped[str | None] = mapped_column(String(36))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    # outbound: held|sending|queued|accepted|sent|delivered|undelivered|failed|blocked
    # inbound: received
    status: Mapped[str] = mapped_column(String(14))
    status_reason: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(10))
    provider_sid: Mapped[str | None] = mapped_column(String(40), unique=True)
    send_after: Mapped[datetime | None] = mapped_column(index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # inbound only: what the text means and what it answered
    intent: Mapped[str | None] = mapped_column(String(10))  # yes|no|stop|start|help|other
    in_reply_to: Mapped[str | None] = mapped_column(String(36))
    processed_at: Mapped[datetime | None] = mapped_column(index=True)
    routed_to: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column()
    delivered_at: Mapped[datetime | None] = mapped_column()


class ContactConsent(IdMixin, Base):
    """Whether a workspace may text a number: the latest opt-in or opt-out, with its source."""

    __tablename__ = "contact_consents"
    __table_args__ = (UniqueConstraint("workspace_id", "channel", "peer_hash"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10), default="sms")
    peer_hash: Mapped[str] = mapped_column(String(64))
    peer_last4: Mapped[str | None] = mapped_column(String(4))
    state: Mapped[str] = mapped_column(String(12))  # opted_in | opted_out
    # staff_entry|intake_form|verbal_on_call|keyword_start|keyword_stop|carrier_opt_out
    source: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(200))  # where the consent is on file, no PHI
    recorded_by: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CallHandoff(IdMixin, Base):
    """A live call handed to staff (warm transfer), or the call-back promised when nobody could
    take it. The written summary is sealed; the staff number is never stored here."""

    __tablename__ = "call_handoffs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str | None] = mapped_column(Text)  # sealed {"summary"}
    staff_label: Mapped[str | None] = mapped_column(String(60))
    # dialing | connected | callback_promised
    state: Mapped[str] = mapped_column(String(18))
    detail: Mapped[str | None] = mapped_column(String(80))  # why it fell back, no PHI
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column()
