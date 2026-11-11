"""Regression suites: simulate an agent version against its scenarios and decide pass or fail.

A run is a grid of cases (scenario × repeat × role). Each case is an ordinary Voice Studio
simulation (VS2: the agent calls an AI counterpart playing the scenario) pinned to the exact
version under test. When its call ends, the platform checks the scenario's rules from the record
(evals.rules) and waits for the AI judge's verdict; a case passes only when both do.

A scenario passes when at least two thirds of its repeats pass (voice runs vary); one that passes
sometimes is "flaky". The run passes when every scenario passes for the candidate. The result is
stored on the version (``AgentVersion.evaluation``) with the config hash it was measured on, so a
later edit to the draft invalidates it and the publish gate asks for a new run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import agents_service, calls_service, scenarios, templates
from careplix_api.auth import Principal
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.evals import rules
from careplix_api.evals.models import EvalCase, EvalRun
from careplix_api.judge.models import RunJudgement
from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.models import Agent, AgentVersion, AuditEvent, CallAttempt, Finding, Scenario
from careplix_api.settings import Settings

log = logging.getLogger("careplix.evals")

ACTIVE = ("queued", "running")
OPEN_CASES = ("queued", "running", "judging")
ENDED = ("Ended", "Failed", "Cancelled")
MAX_REPEATS = 5
MAX_SCENARIOS = 30


def required_passes(repeats: int) -> int:
    """Two thirds of the repeats, rounded up: 1 of 1, 2 of 2, 2 of 3, 3 of 4, 4 of 5."""
    return math.ceil(repeats * 2 / 3)


async def suite_scenarios(session: AsyncSession, agent: Agent) -> list[Scenario]:
    """An agent's default suite: every scenario in its workspace that this agent places."""
    return list(
        await session.scalars(
            select(Scenario)
            .where(Scenario.workspace_id == agent.workspace_id, Scenario.agent_id == agent.id)
            .order_by(Scenario.name)
        )
    )


# --- starting and stopping ------------------------------------------------------------------------


async def start(
    session: AsyncSession,
    principal: Principal,
    agent: Agent,
    *,
    use: str,
    scenario_ids: list[str] | None,
    repeats: int,
    compare: bool,
) -> EvalRun:
    if templates.get_template(agent.template_id)["direction"] != "outbound":
        raise ApiError(422, "suite_outbound_only", "Regression suites run agents that place calls.")
    if use == "draft":
        version = await agents_service.latest_draft(session, agent)
        if version is None:
            raise ApiError(409, "no_draft", "This agent has no draft. Run the published version.")
    else:
        version = await agents_service.current_version(session, agent)
        if version is None:
            raise ApiError(409, "not_published", "This agent has no published version yet.")
    if scenario_ids:
        wanted = list(dict.fromkeys(scenario_ids))
        rows = list(
            await session.scalars(
                select(Scenario).where(
                    Scenario.workspace_id == principal.workspace_id, Scenario.id.in_(wanted)
                )
            )
        )
        if len(rows) != len(wanted):
            raise ApiError(404, "scenario_not_found", "One of those scenarios no longer exists.")
        rows.sort(key=lambda s: s.name)
    else:
        rows = await suite_scenarios(session, agent)
    if not rows:
        raise ApiError(
            422,
            "no_scenarios",
            "This agent has no scenarios yet. In Voice Studio, add scenarios and choose this agent "
            "under 'Agent that calls'.",
        )
    if len(rows) > MAX_SCENARIOS:
        raise ApiError(
            422, "too_many_scenarios", f"A suite runs at most {MAX_SCENARIOS} scenarios."
        )
    if not 1 <= repeats <= MAX_REPEATS:
        raise ApiError(422, "bad_repeats", f"Run each scenario 1 to {MAX_REPEATS} times.")
    busy = await session.scalar(
        select(EvalRun).where(EvalRun.version_id == version.id, EvalRun.state.in_(ACTIVE))
    )
    if busy is not None:
        raise ApiError(
            409, "suite_running", "A suite is already running on this version.", {"run_id": busy.id}
        )

    current = await agents_service.current_version(session, agent)
    baseline = current if compare and current is not None and current.id != version.id else None
    run = EvalRun(
        workspace_id=principal.workspace_id,
        agent_id=agent.id,
        version_id=version.id,
        version_number=version.number,
        version_state=version.state,
        config_hash=agents_service.config_hash(version.config),
        baseline_version_id=baseline.id if baseline else None,
        baseline_number=baseline.number if baseline else None,
        scenarios=[{"id": s.id, "name": s.name} for s in rows],
        repeats=repeats,
        created_by=principal.user_id,
    )
    session.add(run)
    await session.flush()
    seq = 0
    roles = [("candidate", version)] + ([("baseline", baseline)] if baseline else [])
    for repeat in range(1, repeats + 1):  # breadth first: every scenario once before repeats
        for scenario in rows:
            for role, v in roles:
                seq += 1
                session.add(
                    EvalCase(
                        run_id=run.id,
                        workspace_id=run.workspace_id,
                        seq=seq,
                        scenario_id=scenario.id,
                        role=role,
                        version_id=v.id,
                        repeat=repeat,
                    )
                )
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="suite.started",
            target=agent.id,
            detail={"run_id": run.id, "version": version.number, "cases": seq},
        )
    )
    return run


