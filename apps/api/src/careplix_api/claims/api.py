"""Claims and clearinghouse API: claims to follow, their status evidence, the payer directory."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.claims import directory, follow_up, samples, service
from careplix_api.claims.models import Claim, ClaimStatusCheck
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import clearinghouse
from careplix_api.practice.connections import is_test
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()


class SamplesIn(BaseModel):
    scenarios: list[str] | None = Field(default=None, max_length=20)


class RefreshIn(BaseModel):
    query: str = Field(min_length=2, max_length=80)


class PayerIn(BaseModel):
    phone: str | None = Field(default=None, max_length=20)


def _transport(request: Request):
    return request.app.state.integration_transport


@router.get("/clearinghouse")
async def overview(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    """Admin → Clearinghouse: connection, mode, what's available, the payer directory."""
    conn = await service.clearinghouse_connection(session, principal.workspace_id)
    payers = await directory.listing(session, principal.workspace_id)
    await session.commit()
    return {
        "name": clearinghouse.NAME,
        "connected": conn is not None,
        "mode": (clearinghouse.TEST if is_test(conn) else clearinghouse.PRODUCTION)
        if conn
        else None,
        "verified_at": conn.verified_at.isoformat() if conn and conn.verified_at else None,
        "transactions": [
            {"id": "eligibility", "label": "Eligibility and benefits (270/271)", "ready": True},
            {"id": "claim_status", "label": "Claim status (276/277)", "ready": True},
            {"id": "claims", "label": "Claim submission (837P)", "ready": True},
            {"id": "acknowledgements", "label": "Claim acknowledgements (277CA)", "ready": True},
            {"id": "era", "label": "Remittance (835)", "ready": True},
        ],
        "payers": payers,
    }


@router.get("/clearinghouse/payers")
async def payers(
    q: str = "", principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    rows = await directory.listing(session, principal.workspace_id, q)
    await session.commit()
    return rows


@router.post("/clearinghouse/payers/refresh")
async def refresh_payers(
    body: RefreshIn,
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await service.clearinghouse_connection(session, principal.workspace_id)
    if conn is None:
        raise ApiError(409, "clearinghouse_not_connected", "Connect the clearinghouse first.")
    await directory.ensure(session, principal.workspace_id)
    try:
        rows = await directory.refresh(
            session, principal.workspace_id, conn, settings, body.query, _transport(request)
        )
    except clearinghouse.ClearinghouseError as err:
        raise ApiError(502, "clearinghouse_error", err.message) from None
    await session.commit()
    return rows


@router.patch("/clearinghouse/payers/{row_id}")
async def update_payer(
    row_id: str,
    body: PayerIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    from careplix_api.calls_service import normalize_e164  # local: calls_service imports cases

    row = await session.get(directory.ClearinghousePayer, row_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "payer_not_found", "No payer in your directory matches that.")
    row.phone = normalize_e164(body.phone) if body.phone else None
    await session.commit()
    return directory.view(row)


@router.get("/claims")
async def list_claims(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    today = utcnow().date()
    rows = await session.scalars(
        select(Claim)
        .where(Claim.workspace_id == principal.workspace_id)
        .order_by(Claim.submitted_on, Claim.claim_number)
    )
    return [service.view(c, today) for c in rows]


@router.get("/claims/scenarios")
async def scenarios(principal: Principal = Depends(viewer)):
    return [{"key": s.key, "label": s.label, "age_days": s.age_days} for s in samples.SCENARIOS]


@router.post("/claims/samples")
async def add_samples(
    body: SamplesIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    unknown = set(body.scenarios or []) - set(samples.BY_KEY)
    if unknown:
        raise ApiError(422, "unknown_scenario", "Pick listed sample scenarios.")
    today = utcnow().date()
    made = await samples.add(
        session, settings, principal.workspace_id, today=today, scenarios=body.scenarios
    )
    await directory.ensure(session, principal.workspace_id)
    await session.commit()
    return [service.view(c, today) for c in made]


@router.post("/claims/follow-up")
async def run_follow_up(
    request: Request,
    principal: Principal = Depends(require("start_task")),
    settings: Settings = Depends(get_settings),
):
    """Run the follow-up schedule for this workspace now (it also runs every 15 minutes)."""
    return await follow_up.run_due(
        request.app.state.db,
        settings,
        _transport(request),
        workspace_id=principal.workspace_id,
    )


async def _detail(session: AsyncSession, claim: Claim) -> dict:
    now = utcnow()
    return {
        **service.view(claim, now.date()),
        "due": follow_up.due(claim, now),
        "needs_call": follow_up.needs_call(claim, now),
        "checks": [service.check_view(r) for r in await service.checks(session, claim)],
    }


@router.get("/claims/{claim_id}")
async def get_claim(
    claim_id: str, principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    return await _detail(session, await service.get(session, principal.workspace_id, claim_id))


@router.post("/claims/{claim_id}/check")
async def check_claim(
    claim_id: str,
    request: Request,
    via: Literal["auto", "sample", "clearinghouse"] = "auto",
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Ask now. ``via=clearinghouse`` sends a sample claim's check to the clearinghouse too (test
    mode sends the published test claim, never the patient)."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    await service.check(
        session, settings, claim, trigger="manual", via=via, transport=_transport(request)
    )
    reason = follow_up.needs_call(claim, utcnow())
    if reason:
        await follow_up.ensure_case(session, settings, claim, reason)
    await session.commit()
    return await _detail(session, claim)


@router.get("/claims/{claim_id}/checks/{check_id}/response")
async def check_response(
    claim_id: str,
    check_id: str,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The stored 277 (evidence). Reviewers only: live answers can carry patient identifiers."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    row = await session.get(ClaimStatusCheck, check_id)
    if row is None or row.claim_id != claim.id or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "check_not_found", "No status check matches that.")
    # Shown white-labelled; the stored evidence is kept exactly as the clearinghouse sent it.
    opened = json.loads(clearinghouse.white_label(json.dumps(service.evidence(settings, row))))
    return {"id": row.id, "mock": row.mock, "source": row.source, **opened}
