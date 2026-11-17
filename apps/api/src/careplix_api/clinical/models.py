"""Clinical tables. Only the clinical package reads or writes them; each carries workspace_id."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class ApprovedInstruction(IdMixin, Base):
    """A clinician-approved instruction, delivered word for word (OPERATING_PLAN §5.3).

    draft → approved (a clinician signs it; only approved words reach a patient) → retired.
    Editing approved words makes them a new draft version: nothing is sent until a clinician
    approves the new wording. Bodies may use only {first_name} and {practice_phone}; the text
    channel adds the practice name in front.
    """

    __tablename__ = "approved_instructions"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(120))
    topic: Mapped[str] = mapped_column(String(20))  # refill | discharge | previsit | general
    # For refills: the decision this wording tells the patient (approved | denied | needs_visit).
    outcome: Mapped[str | None] = mapped_column(String(20))
    channel: Mapped[str] = mapped_column(String(10), default="any")  # text | call | any
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(10), default="draft")  # draft|approved|retired
    approved_by: Mapped[str | None] = mapped_column(String(36))
    approved_by_name: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
