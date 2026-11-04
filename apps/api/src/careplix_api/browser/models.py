"""Browser-engine tables. Only the browser module reads or writes them (PRODUCT_ARCHITECTURE §3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class BrowserRun(IdMixin, Base):
    """One supervised task on a site."""

    __tablename__ = "browser_runs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    supervisor_id: Mapped[str] = mapped_column(String(36))  # who ticked "Under human supervision"
    supervised: Mapped[bool] = mapped_column(Boolean, default=True)
    task: Mapped[str] = mapped_column(Text)
    start_url: Mapped[str] = mapped_column(String(2000))
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(10))  # gemini | claude
    model_id: Mapped[str] = mapped_column(String(80))
    # starting | planning | awaiting_plan | running | awaiting_approval | paused | done | failed
    # | stopped
    state: Mapped[str] = mapped_column(String(20), default="starting")
    control: Mapped[str] = mapped_column(String(6), default="agent")  # agent | human
    # Set when a person takes over; the agent is told to look again once control comes back.
    took_over: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(40))
    plan: Mapped[list[str]] = mapped_column(JSON, default=list)
    plan_approved_at: Mapped[datetime | None] = mapped_column()
    # Risk tiers the supervisor allowed for the rest of this run ("read", "enter_data").
    allow_for_run: Mapped[list[str]] = mapped_column(JSON, default=list)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(300))
    runtime_session: Mapped[str | None] = mapped_column(String(64))
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    last_presence_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column()


class BrowserStep(IdMixin, Base):
    """One proposed action: the approval card and what happened to it."""

    __tablename__ = "browser_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("browser_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # click | type | key | navigate | ...
    risk: Mapped[str] = mapped_column(String(12))  # read | enter_data | commit | person_only
    # proposed | auto_approved | approved | denied | executed | failed | skipped | by_person
    status: Mapped[str] = mapped_column(String(14), default="proposed")
    description: Mapped[str] = mapped_column(String(400))  # "Click 'Submit request'"
    why: Mapped[str | None] = mapped_column(Text)
    target: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # element under the action
    action: Mapped[str | None] = mapped_column(Text)  # sealed action incl. typed text
    screenshot: Mapped[str | None] = mapped_column(Text)  # sealed JPEG (evidence; contains PHI)
    page_url: Mapped[str | None] = mapped_column(String(2000))
    page_title: Mapped[str | None] = mapped_column(String(300))
    needs_approval_reason: Mapped[str | None] = mapped_column(String(200))
    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column()
    feedback: Mapped[str | None] = mapped_column(String(600))
    outcome: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BrowserSite(IdMixin, Base):
    """Per-workspace site settings: the one-time terms acknowledgement and admin allowances."""

    __tablename__ = "browser_sites"
    __table_args__ = (UniqueConstraint("workspace_id", "host"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    host: Mapped[str] = mapped_column(String(255))
    terms_note: Mapped[str | None] = mapped_column(Text)
    acknowledged_by: Mapped[str | None] = mapped_column(String(36))
    acknowledged_at: Mapped[datetime | None] = mapped_column()
    automation_permitted: Mapped[bool] = mapped_column(Boolean, default=False)
    always_allow: Mapped[list[str]] = mapped_column(JSON, default=list)  # read | enter_data


class BrowserProfile(IdMixin, Base):
    """A person's saved sign-in for a site (cookies and storage), sealed, with an expiry."""

    __tablename__ = "browser_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", "host"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36))
    host: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(Text)  # sealed Playwright storage state
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
