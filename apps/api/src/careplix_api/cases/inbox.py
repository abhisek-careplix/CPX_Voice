"""One review inbox (operating plan D5): everything waiting for a person, in one queue.

Kinds, each with its approver from :mod:`careplix_api.cases.policy`:

- ``result_review``: a finished call whose outcome a reviewer records (``/results/{task}``).
- ``ehr_write``: a proposed EHR write intent (approve or reject here, or on the Practice page).
- ``browser_plan`` / ``browser_step``: the browser agent waiting on its supervisor (live view).
- ``clinician_signoff``: a clinical case a clinician must sign off (the fifth contract part).
- ``hand_off``: a case a playbook or a person handed to staff, or blocked, waiting on its owner.
- ``auth_packet``: a prior auth whose packet is missing items (orders, notes, codes): the staff
  task that completes it (``/prior-auth``).
- ``escalation``: a case past its due date, for its owner.

The old pages (Results, Approvals, Practice) keep working; each item links to its page.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.auth import Principal
from careplix_api.cases import contract, policy, types
from careplix_api.cases.models import PlaybookRun
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Agent, Case, Task, WriteIntent
from careplix_api.settings import Settings

KINDS = (
    "result_review",
    "ehr_write",
    "browser_plan",
    "browser_step",
    "clinician_signoff",
    "hand_off",
    "auth_packet",
    "escalation",
)
KIND_LABELS = {
    "result_review": "Call result",
    "ehr_write": "EHR write",
    "browser_plan": "Browser plan",
    "browser_step": "Browser step",
    "clinician_signoff": "Clinician sign-off",
    "hand_off": "Handed off",
    "auth_packet": "Prior auth packet",
    "escalation": "Overdue case",
}


def _waited(since) -> int:
    return max(0, round((utcnow() - as_utc(since)).total_seconds())) if since else 0


def _item(
    principal: Principal,
    kind: str,
    item_id: str,
    title: str,
    detail: str,
    since,
    href: str,
    *,
    case: Case | None = None,
    decide: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    who = policy.approver(kind, principal.workspace_id)
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "id": item_id,
        "title": title,
        "detail": detail,
        "waiting_s": _waited(since),
        "href": href,
        "case": {"id": case.id, "case_ref": case.case_ref} if case else None,
        "approver": who,
        "can_decide": policy.can_decide(principal, who),
        # Decisions this inbox takes itself; the others open the item's own page.
        "decisions": decide or [],
        **(extra or {}),
    }


async def _results(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    tasks = list(
        await session.scalars(
            select(Task)
            .where(Task.workspace_id == principal.workspace_id, Task.state == "Review")
            .order_by(Task.updated_at)
            .limit(200)
        )
    )
    agents = {
        a.id: a.name
        for a in await session.scalars(
            select(Agent).where(Agent.workspace_id == principal.workspace_id)
        )
    }
    out = []
    for t in tasks:
        case = await session.get(Case, t.case_id) if t.case_id else None
        type_name = types.get(cases.type_id(case))["name"] if case else "Call"
        out.append(
            _item(
                principal,
                "result_review",
                t.id,
                f"Review the call result: {type_name.lower()}",
                " · ".join(
                    x for x in (case.case_ref if case else None, agents.get(t.agent_id)) if x
                ),
                t.updated_at,
                f"/results/{t.id}",
                case=case,
            )
        )
    return out


async def _writes(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    out = []
    for intent in await session.scalars(
        select(WriteIntent)
        .where(WriteIntent.workspace_id == principal.workspace_id, WriteIntent.state == "proposed")
        .order_by(WriteIntent.created_at)
        .limit(200)
    ):
        out.append(
            _item(
                principal,
                "ehr_write",
                intent.id,
                intent.reason,
                f"{intent.kind.replace('_', ' ')} · {intent.level} · from {intent.source}",
                intent.created_at,
                "/practice",
                decide=["approve", "reject"],
                extra={"level": intent.level},
            )
        )
    return out


async def _browser(
    session: AsyncSession, principal: Principal, settings: Settings
) -> list[dict[str, Any]]:
    from careplix_api.browser import api as browser_api

    listed = await browser_api.approvals(principal=principal, session=session, settings=settings)
    out = []
    for i in listed["items"]:
        kind = "browser_plan" if i["type"] == "plan" else "browser_step"
        item = _item(
            principal,
            kind,
            i.get("step_id") or i["run_id"],
            i["title"],
            f"{i['site']} · {i['task']}"[:200],
            None,
            f"/browser/{i['run_id']}",
            extra={"risk": i.get("risk"), "run_id": i["run_id"], "mine": i.get("mine")},
        )
        item["waiting_s"] = i["waiting_s"]
        out.append(item)
    return out


async def _cases(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    out = []
    for case in await session.scalars(
        select(Case).where(
            Case.workspace_id == principal.workspace_id, Case.status.in_(contract.OWED)
        )
    ):
        t = types.get(cases.type_id(case))
        if t["contract"]["clinical"] and not case.clinician_by:
            out.append(
                _item(
                    principal,
                    "clinician_signoff",
                    case.id,
                    f"Sign off: {t['name'].lower()}",
                    t["program"],
                    case.created_at,
                    f"/cases?case={case.id}",
                    case=case,
                    decide=["approve"],
                )
            )
        gap = await _packet_gap(session, case) if case.escalated_at is None else None
        if gap:
            from careplix_api.priorauth.packet import in_sentence

            missing = ", ".join(in_sentence(i["label"]) for i in gap)
            out.append(
                _item(
                    principal,
                    "auth_packet",
                    case.id,
                    "Complete the prior auth packet",
                    f"Missing: {missing}"[:200],
                    case.updated_at,
                    f"/prior-auth?case={case.id}",
                    case=case,
                    extra={"mine": case.owner_id == principal.user_id},
                )
            )
        elif case.escalated_at is None and case.status in ("handed_off", "blocked"):
            out.append(
                _item(
                    principal,
                    "hand_off",
                    case.id,
                    f"{'Blocked' if case.status == 'blocked' else 'Handed off'}: "
                    f"{t['name'].lower()}",
                    case.next_step or case.status_reason or "",
                    case.updated_at,
                    f"/cases?case={case.id}",
                    case=case,
                    extra={"mine": case.owner_id == principal.user_id},
                )
            )
        if case.escalated_at is not None:
            out.append(
                _item(
                    principal,
                    "escalation",
                    case.id,
                    f"Past due: {t['name'].lower()}",
                    case.next_step or case.status_reason or "",
                    case.due_at,
                    f"/cases?case={case.id}",
                    case=case,
                    extra={"mine": case.owner_id == principal.user_id},
                )
            )
    return out


async def _packet_gap(session: AsyncSession, case: Case) -> list[dict[str, Any]] | None:
    """A prior auth blocked on its packet: the missing items (else None)."""
    if case.status != "blocked" or case.playbook != "prior_authorization":
        return None
    run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case.id))
    if run is None or run.state != "blocked":
        return None
    gaps = [i for i in (run.data or {}).get("packet") or [] if i["required"] and not i["present"]]
    return gaps or None


async def items(
    session: AsyncSession, principal: Principal, settings: Settings, kind: str | None = None
) -> dict[str, Any]:
    found = [
        *await _results(session, principal),
        *await _writes(session, principal),
        *await _browser(session, principal, settings),
        *await _cases(session, principal),
    ]
    counts: dict[str, int] = {}
    for i in found:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    shown = [i for i in found if kind is None or i["kind"] == kind]
    shown.sort(key=lambda i: -i["waiting_s"])
    return {
        "items": shown,
        "counts": counts,
        "total": len(found),
        "kinds": [{"kind": k, "label": KIND_LABELS[k]} for k in KINDS],
        "policy": policy.view(principal.workspace_id),
    }


async def decide(
    session: AsyncSession,
    principal: Principal,
    kind: str,
    item_id: str,
    decision: str,
) -> dict[str, Any]:
    """Decisions the inbox takes itself. Returns what to do next (e.g. send an approved write)."""
    who = policy.approver(kind, principal.workspace_id)
    if not policy.can_decide(principal, who):
        raise ApiError(403, "forbidden", f"This needs the {who}'s approval.")
    if kind == "ehr_write":
        from careplix_api.practice import intents

        intent = await session.get(WriteIntent, item_id)
        if intent is None or intent.workspace_id != principal.workspace_id:
            raise ApiError(404, "not_found", "Write not found.")
        if decision not in ("approve", "reject"):
            raise ApiError(422, "bad_decision", "Approve or reject.")
        await intents.decide(session, intent, principal.user_id, decision == "approve")
        return {"send": intent.id if decision == "approve" else None}
    if kind == "clinician_signoff":
        case = await session.get(Case, item_id)
        if case is None or case.workspace_id != principal.workspace_id:
            raise ApiError(404, "case_not_found", "No accessible case matches that reference.")
        if decision != "approve":
            raise ApiError(422, "bad_decision", "Sign off, or open the case to hand it back.")
        await sign_off(session, principal, case)
        return {"case": case.id}
    raise ApiError(422, "open_item", "Open this item to decide it on its own page.")


async def sign_off(session: AsyncSession, principal: Principal, case: Case) -> None:
    if not types.get(cases.type_id(case))["contract"]["clinical"]:
        raise ApiError(422, "not_clinical", "Only clinical cases need a clinician's sign-off.")
    if not policy.can_decide(principal, "clinician"):
        raise ApiError(403, "forbidden", "A clinician signs this off.")
    case.clinician_by, case.clinician_at, case.updated_at = principal.user_id, utcnow(), utcnow()
    contract.add_link(
        session,
        case,
        part="evidence",
        kind="approval",
        ref=principal.user_id,
        label=f"Clinician sign-off by {principal.display_name}",
        actor=principal.user_id,
    )
