"""Prior auth work list, packet and payer rules (``/api/prior-auth``)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import packs
from careplix_api.auth import Principal, require
from careplix_api.cases import contract, playbooks, service, types
from careplix_api.cases.models import PlaybookRun
from careplix_api.db import as_utc
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Case, User, Workspace
from careplix_api.priorauth import packet, rules
from careplix_api.priorauth.models import PriorAuthRule
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
CASE_TYPE = "prior_authorization"
ROUTE_LABEL = {"pas": "Payer API (PAS)", "portal": "Portal", "call": "Payer call", "fax": "Fax"}


async def _section(session: AsyncSession, principal: Principal) -> None:
    ws = await session.get(Workspace, principal.workspace_id)
    if "prior_auth" not in packs.pack(ws.product_line if ws else None)["sections"]:
        raise ApiError(404, "not_in_pack", "This workspace's product line has no prior auth list.")


def _iso(value) -> str | None:
    return as_utc(value).isoformat() if value else None


def _row(case: Case, run: PlaybookRun | None, owners: dict[str, str]) -> dict:
    data = case.data or {}
    run_data = (run.data if run else None) or {}
    items = run_data.get("packet") or []
    auth = run_data.get("auth") or {}
    return {
        "id": case.id,
        "case_ref": case.case_ref,
        "status": case.status or "open",
        "status_reason": case.status_reason,
        "patient": types.value(data, "patient.display_name"),
        "payer": types.value(data, "payer.name"),
        "service": types.value(data, "service.description"),
        "cpt_code": types.value(data, "service.cpt_code"),
        "visit_at": (data.get("visit") or {}).get("at") or types.value(data, "service.date"),
        "auth_status": types.value(data, "auth.status"),
        "auth_number": auth.get("number") or types.value(data, "request.known_auth_reference"),
        "valid_from": types.value(data, "auth.valid_from"),
        "valid_to": types.value(data, "auth.valid_to"),
        "route": run_data.get("route"),
        "route_label": ROUTE_LABEL.get(run_data.get("route") or ""),
        "checks": run_data.get("checks", 0),
        "follow_up_calls": run_data.get("follow_up_calls", 0),
        "missing": [i["label"] for i in items if i["required"] and not i["present"]],
        "packet_checked": bool(items),
        "due_at": _iso(case.due_at),
        "overdue": contract.overdue(case),
        "next_step": case.next_step,
        "owner": owners.get(case.owner_id or ""),
        "source": case.source,
        "source_label": service.SOURCE_LABEL.get(case.source or "", case.source),
        "trigger": data.get("auth_trigger"),
        "playbook_state": run.state if run else None,
        "created_at": _iso(case.created_at),
        "synthetic": case.synthetic,
    }


@router.get("/prior-auth")
async def work_list(
    view: Literal["open", "done", "all"] = "open",
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Prior auth cases with their status, due date, payer and missing packet items."""
    await _section(session, principal)
    cases = list(
        await session.scalars(
            select(Case).where(
                Case.workspace_id == principal.workspace_id, Case.case_type == CASE_TYPE
            )
        )
    )
    if view == "open":
        cases = [c for c in cases if (c.status or "open") in contract.OWED]
    elif view == "done":
        cases = [c for c in cases if (c.status or "open") in contract.TERMINAL]
    runs = {
        r.case_id: r
        for r in await session.scalars(
            select(PlaybookRun).where(PlaybookRun.case_id.in_([c.id for c in cases]))
        )
    }
    owners = {
        u.id: u.display_name
        for u in await session.scalars(
            select(User).where(User.id.in_({c.owner_id for c in cases if c.owner_id}))
        )
    }
    rows = [_row(c, runs.get(c.id), owners) for c in cases]
    # Most urgent first: overdue, then by due date, then newest.
    rows.sort(key=lambda r: (not r["overdue"], r["due_at"] or "9999", r["case_ref"]))
    every = [c.status or "open" for c in cases]
    return {
        "items": rows,
        "counts": {
            "total": len(rows),
            "missing_items": sum(1 for r in rows if r["missing"]),
            "waiting_on_payer": sum(
                1 for r in rows if r["auth_status"] in ("pending_review", "unknown", "not_found")
            ),
            "handed_off": every.count("handed_off") + every.count("blocked"),
            "overdue": sum(1 for r in rows if r["overdue"]),
        },
        "trigger_on": "auth_required" in {t.strip() for t in settings.playbook_triggers.split(",")},
    }


