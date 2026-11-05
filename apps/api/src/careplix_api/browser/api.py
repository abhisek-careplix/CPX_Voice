"""Browser engine API: /api/browser (runs, approvals, take-over, sites, saved sign-ins)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.browser import service
from careplix_api.browser.models import BrowserProfile, BrowserRun, BrowserSite, BrowserStep
from careplix_api.browser.runtime_client import RuntimeUnavailable
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter(prefix="/browser")


def runner(request: Request) -> service.BrowserRunner:
    return request.app.state.browser_runner


class StartIn(BaseModel):
    task: str = Field(min_length=3, max_length=2000)
    start_url: str = Field(min_length=8, max_length=2000)
    model: Literal["gemini", "claude"] | None = None
    supervised: bool = False
    extra_hosts: list[str] = Field(default_factory=list, max_length=5)
    allow_reading: bool = False


class PlanIn(BaseModel):
    decision: Literal["approve", "edit", "cancel"]
    steps: list[str] = Field(default_factory=list, max_length=12)


class StepDecisionIn(BaseModel):
    decision: Literal["approve", "approve_similar", "always_allow", "edit", "deny", "take_over"]
    text: str | None = Field(default=None, max_length=2000)
    feedback: str | None = Field(default=None, max_length=600)


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "take_over", "hand_back", "stop"]


class InputIn(BaseModel):
    kind: Literal["click", "type", "key", "scroll", "back", "navigate"]
    x: int | None = None
    y: int | None = None
    text: str | None = Field(default=None, max_length=2000)
    direction: Literal["up", "down"] | None = None
    amount: int | None = None
    url: str | None = Field(default=None, max_length=2000)


class AckIn(BaseModel):
    host: str = Field(min_length=3, max_length=255)
    terms_note: str | None = Field(default=None, max_length=2000)
    automation_permitted: bool = False


@router.get("/settings")
async def browser_settings(
    request: Request,
    principal: Principal = Depends(require("view")),
    settings: Settings = Depends(get_settings),
):
    return {
        "configured": bool(request.app.state.browser_runtime.configured),
        "default_model": settings.browser_default_model,
        "models": [
            {
                "id": "gemini",
                "label": "Gemini computer use",
                "model_id": settings.browser_gemini_model,
            },
            {
                "id": "claude",
                "label": "Claude computer use",
                "model_id": settings.browser_claude_model,
            },
        ],
        "presence_timeout_s": settings.browser_presence_timeout_s,
        "can_supervise": principal.can("browser_supervise"),
        "is_admin": principal.can("admin"),
    }


@router.get("/runs")
async def list_runs(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    rows = await session.scalars(
        select(BrowserRun)
        .where(BrowserRun.workspace_id == principal.workspace_id)
        .order_by(BrowserRun.created_at.desc())
        .limit(30)
    )
    return [service.run_view(r, settings) for r in rows]


@router.post("/runs")
async def start_run(
    body: StartIn,
    request: Request,
    principal: Principal = Depends(require("browser_supervise")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    run_loop: service.BrowserRunner = Depends(runner),
):
    if not request.app.state.browser_runtime.configured:
        raise ApiError(
            503, "browser_not_configured", "The browser runtime isn't set up in this environment."
        )
    run = await service.start_run(
        session,
        principal,
        settings,
        task=body.task,
        start_url=body.start_url,
        model=body.model,
        supervised=body.supervised,
        extra_hosts=body.extra_hosts,
        allow_reading=body.allow_reading,
    )
    await session.commit()
    run_loop.spawn(run.id)
    return service.run_view(run, settings)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await service.get_run(session, principal, run_id)
    steps = list(
        await session.scalars(
            select(BrowserStep).where(BrowserStep.run_id == run.id).order_by(BrowserStep.seq)
        )
    )
    box = PhiBox(settings)
    mine = run.supervisor_id == principal.user_id
    views = [service.step_view(s, box, with_values=mine) for s in steps]
    pending = next((v for v in views if v["status"] == "proposed"), None)
    return {
        **service.run_view(run, settings),
        "steps": views,
        "pending": pending,
        "is_supervisor": mine,
    }


@router.get("/runs/{run_id}/screen")
async def live_screen(
    run_id: str,
    request: Request,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Live view (JPEG). The supervisor's view polling this is what counts as watching."""
    run = await service.get_run(session, principal, run_id)
    if run.state in service.ENDED or not run.runtime_session:
        raise ApiError(409, "no_screen", "This run has no live screen.")
    await service.record_presence(session, principal, run, settings.browser_presence_timeout_s)
    await session.commit()
    try:
        shot = await request.app.state.browser_runtime.screenshot(run.runtime_session, "jpeg")
    except RuntimeUnavailable as exc:
        raise ApiError(409, "no_screen", str(exc)) from exc
    import base64

    return Response(
        base64.b64decode(shot["image"]),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Page-Url": shot.get("url", "")[:500]},
    )


