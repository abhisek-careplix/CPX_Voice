"""Regression-suite tables. Only the evals package reads or writes them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class EvalRun(IdMixin, Base):
    """One suite run: an agent version simulated against a set of scenarios, each a few times,
    optionally beside the published version as a baseline."""

    __tablename__ = "eval_runs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    version_state: Mapped[str] = mapped_column(String(10))  # draft | published
    config_hash: Mapped[str] = mapped_column(String(80))  # the exact config the result is for
    baseline_version_id: Mapped[str | None] = mapped_column(String(36))
    baseline_number: Mapped[int | None] = mapped_column(Integer)
    scenarios: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{id, name}]
    repeats: Mapped[int] = mapped_column(Integer, default=3)
    # queued | running | passed | failed | stale | cancelled
    state: Mapped[str] = mapped_column(String(10), default="queued")
    summary: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(240))
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column()


class EvalCase(IdMixin, Base):
    """One simulation in a suite run: a scenario, a version (candidate or baseline), a repeat."""

    __tablename__ = "eval_cases"
    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # start order
    scenario_id: Mapped[str] = mapped_column(String(36))
    role: Mapped[str] = mapped_column(String(10))  # candidate | baseline
    version_id: Mapped[str] = mapped_column(String(36))
    repeat: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # queued | running | judging | done | cancelled
    state: Mapped[str] = mapped_column(String(10), default="queued", index=True)
    verdict: Mapped[str | None] = mapped_column(String(10))  # pass | fail | error
    rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    judge: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(240))
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
