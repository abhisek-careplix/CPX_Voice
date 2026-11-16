"""Remittance (835) posting (RC7, workflow R5): payer payments become EHR posting proposals.

:func:`ingest_835` reads one 835 report (:func:`claim_reports.summarize_835`). For each claim
payment it:

1. finds our claim by patient control number (else the payer's claim number);
2. records the payment, adjustments and remark codes as an :class:`EraPosting` (once per
   transaction and payer claim number) and moves the claim to ``paid``, ``partially_paid`` or
   ``denied`` with the amounts;
3. proposes the postings as EHR write intents through the existing approval flow (L3: a person
   approves every financial write): the insurance payment (``payment_post``) and one adjustment per
   contractual or other non-patient group (``adjustment_post``: CO, OA, PI). Patient
   responsibility (PR) is not written off: it stays on the patient's balance.

A claim with no EHR connection or EHR patient (synthetic samples) gets a ``manual`` posting:
staff post it by hand and mark it done. Denied claims propose no posting: the denial route
decides (:mod:`.denials`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import acks
from careplix_api.claims.models import Claim, ClaimTransaction, EraPosting
from careplix_api.db import utcnow
from careplix_api.integrations import claim_reports
from careplix_api.integrations.clearinghouse import DENIED, PAID, STATUS_LABEL
from careplix_api.models import PayerConnection, WriteIntent
from careplix_api.phi import PhiBox
from careplix_api.practice import intents
from careplix_api.practice.connections import EHR, location_id
from careplix_api.settings import Settings

WRITE_OFF_GROUPS = ("CO", "OA", "PI")
REQUESTED_BY = "system:era"
GROUP_TEXT = {
    "CO": "Contractual obligation",
    "PR": "Patient responsibility",
    "OA": "Other adjustment",
    "PI": "Payer-initiated reduction",
    "CR": "Correction or reversal",
}


def _d(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def view(row: EraPosting) -> dict[str, Any]:
    return {
        "id": row.id,
        "claim_id": row.claim_id,
        "transaction_id": row.transaction_id,
        "patient_control_number": row.patient_control_number,
        "payer_claim_number": row.payer_claim_number,
        "payer_name": row.payer_name,
        "claim_status_code": row.claim_status_code,
        "outcome": row.outcome,
        "charge": row.charge,
        "paid": row.paid,
        "patient_responsibility": row.patient_responsibility,
        "adjustments": row.adjustments or [],
        "remarks": row.remarks or [],
        "payment_method": row.payment_method,
        "trace_number": row.trace_number,
        "paid_on": row.paid_on,
        "state": row.state,
        "intent_ids": row.intent_ids or [],
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def find_claim(session: AsyncSession, payment: dict[str, Any]) -> Claim | None:
    claim = await acks.find_claim(session, payment.get("patient_control_number"))
    if claim is None and payment.get("payer_claim_number"):
        claim = await session.scalar(
            select(Claim).where(
                func.upper(Claim.payer_claim_number) == str(payment["payer_claim_number"]).upper(),
                Claim.stage.is_not(None),
            )
        )
    return claim


def apply(claim: Claim, payment: dict[str, Any], now: datetime) -> None:
    """The remittance onto the claim. A reversal reopens it (accepted, waiting for a new one)."""
    outcome = payment["outcome"]
    claim.era_at = now
    if payment.get("payer_claim_number"):
        claim.payer_claim_number = str(payment["payer_claim_number"])[:60]
    if outcome == "reversed":
        claim.stage, claim.paid_total, claim.amount_paid = "accepted", None, None
        return
    claim.stage = outcome
    claim.paid_total = claim.amount_paid = payment["paid"]
    claim.patient_responsibility = payment["patient_responsibility"]
    status = DENIED if outcome == "denied" else PAID
    claim.status, claim.status_label = status, STATUS_LABEL[status]
    if outcome == "partially_paid":
        claim.status_label = "Partially paid"
    claim.status_since = payment.get("paid_on") or now.date().isoformat()
    claim.next_check_at = None


def _ehr_charge_id(claim: Claim, line_id: str | None, charges: dict[str, str]) -> str | None:
    for line in claim.lines or []:
        if line.get("line_id") == line_id and line.get("charge_id") in charges:
            return charges[line["charge_id"]]
    return None


async def _ehr_charges(session: AsyncSession, claim: Claim) -> dict[str, str]:
    """Our charge id → NexHealth charge id, for charges read from the EHR."""
    from careplix_api.claims.models import ClaimCharge

    ids = [line.get("charge_id") for line in claim.lines or [] if line.get("charge_id")]
    if not ids:
        return {}
    rows = await session.scalars(
        select(ClaimCharge).where(ClaimCharge.id.in_(ids), ClaimCharge.source == "ehr")
    )
    return {r.id: r.source_ref.split(":", 1)[-1] for r in rows}


def proposals(
    claim: Claim, payment: dict[str, Any], charges: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The writes that post one claim payment: [{kind, amount, reason, payload}] (no patient
    or location yet). Payment first, then one adjustment per group and reason."""
    charges = charges or {}
    pcn, trace = claim.claim_number, str(payment.get("trace_number") or "")[:20]
    out = []
    paid = _d(payment["paid"])
    if paid > 0:
        splits: dict[str, float] = {}
        for line in payment.get("lines") or []:
            nex = _ehr_charge_id(claim, line.get("line_id"), charges)
            if nex and _d(line.get("paid")) > 0:
                splits[nex] = -float(_d(line["paid"]))
        out.append(
            {
                "kind": "payment_post",
                "amount": f"{paid:.2f}",
                "reason": f"Insurance payment, claim {pcn}",
                "payload": {
                    "amount": -float(paid),
                    "transaction_id": f"ERA-P-{pcn}-{trace}"[:50],
                    "paid_at": f"{payment['paid_on']}T12:00:00+00:00"
                    if payment.get("paid_on")
                    else None,
                    "charge_splits": splits or None,
                    "notes": f"{payment.get('payer') or claim.payer_name} remittance "
                    f"{trace or '(no trace)'}, payer claim {payment.get('payer_claim_number')}",
                },
            }
        )
    totals: dict[tuple[str, str], Decimal] = {}
    for adj in payment.get("adjustments") or []:
        if adj["group"] in WRITE_OFF_GROUPS and _d(adj["amount"]) != 0:
            key = (adj["group"], adj["code"])
            totals[key] = totals.get(key, Decimal("0")) + _d(adj["amount"])
    for (group, code), amount in totals.items():
        out.append(
            {
                "kind": "adjustment_post",
                "amount": f"{amount:.2f}",
                "reason": f"{GROUP_TEXT.get(group, group)} {group}-{code}, claim {pcn}",
                "payload": {
                    "amount": float(amount),
                    "transaction_id": f"ERA-A-{group}{code}-{pcn}-{trace}"[:50],
                    "notes": f"{group}-{code} from remittance {trace or '(no trace)'}",
                },
            }
        )
    return out


