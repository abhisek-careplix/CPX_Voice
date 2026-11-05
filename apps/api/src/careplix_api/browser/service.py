"""Supervised browser runs: plan approval, approval cards, take-over and presence (§6a, §6b).

The loop (``BrowserRunner.drive``) runs inside the API process (the Studio service runs with CPU
always on). The supervisor's decisions arrive through the API and are written to the database;
the loop reads them. Nothing the agent proposes reaches the browser until the rules allow it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.browser import policy
from careplix_api.browser.adapters import Outcome, Shot, make_agent
from careplix_api.browser.models import BrowserProfile, BrowserRun, BrowserSite, BrowserStep
from careplix_api.browser.runtime_client import BrowserRuntime, RuntimeUnavailable
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.judge import service as judge_service
from careplix_api.models import AuditEvent
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings
from careplix_common.safe_logging import scrub

log = logging.getLogger("careplix.browser")

ENDED = ("done", "failed", "stopped")
LIVE = ("starting", "planning", "awaiting_plan", "running", "awaiting_approval", "paused")
MODELS = ("gemini", "claude")
POLL_S = 0.5


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _allowed(host: str, hosts: list[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def audit(session: AsyncSession, workspace_id: str, actor: str, action: str, target: str, **detail):
    session.add(
        AuditEvent(
            workspace_id=workspace_id, actor=actor, action=action, target=target, detail=detail
        )
    )


def model_ids(settings: Settings) -> dict[str, str]:
    return {"gemini": settings.browser_gemini_model, "claude": settings.browser_claude_model}


# --- starting a run --------------------------------------------------------------------------


async def site_row(session: AsyncSession, workspace_id: str, host: str) -> BrowserSite | None:
    return await session.scalar(
        select(BrowserSite).where(
            BrowserSite.workspace_id == workspace_id, BrowserSite.host == host
        )
    )


async def start_run(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    task: str,
    start_url: str,
    model: str | None,
    supervised: bool,
    extra_hosts: list[str],
    allow_reading: bool,
) -> BrowserRun:
    if not supervised:
        raise ApiError(
            422,
            "supervision_required",
            "Tick “Under human supervision”. Browser runs only start with a person watching.",
        )
    parts = urlsplit(start_url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ApiError(422, "bad_url", "Enter the site's full address, starting with https://")
    host = parts.hostname.lower()
    model = model or settings.browser_default_model
    if model not in MODELS:
        raise ApiError(422, "unknown_model", "Choose Gemini or Claude.")
    site = await site_row(session, principal.workspace_id, host)
    if site is None or site.acknowledged_at is None:
        raise ApiError(
            409,
            "site_terms_ack_needed",
            f"Before the first run on {host}, an admin acknowledges the site's terms once.",
            {"host": host},
        )
    running = await session.scalar(
        select(BrowserRun).where(
            BrowserRun.workspace_id == principal.workspace_id,
            BrowserRun.supervisor_id == principal.user_id,
            BrowserRun.state.in_(LIVE),
        )
    )
    if running is not None:
        raise ApiError(
            409,
            "run_active",
            "You're already supervising a run. Stop it first.",
            {"run_id": running.id},
        )
    hosts = [host] + [h.strip().lower() for h in extra_hosts if h.strip()]
    run = BrowserRun(
        workspace_id=principal.workspace_id,
        supervisor_id=principal.user_id,
        supervised=True,
        task=task.strip(),
        start_url=start_url.strip(),
        allowed_hosts=list(dict.fromkeys(hosts)),
        model=model,
        model_id=model_ids(settings)[model],
        state="starting",
        allow_for_run=[policy.READ] if allow_reading else [],
        last_presence_at=utcnow(),
    )
    session.add(run)
    await session.flush()
    audit(
        session,
        principal.workspace_id,
        principal.user_id,
        "browser.run_started",
        run.id,
        host=host,
        model=model,
        supervised=True,
    )
    return run


# --- views -----------------------------------------------------------------------------------


def step_view(step: BrowserStep, box: PhiBox, *, with_values: bool) -> dict[str, Any]:
    action = box.open(step.action) if with_values and step.action else {}
    return {
        "id": step.id,
        "seq": step.seq,
        "kind": step.kind,
        "risk": step.risk,
        "status": step.status,
        "description": step.description,
        "why": step.why,
        "target": step.target,
        "x": action.get("x"),
        "y": action.get("y"),
        "text": action.get("text") if step.kind == "type" else None,
        "url": action.get("url") if step.kind == "navigate" else None,
        "page_url": step.page_url,
        "page_title": step.page_title,
        "needs_approval_reason": step.needs_approval_reason,
        "has_screenshot": bool(step.screenshot),
        "decided_by": step.decided_by,
        "decided_at": step.decided_at,
        "feedback": step.feedback,
        "outcome": step.outcome,
        "created_at": step.created_at,
    }


def run_view(run: BrowserRun, settings: Settings) -> dict[str, Any]:
    away_s = None
    if run.last_presence_at is not None:
        away_s = max(0, round((utcnow() - as_utc(run.last_presence_at)).total_seconds()))
    return {
        "id": run.id,
        "task": run.task,
        "start_url": run.start_url,
        "allowed_hosts": run.allowed_hosts,
        "model": run.model,
        "model_id": run.model_id,
        "state": run.state,
        "control": run.control,
        "pause_reason": run.pause_reason,
        "plan": run.plan,
        "plan_approved": run.plan_approved_at is not None,
        "allow_for_run": run.allow_for_run,
        "result": run.result,
        "error": run.error,
        "step_count": run.step_count,
        "supervisor_id": run.supervisor_id,
        "supervised": run.supervised,
        "supervisor_idle_s": away_s,
        "presence_timeout_s": settings.browser_presence_timeout_s,
        "created_at": run.created_at,
        "ended_at": run.ended_at,
    }


async def get_run(session: AsyncSession, principal: Principal, run_id: str) -> BrowserRun:
    run = await session.get(BrowserRun, run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Browser run not found.")
    return run


def require_supervisor(run: BrowserRun, principal: Principal) -> None:
    if run.supervisor_id != principal.user_id:
        raise ApiError(403, "not_supervisor", "Only the run's supervisor can do that.")


# --- the supervisor's decisions --------------------------------------------------------------


async def decide_plan(
    session: AsyncSession, principal: Principal, run: BrowserRun, decision: str, steps: list[str]
) -> None:
    require_supervisor(run, principal)
    if run.state != "awaiting_plan":
        raise ApiError(409, "no_plan_waiting", "There's no plan waiting for approval.")
    if decision == "cancel":
        run.state, run.ended_at, run.result = "stopped", utcnow(), "Cancelled at the plan."
    else:
        if decision == "edit":
            cleaned = [s.strip()[:240] for s in steps if s.strip()][:12]
            if not cleaned:
                raise ApiError(422, "empty_plan", "A plan needs at least one step.")
            run.plan = cleaned
        run.plan_approved_at = utcnow()
        run.state = "running"
    audit(session, run.workspace_id, principal.user_id, f"browser.plan_{decision}", run.id)


DECISIONS = ("approve", "approve_similar", "always_allow", "edit", "deny", "take_over")


async def decide_step(
    session: AsyncSession,
    principal: Principal,
    run: BrowserRun,
    step: BrowserStep,
    box: PhiBox,
    *,
    decision: str,
    text: str | None,
    feedback: str | None,
) -> None:
    require_supervisor(run, principal)
    if step.run_id != run.id or step.status != "proposed":
        raise ApiError(409, "not_waiting", "This step isn't waiting for a decision.")
    if decision not in DECISIONS:
        raise ApiError(422, "bad_decision", "Unknown decision.")
    if step.risk == policy.PERSON and decision not in ("deny", "take_over"):
        raise ApiError(409, "person_only", "A person must do this step: take over, or deny it.")
    if decision in ("approve_similar", "always_allow") and step.risk not in policy.ALLOWABLE:
        raise ApiError(
            409, "commit_always_asks", "Steps that submit or change data are approved one by one."
        )
    if decision == "always_allow":
        if not principal.can("admin"):
            raise ApiError(403, "forbidden", "Only an admin can always allow on a site.")
        host = host_of(step.page_url or run.start_url)
        site = await site_row(session, run.workspace_id, host)
        if site is not None and step.risk not in (site.always_allow or []):
            site.always_allow = [*(site.always_allow or []), step.risk]
    if decision == "approve_similar" and step.risk not in run.allow_for_run:
        run.allow_for_run = [*run.allow_for_run, step.risk]
    if decision == "edit":
        if step.kind != "type" or text is None:
            raise ApiError(422, "edit_text_only", "Only typed text can be edited.")
        action = box.open(step.action)
        action["text"] = text[:2000]
        step.action = box.seal(action)
        step.description = policy.describe(action, step.target)
    step.decided_by, step.decided_at = principal.user_id, utcnow()
    step.feedback = (feedback or "").strip()[:600] or None
    if decision == "deny":
        step.status = "denied"
    elif decision == "take_over":
        step.status = "by_person"
        run.control, run.state, run.pause_reason = "human", "paused", "human_control"
        run.took_over = True
    else:
        step.status = "approved"
    audit(
        session,
        run.workspace_id,
        principal.user_id,
        f"browser.step_{decision}",
        step.id,
        run_id=run.id,
        risk=step.risk,
        kind=step.kind,
    )


async def control(
    session: AsyncSession, principal: Principal, run: BrowserRun, action: str
) -> None:
    require_supervisor(run, principal)
    if run.state in ENDED:
        raise ApiError(409, "run_ended", "This run has ended.")
    if action == "stop":
        run.state, run.ended_at = "stopped", utcnow()
        run.result = run.result or "Stopped by the supervisor."
    elif action == "pause":
        run.state, run.pause_reason = "paused", "by_supervisor"
    elif action == "resume":
        if run.control == "human":
            raise ApiError(409, "you_have_control", "Hand control back to the agent first.")
        run.pause_reason = None
        run.state = "running" if run.plan_approved_at else "awaiting_plan"
    elif action == "take_over":
        run.control, run.state, run.pause_reason = "human", "paused", "human_control"
        run.took_over = True
    elif action == "hand_back":
        run.control, run.pause_reason = "agent", None
        run.state = "running" if run.plan_approved_at else "awaiting_plan"
    else:
        raise ApiError(422, "bad_action", "Unknown control action.")
    audit(session, run.workspace_id, principal.user_id, f"browser.{action}", run.id)


async def record_presence(
    session: AsyncSession, principal: Principal, run: BrowserRun, timeout_s: float
) -> None:
    """The supervisor is watching. Writes are throttled, but always well inside the away timeout:
    a 5 s throttle against a shorter timeout would leave a returning supervisor marked away."""
    if run.supervisor_id != principal.user_id or run.state in ENDED:
        return
    now = utcnow()
    every = timedelta(seconds=min(5.0, timeout_s / 4))
    if run.last_presence_at is None or now - as_utc(run.last_presence_at) > every:
        run.last_presence_at = now


async def human_input(
    session: AsyncSession,
    principal: Principal,
    run: BrowserRun,
    runtime: BrowserRuntime,
    body: dict[str, Any],
) -> dict[str, Any]:
    """The supervisor drives the browser themselves (take-over). Typed text is never stored."""
    require_supervisor(run, principal)
    if run.control != "human" or run.state in ENDED or not run.runtime_session:
        raise ApiError(409, "not_in_control", "Take over first.")
    kind = body.get("kind")
    if kind not in ("click", "type", "key", "scroll", "back", "navigate"):
        raise ApiError(422, "bad_input", "Unsupported input.")
    action = {k: body.get(k) for k in ("kind", "x", "y", "text", "direction", "amount", "url")}
    action = {k: v for k, v in action.items() if v is not None}
    if kind == "navigate":
        host = host_of(str(body.get("url", "")))
        if host and not _allowed(host, run.allowed_hosts):
            run.allowed_hosts = [*run.allowed_hosts, host]
            await runtime.allow(run.runtime_session, [host])
    result = await runtime.act(run.runtime_session, action)
    seq = await next_seq(session, run.id)
    described = (
        f"Supervisor typed {len(str(body.get('text', '')))} characters"
        if kind == "type"
        else f"Supervisor: {policy.describe(action, None).lower()}"
    )
    session.add(
        BrowserStep(
            workspace_id=run.workspace_id,
            run_id=run.id,
            seq=seq,
            kind=kind,
            risk="read",
            status="by_person",
            description=described,
            page_url=result.get("url"),
            page_title=(result.get("title") or "")[:300],
            decided_by=principal.user_id,
            decided_at=utcnow(),
            outcome="done" if result.get("ok") else str(result.get("error"))[:300],
        )
    )
    return {"ok": bool(result.get("ok")), "error": result.get("error")}


async def next_seq(session: AsyncSession, run_id: str) -> int:
    last = await session.scalar(
        select(BrowserStep.seq).where(BrowserStep.run_id == run_id).order_by(BrowserStep.seq.desc())
    )
    return (last or 0) + 1


# --- saved sign-ins ----------------------------------------------------------------------------


async def load_profile(session: AsyncSession, run: BrowserRun, box: PhiBox) -> dict | None:
    row = await session.scalar(
        select(BrowserProfile).where(
            BrowserProfile.workspace_id == run.workspace_id,
            BrowserProfile.user_id == run.supervisor_id,
            BrowserProfile.host == host_of(run.start_url),
        )
    )
    if row is None or as_utc(row.expires_at) < utcnow():
        return None
    return box.open(row.state).get("state")


async def save_profile(
    session: AsyncSession, run: BrowserRun, box: PhiBox, state: dict, days: int
) -> None:
    host = host_of(run.start_url)
    row = await session.scalar(
        select(BrowserProfile).where(
            BrowserProfile.workspace_id == run.workspace_id,
            BrowserProfile.user_id == run.supervisor_id,
            BrowserProfile.host == host,
        )
    )
    if row is None:
        row = BrowserProfile(workspace_id=run.workspace_id, user_id=run.supervisor_id, host=host)
        session.add(row)
    row.state = box.seal({"state": state}) or ""
    row.updated_at = utcnow()
    row.expires_at = utcnow() + timedelta(days=days)


# --- the run loop ------------------------------------------------------------------------------


class RunEnded(Exception):
    pass


class BrowserRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        runtime: BrowserRuntime,
        agent_factory: Callable | None = None,
    ):
        self.db, self.settings, self.runtime = db, settings, runtime
        self.agent_factory = agent_factory or make_agent
        self._box: PhiBox | None = None
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def box(self) -> PhiBox:
        if self._box is None:  # built on first use: needs the data key outside dev
            self._box = PhiBox(self.settings)
        return self._box

    def spawn(self, run_id: str) -> None:
        task = asyncio.create_task(self.drive(run_id))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def recover(self) -> None:
        """Runs that were live when the service restarted can't continue (their browser is gone)."""
        async with self.db.sessions() as session:
            for run in await session.scalars(select(BrowserRun).where(BrowserRun.state.in_(LIVE))):
                run.state, run.ended_at = "failed", utcnow()
                run.error = "Interrupted by a service restart. Start the run again."
            await session.commit()

    async def _load(self, run_id: str) -> BrowserRun:
        async with self.db.sessions() as session:
            run = await session.get(BrowserRun, run_id)
            if run is None:
                raise RunEnded()
            session.expunge(run)
            return run

    async def _update(self, run_id: str, **fields) -> None:
        async with self.db.sessions() as session:
            run = await session.get(BrowserRun, run_id)
            if run is None or run.state in ENDED:
                raise RunEnded()
            for k, v in fields.items():
                setattr(run, k, v)
            await session.commit()

    async def _shot(self, sid: str) -> Shot:
        s = await self.runtime.screenshot(sid, "png")
        return Shot(s["image"], s.get("url", ""), s.get("width", 1280), s.get("height", 800))

    async def _gate(self, run_id: str) -> tuple[BrowserRun, bool]:
        """Wait until the agent may act: not paused, agent in control, supervisor watching."""
        took_over = False
        timeout = timedelta(seconds=self.settings.browser_presence_timeout_s)
        while True:
            run = await self._load(run_id)
            if run.state in ENDED:
                raise RunEnded()
            if run.control == "human":
                took_over = True
                await asyncio.sleep(POLL_S)
                continue
            if run.state == "paused" and run.pause_reason in ("by_supervisor", "left_sites"):
                await asyncio.sleep(POLL_S)
                continue
            away = run.last_presence_at is None or utcnow() - as_utc(run.last_presence_at) > timeout
            if away:
                if run.state != "paused":
                    await self._update(run_id, state="paused", pause_reason="supervisor_away")
                await asyncio.sleep(POLL_S)
                continue
            took_over = took_over or bool(run.took_over)
            if run.state != "running" or run.took_over:
                await self._update(run_id, state="running", pause_reason=None, took_over=False)
            return run, took_over

    async def _wait_step(self, step_id: str, run_id: str) -> BrowserStep:
        while True:
            async with self.db.sessions() as session:
                step = await session.get(BrowserStep, step_id)
                run = await session.get(BrowserRun, run_id)
                if run is None or run.state in ENDED:
                    raise RunEnded()
                if step is not None and step.status != "proposed":
                    session.expunge(step)
                    return step
            await asyncio.sleep(POLL_S)

    async def drive(self, run_id: str) -> None:
        sid: str | None = None
        run = await self._load(run_id)
        try:
            async with self.db.sessions() as session:
                state = await load_profile(session, run, self.box)
                target = await judge_service.model_target(session, run.workspace_id, self.settings)
            agent = self.agent_factory(run.model, target, self.settings, run.task)
            sid = await self.runtime.open(run.start_url, run.allowed_hosts, state)
            await self._update(run_id, runtime_session=sid, state="planning")
            shot = await self._shot(sid)
            plan = await agent.plan(run.task, shot)
            await self._update(run_id, plan=plan or [run.task], state="awaiting_plan")
            while True:  # wait for the plan decision
                run = await self._load(run_id)
                if run.state in ENDED:
                    raise RunEnded()
                if run.plan_approved_at is not None:
                    break
                await asyncio.sleep(POLL_S)
            agent.use_plan(run.plan)  # the approved, possibly edited, plan
            await self._loop(run_id, sid, agent)
        except RunEnded:
            pass
        except judge_service.JudgeNotConfigured as exc:
            await self._fail(run_id, str(exc))
        except RuntimeUnavailable as exc:
            await self._fail(run_id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("browser run failed", extra={"run_id": run_id, "error": type(exc).__name__})
            await self._fail(run_id, scrub(f"{type(exc).__name__}: {exc}")[:300])
        finally:
            if sid:
                await self._finish_session(run_id, sid)

    async def _fail(self, run_id: str, message: str) -> None:
        with contextlib.suppress(RunEnded):
            await self._update(run_id, state="failed", error=message, ended_at=utcnow())

    async def _finish_session(self, run_id: str, sid: str) -> None:
        with contextlib.suppress(Exception):
            state = await self.runtime.storage_state(sid)
            async with self.db.sessions() as session:
                run = await session.get(BrowserRun, run_id)
                if run is not None and state.get("cookies"):
                    await save_profile(
                        session, run, self.box, state, self.settings.browser_profile_days
                    )
                await session.commit()
        await self.runtime.close(sid)

    async def _loop(self, run_id: str, sid: str, agent) -> None:
        outcomes: list[Outcome] = []
        note: str | None = None
        while True:
            run, took_over = await self._gate(run_id)
            if took_over:
                note = (
                    "The supervisor took control and may have changed the page. "
                    "Look at the current screen and continue the task."
                )
            if run.step_count >= self.settings.browser_max_steps:
                await self._fail(run_id, "Stopped at the step limit. Review the steps and retry.")
                return
            shot = await self._shot(sid)
            turn = await agent.next(shot, outcomes, note)
            outcomes, note = [], None
            if turn.error:
                await self._fail(run_id, turn.error)
                return
            if turn.done:
                await self._update(
                    run_id,
                    state="done",
                    result=(turn.result or "Finished.")[:4000],
                    ended_at=utcnow(),
                )
                return
            stopped = False
            for proposed in turn.actions:
                if stopped:
                    outcomes.append(
                        Outcome(proposed.call_id, False, "Not executed: an earlier action stopped.")
                    )
                    continue
                outcome, stopped = await self._handle(run_id, sid, proposed, turn.note)
                outcomes.append(outcome)

    async def _handle(self, run_id: str, sid: str, proposed, why: str | None):
        if proposed.kind == "screenshot":
            return Outcome(proposed.call_id, True, "OK"), False
        if proposed.kind == "unsupported":
            return Outcome(proposed.call_id, False, "That action isn't available here."), False

        action = proposed.as_runtime()
        element = None
        if proposed.kind in ("click", "double_click", "triple_click", "right_click", "type", "key"):
            element = await self.runtime.inspect(sid, action.get("x"), action.get("y"))
        verdict = policy.classify(action, element)
        forced = proposed.needs_confirmation
        new_host = None
        run = await self._load(run_id)
        if proposed.kind == "navigate":
            new_host = host_of(str(action.get("url", "")))
            if new_host and not _allowed(new_host, run.allowed_hosts):
                forced = f"Opens a site outside this run: {new_host}"
        async with self.db.sessions() as session:
            site = await site_row(session, run.workspace_id, host_of(run.start_url))
            allowed = set(run.allow_for_run) | set((site.always_allow if site else None) or [])
            card = await self.runtime.screenshot(sid, "jpeg")
            ask = policy.needs_approval(verdict.risk, allowed, forced)
            step = BrowserStep(
                workspace_id=run.workspace_id,
                run_id=run_id,
                seq=await next_seq(session, run_id),
                kind=proposed.kind,
                risk=verdict.risk,
                status="proposed" if ask else "auto_approved",
                description=policy.describe(action, element)[:400],
                why=(why or "")[:2000] or None,
                target=element,
                action=self.box.seal(action),
                screenshot=self.box.seal({"jpeg": card["image"]}),
                page_url=card.get("url"),
                page_title=(card.get("title") or "")[:300],
                needs_approval_reason=(forced or verdict.reason or None) if ask else None,
            )
            session.add(step)
            run_row = await session.get(BrowserRun, run_id)
            if ask and run_row is not None:
                run_row.state = "awaiting_approval"
            await session.commit()
            step_id = step.id

        acknowledged = False
        if ask:
            decided = await self._wait_step(step_id, run_id)
            if decided.status == "denied":
                message = "The supervisor denied this action."
                if decided.feedback:
                    message += f" Their feedback: {decided.feedback}"
                await self._set_running(run_id)
                return Outcome(proposed.call_id, False, message), True
            if decided.status == "by_person":
                return (
                    Outcome(
                        proposed.call_id, False, "The supervisor is doing this step themselves."
                    ),
                    True,
                )
            action = self.box.open(decided.action)  # an edit may have changed the text
            acknowledged = bool(proposed.needs_confirmation)
            await self._set_running(run_id)
            if new_host:
                run = await self._load(run_id)
                if not _allowed(new_host, run.allowed_hosts):
                    await self._update(run_id, allowed_hosts=[*run.allowed_hosts, new_host])
                    await self.runtime.allow(sid, [new_host])

        result = await self.runtime.act(sid, action)
        async with self.db.sessions() as session:
            step = await session.get(BrowserStep, step_id)
            run_row = await session.get(BrowserRun, run_id)
            if step is not None:
                step.status = "executed" if result.get("ok") else "failed"
                step.outcome = (
                    "done" if result.get("ok") else str(result.get("error") or "failed")
                )[:300]
            if run_row is not None:
                run_row.step_count += 1
                if result.get("left_allowed_sites") and run_row.state not in ENDED:
                    run_row.state, run_row.pause_reason = "paused", "left_sites"
            await session.commit()
        if not result.get("ok"):
            return Outcome(proposed.call_id, False, str(result.get("error") or "failed")), True
        return Outcome(proposed.call_id, True, "OK", acknowledged=acknowledged), False

    async def _set_running(self, run_id: str) -> None:
        with contextlib.suppress(RunEnded):
            run = await self._load(run_id)
            if run.state == "awaiting_approval":
                await self._update(run_id, state="running")


def card_image(step: BrowserStep, box: PhiBox) -> bytes | None:
    if not step.screenshot:
        return None
    return base64.b64decode(box.open(step.screenshot).get("jpeg", ""))
