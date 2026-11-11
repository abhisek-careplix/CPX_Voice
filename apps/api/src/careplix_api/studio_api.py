"""Studio and operator API (doc 06 API surface; BUILD_PLAN §5.4)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_contracts as contracts
from careplix_api import agents_service, calls_service, connections, readiness, templates
from careplix_api.auth import SESSION_COOKIE, SESSION_TTL, Principal, issue_session, require
from careplix_api.calls_service import ACTIVE_STATES, TERMINAL_TASK_STATES, mask_number
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import (
    Agent,
    AgentVersion,
    AuditEvent,
    CallAttempt,
    CallEvent,
    Case,
    Finding,
    FindingRevision,
    Route,
    Task,
    ToolExecution,
    TranscriptSegment,
    Workspace,
)
from careplix_api.phi import PhiBox
from careplix_api.seed import demo_principal
from careplix_api.settings import Settings, get_settings

router = APIRouter()


async def get_session(request: Request):
    async with request.app.state.db.sessions() as session:
        yield session


def gateway(request: Request):
    return request.app.state.gateway


def runner(request: Request):
    return request.app.state.runner


Session = Depends(get_session)
viewer = require("view")


# --- Session -------------------------------------------------------------------------------------


@router.post("/auth/dev-login")
async def dev_login(
    response: Response, session: AsyncSession = Session, settings: Settings = Depends(get_settings)
):
    if not settings.is_dev:
        raise ApiError(404, "not_found", "Not found.")
    principal = await demo_principal(session)
    if principal is None:
        raise ApiError(503, "not_seeded", "The demo workspace is not initialised.")
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(settings, principal),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=not settings.is_dev,
    )
    return _me(principal)


@router.get("/auth/mode")
async def auth_mode(settings: Settings = Depends(get_settings)):
    return {"mode": "dev" if settings.is_dev else "sso"}


@router.post("/auth/iap-login")
async def iap_login(
    request: Request,
    response: Response,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Exchange the IAP-verified Google identity for a Studio session (cloud deployments)."""
    from careplix_api.auth import verify_iap_assertion
    from careplix_api.models import Membership, User

    email = verify_iap_assertion(request, settings)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name=email.split("@")[0].replace(".", " ").title())
        session.add(user)
        await session.flush()
    membership = await session.scalar(select(Membership).where(Membership.user_id == user.id))
    if membership is None:
        workspace = await session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
        if workspace is None:
            raise ApiError(503, "not_seeded", "No workspace exists yet.")
        membership = Membership(
            user_id=user.id, workspace_id=workspace.id, role=settings.iap_default_role
        )
        session.add(membership)
        session.add(
            AuditEvent(
                workspace_id=workspace.id,
                actor=user.id,
                action="user.provisioned",
                target=user.id,
                detail={"role": membership.role},
            )
        )
    await session.commit()
    principal = Principal(user.id, membership.workspace_id, membership.role, user.display_name)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(settings, principal),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=True,
    )
    return _me(principal)


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


def _me(p: Principal) -> dict[str, Any]:
    from careplix_api.auth import PERMISSIONS

    return {
        "user_id": p.user_id,
        "display_name": p.display_name,
        "role": p.role,
        "workspace_id": p.workspace_id,
        "permissions": sorted(k for k, roles in PERMISSIONS.items() if p.role in roles),
    }