async def _case(session: AsyncSession, principal: Principal, case_id: str) -> Case:
    case = await service.get(session, principal, case_id)
    if (case.case_type or CASE_TYPE) != CASE_TYPE:
        raise ApiError(422, "not_prior_auth", "This case isn't a prior authorization.")
    return case


async def _run(session: AsyncSession, case: Case) -> PlaybookRun | None:
    return await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))


@router.get("/prior-auth/{case_id}/packet")
async def get_packet(
    case_id: str, principal: Principal = Depends(viewer), session: AsyncSession = Session
):
    case = await _case(session, principal, case_id)
    run = await _run(session, case)
    items = await packet.checklist(
        session, case, ((run.data if run else None) or {}).get("packet_provided") or {}
    )
    return {"case_id": case.id, "items": items, "missing": packet.missing(items)}


class ProvideIn(BaseModel):
    item: str = Field(max_length=40)
    ref: str = Field(max_length=80)
    label: str | None = Field(default=None, max_length=120)


@router.post("/prior-auth/{case_id}/packet")
async def provide(
    case_id: str,
    body: ProvideIn,
    request: Request,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    """Attach a packet document by its EHR id. A complete packet resumes a blocked playbook."""
    case = await _case(session, principal, case_id)
    run = await _run(session, case)
    if run is None:
        raise ApiError(409, "no_playbook", "No playbook works this case.")
    run.data = packet.provide(run.data or {}, body.item, body.ref, body.label, principal.user_id)
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor=principal.user_id,
            action="prior_auth.packet_item",
            target=case.id,
            detail={"item": body.item},
        )
    )
    items = await packet.checklist(session, case, run.data.get("packet_provided") or {})
    resumed = False
    if not packet.missing(items) and run.state == "blocked":
        await playbooks.resume(session, case, principal.user_id)
        resumed = True
    await session.commit()
    if resumed and (engine := getattr(request.app.state, "playbooks", None)) is not None:
        await engine.advance(case.id)
    return {
        "case_id": case.id,
        "items": items,
        "missing": packet.missing(items),
        "resumed": resumed,
    }


# --- payer rules ----------------------------------------------------------------------------------


class RuleIn(BaseModel):
    payer: str = Field(default="*", max_length=120)
    appointment_type_ref: str | None = Field(default=None, max_length=40)
    appointment_type_name: str | None = Field(default=None, max_length=120)
    cpt_code: str = Field(min_length=4, max_length=10)
    service_description: str = Field(min_length=2, max_length=160)
    icd_codes: list[str] = Field(default_factory=list, max_length=12)
    lead_days: int = Field(default=14, ge=0, le=90)
    note: str | None = Field(default=None, max_length=200)


@router.get("/prior-auth-rules")
async def list_rules(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    return [r.view() for r in await rules.for_workspace(session, principal.workspace_id)]


def _add(session: AsyncSession, principal: Principal, body: dict) -> PriorAuthRule:
    if not (body.get("appointment_type_ref") or body.get("appointment_type_name")):
        raise ApiError(422, "service_required", "Name the appointment type the rule covers.")
    rule = PriorAuthRule(workspace_id=principal.workspace_id, created_by=principal.user_id, **body)
    session.add(rule)
    return rule


@router.post("/prior-auth-rules")
async def add_rule(
    body: RuleIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    rule = _add(session, principal, body.model_dump())
    await session.commit()
    return rule.view()


@router.post("/prior-auth-rules/samples")
async def add_sample_rules(
    principal: Principal = Depends(require("admin")), session: AsyncSession = Session
):
    have = {
        (r.cpt_code, r.payer) for r in await rules.for_workspace(session, principal.workspace_id)
    }
    made = [
        _add(session, principal, dict(s))
        for s in rules.SAMPLES
        if (s["cpt_code"], s["payer"]) not in have
    ]
    await session.commit()
    return [r.view() for r in made]


@router.delete("/prior-auth-rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    rule = await session.get(PriorAuthRule, rule_id)
    if rule is None or rule.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Rule not found.")
    await session.delete(rule)
    await session.commit()
    return {"deleted": rule_id}