async def cancel(
    session: AsyncSession, principal: Principal, gateway: LiveKitGateway, run: EvalRun
) -> None:
    if run.state not in ACTIVE:
        return
    for case in await _cases(session, run):
        if case.state not in OPEN_CASES:
            continue
        if case.state == "running" and case.attempt_id:
            attempt = await session.get(CallAttempt, case.attempt_id)
            if attempt is not None:
                with contextlib.suppress(ApiError):  # already ending
                    await calls_service.end_by_operator(session, principal, gateway, attempt)
        case.state, case.finished_at = "cancelled", utcnow()
    run.state, run.finished_at = "cancelled", utcnow()
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="suite.cancelled",
            target=run.agent_id,
            detail={"run_id": run.id},
        )
    )


async def get_run(session: AsyncSession, principal: Principal, run_id: str) -> EvalRun:
    run = await session.get(EvalRun, run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Suite run not found.")
    return run


async def _cases(session: AsyncSession, run: EvalRun) -> list[EvalCase]:
    return list(
        await session.scalars(
            select(EvalCase).where(EvalCase.run_id == run.id).order_by(EvalCase.seq)
        )
    )


# --- the runner -----------------------------------------------------------------------------------


async def tick(db: Database, settings: Settings, gateway: LiveKitGateway) -> None:
    """Advance every active run: follow running cases, start queued ones, finish complete runs."""
    async with db.sessions() as session:
        runs = list(
            await session.scalars(
                select(EvalRun).where(EvalRun.state.in_(ACTIVE)).order_by(EvalRun.created_at)
            )
        )
        for run in runs:
            await _advance(session, settings, gateway, run)
            await session.commit()


async def run_forever(db: Database, settings: Settings, gateway: LiveKitGateway) -> None:
    while True:
        try:
            await tick(db, settings, gateway)
        except Exception as exc:
            log.warning("suite loop error", extra={"error": type(exc).__name__})
        await asyncio.sleep(settings.eval_interval_s)


async def _advance(
    session: AsyncSession, settings: Settings, gateway: LiveKitGateway, run: EvalRun
) -> None:
    cases = await _cases(session, run)
    version = await session.get(AgentVersion, run.version_id)
    if version is None or agents_service.config_hash(version.config) != run.config_hash:
        # The draft was edited mid-run: the result would describe neither config.
        for case in cases:
            if case.state == "queued":
                case.state, case.finished_at = "cancelled", utcnow()
        run.state, run.finished_at = "stale", utcnow()
        run.error = "The draft changed during the run. Run the suite again on the saved draft."
        return

    for case in cases:
        if case.state in ("running", "judging"):
            await _follow(session, settings, case)

    running = await session.scalar(
        select(func.count())
        .select_from(EvalCase)
        .where(EvalCase.workspace_id == run.workspace_id, EvalCase.state == "running")
    )
    slots = max(0, settings.eval_concurrency - (running or 0))
    for case in [c for c in cases if c.state == "queued"][:slots]:
        await _start_case(session, settings, gateway, run, case)
        run.state = "running"

    if all(c.state not in OPEN_CASES for c in cases):
        await _finish(session, run, cases)


async def _start_case(
    session: AsyncSession,
    settings: Settings,
    gateway: LiveKitGateway,
    run: EvalRun,
    case: EvalCase,
) -> None:
    scenario = await session.get(Scenario, case.scenario_id)
    version = await session.get(AgentVersion, case.version_id)
    agent = await session.get(Agent, run.agent_id)
    case.started_at = utcnow()
    if scenario is None or version is None or agent is None:
        _close(case, "error", error="The scenario or agent version was deleted.")
        return
    principal = Principal(run.created_by, run.workspace_id, "author", "Regression suite")
    try:
        started = await calls_service.start_browser_session(
            session,
            principal,
            settings,
            gateway,
            agent,
            version.state,
            None,
            scenario_id=scenario.id,
            simulated=True,
            version=version,
            eval_case_id=case.id,
        )
    except ApiError as exc:
        _close(case, "error", error=exc.message)
        return
    case.attempt_id, case.state = started["attempt_id"], "running"


async def _follow(session: AsyncSession, settings: Settings, case: EvalCase) -> None:
    attempt = await session.get(CallAttempt, case.attempt_id) if case.attempt_id else None
    now = utcnow()
    if attempt is None:
        _close(case, "error", error="The simulation's call record is missing.")
        return
    if attempt.state not in ENDED:
        if now - as_utc(case.started_at or now) > timedelta(seconds=settings.eval_case_timeout_s):
            _close(case, "error", error="The simulation didn't finish in time.")
        return
    scenario = await session.get(Scenario, case.scenario_id)
    if scenario is None:
        _close(case, "error", error="The scenario was deleted during the run.")
        return
    if case.rules is None:
        card = await scenarios.scorecard(session, attempt, scenario)
        findings: dict[str, Any] = {}
        for f in await session.scalars(
            select(Finding).where(Finding.attempt_id == attempt.id).order_by(Finding.created_at)
        ):
            findings[f.field] = f.value  # latest wins
        case.rules = rules.check(scenario.expect, card, findings)
        case.state = "judging"

    if attempt.connected_at is None:
        judge: dict[str, Any] | None = {"state": "skipped", "error": "The call never connected."}
    else:
        row = await session.scalar(
            select(RunJudgement).where(RunJudgement.attempt_id == attempt.id)
        )
        judge = None
        if row is not None and row.state in ("done", "failed"):
            judge = {
                "state": row.state,
                "verdict": row.verdict,
                "score": row.score,
                "summary": row.summary,
                "error": row.error,
            }
        elif now - as_utc(attempt.ended_at or now) > timedelta(seconds=settings.eval_judge_wait_s):
            judge = {"state": "failed", "error": "No judgement arrived in time."}
    if judge is None:
        return  # the judge loop hasn't scored it yet
    case.judge = judge
    if not all(r["ok"] for r in case.rules):
        _close(case, "fail")
    elif judge["state"] == "done":
        _close(case, "pass" if judge.get("verdict") == "pass" else "fail")
    else:
        _close(case, "error", error=judge.get("error") or "The judge couldn't score this run.")


def _close(case: EvalCase, verdict: str, error: str | None = None) -> None:
    case.state, case.verdict, case.finished_at = "done", verdict, utcnow()
    case.error = (error or "")[:240] or None


async def _finish(session: AsyncSession, run: EvalRun, cases: list[EvalCase]) -> None:
    run.summary = summarise(run, cases)
    candidate = [s for s in run.summary if s["role"] == "candidate"]
    passed = bool(candidate) and all(s["status"] == "pass" for s in candidate)
    run.state, run.finished_at = ("passed" if passed else "failed"), utcnow()
    version = await session.get(AgentVersion, run.version_id)
    if version is not None:
        version.evaluation = {
            "status": run.state,
            "run_id": run.id,
            "config_hash": run.config_hash,
            "passed": sum(1 for s in candidate if s["status"] == "pass"),
            "total": len(candidate),
            "repeats": run.repeats,
            "finished_at": run.finished_at.isoformat(),
        }


def summarise(run: EvalRun, cases: list[EvalCase]) -> list[dict[str, Any]]:
    out = []
    for role in ("candidate", "baseline"):
        for s in run.scenarios:
            group = [c for c in cases if c.role == role and c.scenario_id == s["id"]]
            if not group:
                continue
            passes = sum(1 for c in group if c.verdict == "pass")
            errors = sum(1 for c in group if c.verdict == "error")
            required = required_passes(len(group))
            if passes >= required:
                status = "pass"
            elif passes:
                status = "flaky"
            elif errors == len(group):
                status = "error"
            else:
                status = "fail"
            out.append(
                {
                    "scenario_id": s["id"],
                    "name": s["name"],
                    "role": role,
                    "passes": passes,
                    "runs": len(group),
                    "required": required,
                    "status": status,
                }
            )
    return out


# --- views ----------------------------------------------------------------------------------------


def run_view(run: EvalRun, cases: list[EvalCase] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": run.id,
        "agent_id": run.agent_id,
        "version": {"number": run.version_number, "state": run.version_state},
        "baseline_version": run.baseline_number,
        "config_hash": run.config_hash,
        "repeats": run.repeats,
        "required_passes": required_passes(run.repeats),
        "state": run.state,
        "scenarios": run.scenarios,
        "summary": run.summary,
        "error": run.error,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }
    if cases is not None:
        done = sum(1 for c in cases if c.state not in OPEN_CASES)
        out["progress"] = {"done": done, "total": len(cases)}
        out["live_summary"] = run.summary or summarise(run, cases)
        out["cases"] = [
            {
                "id": c.id,
                "scenario_id": c.scenario_id,
                "role": c.role,
                "repeat": c.repeat,
                "state": c.state,
                "verdict": c.verdict,
                "attempt_id": c.attempt_id,
                "rules": c.rules or [],
                "judge": c.judge,
                "error": c.error,
            }
            for c in cases
        ]
    return out


async def run_detail(session: AsyncSession, run: EvalRun) -> dict[str, Any]:
    return run_view(run, await _cases(session, run))