@router.get("/runs/{run_id}/steps/{step_id}/screenshot")
async def step_screenshot(
    run_id: str,
    step_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Evidence screenshot of a step. Contains PHI: every opening is audited."""
    run = await service.get_run(session, principal, run_id)
    step = await session.get(BrowserStep, step_id)
    if step is None or step.run_id != run.id:
        raise ApiError(404, "not_found", "Step not found.")
    image = service.card_image(step, PhiBox(settings))
    if image is None:
        raise ApiError(404, "not_found", "No screenshot for this step.")
    service.audit(
        session,
        run.workspace_id,
        principal.user_id,
        "browser.evidence_opened",
        step.id,
        run_id=run.id,
    )
    await session.commit()
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.post("/runs/{run_id}/plan")
async def decide_plan(
    run_id: str,
    body: PlanIn,
    principal: Principal = Depends(require("browser_supervise")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await service.get_run(session, principal, run_id)
    await service.decide_plan(session, principal, run, body.decision, body.steps)
    await session.commit()
    return service.run_view(run, settings)


@router.post("/runs/{run_id}/steps/{step_id}/decision")
async def decide_step(
    run_id: str,
    step_id: str,
    body: StepDecisionIn,
    principal: Principal = Depends(require("browser_supervise")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await service.get_run(session, principal, run_id)
    step = await session.get(BrowserStep, step_id)
    if step is None or step.run_id != run.id:
        raise ApiError(404, "not_found", "Step not found.")
    box = PhiBox(settings)
    await service.decide_step(
        session,
        principal,
        run,
        step,
        box,
        decision=body.decision,
        text=body.text,
        feedback=body.feedback,
    )
    await session.commit()
    return service.step_view(step, box, with_values=True)


@router.post("/runs/{run_id}/control")
async def control(
    run_id: str,
    body: ControlIn,
    request: Request,
    principal: Principal = Depends(require("browser_supervise")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
    run_loop: service.BrowserRunner = Depends(runner),
):
    run = await service.get_run(session, principal, run_id)
    await service.control(session, principal, run, body.action)
    await session.commit()
    if body.action == "stop" and run.runtime_session and run_id not in run_loop._tasks:  # noqa: SLF001
        await request.app.state.browser_runtime.close(run.runtime_session)
    return service.run_view(run, settings)


@router.post("/runs/{run_id}/input")
async def human_input(
    run_id: str,
    body: InputIn,
    request: Request,
    principal: Principal = Depends(require("browser_supervise")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    run = await service.get_run(session, principal, run_id)
    try:
        result = await service.human_input(
            session, principal, run, request.app.state.browser_runtime, body.model_dump()
        )
    except RuntimeUnavailable as exc:
        raise ApiError(409, "runtime_unavailable", str(exc)) from exc
    await service.record_presence(session, principal, run, settings.browser_presence_timeout_s)
    await session.commit()
    return result


@router.get("/approvals")
async def approvals(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """The Approvals inbox: plans and steps waiting for a person, oldest first."""
    box = PhiBox(settings)
    runs = {
        r.id: r
        for r in await session.scalars(
            select(BrowserRun).where(
                BrowserRun.workspace_id == principal.workspace_id,
                BrowserRun.state.in_(("awaiting_plan", "awaiting_approval", "paused")),
            )
        )
    }
    items: list[dict[str, Any]] = []
    now = utcnow()
    for run in runs.values():
        if run.state == "awaiting_plan":
            items.append(
                {
                    "type": "plan",
                    "run_id": run.id,
                    "task": run.task,
                    "site": service.host_of(run.start_url),
                    "title": f"Approve the plan ({len(run.plan)} steps)",
                    "risk": None,
                    "waiting_s": round((now - as_utc(run.created_at)).total_seconds()),
                    "mine": run.supervisor_id == principal.user_id,
                }
            )
    if runs:
        for step in await session.scalars(
            select(BrowserStep)
            .where(BrowserStep.run_id.in_(list(runs)), BrowserStep.status == "proposed")
            .order_by(BrowserStep.created_at)
        ):
            run = runs[step.run_id]
            view = service.step_view(step, box, with_values=False)
            items.append(
                {
                    "type": "step",
                    "run_id": run.id,
                    "step_id": step.id,
                    "task": run.task,
                    "site": service.host_of(run.start_url),
                    "title": view["description"],
                    "risk": step.risk,
                    "waiting_s": round((now - as_utc(step.created_at)).total_seconds()),
                    "mine": run.supervisor_id == principal.user_id,
                }
            )
    items.sort(key=lambda i: -i["waiting_s"])
    return {"items": items}


@router.get("/sites")
async def list_sites(
    principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    rows = await session.scalars(
        select(BrowserSite).where(BrowserSite.workspace_id == principal.workspace_id)
    )
    return [
        {
            "host": s.host,
            "terms_note": s.terms_note,
            "acknowledged_at": s.acknowledged_at,
            "automation_permitted": s.automation_permitted,
            "always_allow": s.always_allow or [],
        }
        for s in rows
    ]


@router.post("/sites/acknowledge")
async def acknowledge_site(
    body: AckIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    """One-time, audited: the admin has read the site's terms and accepts supervised use."""
    host = body.host.strip().lower()
    site = await service.site_row(session, principal.workspace_id, host)
    if site is None:
        site = BrowserSite(workspace_id=principal.workspace_id, host=host)
        session.add(site)
    site.terms_note = (body.terms_note or "").strip() or None
    site.acknowledged_by, site.acknowledged_at = principal.user_id, utcnow()
    site.automation_permitted = body.automation_permitted
    service.audit(
        session,
        principal.workspace_id,
        principal.user_id,
        "browser.site_acknowledged",
        host,
        automation_permitted=body.automation_permitted,
    )
    await session.commit()
    return {"host": host, "acknowledged_at": site.acknowledged_at}


@router.delete("/profiles/{host}")
async def forget_sign_in(
    host: str, principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    await session.execute(
        delete(BrowserProfile).where(
            BrowserProfile.workspace_id == principal.workspace_id,
            BrowserProfile.user_id == principal.user_id,
            BrowserProfile.host == host.lower(),
        )
    )
    service.audit(
        session, principal.workspace_id, principal.user_id, "browser.sign_in_forgotten", host
    )
    await session.commit()
    return {"ok": True}
