"""Care opportunities API: import facts, scan, approve or decline, record the outcome."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.opportunities import service as care
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter()


class ImportIn(BaseModel):
    csv: str = Field(max_length=care.MAX_CSV_CHARS)


class DecideIn(BaseModel):
    decision: Literal["approve", "decline"]
    note: str = Field(default="", max_length=300)


class OutcomeIn(BaseModel):
    outcome: Literal["contacted", "enrolled", "not_interested", "done"]
    note: str = Field(default="", max_length=300)


def _today() -> date:
    return date.today()


async def _listing(session, principal, settings, status=None):
    return await care.listing(session, principal, PhiBox(settings), _today(), status)


@router.get("/care-opportunities")
async def list_opportunities(
    status: str | None = None,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await _listing(session, principal, settings, status)


@router.post("/care-opportunities/facts")
async def import_facts(
    body: ImportIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    box = PhiBox(settings)
    imported = await care.import_csv(session, principal, box, body.csv)
    scanned = await care.scan(session, principal, box, _today())
    await session.commit()
    return {"imported": imported, "scan": scanned}


@router.post("/care-opportunities/sample")
async def load_sample(
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    box = PhiBox(settings)
    imported = await care.import_csv(
        session, principal, box, care.sample_csv(_today()), source="sample"
    )
    scanned = await care.scan(session, principal, box, _today())
    await session.commit()
    return {"imported": imported, "scan": scanned}


@router.post("/care-opportunities/scan")
async def scan(
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    result = await care.scan(session, principal, PhiBox(settings), _today())
    await session.commit()
    return result


@router.post("/care-opportunities/{opportunity_id}/decide")
async def decide(
    opportunity_id: str,
    body: DecideIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    row = await care.get(session, principal, opportunity_id)
    await care.decide(session, principal, row, body.decision, body.note)
    await session.commit()
    return await _listing(session, principal, settings)


@router.post("/care-opportunities/{opportunity_id}/outcome")
async def outcome(
    opportunity_id: str,
    body: OutcomeIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    row = await care.get(session, principal, opportunity_id)
    await care.record_outcome(session, principal, row, body.outcome, body.note)
    await session.commit()
    return await _listing(session, principal, settings)
