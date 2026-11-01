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
    # Connection choices made in the Studio. Env values are fallbacks; secrets never live here.
    outbound_trunk_id: Mapped[str | None] = mapped_column(String(64))
    outbound_verified_at: Mapped[datetime | None] = mapped_column()
    outbound_evidence_attempt_id: Mapped[str | None] = mapped_column(String(36))
    approved_destinations: Mapped[list[str] | None] = mapped_column(JSON)
    approved_model: Mapped[str | None] = mapped_column(String(80))
    gcp_project: Mapped[str | None] = mapped_column(String(80))
    vertex_location: Mapped[str | None] = mapped_column(String(40))
    # Last live model test: {model_id, ok, connect_ms, first_audio_ms, error, at}
    model_check: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class WorkerProbe(IdMixin, Base):
    """A dispatch sent only to prove a worker answers (readiness: agent worker)."""

    __tablename__ = "worker_probes"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    room_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    acked_at: Mapped[datetime | None] = mapped_column()
    runtime_build: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(String(80))


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
    # LiveKit object IDs for this route, e.g. {"dispatch_rule_id": "SDR_..."}
    provider_refs: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_call_at: Mapped[datetime | None] = mapped_column()
    evidence_attempt_id: Mapped[str | None] = mapped_column(String(36))


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
    # Electronic payer check (PAS $inquire), keyed by phase: {"pre_call": {...}, "post_call": {...}}
    # A phase whose value is {"state": "pending"} is picked up by the integration worker.
    electronic_check: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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
    # "call" (heard on a call, transcript evidence) or "system" (read from a payer system).
    source: Mapped[str | None] = mapped_column(String(10), default="call")
    integration_call_id: Mapped[str | None] = mapped_column(String(36))


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


class PayerConnection(IdMixin, Base):
    """A payer (or intermediary) endpoint for one workspace. Secrets are references only."""

    __tablename__ = "payer_connections"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="pas")  # pas (FHIR); x12 later
    base_url: Mapped[str] = mapped_column(String(300))
    ig_version: Mapped[str] = mapped_column(String(20), default="pas-stu2")
    auth_type: Mapped[str] = mapped_column(String(20), default="none")  # none|smart_backend
    # {"client_id", "token_url", "key_env", "kid", "scope"}; key_env names an env variable.
    auth_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Payer names this connection answers for (matched against the case's payer).
    payer_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column()
    capability: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class IntegrationCall(IdMixin, Base):
    """Audit of every external call. Holds outcomes and a request hash, never patient payloads."""

    __tablename__ = "integration_calls"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("payer_connections.id"), index=True)
    operation: Mapped[str] = mapped_column(String(40))  # metadata|pas.inquire|pas.submit
    phase: Mapped[str | None] = mapped_column(String(20))  # verify|pre_call|post_call|manual
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(20))  # success|not_found|error|timeout
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str | None] = mapped_column(String(80))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


# --- Practice layer (docs/solution/EMR_LAYER_SOLUTION_DESIGN.md §2) ------------------------------


class EhrRecord(IdMixin, Base):
    """Practice mirror: only the fields a playbook uses, keyed by the EHR source record.

    ``attrs`` holds identifiers, codes and timestamps; patient identifiers live only in ``pii``,
    sealed by :class:`careplix_api.phi.PhiBox`.
    """

    __tablename__ = "ehr_records"
    __table_args__ = (UniqueConstraint("connection_id", "resource", "source_id"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("payer_connections.id"), index=True)
    # location|provider|appointment_type|patient|insurance_plan|coverage|appointment
    resource: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(40))
    patient_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    starts_at: Mapped[datetime | None] = mapped_column(index=True)
    status: Mapped[str | None] = mapped_column(String(20))
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pii: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column()
    synced_at: Mapped[datetime] = mapped_column(default=utcnow)


class SyncCursor(IdMixin, Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("connection_id", "resource"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("payer_connections.id"))
    resource: Mapped[str] = mapped_column(String(30))
    high_water: Mapped[datetime | None] = mapped_column()  # newest source updated_at seen
    last_full_at: Mapped[datetime | None] = mapped_column()
    last_run_at: Mapped[datetime | None] = mapped_column()
    rows: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)  # cumulative, for the cost meter
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))


class WebhookEvent(IdMixin, Base):
    """A verified webhook delivery. Stores identifiers only; the record is re-read from source."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "delivery_key"),)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(20))  # nexhealth|stedi
    delivery_key: Mapped[str] = mapped_column(String(80))
    event_name: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column()
    outcome: Mapped[str | None] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(String(200))


class WriteIntent(IdMixin, Base):
    """Every EHR write: proposed → approved → sent → accepted → written (or needs review).

    A NexHealth 2xx only means *accepted*; the EHR has the record once an insertion webhook or a
    read-back shows its ``foreign_id`` (EMR design §2.4). NexHealth never retries a failed write.
    """

    __tablename__ = "write_intents"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("payer_connections.id"))
    kind: Mapped[str] = mapped_column(String(30))  # patient_alert | …
    level: Mapped[str] = mapped_column(String(4))  # L2 non-financial | L3 financial/payer-facing
    target_resource: Mapped[str] = mapped_column(String(30))
    target_source_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str | None] = mapped_column(Text)  # sealed: free text may carry PHI
    payload_hash: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(200))  # why, without patient details
    source: Mapped[str] = mapped_column(String(40))  # manual | eligibility | backfill | …
    # proposed|approved|sending|accepted|written|needs_review|rejected|failed
    state: Mapped[str] = mapped_column(String(16), default="proposed")
    confirmation: Mapped[str | None] = mapped_column(String(20))  # pending|ehr_written|not_reported
    requested_by: Mapped[str] = mapped_column(String(36))
    approved_by: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column()
    remote_id: Mapped[str | None] = mapped_column(String(40))
    foreign_id: Mapped[str | None] = mapped_column(String(80))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column()
    resolved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EligibilityCheck(IdMixin, Base):
    """One eligibility verification for an appointment (FO-1). Summary holds outcomes only."""

    __tablename__ = "eligibility_checks"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    ehr_connection_id: Mapped[str] = mapped_column(ForeignKey("payer_connections.id"))
    clearinghouse_connection_id: Mapped[str | None] = mapped_column(String(36))
    appointment_ref: Mapped[str] = mapped_column(String(40), index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    coverage_ref: Mapped[str | None] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(12))  # booking|t72|t24|manual
    state: Mapped[str] = mapped_column(String(10), default="pending")  # pending|done|failed|skipped
    mock: Mapped[bool] = mapped_column(Boolean, default=False)
    payer_id: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(200))
    stedi_ref: Mapped[str | None] = mapped_column(String(80))
    integration_call_id: Mapped[str | None] = mapped_column(String(36))
    reused_from: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column()
