"""Connections: set up LiveKit telephony and Gemini Live from the Studio, with evidence.

Every action here is administrator-only and audited. Provider secrets pass through to LiveKit
(carrier SIP credentials) or stay in the environment (LiveKit API secret, Google credentials); the
workspace keeps only selections and verification results.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import agents_service, calls_service, connections, templates
from careplix_api.auth import Principal, require
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, CallAttempt, Route, WorkerProbe, Workspace
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, gateway, runner
from careplix_common.gemini import credentials_from_json

router = APIRouter(prefix="/connections")
admin = require("admin")


def _audit(session: AsyncSession, p: Principal, action: str, target: str, detail: dict) -> None:
    session.add(
        AuditEvent(
            workspace_id=p.workspace_id,
            actor=p.user_id,
            action=action,
            target=target,
            detail=detail,
        )
    )


async def _workspace(session: AsyncSession, p: Principal) -> Workspace:
    return await session.get(Workspace, p.workspace_id)


# --- Overview ------------------------------------------------------------------------------------


@router.get("")
async def overview(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
):
    ws = await _workspace(session, principal)
    eff = connections.effective(ws, settings)
    livekit: dict[str, Any] = {
        "configured": gw.configured,
        "project": settings.livekit_url.split("://", 1)[-1] if gw.configured else None,
        "agent_name": settings.livekit_agent_name,
    }
    if gw.configured:
        try:
            livekit["inventory"] = await gw.inventory()
        except Exception as exc:  # noqa: BLE001 - shown as a connection error
            livekit["error"] = type(exc).__name__
    probe = await session.scalar(
        select(WorkerProbe)
        .where(WorkerProbe.workspace_id == principal.workspace_id)
        .order_by(WorkerProbe.created_at.desc())
        .limit(1)
    )
    return {
        "livekit": livekit,
        "outbound": {
            "trunk_id": eff.outbound_trunk_id or None,
            "trunk_from_env": bool(settings.livekit_outbound_trunk_id and not ws.outbound_trunk_id),
            "verified": eff.outbound_verified,
            "verified_at": ws.outbound_verified_at,
            "evidence_attempt_id": ws.outbound_evidence_attempt_id,
            "approved_destinations": sorted(ws.approved_destinations or []),
            "env_destinations": sorted(settings.approved_destinations),
        },
        "model": {
            "approved_model": eff.approved_model or None,
            "gcp_project": eff.gcp_project or None,
            "vertex_location": eff.vertex_location or None,
            "known_models": connections.KNOWN_LIVE_MODELS,
            "check": ws.model_check,
            "verified": eff.model_verified,
            "credential_source": "service_account_secret"
            if settings.google_service_account_json.get_secret_value().strip()
            else "application_default",
        },
        "worker_probe": _probe_view(probe) if probe else None,
    }


# --- Agent worker probe --------------------------------------------------------------------------


def _probe_view(p: WorkerProbe) -> dict[str, Any]:
    status = "answered" if p.acked_at else "failed" if p.error else "waiting"
    if status == "waiting" and (utcnow() - as_utc(p.created_at)).total_seconds() > 30:
        status = "no_worker"
    return {
        "id": p.id,
        "status": status,
        "created_at": p.created_at,
        "acked_at": p.acked_at,
        "runtime_build": p.runtime_build,
        "error": p.error,
        "latency_ms": round((as_utc(p.acked_at) - as_utc(p.created_at)).total_seconds() * 1000)
        if p.acked_at
        else None,
    }


@router.post("/agent-test")
async def agent_test(
    principal: Principal = Depends(admin), session: AsyncSession = Session, gw=Depends(gateway)
):
    if not gw.configured:
        raise ApiError(503, "voice_not_configured", "Connect LiveKit first.")
    probe = WorkerProbe(
        workspace_id=principal.workspace_id, room_name=f"probe_{secrets.token_hex(6)}"
    )
    session.add(probe)
    await session.commit()
    try:
        await gw.dispatch_probe(probe.room_name, probe.id)
    except Exception as exc:
        probe.error = f"dispatch_failed:{type(exc).__name__}"
        await session.commit()
        raise ApiError(503, "dispatch_failed", "LiveKit did not accept the test dispatch.") from exc
    return _probe_view(probe)


@router.get("/agent-test/{probe_id}")
async def agent_test_status(
    probe_id: str, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    probe = await session.get(WorkerProbe, probe_id)
    if probe is None or probe.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Probe not found.")
    return _probe_view(probe)


# --- Inbound numbers -----------------------------------------------------------------------------


class InboundIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    number: str
    agent_id: str
    provider: Literal["livekit_number", "carrier"] = "livekit_number"


@router.post("/inbound")
async def connect_inbound(
    body: InboundIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    if not gw.configured:
        raise ApiError(503, "voice_not_configured", "Connect LiveKit first.")
    agent = await agents_service.get_agent(session, principal, body.agent_id)
    if templates.get_template(agent.template_id)["direction"] != "inbound":
        raise ApiError(422, "agent_not_inbound", "Only inbound agents can answer a number.")
    number = calls_service.normalize_e164(body.number)
    if await session.scalar(select(Route).where(Route.number_e164 == number)):
        raise ApiError(409, "route_exists", "That number is already connected.")

    route = Route(
        workspace_id=principal.workspace_id,
        name=body.name,
        provider=body.provider,
        direction="inbound",
        number_e164=number,
        agent_id=agent.id,
        # LiveKit-purchased numbers are inbound-only and cannot forward (L1). Nothing is verified
        # until a real call qualifies the route.
        capabilities={
            "inbound": True,
            "outbound": False,
            "dtmf": True,
            "recording": False,
            "transfer": False,
        },
    )
    session.add(route)
    await session.flush()

    # Reuse a rule that already sends this number (or every number in the project) to our agent;
    # a second project-wide rule would conflict with the first.
    inventory = await gw.inventory()
    ours = [r for r in inventory["dispatch_rules"] if gw.settings.livekit_agent_name in r["agents"]]
    existing = next((r for r in ours if number in r["numbers"]), None) or next(
        (r for r in ours if not r["numbers"] and not r["trunk_ids"]), None
    )
    try:
        rule = existing or await gw.create_inbound_rule(f"CarePlix · {body.name}", number, route.id)
    except Exception as exc:
        await session.rollback()
        raise ApiError(
            502,
            "livekit_rejected",
            "LiveKit did not accept the dispatch rule. Check the number belongs to this project.",
        ) from exc
    rule_id = rule["id"]
    scope = "number" if number in rule["numbers"] else "project"
    route.provider_refs = {"dispatch_rule_id": rule_id, "reused": bool(existing), "scope": scope}
    _audit(
        session,
        principal,
        "route.connected",
        number[-4:],
        {"agent_id": agent.id, "dispatch_rule_id": rule_id},
    )
    await session.commit()
    return {
        "id": route.id,
        "number": number,
        "dispatch_rule_id": rule_id,
        "reused": bool(existing),
        "scope": scope,
    }


class QualifyIn(BaseModel):
    attempt_id: str | None = None


@router.post("/inbound/{route_id}/qualify")
async def qualify_inbound(
    route_id: str,
    body: QualifyIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
):
    route = await session.get(Route, route_id)
    if route is None or route.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Route not found.")
    query = select(CallAttempt).where(
        CallAttempt.route_id == route.id,
        CallAttempt.state == "Ended",
        CallAttempt.connected_at.is_not(None),
    )
    if body.attempt_id:
        query = query.where(CallAttempt.id == body.attempt_id)
    attempt = await session.scalar(query.order_by(CallAttempt.created_at.desc()).limit(1))
    if attempt is None:
        raise ApiError(
            409,
            "no_evidence",
            "Call the number first. A completed inbound call is required as evidence.",
        )
    route.verified_at, route.evidence_attempt_id = utcnow(), attempt.id
    _audit(
        session, principal, "route.qualified", route.number_e164[-4:], {"attempt_id": attempt.id}
    )
    await session.commit()
    return {"verified_at": route.verified_at, "evidence_attempt_id": attempt.id}


# --- Outbound calling ----------------------------------------------------------------------------


class TrunkIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    address: str = Field(
        min_length=3, max_length=200, description="Carrier SIP domain, e.g. example.pstn.twilio.com"
    )
    number: str
    username: str = Field(default="", max_length=120)
    password: str = Field(default="", max_length=200)
    transport: Literal["auto", "udp", "tcp", "tls"] = "auto"


@router.post("/outbound/trunks")
async def add_trunk(
    body: TrunkIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    if not gw.configured:
        raise ApiError(503, "voice_not_configured", "Connect LiveKit first.")
    number = calls_service.normalize_e164(body.number)
    try:
        trunk_id = await gw.create_outbound_trunk(
            body.name, body.address.strip(), number, body.username, body.password, body.transport
        )
    except Exception as exc:
        raise ApiError(
            502, "livekit_rejected", "LiveKit did not accept the trunk. Check the SIP address."
        ) from exc
    ws = await _workspace(session, principal)
    ws.outbound_trunk_id, ws.outbound_verified_at, ws.outbound_evidence_attempt_id = (
        trunk_id,
        None,
        None,
    )
    _audit(
        session, principal, "outbound.trunk_created", trunk_id, {"address": body.address.strip()}
    )
    await session.commit()
    return {"trunk_id": trunk_id}


class OutboundIn(BaseModel):
    trunk_id: str | None = None
    approved_destinations: list[str] | None = Field(default=None, max_length=20)


@router.put("/outbound")
async def update_outbound(
    body: OutboundIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    ws = await _workspace(session, principal)
    if body.trunk_id is not None:
        if body.trunk_id and gw.configured:
            known = {t["id"] for t in (await gw.inventory())["outbound_trunks"]}
            if body.trunk_id not in known:
                raise ApiError(422, "unknown_trunk", "That trunk is not in the LiveKit project.")
        if body.trunk_id != ws.outbound_trunk_id:
            ws.outbound_trunk_id = body.trunk_id or None
            ws.outbound_verified_at = ws.outbound_evidence_attempt_id = None
        _audit(session, principal, "outbound.trunk_selected", body.trunk_id or "-", {})
    if body.approved_destinations is not None:
        numbers = sorted({calls_service.normalize_e164(n) for n in body.approved_destinations})
        ws.approved_destinations = numbers
        _audit(session, principal, "outbound.destinations_changed", "-", {"count": len(numbers)})
    await session.commit()
    return {"ok": True}


class TestCallIn(BaseModel):
    agent_id: str
    destination: str


@router.post("/outbound/test-call")
async def test_call(
    body: TestCallIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
    run=Depends(runner),
):
    agent = await agents_service.get_agent(session, principal, body.agent_id)
    attempt = await calls_service.start_test_call(
        session, principal, settings, gw, run, agent, body.destination
    )
    return {"attempt_id": attempt.id}


@router.post("/outbound/qualify")
async def qualify_outbound(
    body: QualifyIn, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    ws = await _workspace(session, principal)
    attempt = await session.get(CallAttempt, body.attempt_id or "")
    if (
        attempt is None
        or attempt.workspace_id != principal.workspace_id
        or attempt.direction != "outbound"
        or attempt.connected_at is None
        or attempt.state != "Ended"
        or attempt.pins.get("outbound_trunk_id") != ws.outbound_trunk_id
    ):
        raise ApiError(
            409, "no_evidence", "Use a completed, connected test call on the selected trunk."
        )
    ws.outbound_verified_at, ws.outbound_evidence_attempt_id = utcnow(), attempt.id
    _audit(
        session,
        principal,
        "outbound.qualified",
        ws.outbound_trunk_id or "-",
        {"attempt_id": attempt.id},
    )
    await session.commit()
    return {"verified_at": ws.outbound_verified_at}


# --- Gemini Live ---------------------------------------------------------------------------------


class ModelIn(BaseModel):
    model_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9.\-]+$")
    gcp_project: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9\-]+$")
    vertex_location: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9\-]+$")


@router.put("/model")
async def set_model(
    body: ModelIn, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    ws = await _workspace(session, principal)
    ws.approved_model, ws.gcp_project, ws.vertex_location = (
        body.model_id,
        body.gcp_project,
        body.vertex_location,
    )
    _audit(session, principal, "model.approved", body.model_id, {"location": body.vertex_location})
    await session.commit()
    return {"ok": True}


@router.post("/model/test")
async def test_model(
    request: Request,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Open a real Gemini Live session from the API's credentials and time the first audio.

    This proves model access from the control plane. The hosted worker's own credential path is
    proven separately by a Browser Test.
    """
    eff = await connections.load(session, principal.workspace_id, settings)
    if not (eff.approved_model and eff.gcp_project and eff.vertex_location):
        raise ApiError(422, "model_not_configured", "Choose a project, region and model first.")
    try:
        credentials = credentials_from_json(settings.google_service_account_json.get_secret_value())
    except ValueError as exc:
        raise ApiError(422, "bad_credentials", str(exc)) from exc
    result = await request.app.state.model_prober(
        eff.gcp_project, eff.vertex_location, eff.approved_model, credentials
    )
    ws = await _workspace(session, principal)
    ws.model_check = result.as_dict()
    _audit(
        session,
        principal,
        "model.tested",
        eff.approved_model,
        {"ok": result.ok, "error": result.error},
    )
    await session.commit()
    return ws.model_check
