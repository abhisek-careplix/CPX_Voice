"""Regression suites API: an agent's suite, its runs, and starting or cancelling one."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import agents_service, redteam
from careplix_api.auth import Principal, require
from careplix_api.evals import rules
from careplix_api.evals import service as evals
from careplix_api.evals.models import EvalRun
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, gateway

router = APIRouter()


class SuiteRunIn(BaseModel):
    use: Literal["draft", "published"] = "draft"
    scenario_ids: list[str] | None = Field(default=None, max_length=evals.MAX_SCENARIOS)
    repeats: int = 3
    compare: bool = True  # also run the published version as a baseline


@router.get("/agents/{agent_id}/suite")
async def get_suite(
    agent_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    draft = await agents_service.latest_draft(session, agent)
    current = await agents_service.current_version(session, agent)
    runs = list(
        await session.scalars(
            select(EvalRun)
            .where(EvalRun.agent_id == agent.id, EvalRun.workspace_id == principal.workspace_id)
            .order_by(EvalRun.created_at.desc())
            .limit(10)
        )
    )
    return {
        "scenarios": [
            {
                "id": s.id,
                "name": s.name,
                "category": s.category,
                "expect": rules.clean(s.expect),
                "checks": len(s.success_checks or []),
            }
            for s in await evals.suite_scenarios(session, agent)
        ],
        "required_to_publish": agents_service.suite_required(agent, settings),
        "draft": agents_service.suite_result(draft) if draft else None,
        "published": agents_service.suite_result(current) if current else None,
        "runs": [evals.run_view(r) for r in runs],
        "max_repeats": evals.MAX_REPEATS,
    }


@router.post("/agents/{agent_id}/suite/runs")
async def start_suite_run(
    agent_id: str,
    body: SuiteRunIn,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
):
    agent = await agents_service.get_agent(session, principal, agent_id)
    run = await evals.start(
        session,
        principal,
        agent,
        use=body.use,
        scenario_ids=body.scenario_ids,
        repeats=body.repeats,
        compare=body.compare,
    )
    await session.commit()
    return await evals.run_detail(session, run)


@router.get("/red-team/attacks")
async def red_team_attacks(principal: Principal = Depends(require("view"))):
    return redteam.view()


@router.post("/agents/{agent_id}/red-team")
async def add_red_team(
    agent_id: str,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Add (or refresh) the agent's red-team scenarios; its suite then runs them."""
    agent = await agents_service.get_agent(session, principal, agent_id)
    result = await redteam.add_for_agent(session, principal, settings, agent)
    await session.commit()
    return result


@router.get("/suite-runs/{run_id}")
async def get_suite_run(
    run_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
):
    return await evals.run_detail(session, await evals.get_run(session, principal, run_id))


@router.post("/suite-runs/{run_id}/cancel")
async def cancel_suite_run(
    run_id: str,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
    gw=Depends(gateway),
):
    run = await evals.get_run(session, principal, run_id)
    await evals.cancel(session, principal, gw, run)
    await session.commit()
    return await evals.run_detail(session, run)
