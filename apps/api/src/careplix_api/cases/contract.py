"""The completion contract (operating plan §2): a case is done only when its work is proven.

Four parts, plus a fifth for clinical work:

1. **Evidence**: at least one evidence link (a call, transcript, payer response, EHR record).
2. **System of record updated**: the acknowledgement id the EHR, clearinghouse or payer gave.
3. **People informed**: a message to each audience the case type names (the patient and the staff
   owner; payer work tells staff only), with a delivery status that isn't ``failed``.
4. **Owned next action**: either "nothing left", or a named owner, a due date and the next step.
5. **Clinician reviewed** (clinical types): a named clinician signed off.

Status values: ``open`` and ``in_progress`` while work goes on; ``done`` (contract holds),
``handed_off`` (a person owns the next step), ``blocked`` (with the reason) and ``failed``.
``closed`` is kept for rows from before Cases v2. A case past its due date escalates to its owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import types
from careplix_api.cases.models import CaseLink
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Case

ACTIVE = ("open", "in_progress")
# Statuses that still carry work, so a missed due date escalates.
OWED = ("open", "in_progress", "handed_off", "blocked")
TERMINAL = ("done", "failed", "closed")
STATUSES = ("open", "in_progress", "done", "handed_off", "blocked", "failed", "closed")
AUDIENCES = ("patient", "caregiver", "staff", "payer")
DELIVERY = ("queued", "sent", "delivered", "read", "failed")
EVIDENCE_KINDS = (
    "task",
    "call",
    "transcript",
    "attempt",
    "message",
    "write_intent",
    "x12",
    "screenshot",
    "ehr_record",
    "approval",
    "browser_run",
    "note",
)
DEFAULT_HANDOFF_DUE = timedelta(days=1)

PART_LABELS = {
    "evidence": "Evidence",
    "system_of_record": "System of record updated",
    "informed": "People informed",
    "next_action": "Owned next action",
    "clinician": "Clinician reviewed",
}


def spec(case: Case) -> dict[str, Any]:
    return types.get(cases.type_id(case))["contract"]


async def links(session: AsyncSession, case: Case) -> list[CaseLink]:
    return list(
        await session.scalars(
            select(CaseLink).where(CaseLink.case_id == case.id).order_by(CaseLink.created_at)
        )
    )


def evaluate(case: Case, case_links: list[CaseLink]) -> dict[str, Any]:
    """Each part with whether it holds and what's missing; ``holds`` is the whole verdict."""
    contract = spec(case)
    evidence = [lk for lk in case_links if lk.part == "evidence"]
    told = {
        lk.audience
        for lk in case_links
        if lk.part == "informed" and lk.delivery != "failed" and lk.audience
    }
    need = list(contract["informed"])
    untold = [a for a in need if a not in told]
    owned = bool(case.owner_id and case.due_at and case.next_step)
    parts = [
        {
            "key": "evidence",
            "met": bool(evidence),
            "detail": f"{len(evidence)} linked" if evidence else "Link what the agent saw or heard",
        },
        {
            "key": "system_of_record",
            "met": bool(case.sor_ack),
            "detail": f"{case.sor_system or 'ack'}: {case.sor_ack}"
            if case.sor_ack
            else contract["sor"],
        },
        {
            "key": "informed",
            "met": not untold,
            "detail": "Told: " + ", ".join(a for a in need if a in told)
            if not untold
            else "Still to tell: " + ", ".join(untold),
        },
        {
            "key": "next_action",
            "met": bool(case.nothing_left) or owned,
            "detail": "Nothing left"
            if case.nothing_left
            else (
                case.next_step
                if owned
                else "Say there's nothing left, or name owner, due date and next step"
            ),
        },
    ]
    if contract["clinical"]:
        parts.append(
            {
                "key": "clinician",
                "met": bool(case.clinician_by),
                "detail": "Signed off" if case.clinician_by else "A clinician has to sign off",
            }
        )
    for p in parts:
        p["label"] = PART_LABELS[p["key"]]
    return {
        "holds": all(p["met"] for p in parts),
        "missing": [p["key"] for p in parts if not p["met"]],
        "parts": parts,
    }


def _audit(session: AsyncSession, case: Case, actor: str, action: str, detail: dict) -> None:
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor=actor,
            action=action,
            target=case.id,
            detail=detail,
        )
    )


def add_link(
    session: AsyncSession,
    case: Case,
    *,
    part: str,
    kind: str,
    label: str,
    ref: str | None = None,
    audience: str | None = None,
    channel: str | None = None,
    delivery: str | None = None,
    actor: str | None = None,
) -> CaseLink:
    if part not in ("evidence", "informed"):
        raise ApiError(422, "bad_part", "A link is evidence or a message that informed someone.")
    if part == "evidence" and kind not in EVIDENCE_KINDS:
        raise ApiError(422, "bad_kind", f"Evidence is one of {', '.join(EVIDENCE_KINDS)}.")
    if part == "informed":
        if audience not in AUDIENCES:
            raise ApiError(422, "bad_audience", f"Say who was told: {', '.join(AUDIENCES)}.")
        if delivery not in DELIVERY:
            raise ApiError(422, "bad_delivery", f"Delivery is one of {', '.join(DELIVERY)}.")
    link = CaseLink(
        workspace_id=case.workspace_id,
        case_id=case.id,
        part=part,
        kind=kind,
        ref=(ref or None) and ref[:120],
        label=label[:200],
        audience=audience,
        channel=channel,
        delivery=delivery,
        created_by=actor,
    )
    session.add(link)
    case.updated_at = utcnow()
    return link


