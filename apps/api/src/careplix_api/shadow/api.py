"""Shadow mode API: preview a worklist file, start a run, follow it, settle disagreements."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.errors import ApiError
from careplix_api.settings import Settings, get_settings
from careplix_api.shadow import service as shadow
from careplix_api.shadow.models import ShadowRun
from careplix_api.studio_api import Session, gateway

router = APIRouter()

CSV_LIMIT = shadow.MAX_CSV_CHARS


class PreviewIn(BaseModel):
    csv: str = Field(max_length=CSV_LIMIT)


class MappingIn(BaseModel):
    external_ref: str | None = Field(default=None, max_length=120)
    destination: str | None = Field(default=None, max_length=120)
    duration_s: str | None = Field(default=None, max_length=120)
    resolved: str | None = Field(default=None, max_length=120)
    facts: dict[str, str] = Field(default_factory=dict)  # fact name -> column
    values: dict[str, str] = Field(default_factory=dict)  # finding field -> column


class ShadowRunIn(BaseModel):
    name: str = Field(max_length=120)
    incumbent: str = Field(max_length=80)
    agent_id: str
    use: Literal["draft", "published"] = "published"
    scenario_id: str
    mode: Literal["replay", "live"] = "replay"
    csv: str = Field(max_length=CSV_LIMIT)
    mapping: MappingIn


class AdjudicateIn(BaseModel):
    field: str = Field(max_length=60)
    verdict: Literal["ours", "theirs", "both", "neither"] | None


@router.post("/shadow-runs/preview")
async def preview(body: PreviewIn, principal: Principal = Depends(require("browser_test"))):
    return shadow.preview(body.csv)


@router.get("/shadow-runs")
async def list_runs(
    principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    runs = await session.scalars(
        select(ShadowRun)
        .where(ShadowRun.workspace_id == principal.workspace_id)
        .order_by(ShadowRun.created_at.desc())
        .limit(50)
    )
    return [shadow.run_view(r) for r in runs]


@router.post("/shadow-runs")
async def create_run(
    body: ShadowRunIn,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    if body.mode == "live" and not principal.can("start_task"):
        raise ApiError(
            403, "forbidden", "Only people who can start calls can run live shadow calls."
        )
    run = await shadow.create(
        session,
        principal,
        settings,
        name=body.name,
        incumbent=body.incumbent,
        agent_id=body.agent_id,
        use=body.use,
        scenario_id=body.scenario_id,
        mode=body.mode,
        csv_text=body.csv,
        mapping=body.mapping.model_dump(),
    )
    await session.commit()
    return await shadow.run_detail(session, run, settings, principal)


@router.get("/shadow-runs/{run_id}")
async def get_run(
    run_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await shadow.get_run(session, principal, run_id)
    return await shadow.run_detail(session, run, settings, principal)


@router.post("/shadow-runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
):
    run = await shadow.get_run(session, principal, run_id)
    await shadow.cancel(session, principal, gw, run)
    await session.commit()
    return await shadow.run_detail(session, run, settings, principal)


@router.post("/shadow-runs/{run_id}/items/{item_id}/adjudicate")
async def adjudicate(
    run_id: str,
    item_id: str,
    body: AdjudicateIn,
    principal: Principal = Depends(require("review")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await shadow.get_run(session, principal, run_id)
    await shadow.adjudicate(session, principal, run, item_id, body.field, body.verdict)
    await session.commit()
    return await shadow.run_detail(session, run, settings, principal)
