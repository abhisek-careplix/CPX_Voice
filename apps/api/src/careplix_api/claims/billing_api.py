"""Billing API (RC3–RC8): charges, claims we build and submit, and what comes back.

Mounted before :mod:`.api` so its fixed paths (``/claims/charges``, ``/claims/work``, …) win over
``/claims/{claim_id}``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.claims import billing, charges, service, x837
from careplix_api.claims.models import ClaimCharge
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import PayerConnection
from careplix_api.practice.connections import EHR
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
EDITABLE = ("draft", "ready", "blocked", "rejected")


def _transport(request: Request):
    return request.app.state.integration_transport


class VisitsIn(BaseModel):
    visits: list[str] | None = Field(default=None, max_length=20)


class BuildIn(BaseModel):
    charge_ids: list[str] | None = Field(default=None, max_length=200)


class ProfileIn(BaseModel):
    organization: str | None = Field(default=None, max_length=60)
    npi: str | None = Field(default=None, max_length=12)
    tax_id: str | None = Field(default=None, max_length=11)
    taxonomy: str | None = Field(default=None, max_length=10)
    address1: str | None = Field(default=None, max_length=55)
    city: str | None = Field(default=None, max_length=30)
    state: str | None = Field(default=None, max_length=2)
    postal_code: str | None = Field(default=None, max_length=10)
    submitter_name: str | None = Field(default=None, max_length=60)
    contact_phone: str | None = Field(default=None, max_length=14)


class LineEdit(BaseModel):
    line_id: str = Field(max_length=10)
    modifiers: list[str] | None = Field(default=None, max_length=4)
    units: str | None = Field(default=None, max_length=8)
    dx: list[int] | None = Field(default=None, max_length=4)


class ClaimEdit(BaseModel):
    diagnoses: list[str] | None = Field(default=None, max_length=12)
    place_of_service: str | None = Field(default=None, min_length=2, max_length=2)
    lines: list[LineEdit] | None = Field(default=None, max_length=50)
    test_outcome: Literal["paid", "partial", "denied", "none"] | None = None


# --- billing profile -----------------------------------------------------------------------------


@router.get("/clearinghouse/billing-profile")
async def get_profile(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    row = await billing.get(session, principal.workspace_id)
    await session.commit()
    return billing.view(row)


@router.put("/clearinghouse/billing-profile")
async def put_profile(
    body: ProfileIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    row = billing.update(
        await billing.get(session, principal.workspace_id), body.model_dump(exclude_none=True)
    )
    await session.commit()
    return billing.view(row)


# --- charges and draft claims ------------------------------------------------------------------


@router.get("/claims/charges")
async def list_charges(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    rows = await session.scalars(
        select(ClaimCharge)
        .where(ClaimCharge.workspace_id == principal.workspace_id)
        .order_by(ClaimCharge.created_at.desc(), ClaimCharge.source_ref)
        .limit(300)
    )
    return [charges.view(c) for c in rows]


@router.get("/claims/charges/visits")
async def visit_kinds(principal: Principal = Depends(viewer)):
    return [
        {"key": v.key, "label": v.label, "test_outcome": v.test_outcome} for v in charges.VISITS
    ]


@router.post("/claims/charges/samples")
async def add_charge_samples(
    body: VisitsIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    if set(body.visits or []) - set(charges.VISITS_BY_KEY):
        raise ApiError(422, "unknown_visit", "Pick listed sample visits.")
    made = await charges.add_samples(
        session, settings, principal.workspace_id, today=utcnow().date(), keys=body.visits
    )
    await session.commit()
    return [charges.view(c) for c in made]


@router.post("/claims/charges/pull")
async def pull_charges(
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Read recent charges from each connected NexHealth practice."""
    conns = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == principal.workspace_id,
                PayerConnection.kind == EHR,
                PayerConnection.enabled.is_(True),
            )
        )
    )
    if not conns:
        raise ApiError(409, "no_ehr", "Connect the practice's EHR (NexHealth) first.")
    out = []
    for conn in conns:
        result = await charges.pull_ehr(session, conn, settings, _transport(request))
        out.append({"connection_id": conn.id, "name": conn.name, **result})
    await session.commit()
    return out


@router.post("/claims/build")
async def build_claims(
    body: BuildIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    made = await charges.build_claims(
        session, settings, principal.workspace_id, charge_ids=body.charge_ids
    )
    await session.commit()
    today = utcnow().date()
    return [service.view(c, today) for c in made]


@router.patch("/claims/{claim_id}")
async def edit_claim(
    claim_id: str,
    body: ClaimEdit,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """Fix a claim before (re)submission: diagnoses, place of service, line modifiers, units and
    diagnosis pointers. The scrub runs again before it can go out."""
    claim = await service.get(session, principal.workspace_id, claim_id)
    if claim.stage not in EDITABLE:
        raise ApiError(409, "not_editable", "Only claims that haven't gone out can be edited.")
    if body.diagnoses is not None:
        claim.diagnoses = [x837.icd10(d) for d in body.diagnoses if x837.icd10(d)]
    if body.place_of_service is not None:
        claim.place_of_service = body.place_of_service
    if body.test_outcome is not None:
        claim.test_outcome = None if body.test_outcome == "none" else body.test_outcome
    if body.lines:
        edits = {e.line_id: e for e in body.lines}
        lines = []
        for line in claim.lines or []:
            edit = edits.get(line.get("line_id"))
            if edit is not None:
                line = dict(line)
                if edit.modifiers is not None:
                    line["modifiers"] = [m.strip().upper() for m in edit.modifiers if m.strip()]
                if edit.units is not None:
                    line["units"] = edit.units
                if edit.dx is not None:
                    line["dx"] = edit.dx
            lines.append(line)
        claim.lines = lines
    claim.billed_amount = x837.total(claim.lines or [])
    claim.scrub = None
    if claim.stage in ("ready", "blocked"):
        claim.stage = "draft"
    await session.commit()
    return service.view(claim, utcnow().date())