@router.get("/me")
async def me(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    workspace = await session.get(Workspace, principal.workspace_id)
    return {
        **_me(principal),
        "workspace_name": workspace.name,
        "emergency_stop": workspace.emergency_stop,
    }


# --- Workspace & readiness -----------------------------------------------------------------------


@router.get("/readiness")
async def get_readiness(
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
):
    result = await readiness.run_all(settings, session, principal.workspace_id, gw)
    return result.as_dict()


@router.get("/workspace/summary")
async def workspace_summary(
    principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    ws = principal.workspace_id
    active = list(
        await session.scalars(
            select(CallAttempt)
            .where(CallAttempt.workspace_id == ws, CallAttempt.state.in_(ACTIVE_STATES))
            .order_by(CallAttempt.created_at.desc())
        )
    )
    review = list(
        await session.scalars(
            select(Task)
            .where(Task.workspace_id == ws, Task.state == "Review")
            .order_by(Task.updated_at.desc())
            .limit(10)
        )
    )
    recent = list(
        await session.scalars(
            select(Task)
            .where(Task.workspace_id == ws, Task.state.in_(TERMINAL_TASK_STATES))
            .order_by(Task.updated_at.desc())
            .limit(8)
        )
    )
    agents = {a.id: a for a in await session.scalars(select(Agent).where(Agent.workspace_id == ws))}
    return {
        "active_calls": [await _attempt_view(session, a, agents) for a in active],
        "needs_attention": [await _task_view(session, t, agents) for t in review],
        "recent_outcomes": [await _task_view(session, t, agents) for t in recent],
        "agents": {
            "total": len(agents),
            "published": sum(1 for a in agents.values() if a.current_version_id),
        },
    }


@router.post("/workspace/emergency-stop")
async def emergency_stop(
    body: dict,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    workspace = await session.get(Workspace, principal.workspace_id)
    workspace.emergency_stop = bool(body.get("enabled", True))
    session.add(
        AuditEvent(
            workspace_id=workspace.id,
            actor=principal.user_id,
            action="workspace.emergency_stop",
            target=workspace.id,
            detail={"enabled": workspace.emergency_stop},
        )
    )
    await session.commit()
    ended = 0
    if workspace.emergency_stop:
        for attempt in list(
            await session.scalars(
                select(CallAttempt).where(
                    CallAttempt.workspace_id == workspace.id, CallAttempt.state.in_(ACTIVE_STATES)
                )
            )
        ):
            await calls_service.end_by_operator(session, principal, gw, attempt)
            ended += 1
    return {"emergency_stop": workspace.emergency_stop, "calls_ended": ended}


# --- Catalog -------------------------------------------------------------------------------------


@router.get("/templates")
async def list_templates(principal: Principal = Depends(viewer)):
    return [
        {
            k: t[k]
            for k in (
                "id",
                "version",
                "name",
                "description",
                "direction",
                "objective",
                "output_fields",
                "disclose_allowlist",
                "required_context",
            )
        }
        | {"tools": [x["name"] for x in t["default_config"]["tools"]]}
        for t in templates.all_templates().values()
    ]


@router.get("/tools")
async def list_tools(principal: Principal = Depends(viewer)):
    return list(contracts.tool_registry().values())


@router.get("/cases")
async def list_cases(
    q: str = "", principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    cases = await session.scalars(
        select(Case).where(Case.workspace_id == principal.workspace_id).order_by(Case.case_ref)
    )
    needle = q.strip().lower()
    out = []
    for c in cases:
        ctx = templates.case_context(c.data)
        fields = (c.case_ref, ctx["patient_name"], ctx["service_description"], ctx["payer_name"])
        label = " ".join(fields).lower()
        if needle and needle not in label:
            continue
        out.append({"id": c.id, "case_ref": c.case_ref, "synthetic": c.synthetic, "context": ctx})
    return out


@router.get("/routes")
async def list_routes(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    routes = await session.scalars(
        select(Route).where(Route.workspace_id == principal.workspace_id)
    )
    return [_route_view(r) for r in routes]


def _route_view(r: Route) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "provider": r.provider,
        "direction": r.direction,
        "number": r.number_e164,
        "agent_id": r.agent_id,
        "capabilities": r.capabilities,
        "verified_at": r.verified_at,
        "provider_refs": r.provider_refs or {},
        "last_call_at": r.last_call_at,
    }


# --- Agents --------------------------------------------------------------------------------------


async def _agent_view(
    session: AsyncSession, agent: Agent, settings: Settings, full: bool = False
) -> dict[str, Any]:
    vs = await agents_service.versions(session, agent)
    eff = await connections.load(session, agent.workspace_id, settings)
    draft = next((v for v in vs if v.state == "draft"), None)
    current = next((v for v in vs if v.id == agent.current_version_id), None)
    template = templates.get_template(agent.template_id)
    view = {
        "id": agent.id,
        "name": agent.name,
        "template": {
            "id": template["id"],
            "name": template["name"],
            "direction": template["direction"],
        },
        "draft": _version_view(draft) if draft else None,
        "current": _version_view(current) if current else None,
        "updated_at": max((as_utc(v.updated_at) for v in vs), default=None),
    }
    if full:
        view["versions"] = [_version_view(v) for v in vs if v.state != "draft"]
        view["draft_config"] = (draft or current).config if (draft or current) else None
        view["draft_issues"] = (
            agents_service.validation_issues(draft.config, settings, eff.approved_model)
            if draft
            else []
        )
        view["approved_model"] = eff.approved_model or None
    return view


def _version_view(v: AgentVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "number": v.number,
        "state": v.state,
        "config_hash": v.config_hash,
        "change_note": v.change_note,
        "updated_at": v.updated_at,
        "published_at": v.published_at,
        "policy_version": v.policy_version,
        "suite": agents_service.suite_result(v),
    }


@router.get("/agents")
async def list_agents(
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agents = await session.scalars(
        select(Agent).where(Agent.workspace_id == principal.workspace_id).order_by(Agent.created_at)
    )
    return [await _agent_view(session, a, settings) for a in agents]


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    template_id: str


@router.post("/agents")
async def create_agent(
    body: AgentIn,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.create_agent(
        session, principal, settings, body.name, body.template_id
    )
    await session.commit()
    return await _agent_view(session, agent, settings, full=True)


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    return await _agent_view(session, agent, settings, full=True)


@router.put("/agents/{agent_id}/draft")
async def save_draft(
    agent_id: str,
    body: dict,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    await agents_service.save_draft(session, principal, agent, body.get("config") or {})
    await session.commit()
    return await _agent_view(session, agent, settings, full=True)


@router.get("/agents/{agent_id}/publish-preview")
async def publish_preview(
    agent_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    return await agents_service.publish_preview(session, agent, settings)


class PublishIn(BaseModel):
    change_note: str = Field(default="", max_length=500)
    # Admins only: publish an outbound agent without a passed regression suite (audited).
    override_reason: str = Field(default="", max_length=500)


@router.post("/agents/{agent_id}/publish")
async def publish(
    agent_id: str,
    body: PublishIn,
    principal: Principal = Depends(require("publish")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    await agents_service.publish(
        session, principal, agent, settings, body.change_note, override_reason=body.override_reason
    )
    await session.commit()
    return await _agent_view(session, agent, settings, full=True)


class RollbackIn(BaseModel):
    version_id: str


@router.post("/agents/{agent_id}/rollback")
async def rollback(
    agent_id: str,
    body: RollbackIn,
    principal: Principal = Depends(require("publish")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    await agents_service.rollback(session, principal, agent, body.version_id)
    await session.commit()
    return await _agent_view(session, agent, settings, full=True)


class BrowserSessionIn(BaseModel):
    agent_id: str
    use: Literal["draft", "published"] = "draft"
    case_id: str | None = None


@router.post("/browser-sessions")
async def browser_session(
    body: BrowserSessionIn,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
):
    agent = await agents_service.get_agent(session, principal, body.agent_id)
    return await calls_service.start_browser_session(
        session, principal, settings, gw, agent, body.use, body.case_id
    )


# --- Tasks & calls -------------------------------------------------------------------------------


class TaskIn(BaseModel):
    agent_id: str
    case_id: str
    destination: str
    additional_instructions: str | None = None


@router.post("/tasks")
async def create_task(
    body: TaskIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    task = await calls_service.create_task(
        session,
        principal,
        settings,
        body.agent_id,
        body.case_id,
        body.destination,
        body.additional_instructions,
    )
    await session.commit()
    return await _task_view(session, task)


@router.get("/tasks")
async def list_tasks(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    tasks = await session.scalars(
        select(Task)
        .where(Task.workspace_id == principal.workspace_id)
        .order_by(Task.created_at.desc())
        .limit(100)
    )
    agents = {
        a.id: a
        for a in await session.scalars(
            select(Agent).where(Agent.workspace_id == principal.workspace_id)
        )
    }
    return [await _task_view(session, t, agents) for t in tasks]


@router.post("/tasks/{task_id}/calls")
async def start_call(
    task_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
    run=Depends(runner),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    task = await _task(session, principal, task_id)
    attempt = await calls_service.start_call(
        session, principal, settings, gw, run, task, idempotency_key
    )
    return await _attempt_view(session, attempt)


async def _task(session: AsyncSession, principal: Principal, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Task not found.")
    return task


async def _task_view(
    session: AsyncSession, t: Task, agents: dict[str, Agent] | None = None
) -> dict[str, Any]:
    agent = (agents or {}).get(t.agent_id) or await session.get(Agent, t.agent_id)
    case = await session.get(Case, t.case_id) if t.case_id else None
    version = await session.get(AgentVersion, t.agent_version_id)
    return {
        "id": t.id,
        "kind": t.kind,
        "state": t.state,
        "objective": t.objective,
        "additional_instructions": t.additional_instructions,
        "destination": t.destination_e164,
        "agent": {"id": agent.id, "name": agent.name, "version": version.number},
        "case": {"id": case.id, "case_ref": case.case_ref, "synthetic": case.synthetic}
        if case
        else None,
        "outcome_reason": t.outcome_reason,
        "next_action": t.next_action,
        "electronic_check": t.electronic_check,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


async def _attempt(session: AsyncSession, principal: Principal, attempt_id: str) -> CallAttempt:
    attempt = await session.get(CallAttempt, attempt_id)
    if attempt is None or attempt.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Call not found.")
    return attempt


async def _attempt_view(
    session: AsyncSession, a: CallAttempt, agents: dict[str, Agent] | None = None
) -> dict[str, Any]:
    agent = (agents or {}).get(a.agent_id) or await session.get(Agent, a.agent_id)
    case = await session.get(Case, a.case_id) if a.case_id else None
    return {
        "id": a.id,
        "task_id": a.task_id,
        "direction": a.direction,
        "sandbox": a.sandbox,
        "state": a.state,
        "substate": a.substate,
        "disposition": a.disposition,
        "end_reason": a.end_reason,
        "destination": mask_number(a.destination_e164),
        "caller_reported": mask_number(a.caller_e164),
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "version": a.pins.get("agent_version"),
            "version_state": a.pins.get("version_state"),
        },
        "case_ref": case.case_ref if case else None,
        "pins": {
            k: a.pins.get(k) for k in ("model_id", "policy_version", "runtime_build", "config_hash")
        },
        "created_at": a.created_at,
        "connected_at": a.connected_at,
        "ended_at": a.ended_at,
    }


@router.get("/calls")
async def list_calls(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    attempts = await session.scalars(
        select(CallAttempt)
        .where(CallAttempt.workspace_id == principal.workspace_id)
        .order_by(CallAttempt.created_at.desc())
        .limit(100)
    )
    agents = {
        a.id: a
        for a in await session.scalars(
            select(Agent).where(Agent.workspace_id == principal.workspace_id)
        )
    }
    return [await _attempt_view(session, a, agents) for a in attempts]


async def _told(
    session: AsyncSession, principal: Principal, settings: Settings, a: CallAttempt
) -> dict[str, Any]:
    """What this run's agent was told: persona at its pinned version, who it acted for, its
    objective and the facts it could share (values only for people who may edit agents)."""
    version = await session.get(AgentVersion, a.agent_version_id)
    identity = version.config["identity"] if version else {}
    brief = PhiBox(settings).open(a.brief)
    context = brief.get("context") or {}
    return {
        "recorded": bool(brief),
        "persona": {"name": identity.get("name"), "greeting": identity.get("greeting")},
        "acting_for": (brief.get("authority") or {}).get("on_behalf_of"),
        "objective": brief.get("objective"),
        "scenario_id": a.scenario_id,
        "facts": context if principal.can("edit_draft") else dict.fromkeys(context, "•••"),
    }


@router.get("/calls/{attempt_id}")
async def get_call(
    attempt_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    attempt = await _attempt(session, principal, attempt_id)
    return {
        **await _attempt_view(session, attempt),
        "told": await _told(session, principal, settings, attempt),
        "findings": await _findings(session, Finding.attempt_id == attempt.id),
        "transcript": await _transcript(session, [attempt.id]),
        "tools": [
            {
                "tool": t.tool,
                "state": t.state,
                "rejection_code": t.rejection_code,
                "at": t.created_at,
            }
            for t in await session.scalars(
                select(ToolExecution)
                .where(ToolExecution.attempt_id == attempt.id)
                .order_by(ToolExecution.created_at)
            )
        ],
    }


@router.post("/calls/{attempt_id}/end")
async def end_call(
    attempt_id: str,
    principal: Principal = Depends(require("stop_calls")),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    attempt = await _attempt(session, principal, attempt_id)
    await calls_service.end_by_operator(session, principal, gw, attempt)
    return await _attempt_view(session, attempt)


@router.get("/calls/{attempt_id}/events")
async def call_events(
    attempt_id: str,
    request: Request,
    cursor: int = 0,
    principal: Principal = Depends(viewer),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Server-sent events, resumable from a cursor after refresh (FR08, T11)."""
    async with request.app.state.db.sessions() as session:
        await _attempt(session, principal, attempt_id)
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else cursor

    async def stream():
        position, idle = start, 0.0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            async with request.app.state.db.sessions() as session:
                rows = list(
                    await session.scalars(
                        select(CallEvent)
                        .where(CallEvent.attempt_id == attempt_id, CallEvent.seq > position)
                        .order_by(CallEvent.seq)
                        .limit(200)
                    )
                )
                segments = {}
                seg_ids = [
                    r.payload.get("segment_id") for r in rows if r.type == "transcript.segment"
                ]
                if seg_ids:
                    segments = {
                        s.id: s
                        for s in await session.scalars(
                            select(TranscriptSegment).where(TranscriptSegment.id.in_(seg_ids))
                        )
                    }
                attempt = await session.get(CallAttempt, attempt_id)
            for r in rows:
                data = {
                    "seq": r.seq,
                    "type": r.type,
                    "source": r.source,
                    "occurred_at": as_utc(r.occurred_at).isoformat(),
                    "payload": r.payload,
                }
                if r.type == "transcript.segment" and (
                    seg := segments.get(r.payload.get("segment_id"))
                ):
                    data["payload"] = {**r.payload, "text": seg.text}
                position = r.seq
                yield f"id: {r.seq}\nevent: call\ndata: {json.dumps(data, default=str)}\n\n"
            if rows:
                idle = 0.0
            elif attempt.state not in ACTIVE_STATES:
                yield "event: end\ndata: {}\n\n"
                return
            else:
                idle += 0.4
                if idle >= 15:
                    idle = 0.0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- Results -------------------------------------------------------------------------------------


async def _findings(session: AsyncSession, where) -> list[dict[str, Any]]:
    rows = list(await session.scalars(select(Finding).where(where).order_by(Finding.created_at)))
    revisions: dict[str, list] = {}
    if rows:
        for rev in await session.scalars(
            select(FindingRevision)
            .where(FindingRevision.finding_id.in_([f.id for f in rows]))
            .order_by(FindingRevision.created_at)
        ):
            revisions.setdefault(rev.finding_id, []).append(
                {
                    "prior_value": rev.prior_value,
                    "new_value": rev.new_value,
                    "prior_status": rev.prior_status,
                    "new_status": rev.new_status,
                    "reason": rev.reason,
                    "actor": rev.actor,
                    "at": rev.created_at,
                }
            )
    return [
        {
            "id": f.id,
            "attempt_id": f.attempt_id,
            "field": f.field,
            "value": f.value,
            "status": f.status,
            "evidence_segment_ids": f.evidence_segment_ids,
            "original_phrasing": f.original_phrasing,
            "null_reason": f.null_reason,
            "source": f.source or "call",
            "integration_call_id": f.integration_call_id,
            "created_at": f.created_at,
            "revisions": revisions.get(f.id, []),
        }
        for f in rows
    ]


async def _transcript(session: AsyncSession, attempt_ids: list[str]) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.attempt_id.in_(attempt_ids))
        .order_by(TranscriptSegment.occurred_at)
    )
    return [
        {
            "id": s.id,
            "attempt_id": s.attempt_id,
            "speaker": s.speaker,
            "text": s.text,
            "playout": s.playout,
            "at": s.occurred_at,
        }
        for s in rows
    ]


@router.get("/tasks/{task_id}/result")
async def task_result(
    task_id: str, principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    task = await _task(session, principal, task_id)
    attempts = list(
        await session.scalars(
            select(CallAttempt)
            .where(CallAttempt.task_id == task.id)
            .order_by(CallAttempt.created_at)
        )
    )
    template = templates.get_template((await session.get(Agent, task.agent_id)).template_id)
    return {
        "task": await _task_view(session, task),
        "attempts": [await _attempt_view(session, a) for a in attempts],
        "findings": await _findings(session, Finding.task_id == task.id),
        "transcript": await _transcript(session, [a.id for a in attempts]),
        "expected_fields": template["output_fields"],
        "critical_fields": template["critical_fields"],
    }


class RevisionIn(BaseModel):
    value: str | None
    status: Literal["reviewed", "conflicting"] = "reviewed"
    reason: str = Field(min_length=3, max_length=500)


@router.post("/findings/{finding_id}/revisions")
async def revise_finding(
    finding_id: str,
    body: RevisionIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
):
    finding = await session.get(Finding, finding_id)
    if finding is None or finding.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Finding not found.")
    session.add(
        FindingRevision(
            finding_id=finding.id,
            prior_value=finding.value,
            new_value=body.value,
            prior_status=finding.status,
            new_status=body.status,
            reason=body.reason,
            actor=principal.display_name,
        )
    )
    finding.value, finding.status = body.value, body.status
    await session.commit()
    return (await _findings(session, Finding.id == finding.id))[0]


class OutcomeIn(BaseModel):
    outcome: Literal["Completed", "Partial", "Unsuccessful", "Cancelled"]
    reason: str = Field(min_length=3, max_length=1000)
    next_action: str | None = Field(default=None, max_length=500)


@router.post("/tasks/{task_id}/outcome")
async def record_outcome(
    task_id: str,
    body: OutcomeIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
):
    task = await _task(session, principal, task_id)
    if task.state in ("Running",):
        raise ApiError(
            409, "task_running", "Wait for the call to finish before recording an outcome."
        )
    task.state, task.outcome_reason, task.outcome_by = body.outcome, body.reason, principal.user_id
    task.next_action = body.next_action
    task.updated_at = utcnow()
    session.add(
        AuditEvent(
            workspace_id=task.workspace_id,
            actor=principal.user_id,
            action="task.outcome",
            target=task.id,
            detail={"outcome": body.outcome},
        )
    )
    await session.commit()
    return await _task_view(session, task)
