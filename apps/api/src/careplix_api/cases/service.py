"""Case queries, creation and the plan for working a case (which agent, which number)."""

from __future__ import annotations

import json
import random
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api import agents_service, connections
from careplix_api.auth import Principal
from careplix_api.cases import types
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Agent, AuditEvent, Case, Task
from careplix_api.settings import Settings

STATUSES = ("open", "in_progress", "done", "closed")
SOURCES = ("sample", "manual", "import", "care_opportunity", "backfill", "inbound", "api")
SOURCE_LABEL = {
    "sample": "Synthetic sample",
    "manual": "Entered by hand",
    "import": "Worklist import",
    "care_opportunity": "Care opportunity",
    "backfill": "Cancellation backfill",
    "inbound": "Inbound call",
    "api": "API",
}
# Task outcomes that finish the work; the others leave the case open for another try.
DONE_OUTCOMES = ("Completed",)
OPEN_TASK_STATES = ("Draft", "Ready", "Running", "Review")


def status_of(case: Case) -> str:
    return case.status or "open"


def _headline(case: Case) -> str:
    t = types.get(cases.type_id(case))
    parts = [types.value(case.data, p) for p in t["headline"]]
    return " · ".join(str(p) for p in parts if p not in (None, ""))


def row(case: Case, tasks: list[Task] | None = None) -> dict[str, Any]:
    tid = cases.type_id(case)
    t = types.get(tid)
    tasks = tasks or []
    last = max(tasks, key=lambda x: x.created_at) if tasks else None
    return {
        "id": case.id,
        "case_ref": case.case_ref,
        "case_type": tid,
        "type_name": t["name"],
        "program": t["program"],
        "status": status_of(case),
        "source": case.source or "sample",
        "source_label": SOURCE_LABEL.get(case.source or "sample", case.source),
        "synthetic": case.synthetic,
        "subject": types.value(case.data, t["subject"]),
        "headline": _headline(case),
        "counterparty": {
            "role": t["counterparty"]["role"],
            "label": t["counterparty"]["label"],
            "name": types.value(case.data, t["counterparty"]["name"]),
        },
        "missing": types.missing(tid, case.data),
        "context": cases.context(case),
        "task_count": len(tasks),
        "last_task_state": last.state if last else None,
        "created_at": as_utc(case.created_at).isoformat() if case.created_at else None,
    }


async def _tasks_by_case(session: AsyncSession, workspace_id: str) -> dict[str, list[Task]]:
    out: dict[str, list[Task]] = {}
    for t in await session.scalars(
        select(Task).where(Task.workspace_id == workspace_id, Task.case_id.is_not(None))
    ):
        out.setdefault(t.case_id, []).append(t)
    return out


async def list_cases(
    session: AsyncSession,
    principal: Principal,
    q: str = "",
    case_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(Case).where(Case.workspace_id == principal.workspace_id).order_by(Case.case_ref)
    )
    tasks = await _tasks_by_case(session, principal.workspace_id)
    needle = q.strip().lower()
    out = []
    for case in rows:
        item = row(case, tasks.get(case.id))
        if case_type and item["case_type"] != case_type:
            continue
        if status and item["status"] != status:
            continue
        if needle:
            hay = " ".join(
                str(v)
                for v in (
                    item["case_ref"],
                    item["type_name"],
                    item["subject"],
                    item["headline"],
                    item["counterparty"]["name"],
                )
                if v
            ).lower()
            if needle not in hay:
                continue
        out.append(item)
    return out


async def get(session: AsyncSession, principal: Principal, case_id: str) -> Case:
    case = await session.get(Case, case_id)
    if case is None or case.workspace_id != principal.workspace_id:
        raise ApiError(404, "case_not_found", "No accessible case matches that reference.")
    return case


