"""Shadow mode: run CarePlix Voice on an incumbent vendor's worklist and compare, side by side.

A customer exports the work its current vendor (SuperDial, Infinitus and so on) already did: one
row per call, with the case facts and the answers that vendor recorded. A shadow run imports that
CSV, works every row again with a CarePlix agent, and compares the answers field by field. People
then settle each disagreement (who was right), and the run's summary becomes the parity report a
customer takes to its steering committee before switching.

Each row is worked like a regression-suite case (evals.service): an ordinary Voice Studio call on
the run's scenario, pinned to one agent version, with the row's own facts in place of the
scenario's. ``replay`` rows are simulations whose AI counterpart answers from the incumbent's
record; ``live`` rows are real calls to approved numbers, one at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import agents_service, calls_service, connections, templates
from careplix_api.auth import Principal
from careplix_api.db import Database, as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.models import (
    Agent,
    AgentVersion,
    AuditEvent,
    CallAttempt,
    Finding,
    Scenario,
    Task,
)
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings
from careplix_api.shadow import compare
from careplix_api.shadow.models import ShadowItem, ShadowRun

log = logging.getLogger("careplix.shadow")

ACTIVE = ("queued", "running")
ENDED = ("Ended", "Failed", "Cancelled")
MODES = ("replay", "live")
MAX_ITEMS = 200
MAX_CSV_CHARS = 1_000_000
MAX_FACTS = 20
_TRUE = {"1", "true", "yes", "y", "resolved", "complete", "completed", "success", "done"}
_FALSE = {"0", "false", "no", "n", "unresolved", "incomplete", "failed", "fail", "open"}


# --- CSV import -----------------------------------------------------------------------------------


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    if len(text) > MAX_CSV_CHARS:
        raise ApiError(413, "csv_too_large", "Keep the file under 1 MB; split larger worklists.")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = [c.strip() for c in reader.fieldnames or [] if c and c.strip()]
    if not columns:
        raise ApiError(422, "csv_empty", "The file has no header row.")
    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ApiError(422, "csv_empty", "The file has a header row but no data rows.")
    return columns, rows


def preview(text: str) -> dict[str, Any]:
    columns, rows = parse_csv(text)
    return {"columns": columns, "sample": rows[:5], "total": len(rows), "max_items": MAX_ITEMS}


def _seconds(raw: str) -> int | None:
    """ "184", "184s", "3:04" or "3m 4s" → seconds."""
    raw = raw.strip().lower()
    if not raw:
        return None
    if m := re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", raw):
        parts = [int(p) for p in m.groups() if p is not None]
        return (
            parts[0] * 60 + parts[1]
            if len(parts) == 2
            else parts[0] * 3600 + parts[1] * 60 + parts[2]
        )
    m = re.fullmatch(r"(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?", raw)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    with contextlib.suppress(ValueError):
        return max(0, int(float(raw)))
    return None


def _flag(raw: str) -> bool | None:
    word = raw.strip().lower()
    return True if word in _TRUE else False if word in _FALSE else None


def _fact_name(name: str) -> str:
    return "_".join(name.strip().lower().split())[:40]


# --- creating, cancelling, settling ---------------------------------------------------------------


async def create(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    name: str,
    incumbent: str,
    agent_id: str,
    use: str,
    scenario_id: str,
    mode: str,
    csv_text: str,
    mapping: dict[str, Any],
) -> ShadowRun:
    if mode not in MODES:
        raise ApiError(422, "bad_mode", "Choose replay or live.")
    agent = await agents_service.get_agent(session, principal, agent_id)
    template = templates.get_template(agent.template_id)
    if template["direction"] != "outbound":
        raise ApiError(422, "agent_not_outbound", "Choose an agent that places calls.")
    version = await (
        agents_service.latest_draft(session, agent)
        if use == "draft"
        else agents_service.current_version(session, agent)
    )
    if version is None:
        raise ApiError(
            409,
            "no_version",
            "This agent has no draft." if use == "draft" else "Publish the agent first.",
        )
    if mode == "live" and version.state != "published":
        raise ApiError(422, "live_needs_published", "Live shadow calls use the published version.")
    scenario = await session.get(Scenario, scenario_id)
    if scenario is None or scenario.workspace_id != principal.workspace_id:
        raise ApiError(
            404, "scenario_not_found", "Choose a Voice Studio scenario as the call plan."
        )

    values_map: dict[str, str] = {k: v for k, v in (mapping.get("values") or {}).items() if v}
    if not values_map:
        raise ApiError(422, "no_fields", "Map at least one of the incumbent's answer columns.")
    unknown = [f for f in values_map if f not in template["output_fields"]]
    if unknown:
        raise ApiError(
            422,
            "unknown_field",
            f"{', '.join(unknown)} isn't something this agent records. "
            f"Choose from: {', '.join(template['output_fields'])}.",
        )
    facts_map = {_fact_name(k): v for k, v in (mapping.get("facts") or {}).items() if v and k}
    if len(facts_map) > MAX_FACTS:
        raise ApiError(422, "too_many_facts", f"Share at most {MAX_FACTS} facts per call.")
    ref_col = mapping.get("external_ref") or ""
    dest_col = mapping.get("destination") or ""
    if mode == "live" and not dest_col:
        raise ApiError(422, "no_destination", "Live shadow calls need a phone-number column.")

    columns, rows = parse_csv(csv_text)
    used = [ref_col, dest_col, mapping.get("duration_s"), mapping.get("resolved")]
    missing = sorted(
        {c for c in [*used, *facts_map.values(), *values_map.values()] if c and c not in columns}
    )
    if missing:
        raise ApiError(422, "unknown_column", f"The file has no column named {', '.join(missing)}.")
    if len(rows) > MAX_ITEMS:
        raise ApiError(422, "too_many_rows", f"A shadow run takes at most {MAX_ITEMS} rows.")

    eff = await connections.load(session, principal.workspace_id, settings)
    box = PhiBox(settings)
    run = ShadowRun(
        workspace_id=principal.workspace_id,
        name=name.strip()[:120] or "Shadow run",
        incumbent=incumbent.strip()[:80] or "Current vendor",
        agent_id=agent.id,
        version_id=version.id,
        version_number=version.number,
        version_state=version.state,
        scenario_id=scenario.id,
        mode=mode,
        fields=list(values_map),
        created_by=principal.user_id,
    )
    session.add(run)
    await session.flush()
    for seq, row in enumerate(rows, 1):
        destination = None
        if dest_col and row.get(dest_col):
            destination = calls_service.normalize_e164(row[dest_col])
        if mode == "live":
            if destination is None:
                raise ApiError(422, "no_destination", f"Row {seq} has no phone number.")
            if destination not in eff.approved_destinations:
                raise ApiError(
                    422,
                    "destination_not_approved",
                    f"Row {seq} calls {calls_service.mask_number(destination)}, which isn't an "
                    "approved number. Add it under Connections first.",
                )
        session.add(
            ShadowItem(
                run_id=run.id,
                workspace_id=run.workspace_id,
                seq=seq,
                external_ref=(row.get(ref_col) or f"row-{seq}")[:80],
                destination_e164=destination,
                facts=box.seal({name: row.get(col, "")[:300] for name, col in facts_map.items()}),
                incumbent_values=box.seal(
                    {field: row.get(col, "")[:500] for field, col in values_map.items()}
                ),
                incumbent_metrics={
                    "duration_s": _seconds(row.get(mapping.get("duration_s") or "", "")),
                    "resolved": _flag(row.get(mapping.get("resolved") or "", "")),
                },
            )
        )
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="shadow.created",
            target=agent.id,
            detail={"run_id": run.id, "incumbent": run.incumbent, "mode": mode, "rows": len(rows)},
        )
    )
    return run


async def get_run(session: AsyncSession, principal: Principal, run_id: str) -> ShadowRun:
    run = await session.get(ShadowRun, run_id)
    if run is None or run.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Shadow run not found.")
    return run


async def items(session: AsyncSession, run: ShadowRun) -> list[ShadowItem]:
    return list(
        await session.scalars(
            select(ShadowItem).where(ShadowItem.run_id == run.id).order_by(ShadowItem.seq)
        )
    )


async def cancel(
    session: AsyncSession, principal: Principal, gateway: LiveKitGateway, run: ShadowRun
) -> None:
    if run.state not in ACTIVE:
        return
    for item in await items(session, run):
        if item.state not in ACTIVE:
            continue
        if item.state == "running" and item.attempt_id:
            attempt = await session.get(CallAttempt, item.attempt_id)
            if attempt is not None:
                with contextlib.suppress(ApiError):  # already ending
                    await calls_service.end_by_operator(session, principal, gateway, attempt)
        item.state, item.finished_at = "cancelled", utcnow()
    run.state, run.finished_at = "cancelled", utcnow()
    run.summary = summary(run, await items(session, run))
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="shadow.cancelled",
            target=run.agent_id,
            detail={"run_id": run.id},
        )
    )


async def adjudicate(
    session: AsyncSession,
    principal: Principal,
    run: ShadowRun,
    item_id: str,
    field: str,
    verdict: str | None,
) -> ShadowItem:
    """A person settles one disagreement: whose answer was right."""
    if verdict is not None and verdict not in compare.ADJUDICATIONS:
        raise ApiError(422, "bad_verdict", "Choose ours, theirs, both or neither.")
    item = await session.get(ShadowItem, item_id)
    if item is None or item.run_id != run.id:
        raise ApiError(404, "not_found", "That row isn't in this shadow run.")
    rows = [dict(r) for r in item.comparison or []]
    row = next((r for r in rows if r["field"] == field), None)
    if row is None or row["outcome"] not in ("mismatch", "ours_only", "theirs_only"):
        raise ApiError(422, "nothing_to_settle", "The two answers for that field don't differ.")
    row["adjudication"] = verdict
    item.comparison = rows  # reassign: JSON columns don't track in-place edits
    if run.state not in ACTIVE:
        run.summary = summary(run, await items(session, run))
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="shadow.adjudicated",
            target=run.id,
            detail={"item_id": item.id, "field": field, "verdict": verdict},
        )
    )
    return item


# --- the runner -----------------------------------------------------------------------------------


async def tick(
    db: Database, settings: Settings, gateway: LiveKitGateway, runner: calls_service.CallRunner
) -> None:
    """Advance every active run: follow running rows, start queued ones, finish complete runs."""
    async with db.sessions() as session:
        runs = list(
            await session.scalars(
                select(ShadowRun).where(ShadowRun.state.in_(ACTIVE)).order_by(ShadowRun.created_at)
            )
        )
        for run in runs:
            await _advance(session, settings, gateway, runner, run)
            await session.commit()


async def run_forever(
    db: Database, settings: Settings, gateway: LiveKitGateway, runner: calls_service.CallRunner
) -> None:
    while True:
        try:
            await tick(db, settings, gateway, runner)
        except Exception as exc:
            log.warning("shadow loop error", extra={"error": type(exc).__name__})
        await asyncio.sleep(settings.eval_interval_s)


async def _advance(
    session: AsyncSession,
    settings: Settings,
    gateway: LiveKitGateway,
    runner: calls_service.CallRunner,
    run: ShadowRun,
) -> None:
    rows = await items(session, run)
    for item in rows:
        if item.state == "running":
            await _follow(session, settings, run, item)

    if run.mode == "live":
        # Real calls go one at a time per run, inside the workspace's normal call limit.
        slots = 0 if any(i.state == "running" for i in rows) else 1
    else:
        running = await session.scalar(
            select(func.count())
            .select_from(ShadowItem)
            .where(ShadowItem.workspace_id == run.workspace_id, ShadowItem.state == "running")
        )
        slots = max(0, settings.eval_concurrency - (running or 0))
    for item in [i for i in rows if i.state == "queued"][:slots]:
        await _start_item(session, settings, gateway, runner, run, item)
        if item.state == "running":
            run.state = "running"

    if all(i.state not in ACTIVE for i in rows):
        run.state, run.finished_at = "done", utcnow()
        run.summary = summary(run, rows)


async def _start_item(
    session: AsyncSession,
    settings: Settings,
    gateway: LiveKitGateway,
    runner: calls_service.CallRunner,
    run: ShadowRun,
    item: ShadowItem,
) -> None:
    version = await session.get(AgentVersion, run.version_id)
    agent = await session.get(Agent, run.agent_id)
    scenario = await session.get(Scenario, run.scenario_id)
    if version is None or agent is None or scenario is None:
        _close(item, error="The agent version or scenario was deleted.")
        return
    principal = Principal(run.created_by, run.workspace_id, "author", "Shadow mode")
    item.started_at = utcnow()
    try:
        if run.mode == "replay":
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
                shadow_item_id=item.id,
            )
            item.attempt_id = started["attempt_id"]
        else:
            task = Task(
                workspace_id=run.workspace_id,
                kind="outbound",
                created_by=run.created_by,
                agent_id=agent.id,
                agent_version_id=version.id,
                destination_e164=item.destination_e164,
                objective=f"Shadow mode: {run.name}",
                scenario_id=scenario.id,
                state="Ready",
            )
            session.add(task)
            await session.flush()
            attempt = await calls_service.start_call(
                session,
                principal,
                settings,
                gateway,
                runner,
                task,
                f"shadow-{item.id}",
                extra_pins={"shadow_item_id": item.id},
            )
            item.attempt_id = attempt.id
    except ApiError as exc:
        if exc.code == "capacity_exhausted":
            item.started_at = None  # another call holds the line; try again next tick
            return
        _close(item, error=exc.message)
        return
    item.state = "running"


async def _follow(
    session: AsyncSession, settings: Settings, run: ShadowRun, item: ShadowItem
) -> None:
    attempt = await session.get(CallAttempt, item.attempt_id) if item.attempt_id else None
    if attempt is None:
        _close(item, error="The call record is missing.")
        return
    now = utcnow()
    if attempt.state not in ENDED:
        limit = (
            settings.eval_case_timeout_s
            if run.mode == "replay"
            else settings.max_call_duration_s + settings.max_hold_s + 600
        )
        if now - as_utc(item.started_at or now) > timedelta(seconds=limit):
            _close(item, error="The call didn't finish in time.")
        return
    if attempt.connected_at is None:
        _close(item, error="The call never connected.")
        return
    ours: dict[str, Any] = {}
    for f in await session.scalars(
        select(Finding).where(Finding.attempt_id == attempt.id).order_by(Finding.created_at)
    ):
        ours[f.field] = f.value  # latest wins
    box = PhiBox(settings)
    theirs = box.open(item.incumbent_values)
    picked = {k: ours.get(k) for k in run.fields}
    item.our_values = box.seal(picked)
    item.our_metrics = {
        "duration_s": int(
            (as_utc(attempt.ended_at or now) - as_utc(attempt.connected_at)).total_seconds()
        ),
        "resolved": compare.resolved(run.fields, picked),
    }
    item.comparison = compare.compare(run.fields, picked, theirs)
    _close(item)


def _close(item: ShadowItem, error: str | None = None) -> None:
    item.state, item.finished_at = "done", utcnow()
    item.error = (error or "")[:240] or None


# --- what the call is told ------------------------------------------------------------------------


async def item_for_attempt(session: AsyncSession, attempt: CallAttempt) -> ShadowItem | None:
    item_id = (attempt.pins or {}).get("shadow_item_id")
    if not item_id:
        return None
    item = await session.get(ShadowItem, item_id)
    return item if item and item.workspace_id == attempt.workspace_id else None


def item_facts(item: ShadowItem, box: PhiBox) -> dict[str, Any]:
    return box.open(item.facts)


def item_record(item: ShadowItem, box: PhiBox) -> dict[str, Any]:
    """In a replay, what the simulated other side's records say: the incumbent's answers."""
    return box.open(item.incumbent_values)


# --- views ----------------------------------------------------------------------------------------


def summary(run: ShadowRun, rows: list[ShadowItem]) -> dict[str, Any]:
    return compare.summarise(
        run.fields,
        [
            {
                "state": i.state,
                "comparison": i.comparison,
                "incumbent_metrics": i.incumbent_metrics or {},
                "our_metrics": i.our_metrics or {},
            }
            for i in rows
        ],
    )


def run_view(run: ShadowRun, rows: list[ShadowItem] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": run.id,
        "name": run.name,
        "incumbent": run.incumbent,
        "agent_id": run.agent_id,
        "version": {"number": run.version_number, "state": run.version_state},
        "scenario_id": run.scenario_id,
        "mode": run.mode,
        "fields": run.fields,
        "state": run.state,
        "summary": run.summary,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }
    if rows is not None:
        out["summary"] = run.summary or summary(run, rows)
    return out


def item_view(item: ShadowItem, box: PhiBox, principal: Principal) -> dict[str, Any]:
    show = principal.can("edit_draft")

    def values(token: str | None) -> dict[str, Any]:
        opened = box.open(token)
        return opened if show else dict.fromkeys(opened, "•••")

    return {
        "id": item.id,
        "seq": item.seq,
        "external_ref": item.external_ref,
        "destination": calls_service.mask_number(item.destination_e164),
        "state": item.state,
        "attempt_id": item.attempt_id,
        "theirs": values(item.incumbent_values),
        "ours": values(item.our_values),
        "incumbent_metrics": item.incumbent_metrics or {},
        "our_metrics": item.our_metrics or {},
        "comparison": item.comparison,
        "error": item.error,
    }


async def run_detail(
    session: AsyncSession, run: ShadowRun, settings: Settings, principal: Principal
) -> dict[str, Any]:
    rows = await items(session, run)
    box = PhiBox(settings)
    return {**run_view(run, rows), "items": [item_view(i, box, principal) for i in rows]}
