"""Cases v2 tables: contract links, contact attempts and playbook runs.

Only the cases package reads or writes them. Every table carries ``workspace_id`` so the tenant
wall scopes it like any other workspace-owned row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class CaseLink(IdMixin, Base):
    """One piece of the completion contract that is a list: evidence, or a person informed.

    ``part="evidence"``: what the agent saw or was told (a call, a transcript, a 271, a screenshot,
    an EHR record). ``part="informed"``: a message that told someone what happened, with its
    channel, message id and delivery status. Labels never carry patient details.
    """

    __tablename__ = "case_links"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    part: Mapped[str] = mapped_column(String(10))  # evidence | informed
    # task | call | transcript | attempt | message | notice | write_intent | x12 | screenshot |
    # ehr_record | approval | note
    kind: Mapped[str] = mapped_column(String(20))
    ref: Mapped[str | None] = mapped_column(String(120))
    label: Mapped[str] = mapped_column(String(200))
    audience: Mapped[str | None] = mapped_column(String(12))  # patient|caregiver|staff|payer
    channel: Mapped[str | None] = mapped_column(String(10))  # call|text|email|fax|studio|…
    delivery: Mapped[str | None] = mapped_column(String(12))  # queued|sent|delivered|failed
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ContactAttempt(IdMixin, Base):
    """One try to reach one person about a case, on one channel (D2).

    A case may have many at once ("contact many"); the first confirmed yes wins the case's
    ``winner_attempt_id`` and the rest are told it's filled. A call attempt points at its Task; a
    text, email or fax attempt carries the provider's message id.

    queued → sent → confirmed | declined | no_answer | failed
    queued|sent → superseded (someone else said yes first; notified)
    a yes after the winner → too_late (notified)
    """

    __tablename__ = "contact_attempts"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    person_ref: Mapped[str | None] = mapped_column(String(60))  # EHR patient id / person id
    person_label: Mapped[str] = mapped_column(String(60))  # initials or a role, never a full name
    audience: Mapped[str] = mapped_column(String(12), default="patient")
    channel: Mapped[str] = mapped_column(String(10))  # call | text | email | fax | browser
    address: Mapped[str | None] = mapped_column(Text)  # sealed {"address": "+1…"}
    address_hint: Mapped[str | None] = mapped_column(String(20))  # "…0142"
    state: Mapped[str] = mapped_column(String(12), default="queued")
    task_id: Mapped[str | None] = mapped_column(String(36))  # call attempts
    message_id: Mapped[str | None] = mapped_column(String(80))  # text, email, fax
    detail: Mapped[str | None] = mapped_column(String(200))
    answered_at: Mapped[datetime | None] = mapped_column()
    notice: Mapped[str | None] = mapped_column(String(10))  # sent | skipped | failed
    notice_ref: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class PlaybookRun(IdMixin, Base):
    """A playbook working one case: which step it is on and when it next looks again."""

    __tablename__ = "playbook_runs"
    __table_args__ = (UniqueConstraint("case_id"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    playbook_id: Mapped[str] = mapped_column(String(40))
    step: Mapped[int] = mapped_column(Integer, default=0)
    # running | waiting | done | handed_off | blocked | failed
    state: Mapped[str] = mapped_column(String(12), default="running", index=True)
    next_run_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    tries: Mapped[int] = mapped_column(Integer, default=0)
    leased_until: Mapped[datetime | None] = mapped_column()  # one worker at a time
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