async def plan(
    session: AsyncSession, principal: Principal, settings: Settings, case: Case
) -> dict[str, Any]:
    """How this case would be worked: the agents that can take it and the number to call."""
    tid = cases.type_id(case)
    t = types.get(tid)
    agents = []
    for agent in await session.scalars(
        select(Agent)
        .where(Agent.workspace_id == principal.workspace_id, Agent.template_id.in_(t["agents"]))
        .order_by(Agent.created_at)
    ):
        version = await agents_service.current_version(session, agent)
        agents.append(
            {
                "id": agent.id,
                "name": agent.name,
                "template_id": agent.template_id,
                "published": version is not None,
                "version": version.number if version else None,
            }
        )
    suggested = next((a["id"] for a in agents if a["published"]), None)
    from careplix_api.calls_service import normalize_e164  # local: calls_service imports cases

    eff = await connections.load(session, principal.workspace_id, settings)
    approved = sorted(eff.approved_destinations)
    raw = types.value(case.data, t["counterparty"]["phone"])
    on_file = None
    if raw:
        try:
            on_file = normalize_e164(str(raw))
        except ApiError:
            on_file = None
    return {
        "agents": agents,
        "suggested_agent_id": suggested,
        "destination": {
            "label": t["counterparty"]["label"],
            "name": types.value(case.data, t["counterparty"]["name"]),
            "on_file": on_file,
            "on_file_approved": bool(on_file and on_file in eff.approved_destinations),
            "approved": approved,
        },
        "objective": cases.objective(case),
        "checks": list(t["checks"]),
        "packs": list(t["packs"]),
        "shareable": cases.shareable(case),
    }


async def detail(
    session: AsyncSession, principal: Principal, settings: Settings, case: Case
) -> dict[str, Any]:
    tasks = list(
        await session.scalars(
            select(Task).where(Task.case_id == case.id).order_by(Task.created_at.desc())
        )
    )
    agents = {
        a.id: a.name
        for a in await session.scalars(
            select(Agent).where(Agent.workspace_id == principal.workspace_id)
        )
    }
    t = types.get(cases.type_id(case))
    facts = cases.context(case)
    return {
        **row(case, tasks),
        "fields": [
            {
                "key": f["key"],
                "label": f["label"],
                "kind": f["kind"],
                "required": f["required"],
                "share": f["share"],
                "value": facts.get(f["key"]),
            }
            for f in t["fields"]
        ],
        "source_ref": case.source_ref,
        "tasks": [
            {
                "id": task.id,
                "state": task.state,
                "agent": agents.get(task.agent_id),
                "destination": task.destination_e164,
                "created_at": as_utc(task.created_at).isoformat(),
                "outcome_reason": task.outcome_reason,
            }
            for task in tasks
        ],
        "plan": await plan(session, principal, settings, case),
    }


async def _next_ref(session: AsyncSession, workspace_id: str, type_id: str) -> str:
    prefix = f"SYN-{types.get(type_id)['prefix']}-"
    refs = await session.scalars(
        select(Case.case_ref).where(
            Case.workspace_id == workspace_id, Case.case_ref.startswith(prefix)
        )
    )
    numbers = [int(r[len(prefix) :]) for r in refs if r[len(prefix) :].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def _audit(session: AsyncSession, principal: Principal, action: str, case: Case, detail: dict):
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=case.id,
            detail=detail,
        )
    )


