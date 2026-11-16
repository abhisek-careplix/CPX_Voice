"""Claim acknowledgements (277CA) and the rejection work queue (RC5, workflow R3).

Each acknowledgement names claims by our patient control number. :func:`ingest_277` finds each
claim (control numbers are unique across workspaces, so a webhook needs no tenant), keeps the
claim's part of the report as evidence, and moves the claim:

- accepted (A0/A1/A2/A5): ``accepted`` with the payer's claim number when the payer sent one;
- rejected (A3/A4/A6/A7/A8 at claim, line or provider level): ``rejected`` and one rejection in the
  work queue with the payer's codes and text, a category and what to fix (:func:`categorize`);
- anything else (pending, E*): kept as evidence, the claim stays where it is.

A rejected claim never reached adjudication, so it is fixed and submitted again (a new
submission of the same claim). A later acceptance closes its open rejections.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims.models import Claim, ClaimTransaction, ClaimWork
from careplix_api.db import utcnow
from careplix_api.integrations import claim_reports
from careplix_api.integrations.clearinghouse import ACCEPTED, REJECTED, STATUS_LABEL
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

FINAL_STAGES = ("paid", "partially_paid", "denied", "void")

# (category, entity codes, words in the payer's text, what to fix, where in the Studio)
CATEGORIES: tuple[tuple[str, set[str], tuple[str, ...], str, str], ...] = (
    (
        "duplicate",
        set(),
        ("duplicate",),
        "The payer already has this claim: don't resubmit; check the original claim's status.",
        "none",
    ),
    (
        "enrollment",
        set(),
        ("enroll", "not authorized to submit", "trading partner"),
        "The provider isn't enrolled to send claims to this payer through the clearinghouse: "
        "finish the enrollment, then resubmit.",
        "enrollment",
    ),
    (
        "patient_identity",
        {"IL", "QC", "03"},
        ("subscriber", "member", "insured", "patient", "birth", "eligib"),
        "Check the member ID, name and birth date against the insurance card (an eligibility "
        "check confirms them), correct the patient, then resubmit.",
        "patient",
    ),
    (
        "provider_identity",
        {"85", "82", "87", "77", "DN", "DK"},
        ("npi", "provider", "taxonomy", "tax id", "tin", "billing"),
        "Check the billing NPI, tax ID, taxonomy and address in the billing profile, then "
        "resubmit.",
        "billing_profile",
    ),
    (
        "coding",
        set(),
        ("procedure", "cpt", "hcpcs", "modifier", "diagnos", "icd", "code"),
        "Fix the procedure, modifier or diagnosis named by the payer, then resubmit.",
        "lines",
    ),
    (
        "dates",
        set(),
        ("date", "timely"),
        "Check the dates of service (and timely filing), then resubmit.",
        "lines",
    ),
    (
        "payer",
        {"PR"},
        ("payer", "not found", "invalid receiver"),
        "Check the payer ID for this plan in the payer directory, then resubmit.",
        "payer",
    ),
)
OTHER = ("other", "Read the payer's message, correct the claim, then resubmit.", "claim")


def categorize(reasons: list[dict[str, Any]]) -> tuple[str, str, str]:
    """(category, suggestion, fix area) from the payer's codes and text."""
    entities = {str(r.get("entity") or "").upper() for r in reasons}
    text = " ".join(
        str(r.get(k) or "") for r in reasons for k in ("text", "message", "entity_text")
    ).lower()
    for category, codes, words, suggestion, fix in CATEGORIES:
        if any(re.search(rf"\b{re.escape(w)}", text) for w in words):
            return category, suggestion, fix
        if codes & entities:
            return category, suggestion, fix
    return OTHER