async def _ehr(session: AsyncSession, claim: Claim) -> PayerConnection | None:
    if not claim.ehr_connection_id or not claim.patient_ref:
        return None
    conn = await session.get(PayerConnection, claim.ehr_connection_id)
    return conn if conn is not None and conn.kind == EHR and conn.enabled else None


async def propose(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    posting: EraPosting,
    payment: dict[str, Any],
) -> None:
    """Turn a posting into write intents for staff approval, or mark it for posting by hand."""
    if posting.outcome in ("denied", "reversed"):
        posting.state = "review"
        posting.note = (
            "Denied: see the denial work item." if posting.outcome == "denied" else "Reversal."
        )
        return
    writes = proposals(claim, payment, await _ehr_charges(session, claim))
    if not writes:
        posting.state, posting.note = "posted", "Nothing to post."
        return
    conn = await _ehr(session, claim)
    if conn is None:
        posting.state = "manual"
        posting.note = "No EHR patient for this claim: post the payment and adjustments by hand."
        return
    ids = []
    for write in writes:
        payload = {
            "location_id": location_id(conn),
            "patient_id": int(claim.patient_ref) if str(claim.patient_ref).isdigit() else None,
            **{k: v for k, v in write["payload"].items() if v is not None},
        }
        intent = await intents.propose(
            session,
            conn,
            settings,
            kind=write["kind"],
            target_source_id=str(claim.patient_ref),
            payload=payload,
            reason=write["reason"],
            source="era",
            requested_by=REQUESTED_BY,
        )
        ids.append(intent.id)
    posting.intent_ids, posting.state = ids, "proposed"
    posting.note = f"{len(ids)} posting(s) waiting for approval."