async def create(
    session: AsyncSession,
    principal: Principal,
    type_id: str,
    values: dict[str, Any],
    *,
    source: str = "manual",
    source_ref: str | None = None,
) -> Case:
    if type_id not in types.CASE_TYPES:
        raise ApiError(422, "unknown_case_type", "Choose one of the listed case types.")
    if source not in SOURCES:
        raise ApiError(422, "unknown_source", "Unknown case source.")
    # Until the PHI gate, every case is synthetic, and hand-entered people must say so.
    if source == "manual" and not types.is_synthetic_text(values):
        raise ApiError(
            422,
            "synthetic_only",
            "Cases hold synthetic data only for now. Use a made-up patient name that includes "
            '"Synthetic", "Sample", "Example" or "Test".',
        )
    known = {f["key"]: f for f in types.get(type_id)["fields"]}
    for key, val in values.items():
        if key not in known:
            raise ApiError(422, "unknown_field", f"{key} is not a field of this case type.")
        if isinstance(val, str) and len(val) > 400:
            raise ApiError(
                422, "field_too_long", f"Keep {known[key]['label']} under 400 characters."
            )
        if known[key]["kind"] == "number" and val not in (None, "") and not str(val).isdigit():
            raise ApiError(422, "not_a_number", f"{known[key]['label']} must be a whole number.")
    ref = await _next_ref(session, principal.workspace_id, type_id)
    data = types.build(type_id, ref, values)
    gaps = types.missing(type_id, data)
    if gaps:
        labels = ", ".join(known[k]["label"] for k in gaps)
        raise ApiError(422, "case_incomplete", f"Fill in: {labels}.", {"missing": gaps})
    case = Case(
        workspace_id=principal.workspace_id,
        case_ref=ref,
        synthetic=True,
        data=data,
        case_type=type_id,
        status="open",
        source=source,
        source_ref=source_ref,
        created_by=principal.user_id,
    )
    session.add(case)
    await session.flush()
    _audit(session, principal, "case.create", case, {"case_type": type_id, "source": source})
    return case


async def add_samples(
    session: AsyncSession, principal: Principal, type_id: str, count: int
) -> list[Case]:
    if type_id not in types.CASE_TYPES:
        raise ApiError(422, "unknown_case_type", "Choose one of the listed case types.")
    existing = await session.scalar(
        select(func.count()).select_from(Case).where(Case.workspace_id == principal.workspace_id)
    )
    rng = random.Random()
    made = []
    for i in range(count):
        values = types.sample_values(type_id, rng, (existing or 0) + i + 1)
        values = {
            k: v for k, v in values.items() if k in {f["key"] for f in types.get(type_id)["fields"]}
        }
        made.append(await create(session, principal, type_id, values, source="sample"))
    return made


async def set_status(session: AsyncSession, principal: Principal, case: Case, status: str) -> None:
    if status not in ("open", "closed"):
        raise ApiError(422, "bad_status", "A case can be reopened or closed by hand.")
    case.status, case.updated_at = status, utcnow()
    _audit(session, principal, f"case.{'reopen' if status == 'open' else 'close'}", case, {})


def on_task_created(case: Case) -> None:
    if status_of(case) in ("open", "done"):
        case.status, case.updated_at = "in_progress", utcnow()


async def on_outcome(session: AsyncSession, task: Task) -> None:
    """A reviewed outcome moves the case: done when completed, back to open otherwise."""
    if not task.case_id:
        return
    case = await session.get(Case, task.case_id)
    if case is None or status_of(case) == "closed":
        return
    others = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.case_id == case.id, Task.id != task.id, Task.state.in_(OPEN_TASK_STATES))
    )
    if task.state in DONE_OUTCOMES:
        case.status = "done"
    elif not others:
        case.status = "open"
    case.updated_at = utcnow()


async def backfill(session: AsyncSession) -> None:
    """Give rows from before case types their type, status and source, and the seeded prior auths
    the payer's number their fixture now carries (idempotent)."""
    from careplix_api.seed import FIXTURES  # local: seed imports agents_service at module level

    phones = {}
    for path in FIXTURES.glob("*.json"):
        data = json.loads(path.read_text())
        if (data.get("payer") or {}).get("phone"):
            phones[data["case_id"]] = data["payer"]["phone"]
    for case in await session.scalars(select(Case).where(Case.case_type.is_(None))):
        case.case_type = cases.type_id(case)
        case.status = case.status or "open"
        case.source = case.source or "sample"
        payer = (case.data or {}).get("payer") or {}
        if case.case_ref in phones and not payer.get("phone"):
            case.data = {**case.data, "payer": {**payer, "phone": phones[case.case_ref]}}
    await session.commit()
