"""Person memory tables (OPERATING_PLAN D4, P3). Only the people package reads or writes them.

Identity details (names, dates of birth, phone numbers, addresses, home coordinates) are sealed
with :class:`careplix_api.phi.PhiBox`. A patient linked to the practice mirror keeps no copy of
what the EHR already holds: their name and date of birth are read from the ``EhrRecord``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class Person(IdMixin, Base):
    """One person the workspace works with: patient, client, family member, caregiver, staff
    member or payer contact. Proxies are family members (or others) with an authorisation on a
    :class:`Relationship`."""

    __tablename__ = "people"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # patient|client|family|caregiver|staff|payer_contact
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active|inactive
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sealed: first_name, last_name, preferred_name, date_of_birth, phone, email, address, zip,
    # member_id, lat, lon. Empty for a linked patient's EHR-held fields.
    pii: Mapped[str | None] = mapped_column(Text)
    # Link to the practice mirror (resource "patient") instead of copying EHR data.
    ehr_connection_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ehr_patient_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    organisation: Mapped[str | None] = mapped_column(String(120))  # payer contacts, staff team
    role_title: Mapped[str | None] = mapped_column(String(80))
    # Contact rules
    language: Mapped[str] = mapped_column(String(12), default="en")
    preferred_channel: Mapped[str] = mapped_column(String(8), default="voice")  # voice|sms|email
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_contact_reason: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(40), default="America/Chicago")
    # [{"days": [0..6] (Mon=0), "start": "09:00", "end": "17:00"}]: when they may be contacted.
    contact_windows: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Per channel: {"sms": {"state": "granted|revoked|unknown", "at": iso, "source": "..."}}.
    # Workstream C's text consent check reads and writes the same shape.
    consent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Workforce (caregivers) and preferences (patients, clients)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Same shape as contact_windows: when they can work (caregivers) or prefer visits.
    availability: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    max_travel_km: Mapped[float | None] = mapped_column(Float)
    # {"offered": n, "accepted": n, "completed": n, "no_shows": n, "late": n}
    reliability: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Credential(IdMixin, Base):
    """A caregiver's licence, certification or compliance check, with its expiry."""

    __tablename__ = "person_credentials"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # see people.policy.CREDENTIALS
    identifier: Mapped[str | None] = mapped_column(String(60))  # licence number (synthetic)
    issued_on: Mapped[date | None] = mapped_column()
    expires_on: Mapped[date | None] = mapped_column()
    verified_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Relationship(IdMixin, Base):
    """How two people relate, from the subject's side (the patient or client).

    ``proxy`` marks an authorised representative: what they may act on (``scope``), from when
    and until when. Revoking keeps the row as evidence. Client fit between a client and a
    caregiver is a relationship too (``preferred_caregiver`` / ``declined_caregiver``).
    """

    __tablename__ = "person_relationships"
    __table_args__ = (UniqueConstraint("subject_id", "related_id", "relation"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    related_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    relation: Mapped[str] = mapped_column(String(24))  # see people.policy.RELATIONS
    proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)  # see people.policy.TOPICS
    starts_on: Mapped[date | None] = mapped_column()
    expires_on: Mapped[date | None] = mapped_column()
    evidence: Mapped[str | None] = mapped_column(String(200))  # e.g. "HIPAA form on file 2026-03"
    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Observation(IdMixin, Base):
    """Something learned about a person: a preference, availability, a note after a call.

    The text is sealed. ``source_attempt_id`` is the call it came from; ``visibility`` decides
    whether agents may read it (``agent``) or only staff (``staff``).
    """

    __tablename__ = "person_observations"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    # preference|availability|contact|schedule|care_plan|billing|note
    topic: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)  # sealed {"text": ...}
    visibility: Mapped[str] = mapped_column(String(8), default="agent")  # agent|staff
    source: Mapped[str] = mapped_column(String(8))  # call|staff|import|sample
    source_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)  # staff reviewed
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class VerificationPolicy(IdMixin, Base):
    """The workspace's identity and proxy rules (see ``people.policy.DEFAULT_POLICY``)."""

    __tablename__ = "verification_policies"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VerificationEvent(IdMixin, Base):
    """Evidence of every identity or proxy check an agent made: which identifiers were checked
    (names only, never values), the level reached and the outcome."""

    __tablename__ = "verification_events"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    check: Mapped[str] = mapped_column(String(12))  # identity|step_up|proxy|disclosure
    person_id: Mapped[str | None] = mapped_column(String(36), index=True)  # who was checked
    subject_id: Mapped[str | None] = mapped_column(String(36), index=True)  # proxy: for whom
    outcome: Mapped[str] = mapped_column(String(12))  # passed|failed|refused
    reason: Mapped[str | None] = mapped_column(String(40))
    identifiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    topic: Mapped[str | None] = mapped_column(String(16))
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