async def ingest_835(
    session: AsyncSession,
    settings: Settings,
    report: dict[str, Any],
    *,
    transaction_id: str,
    mode: str,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or utcnow()
    box = PhiBox(settings)
    counts = {"claims": 0, "paid": 0, "partially_paid": 0, "denied": 0, "unmatched": 0}
    for payment in claim_reports.summarize_835(report):
        claim = await find_claim(session, payment)
        if claim is None:
            counts["unmatched"] += 1
            continue
        number = str(payment.get("payer_claim_number") or payment["patient_control_number"])
        seen = await session.scalar(
            select(EraPosting.id).where(
                EraPosting.workspace_id == claim.workspace_id,
                EraPosting.transaction_id == transaction_id,
                EraPosting.payer_claim_number == number[:60],
            )
        )
        if seen:
            continue
        posting = EraPosting(
            workspace_id=claim.workspace_id,
            claim_id=claim.id,
            transaction_id=transaction_id,
            patient_control_number=str(payment["patient_control_number"] or "")[:40],
            payer_claim_number=number[:60],
            payer_name=(payment.get("payer") or claim.payer_name or "")[:160],
            claim_status_code=payment.get("status_code"),
            outcome=payment["outcome"],
            charge=payment["charge"],
            paid=payment["paid"],
            patient_responsibility=payment["patient_responsibility"],
            adjustments=[
                {**a, "text": a.get("text") or GROUP_TEXT.get(a["group"])}
                for a in payment["adjustments"]
            ],
            remarks=payment["remarks"],
            payment_method=payment.get("payment_method"),
            trace_number=(payment.get("trace_number") or None),
            paid_on=payment.get("paid_on"),
        )
        session.add(posting)
        apply(claim, payment, now)
        await session.flush()
        await propose(session, settings, claim, posting, payment)
        await after_posting(session, settings, claim, posting, payment, now)
        session.add(
            ClaimTransaction(
                workspace_id=claim.workspace_id,
                claim_id=claim.id,
                kind="835",
                mode=mode,
                transaction_id=transaction_id,
                ok=payment["outcome"] != "denied",
                summary={
                    "outcome": payment["outcome"],
                    "paid": payment["paid"],
                    "patient_responsibility": payment["patient_responsibility"],
                    "codes": [f"{a['group']}-{a['code']}" for a in payment["adjustments"]],
                    "remarks": payment["remarks"],
                },
                raw=box.seal({"remittance": payment}),
            )
        )
        counts["claims"] += 1
        if payment["outcome"] in counts:
            counts[payment["outcome"]] += 1
    await session.flush()
    return counts


async def after_posting(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    posting: EraPosting,
    payment: dict[str, Any],
    now: datetime,
) -> None:
    """Hook for what follows a remittance (denial routing)."""
    return None


async def refresh_state(session: AsyncSession, posting: EraPosting) -> EraPosting:
    """``posted`` once every write is in the EHR (or accepted with no confirmation to wait for);
    ``needs_review`` if any failed or was refused."""
    if posting.state not in ("proposed", "posting") or not posting.intent_ids:
        return posting
    rows = list(
        await session.scalars(
            select(WriteIntent)
            .where(WriteIntent.id.in_(posting.intent_ids))
            .execution_options(populate_existing=True)  # sent from other sessions
        )
    )
    states = {r.state for r in rows}
    if "manual" in states:
        posting.state = "manual"
        posting.note = "The EHR doesn't take this write: post it by hand."
    elif states & {"failed", "needs_review", "rejected"}:
        posting.state = "needs_review"
        posting.note = "A posting failed or was refused: check the writes."
    elif states <= {"written", "accepted"}:
        posting.state = "posted"
        posting.note = "Posted to the EHR."
    elif states & {"approved", "sending", "accepted", "written"}:
        posting.state = "posting"
    return posting
