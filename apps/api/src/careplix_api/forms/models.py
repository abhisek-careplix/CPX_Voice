"""Form-link table. Only the forms package reads or writes it."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class FormLink(IdMixin, Base):
    """One secure link to one form for one case. The token is never stored, only its hash."""

    __tablename__ = "form_links"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # questionnaire | intake
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column()
    opened_at: Mapped[datetime | None] = mapped_column()
    submitted_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()
    answers: Mapped[str | None] = mapped_column(Text)  # sealed
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
