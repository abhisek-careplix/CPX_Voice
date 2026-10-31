"""Worker-only API (ADR09): service credential for claims, attempt-scoped token for the rest."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from careplix_api import calls_service, tools_service
from careplix_api.auth import AttemptGrant, require_attempt_token, require_worker_service
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import CallAttempt, WorkerProbe
from careplix_api.settings import Settings, get_settings

router = APIRouter(prefix="/internal")


class ClaimIn(BaseModel):
    room: str = Field(max_length=64)
    job_id: str = Field(max_length=80)
    runtime_build: str = Field(max_length=80)


class InboundClaimIn(ClaimIn):
    # All from LiveKit (SIP attributes, our dispatch rule's metadata), never from the caller.
    called_number: str = Field(default="", max_length=32)
    caller_number: str | None = Field(default=None, max_length=32)
    route_id: str | None = Field(default=None, max_length=36)
    rule_id: str | None = Field(default=None, max_length=64)


@router.post("/attempts/{attempt_id}/claim", dependencies=[Depends(require_worker_service)])
async def claim(
    attempt_id: str, body: ClaimIn, request: Request, settings: Settings = Depends(get_settings)
):
    async with request.app.state.db.sessions() as session:
        return await calls_service.claim(
            session, settings, attempt_id, body.room, body.job_id, body.runtime_build
        )


@router.post("/inbound/claim", dependencies=[Depends(require_worker_service)])
async def inbound_claim(
    body: InboundClaimIn, request: Request, settings: Settings = Depends(get_settings)
):
    async with request.app.state.db.sessions() as session:
        return await calls_service.inbound_claim(
            session,
            settings,
            body.room,
            body.job_id,
            body.runtime_build,
            body.called_number,
            body.caller_number,
            body.route_id,
            body.rule_id,
        )


async def _attempt(session, grant: AttemptGrant) -> CallAttempt:
    attempt = await session.get(CallAttempt, grant.attempt_id)
    if attempt is None or attempt.workspace_id != grant.workspace_id:
        raise ApiError(404, "attempt_not_found", "Attempt not found.")
    return attempt


class EventsIn(BaseModel):
    events: list[dict[str, Any]] = Field(max_length=200)


@router.post("/attempts/{attempt_id}/events")
async def post_events(
    body: EventsIn, request: Request, grant: AttemptGrant = Depends(require_attempt_token)
):
    async with request.app.state.db.sessions() as session:
        attempt = await _attempt(session, grant)
        accepted = await calls_service.ingest_worker_events(session, attempt, body.events)
        await session.commit()
        return {"accepted": accepted, "state": attempt.state}


class ToolIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=20)


@router.post("/attempts/{attempt_id}/tools/{name}")
async def run_tool(
    name: str, body: ToolIn, request: Request, grant: AttemptGrant = Depends(require_attempt_token)
):
    async with request.app.state.db.sessions() as session:
        attempt = await _attempt(session, grant)
        result = await tools_service.execute(
            session, attempt, name, body.call_id, body.arguments, body.evidence_segment_ids
        )
        await session.commit()
        return result


class ProbeAckIn(BaseModel):
    runtime_build: str = Field(max_length=80)
    room: str = Field(max_length=64)


@router.post("/probes/{probe_id}/ack", dependencies=[Depends(require_worker_service)])
async def ack_probe(probe_id: str, body: ProbeAckIn, request: Request):
    """A worker answered a Connections > Test agent dispatch."""
    async with request.app.state.db.sessions() as session:
        probe = await session.get(WorkerProbe, probe_id)
        if probe is None or probe.room_name != body.room:
            raise ApiError(404, "probe_not_found", "Unknown probe.")
        if probe.acked_at is None:
            probe.acked_at, probe.runtime_build = utcnow(), body.runtime_build
            await session.commit()
    if request.app.state.gateway.configured:
        # The probe room also expires on its own, so a failed delete is harmless.
        with contextlib.suppress(Exception):
            await request.app.state.gateway.delete_room(body.room)
    return {"ok": True}
