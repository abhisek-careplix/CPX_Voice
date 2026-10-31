"""Control-plane records (doc 06 core records, BUILD_PLAN §5.1).

Every customer-scoped table carries ``workspace_id``; services always filter by the workspace of
the authenticated principal, never by a client-supplied value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class Workspace(IdMixin, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(120))
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class User(IdMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))


class Membership(IdMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    role: Mapped[str] = mapped_column(String(20))  # author|operator|publisher|admin|reviewer


class Agent(IdMixin, Base):
    __tablename__ = "agents"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    template_id: Mapped[str] = mapped_column(String(60))
    # Version that new tasks, browser tests of "published" and inbound routes use (route pointer).
    current_version_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AgentVersion(IdMixin, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "number"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(12))  # draft|published|retired
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    config_hash: Mapped[str | None] = mapped_column(String(80))
    policy_version: Mapped[str | None] = mapped_column(String(20))
    change_note: Mapped[str | None] = mapped_column(Text)
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    published_by: Mapped[str | None] = mapped_column(String(36))
    published_at: Mapped[datetime | None] = mapped_column()


class Route(IdMixin, Base):
    __tablename__ = "routes"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(30))  # livekit_number|carrier
    direction: Mapped[str] = mapped_column(String(10))  # inbound|outbound
    number_e164: Mapped[str] = mapped_column(String(20), unique=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column()


class Case(IdMixin, Base):
    __tablename__ = "cases"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    case_ref: Mapped[str] = mapped_column(String(30))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class Task(IdMixin, Base):
    __tablename__ = "tasks"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="outbound")  # outbound|inbound
    created_by: Mapped[str] = mapped_column(String(36))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    destination_e164: Mapped[str | None] = mapped_column(String(20))
    objective: Mapped[str] = mapped_column(Text)
    additional_instructions: Mapped[str | None] = mapped_column(Text)
    # Draft|Ready|Running|Review|Completed|Partial|Unsuccessful|Cancelled
    state: Mapped[str] = mapped_column(String(14), default="Ready")
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    outcome_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CallAttempt(IdMixin, Base):
    __tablename__ = "call_attempts"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"))
    route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.id"))
    # Case bound to this attempt: from the task, or after inbound caller verification.
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    direction: Mapped[str] = mapped_column(String(10))  # outbound|inbound|browser
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    room_name: Mapped[str] = mapped_column(String(64), unique=True)
    # Created|Queued|Dialing|Connected|Ending|Ended|Failed|Cancelled
    state: Mapped[str] = mapped_column(String(12), default="Created")
    substate: Mapped[str | None] = mapped_column(String(14))  # IVR|Hold|Conversation|Transferring
    disposition: Mapped[str | None] = mapped_column(String(30))
    end_reason: Mapped[str | None] = mapped_column(String(60))
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    destination_e164: Mapped[str | None] = mapped_column(String(20))
    caller_e164: Mapped[str | None] = mapped_column(String(20))
    pins: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime | None] = mapped_column()
    last_heartbeat_at: Mapped[datetime | None] = mapped_column()
    connected_at: Mapped[datetime | None] = mapped_column()
    ended_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    next_event_seq: Mapped[int] = mapped_column(Integer, default=1)


class CallEvent(Base):
    __tablename__ = "call_events"
    __table_args__ = (UniqueConstraint("attempt_id", "seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # producer event_id: dedup key
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # server-assigned resume cursor
    type: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), index=True)
    speaker: Mapped[str] = mapped_column(String(12))  # agent|counterpart
    text: Mapped[str] = mapped_column(Text)
    playout: Mapped[str] = mapped_column(String(12))  # played|interrupted|unverified
    occurred_at: Mapped[datetime] = mapped_column()


class ToolExecution(IdMixin, Base):
    __tablename__ = "tool_executions"
    __table_args__ = (UniqueConstraint("attempt_id", "call_id"),)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), index=True)
    call_id: Mapped[str] = mapped_column(String(80))  # model tool-call id: idempotency key
    tool: Mapped[str] = mapped_column(String(40))
    tool_version: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(20))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(12))  # Authorized|Succeeded|Failed|Rejected|Unknown
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    rejection_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Finding(IdMixin, Base):
    __tablename__ = "findings"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("call_attempts.id"), index=True)
    field: Mapped[str] = mapped_column(String(60))
    value: Mapped[str | None] = mapped_column(Text)
    # provisional|confirmed_by_counterpart|reviewed|conflicting
    status: Mapped[str] = mapped_column(String(26))
    evidence_segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    original_phrasing: Mapped[str | None] = mapped_column(Text)
    null_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FindingRevision(IdMixin, Base):
    __tablename__ = "finding_revisions"
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    prior_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    prior_status: Mapped[str] = mapped_column(String(26))
    new_status: Mapped[str] = mapped_column(String(26))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditEvent(IdMixin, Base):
    __tablename__ = "audit_events"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