async def has_link(session: AsyncSession, case: Case, part: str, kind: str, ref: str) -> bool:
    return (
        await session.scalar(
            select(CaseLink.id).where(
                CaseLink.case_id == case.id,
                CaseLink.part == part,
                CaseLink.kind == kind,
                CaseLink.ref == ref,
            )
        )
    ) is not None


def acknowledge(case: Case, system: str, ack: str) -> None:
    case.sor_system, case.sor_ack, case.updated_at = system[:40], ack[:120], utcnow()


def set_next_action(
    case: Case,
    *,
    owner_id: str | None = None,
    due_at: datetime | None = None,
    next_step: str | None = None,
    nothing_left: bool | None = None,
) -> None:
    if nothing_left:
        case.nothing_left, case.next_step = True, None
    else:
        if nothing_left is False:
            case.nothing_left = False
        if owner_id is not None:
            case.owner_id = owner_id or None
        if due_at is not None:
            case.due_at, case.escalated_at = due_at, None  # a new due date re-arms escalation
        if next_step is not None:
            case.next_step = next_step.strip()[:300] or None
    case.updated_at = utcnow()


async def transition(
    session: AsyncSession,
    case: Case,
    status: str,
    actor: str,
    *,
    reason: str | None = None,
    owner_id: str | None = None,
    due_at: datetime | None = None,
    next_step: str | None = None,
) -> None:
    """Move a case to ``status``, enforcing the contract and each status's requirements."""
    if status not in STATUSES:
        raise ApiError(422, "bad_status", f"Status is one of {', '.join(STATUSES)}.")
    reason = (reason or "").strip()[:300] or None
    if status == "done":
        verdict = evaluate(case, await links(session, case))
        if not verdict["holds"]:
            labels = ", ".join(PART_LABELS[k].lower() for k in verdict["missing"])
            raise ApiError(
                409,
                "contract_unmet",
                f"This case can't be done yet. Missing: {labels}.",
                {"missing": verdict["missing"]},
            )
    elif status == "handed_off":
        set_next_action(case, owner_id=owner_id, due_at=due_at, next_step=next_step or reason)
        if not case.owner_id:
            case.owner_id = actor if actor and not actor.startswith("playbook") else None
        if not case.owner_id or not case.next_step:
            raise ApiError(
                422, "owner_required", "Handing off needs a person who owns it and the next step."
            )
        if not case.due_at:
            case.due_at = utcnow() + DEFAULT_HANDOFF_DUE
        case.nothing_left = False
    elif status in ("blocked", "failed") and not reason:
        raise ApiError(422, "reason_required", f"Say why the case is {status}.")
    previous = case.status or "open"
    case.status = status
    case.status_reason = reason if status in ("blocked", "failed", "handed_off") else None
    case.closed_at = utcnow() if status in TERMINAL else None
    case.updated_at = utcnow()
    _audit(session, case, actor, f"case.{status}", {"from": previous, "reason": reason})


def overdue(case: Case, now: datetime | None = None) -> bool:
    return bool(
        case.due_at and (case.status or "open") in OWED and as_utc(case.due_at) < (now or utcnow())
    )


async def escalate_overdue(
    session: AsyncSession, settings=None, now: datetime | None = None
) -> list[Case]:
    """Escalate each case past its due date to its owner, once per due date.

    Runs in the background loop across workspaces (no tenant), so it filters by status only.
    The owner sees it in the review inbox; a staff alert channel (Slack/Teams, workstream C) plugs
    in through :func:`careplix_api.cases.attempts.notify_staff`.
    """
    from careplix_api.cases import attempts  # local: attempts imports this module

    now = now or utcnow()
    found = list(
        await session.scalars(
            select(Case).where(
                Case.due_at.is_not(None),
                Case.due_at < now,
                Case.escalated_at.is_(None),
                Case.status.in_(OWED),
            )
        )
    )
    for case in found:
        case.escalated_at = now
        owner = case.owner_id or case.created_by
        if not case.owner_id and owner and not owner.startswith("playbook"):
            case.owner_id = owner
        _audit(
            session,
            case,
            "playbook",
            "case.escalated",
            {"owner": case.owner_id, "due_at": as_utc(case.due_at).isoformat()},
        )
        await attempts.notify_staff(
            session,
            case,
            f"Case {case.case_ref} is past its due date and needs its owner.",
            kind="escalation",
            settings=settings,
        )
    return found
