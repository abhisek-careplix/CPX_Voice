"""Care-opportunity tables. Only the opportunities package reads or writes them."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class ClinicalFact(IdMixin, Base):
    """One fact about a patient the rules read: who they are (``patient``), a diagnosis
    (``condition``), a service already given (``service``), a hospital discharge or a screening
    score. Diagnoses and demographics are PHI, so the fact itself is sealed (``PhiBox``)."""

    __tablename__ = "clinical_facts"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    patient_ref: Mapped[str] = mapped_column(String(60), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # patient|condition|service|discharge|screening
    payload: Mapped[str | None] = mapped_column(Text)  # sealed
    source: Mapped[str] = mapped_column(String(12), default="import")  # import | sample | ehr
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CareOpportunity(IdMixin, Base):
    """A service a patient qualifies for and hasn't had. A clinician approves or declines it
    before anyone contacts the patient; staff (or the voice agent) then record the outcome.

    proposed → approved | declined; approved → contacted → enrolled | not_interested;
    a proposed one the rules no longer find becomes ``resolved``."""

    __tablename__ = "care_opportunities"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    patient_ref: Mapped[str] = mapped_column(String(60), index=True)
    program: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    due_by: Mapped[date | None] = mapped_column()
    why: Mapped[str | None] = mapped_column(Text)  # sealed list of reasons (names diagnoses)
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
