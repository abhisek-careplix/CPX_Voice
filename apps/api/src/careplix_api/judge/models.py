"""Judge-owned table. Only the judge module reads or writes it (PRODUCT_ARCHITECTURE §3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class RunJudgement(IdMixin, Base):
    __tablename__ = "judge_run_judgements"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), unique=True)
    scenario_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(10), default="pending")  # pending | done | failed
    model_id: Mapped[str | None] = mapped_column(String(80))
    verdict: Mapped[str | None] = mapped_column(String(10))  # pass | partial | fail
    score: Mapped[int | None] = mapped_column(Integer)  # 0..100
    summary: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    limits: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    coaching: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(240))
    requested_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    judged_at: Mapped[datetime | None] = mapped_column()
