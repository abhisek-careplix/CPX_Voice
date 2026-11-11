"""Shadow-mode tables. Only the shadow package reads or writes them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class ShadowRun(IdMixin, Base):
    """One side-by-side comparison: a worklist the incumbent vendor already worked, run again by a
    CarePlix agent, with the two sets of answers compared field by field.

    ``mode`` is how CarePlix works each item. ``replay``: a simulation in which an AI plays the
    other side using the answers the incumbent recorded, which measures whether CarePlix gets the
    same answers. ``live``: a real call to the item's number (approved destinations only), which
    measures CarePlix against the real line.
    """

    __tablename__ = "shadow_runs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    incumbent: Mapped[str] = mapped_column(String(80))  # the vendor being compared, e.g. SuperDial
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    version_id: Mapped[str] = mapped_column(String(36))  # pinned when the run is created
    version_number: Mapped[int] = mapped_column(Integer)
    version_state: Mapped[str] = mapped_column(String(10))
    scenario_id: Mapped[str] = mapped_column(String(36))  # the call plan every item shares
    mode: Mapped[str] = mapped_column(String(10), default="replay")  # replay | live
    fields: Mapped[list[str]] = mapped_column(JSON, default=list)  # finding fields compared
    # queued | running | done | cancelled
    state: Mapped[str] = mapped_column(String(10), default="queued")
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column()


class ShadowItem(IdMixin, Base):
    """One worklist row: what the incumbent recorded for it, and what CarePlix found.

    The case facts and both sides' answers can identify a patient, so they are sealed at rest
    (``PhiBox``); the comparison keeps only outcomes per field.
    """

    __tablename__ = "shadow_items"
    run_id: Mapped[str] = mapped_column(ForeignKey("shadow_runs.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    external_ref: Mapped[str] = mapped_column(String(80))  # the incumbent's id for the row
    destination_e164: Mapped[str | None] = mapped_column(String(20))
    facts: Mapped[str | None] = mapped_column(Text)  # sealed {name: value} the agent may share
    incumbent_values: Mapped[str | None] = mapped_column(Text)  # sealed {field: value}
    # Not sensitive: {"duration_s": int | None, "resolved": bool | None}
    incumbent_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued | running | done | cancelled
    state: Mapped[str] = mapped_column(String(10), default="queued", index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    our_values: Mapped[str | None] = mapped_column(Text)  # sealed {field: value}
    our_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # [{"field", "outcome": match|mismatch|ours_only|theirs_only|neither, "adjudication"}]
    comparison: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(240))
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
