"""Voice Studio API: scenarios, rehearsals, live runs and scorecards (VOICE_STUDIO_PLAN VS1)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import scenarios
from careplix_api.auth import Principal, require
from careplix_api.errors import ApiError
from careplix_api.judge import service as judge_service
from careplix_api.models import AuditEvent, CallAttempt, Scenario
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, gateway, runner

router = APIRouter(prefix="/scenarios")


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = "other"
    agent_id: str | None = None
    counterpart: str = Field(min_length=1, max_length=120)
    on_behalf_of: str = Field(min_length=1, max_length=120)
    destination: str | None = Field(default=None, max_length=32)
    goal: str = Field(min_length=1, max_length=1200)
    facts: dict[str, str] = Field(default_factory=dict)
    limits: list[str] = Field(default_factory=list, max_length=10)
    success_checks: list[str] = Field(default_factory=list, max_length=10)
    rehearsal_script: str | None = Field(default=None, max_length=1500)
    simulation: dict[str, Any] | None = None
    expect: dict[str, Any] | None = None


def _audit(session: AsyncSession, principal: Principal, action: str, target: str) -> None:
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=target,
            detail={},
        )
    )


@router.get("")
async def list_scenarios(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    rows = await session.scalars(
        select(Scenario)
        .where(Scenario.workspace_id == principal.workspace_id)
        .order_by(Scenario.updated_at.desc())
    )
    box = PhiBox(settings)
    return {
        "categories": list(scenarios.CATEGORIES),
        "scenarios": [scenarios.view(s, box, with_facts=False) for s in rows],
    }


@router.post("")
async def create_scenario(
    body: ScenarioIn,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    scenario = Scenario(workspace_id=principal.workspace_id, created_by=principal.user_id)
    scenarios.apply(scenario, body.model_dump(), PhiBox(settings))
    session.add(scenario)
    await session.flush()
    _audit(session, principal, "scenario.created", scenario.id)
    await session.commit()
    return scenarios.view(scenario, PhiBox(settings))


@router.post("/examples")
async def add_examples(
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The starter library and the 'Call on my behalf' agent (published if the model is set)."""
    added = await scenarios.add_examples(session, principal, settings)
    await session.commit()
    return {"added": len(added)}


@router.get("/{scenario_id}")
async def get_scenario(
    scenario_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    scenario = await scenarios.get(session, principal, scenario_id)
    cards = await scenarios.runs(session, scenario)
    return {
        **scenarios.view(scenario, PhiBox(settings), with_facts=principal.can("edit_draft")),
        "runs": cards,
        "judge_summary": scenarios.judge_summary(cards),
    }


@router.put("/{scenario_id}")
async def update_scenario(
    scenario_id: str,
    body: ScenarioIn,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    scenario = await scenarios.get(session, principal, scenario_id)
    scenarios.apply(scenario, body.model_dump(), PhiBox(settings))
    _audit(session, principal, "scenario.updated", scenario.id)
    await session.commit()
    return scenarios.view(scenario, PhiBox(settings))


@router.delete("/{scenario_id}")
async def delete_scenario(
    scenario_id: str,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
):
    scenario = await scenarios.get(session, principal, scenario_id)
    _audit(session, principal, "scenario.deleted", scenario.id)
    await session.delete(scenario)  # past runs keep their calls and transcripts
    await session.commit()
    return {"ok": True}


class RunIn(BaseModel):
    mode: Literal["rehearse", "simulate", "live"]
    use: Literal["draft", "published"] = "published"


@router.post("/{scenario_id}/runs")
async def run_scenario(
    scenario_id: str,
    body: RunIn,
    principal: Principal = Depends(require("browser_test")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    gw=Depends(gateway),
    run=Depends(runner),
):
    """Rehearse: the agent calls your browser and you play the other side. Live: a real call."""
    scenario = await scenarios.get(session, principal, scenario_id)
    if body.mode == "rehearse":
        return await scenarios.rehearse(session, principal, settings, gw, scenario, body.use)
    if body.mode == "simulate":
        return await scenarios.simulate(session, principal, settings, gw, scenario, body.use)
    if not principal.can("start_task"):
        raise ApiError(403, "forbidden", "Your role does not allow live calls.")
    attempt = await scenarios.run_live(session, principal, settings, gw, run, scenario)
    return {"attempt_id": attempt.id, "state": attempt.state}


@router.post("/{scenario_id}/runs/{attempt_id}/judge")
async def judge_run(
    scenario_id: str,
    attempt_id: str,
    request: Request,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Score (or re-score) a finished run with the AI judge."""
    scenario = await scenarios.get(session, principal, scenario_id)
    attempt = await session.get(CallAttempt, attempt_id)
    if attempt is None or attempt.scenario_id != scenario.id:
        raise ApiError(404, "not_found", "Run not found.")
    if attempt.state not in judge_service.ENDED:
        raise ApiError(409, "run_active", "The run is still going. Score it once it has ended.")
    row = await judge_service.judge_attempt(
        session, attempt, scenario, settings, request.app.state.judge, principal.user_id
    )
    _audit(session, principal, "scenario.run_judged", attempt.id)
    await session.commit()
    return judge_service.view(row)