def work_view(row: ClaimWork) -> dict[str, Any]:
    return {
        "id": row.id,
        "claim_id": row.claim_id,
        "kind": row.kind,
        "category": row.category,
        "route": row.route,
        "state": row.state,
        "title": row.title,
        "reasons": row.reasons or [],
        "suggestion": row.suggestion,
        "amount": row.amount,
        "due_on": row.due_on,
        "case_id": row.case_id,
        "appeal_id": row.appeal_id,
        "intent_ids": row.intent_ids or [],
        "resolution": row.resolution,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


async def reject(
    session: AsyncSession,
    claim: Claim,
    *,
    source_ref: str,
    reasons: list[dict[str, Any]],
    now: datetime | None = None,
) -> ClaimWork:
    """Mark the claim rejected and put it in the rejection queue (once per source)."""
    now = now or utcnow()
    claim.stage, claim.ack_status, claim.ack_at = "rejected", "rejected", now
    claim.status, claim.status_label = REJECTED, STATUS_LABEL[REJECTED]
    claim.status_since = now.date().isoformat()
    existing = await session.scalar(
        select(ClaimWork).where(
            ClaimWork.workspace_id == claim.workspace_id,
            ClaimWork.claim_id == claim.id,
            ClaimWork.kind == "rejection",
            ClaimWork.source_ref == source_ref[:80],
        )
    )
    if existing is not None:
        return existing
    category, suggestion, fix = categorize(reasons)
    first = next((r for r in reasons if r.get("text") or r.get("message")), {})
    headline = str(first.get("text") or first.get("message") or "Rejected")
    row = ClaimWork(
        workspace_id=claim.workspace_id,
        claim_id=claim.id,
        kind="rejection",
        source_ref=source_ref[:80],
        category=category,
        route="fix_resubmit" if category != "duplicate" else "payer_call",
        title=f"{claim.claim_number}: {headline}"[:160],
        reasons=[{**r, "fix": fix} for r in reasons][:20],
        suggestion=suggestion,
        amount=claim.billed_amount,
    )
    session.add(row)
    await session.flush()
    return row


async def close_rejections(
    session: AsyncSession, claim: Claim, *, actor: str, now: datetime | None = None
) -> int:
    rows = list(
        await session.scalars(
            select(ClaimWork).where(
                ClaimWork.workspace_id == claim.workspace_id,
                ClaimWork.claim_id == claim.id,
                ClaimWork.kind == "rejection",
                ClaimWork.state == "open",
            )
        )
    )
    for row in rows:
        row.state, row.resolved_at, row.resolved_by = "done", now or utcnow(), actor
        row.resolution = "Corrected and resubmitted."
    return len(rows)


async def find_claim(session: AsyncSession, control_number: str | None) -> Claim | None:
    """Our claim for a control number (payers may change case or cut it to 30 characters)."""
    number = str(control_number or "").strip().upper()
    if not number:
        return None
    return await session.scalar(
        select(Claim).where(func.upper(Claim.claim_number) == number, Claim.stage.is_not(None))
    )


def _reasons(ack: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for level, statuses in (
        ("claim", ack.get("statuses") or []),
        ("provider", ack.get("provider_statuses") or []),
        *[(f"line {line.get('line_id')}", line.get("statuses") or []) for line in ack["lines"]],
    ):
        for s in statuses:
            if s.get("category") in claim_reports.ACCEPT_CATEGORIES:
                continue
            out.append(
                {
                    "kind": "277",
                    "level": level,
                    "category": s.get("category"),
                    "code": s.get("code"),
                    "text": s.get("text"),
                    "message": s.get("message"),
                    "entity": s.get("entity"),
                    "entity_text": s.get("entity_text"),
                }
            )
    return out


async def apply_ack(
    session: AsyncSession, claim: Claim, ack: dict[str, Any], *, ref: str, now: datetime
) -> str:
    """One claim's acknowledgement. Returns accepted | rejected | pending | ignored."""
    verdict = ack["verdict"]
    if claim.stage in FINAL_STAGES:
        return "ignored"  # a remittance already settled it
    if verdict == claim_reports.REJECTED:
        await reject(session, claim, source_ref=ref, reasons=_reasons(ack), now=now)
        return "rejected"
    if verdict == claim_reports.ACCEPTED and claim.stage in ("submitted", "accepted"):
        claim.stage, claim.ack_status, claim.ack_at = "accepted", "accepted", now
        claim.status, claim.status_label = ACCEPTED, STATUS_LABEL[ACCEPTED]
        claim.status_since = ack.get("effective_date") or now.date().isoformat()
        number = ack.get("payer_claim_number")
        if number and str(number) != claim.claim_number:
            claim.payer_claim_number = str(number)[:60]
        await close_rejections(session, claim, actor="system:claims", now=now)
        return "accepted"
    return "pending"


async def ingest_277(
    session: AsyncSession,
    settings: Settings,
    report: dict[str, Any],
    *,
    transaction_id: str,
    mode: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """Apply one 277CA report. Already-seen reports are skipped per claim."""
    now = now or utcnow()
    box = PhiBox(settings)
    counts = {"claims": 0, "accepted": 0, "rejected": 0, "pending": 0, "unmatched": 0}
    for i, ack in enumerate(claim_reports.summarize_277ca(report)):
        claim = await find_claim(session, ack["patient_control_number"])
        if claim is None:
            counts["unmatched"] += 1
            continue
        ref = f"277:{transaction_id}:{i}"
        seen = await session.scalar(
            select(ClaimTransaction.id).where(
                ClaimTransaction.claim_id == claim.id,
                ClaimTransaction.kind == "277ca",
                ClaimTransaction.transaction_id == transaction_id,
            )
        )
        if seen:
            continue
        outcome = await apply_ack(session, claim, ack, ref=ref, now=now)
        counts["claims"] += 1
        counts[outcome if outcome in counts else "pending"] += 1
        session.add(
            ClaimTransaction(
                workspace_id=claim.workspace_id,
                claim_id=claim.id,
                kind="277ca",
                mode=mode,
                transaction_id=transaction_id,
                ok=outcome != "rejected",
                summary={
                    "verdict": ack["verdict"],
                    "applied": outcome,
                    "payer": ack.get("payer"),
                    "payer_claim_number": ack.get("payer_claim_number"),
                    "codes": [
                        f"{s.get('category')}/{s.get('code')}"
                        for s in ack.get("statuses") or []
                        if s.get("category")
                    ],
                },
                raw=box.seal({"acknowledgement": ack}),
            )
        )
    await session.flush()
    return counts
