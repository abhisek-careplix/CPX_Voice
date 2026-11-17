"""Denials (RC8, workflow R6): classify the payer's codes and route the work.

A remittance denies money when the claim is denied, or when a line is adjusted to nothing under a
non-contractual reason. :func:`classify` turns the CARC (claim adjustment reason) and RARC
(remark) codes into a denial category; each category has a default route:

- ``fix_resubmit``: a corrected claim (replacement of the payer's claim) fixes it: coding,
  missing information, the patient's identity or coverage on file;
- ``appeal``: the claim was right and the payer disagrees: medical necessity, authorization,
  timely filing (the 277CA acceptance date is the proof), bundling of a distinct service;
- ``payer_call``: we need the payer to explain (duplicates, pending or unclear reasons): a
  ``claim_denial`` case the calling agent works;
- ``write_off``: not recoverable from the payer under the contract (non-covered on a
  contractual group): an adjustment proposed as an EHR write intent for staff to approve.

Contractual reductions (CO-45 and friends) and patient responsibility (PR) are not denials.
Staff can change the route of any denial; nothing financial happens without approval.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims.models import Claim, ClaimWork, EraPosting
from careplix_api.db import utcnow
from careplix_api.settings import Settings

FIX, APPEAL, CALL, WRITE_OFF = "fix_resubmit", "appeal", "payer_call", "write_off"
ROUTES = (FIX, APPEAL, CALL, WRITE_OFF)
# Not denials: the contract's reduction, sequestration, and the patient's share.
CONTRACTUAL = frozenset({"45", "253", "59", "94", "131", "144", "237"})
PATIENT_SHARE = frozenset({"1", "2", "3", "66", "142"})

# category → (default route, label, what to do)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "eligibility": (
        FIX,
        "Patient or coverage",
        "Run an eligibility check; correct the member ID, name or birth date, or bill the "
        "coverage that was active on the date of service, then send a corrected claim.",
    ),
    "coordination": (
        FIX,
        "Other insurance first",
        "Another payer is primary: bill it first, then send this payer the claim with the "
        "primary's payment.",
    ),
    "coding": (
        FIX,
        "Coding",
        "Correct the procedure, modifier, diagnosis or place of service and send a corrected "
        "claim.",
    ),
    "missing_information": (
        FIX,
        "Missing information",
        "Send the missing information or records (a corrected claim with an attachment).",
    ),
    "authorization": (
        APPEAL,
        "Authorization",
        "Appeal with the authorization number and approval, or request a retro authorization.",
    ),
    "medical_necessity": (
        APPEAL,
        "Medical necessity",
        "Appeal with the visit notes and the clinical reason for the service.",
    ),
    "timely_filing": (
        APPEAL,
        "Timely filing",
        "Appeal with proof of timely filing (the clearinghouse acceptance report).",
    ),
    "bundling": (
        APPEAL,
        "Bundled with another service",
        "If the service was separate and distinct, appeal (or correct with the right modifier); "
        "otherwise accept the bundling.",
    ),
    "duplicate": (
        CALL,
        "Duplicate",
        "Confirm with the payer that the original claim was paid; don't resubmit.",
    ),
    "non_covered": (
        WRITE_OFF,
        "Not covered",
        "Not covered under the plan: write it off (contractual) or, with a signed waiver, bill "
        "the patient.",
    ),
    "provider": (
        CALL,
        "Provider enrollment",
        "The payer has a problem with the provider's enrollment or credentials: call to confirm "
        "and fix the enrollment, then send a corrected claim.",
    ),
    "other": (
        CALL,
        "Other",
        "Call the payer to find out exactly why and what fixes it.",
    ),
}

# CARC → category (the most common professional denials).
CARC: dict[str, str] = {
    **dict.fromkeys(("26", "27", "31", "32", "33", "177", "200", "201"), "eligibility"),
    **dict.fromkeys(("22", "23", "24", "109"), "coordination"),
    **dict.fromkeys(
        ("4", "5", "6", "7", "8", "9", "10", "11", "125", "146", "167", "181", "182", "189"),
        "coding",
    ),
    **dict.fromkeys(("16", "226", "227", "252", "A1"), "missing_information"),
    **dict.fromkeys(("15", "39", "62", "197", "198", "243", "288"), "authorization"),
    **dict.fromkeys(("50", "55", "56", "57", "150", "151", "152"), "medical_necessity"),
    "29": "timely_filing",
    **dict.fromkeys(("97", "234", "236", "B15", "B20"), "bundling"),
    "18": "duplicate",
    **dict.fromkeys(("49", "96", "119", "35", "149", "204", "B9", "170"), "non_covered"),
    **dict.fromkeys(("B7", "184", "185", "206", "207", "208", "52", "38"), "provider"),
}
# RARC → category, when the remark is more specific than the reason.
RARC: dict[str, str] = {
    "M127": "missing_information",
    "N706": "missing_information",
    "M27": "missing_information",
    "MA130": "coding",
    "M51": "coding",
    "M76": "coding",
    "N54": "authorization",
    "N130": "non_covered",
    "N95": "non_covered",
    "M86": "duplicate",
    "N290": "provider",
}
# Appeal windows from the remittance date (days). Medicare redetermination: 120 days
# (42 CFR 405.942); commercial plans commonly 180 (ACA internal appeals); corrected claims are
# usually accepted within the plan's timely filing, 90 days is the common floor.
APPEAL_DAYS = {"medicare": 120, "medicare_advantage": 60, "medicaid": 90}
DEFAULT_APPEAL_DAYS = 180
CORRECTED_CLAIM_DAYS = 90


def _d(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def denial_rows(posting: EraPosting | dict[str, Any]) -> list[dict[str, Any]]:
    """The adjustments that deny money: not contractual, not the patient's share."""
    adjustments = (
        posting.get("adjustments") if isinstance(posting, dict) else posting.adjustments
    ) or []
    return [
        a
        for a in adjustments
        if a.get("group") in ("CO", "OA", "PI")
        and str(a.get("code")) not in CONTRACTUAL
        and _d(a.get("amount")) > 0
    ]


def classify(codes: list[str], remarks: list[str]) -> str:
    """The denial category: remark codes first (more specific), then reason codes."""
    for remark in remarks:
        if remark in RARC:
            return RARC[remark]
    for code in codes:
        if code in CARC:
            return CARC[code]
    return "other"


def deadline(plan_kind: str | None, route: str, paid_on: str | None, today: date) -> str:
    start = date.fromisoformat(paid_on) if paid_on else today
    if route == FIX:
        days = CORRECTED_CLAIM_DAYS
    else:
        days = APPEAL_DAYS.get(plan_kind or "", DEFAULT_APPEAL_DAYS)
    return (start + timedelta(days=days)).isoformat()


async def route_posting(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    posting: EraPosting,
    now: datetime | None = None,
) -> ClaimWork | None:
    """Open the denial work item for a remittance that denied money (once per posting)."""
    rows = denial_rows(posting)
    if not rows and posting.outcome != "denied":
        return None
    now = now or utcnow()
    ref = f"835:{posting.id}"
    existing = await session.scalar(
        select(ClaimWork).where(
            ClaimWork.workspace_id == claim.workspace_id,
            ClaimWork.claim_id == claim.id,
            ClaimWork.kind == "denial",
            ClaimWork.source_ref == ref,
        )
    )
    if existing is not None:
        return existing
    codes = [str(r["code"]) for r in rows]
    category = classify(codes, list(posting.remarks or []))
    route, label, suggestion = CATEGORIES[category]
    amount = sum((_d(r["amount"]) for r in rows), Decimal("0"))
    if amount <= 0:
        amount = _d(posting.charge) - _d(posting.paid) - _d(posting.patient_responsibility)
    reasons = [
        {
            "kind": "carc",
            "group": r["group"],
            "code": r["code"],
            "amount": r["amount"],
            "text": r.get("text"),
            "line_id": r.get("line_id"),
        }
        for r in rows
    ] + [{"kind": "rarc", "code": code, "text": None} for code in posting.remarks or []]
    shown = ", ".join(f"{r['group']}-{r['code']}" for r in rows) or "no reason code"
    work = ClaimWork(
        workspace_id=claim.workspace_id,
        claim_id=claim.id,
        kind="denial",
        source_ref=ref,
        category=category,
        route=route,
        title=f"{claim.claim_number}: {label} ({shown})"[:160],
        reasons=reasons[:20],
        suggestion=suggestion,
        amount=f"{amount:.2f}",
        due_on=deadline(claim.plan_kind, route, posting.paid_on, now.date()),
    )
    session.add(work)
    await session.flush()
    await act(session, settings, claim, work, posting)
    return work


async def act(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    work: ClaimWork,
    posting: EraPosting | None = None,
) -> None:
    """What the route starts on its own: the payer call case, the appeal draft or the write-off
    proposal. A corrected claim is made when a person asks for it (:func:`corrected_claim`)."""
    if work.route == CALL and not work.case_id:
        await open_call(session, settings, claim, work)
    elif work.route == APPEAL and not work.appeal_id:
        from careplix_api.claims import appeals  # local: appeals imports this module

        await appeals.draft(session, settings, claim, work)
    elif work.route == WRITE_OFF and not work.intent_ids:
        await propose_write_off(session, settings, claim, work)


async def open_call(
    session: AsyncSession, settings: Settings, claim: Claim, work: ClaimWork
) -> str | None:
    from careplix_api.cases import service as cases_service
    from careplix_api.claims import directory
    from careplix_api.claims.follow_up import _principal, case_values
    from careplix_api.errors import ApiError
    from careplix_api.models import Case

    existing = await session.scalar(
        select(Case).where(
            Case.workspace_id == claim.workspace_id, Case.source_ref == f"denial:{work.id}"
        )
    )
    if existing is not None:
        work.case_id = existing.id
        return existing.id
    if not claim.payer_phone:
        row = await directory.find(session, claim.workspace_id, claim.payer_id)
        claim.payer_phone = row.phone if row and row.phone else None
    codes = [f"{r.get('group')}-{r['code']}" for r in work.reasons if r["kind"] == "carc"]
    codes += [r["code"] for r in work.reasons if r["kind"] == "rarc"]
    values = {
        **case_values(settings, claim),
        "claim_number": claim.payer_claim_number or claim.claim_number,
        "reason_codes": ", ".join(codes) or "no reason code",
        "denied_amount": work.amount,
    }
    try:
        case = await cases_service.create(
            session,
            _principal(claim.workspace_id),
            "claim_denial",
            {k: v for k, v in values.items() if v not in (None, "")},
            source="claim_denial",
            source_ref=f"denial:{work.id}",
        )
    except ApiError as err:
        work.resolution = f"Payer call not opened: {err.message}"[:200]
        return None
    work.case_id = case.id
    return case.id


async def propose_write_off(
    session: AsyncSession, settings: Settings, claim: Claim, work: ClaimWork
) -> None:
    """The denied amount as an adjustment for staff approval (never automatic)."""
    from careplix_api.claims.era import _ehr
    from careplix_api.practice import intents
    from careplix_api.practice.connections import location_id

    conn = await _ehr(session, claim)
    if conn is None:
        work.resolution = "No EHR patient: write it off in the EHR by hand once approved."
        return
    codes = "/".join(str(r["code"]) for r in work.reasons if r["kind"] == "carc")
    intent = await intents.propose(
        session,
        conn,
        settings,
        kind="adjustment_post",
        target_source_id=str(claim.patient_ref),
        payload={
            "location_id": location_id(conn),
            "patient_id": int(claim.patient_ref) if str(claim.patient_ref).isdigit() else None,
            "amount": float(_d(work.amount)),
            "transaction_id": f"DEN-WO-{claim.claim_number}-{work.id[:8]}"[:50],
            "notes": f"Denial write-off {codes} for claim {claim.claim_number}",
        },
        reason=f"Denial write-off ({codes}), claim {claim.claim_number}"[:200],
        source="denial",
        requested_by="system:denials",
    )
    work.intent_ids = [intent.id]


async def corrected_claim(session: AsyncSession, claim: Claim, work: ClaimWork | None) -> Claim:
    """A draft replacement of an adjudicated claim: same visit, a new control number, the
    payer's claim number as the original reference (claim frequency 7)."""
    from careplix_api.claims import x837

    if not claim.payer_claim_number:
        raise ValueError("The payer's claim number is needed to replace a claim.")
    pcn = x837.new_control_number()
    new = Claim(
        workspace_id=claim.workspace_id,
        source="charges",
        source_ref=pcn,
        ehr_connection_id=claim.ehr_connection_id,
        synthetic=claim.synthetic,
        claim_number=pcn,
        payer_id=claim.payer_id,
        payer_name=claim.payer_name,
        payer_phone=claim.payer_phone,
        plan_kind=claim.plan_kind,
        provider_name=claim.provider_name,
        provider_npi=claim.provider_npi,
        patient_ref=claim.patient_ref,
        patient=claim.patient,
        date_of_service=claim.date_of_service,
        billed_amount=claim.billed_amount,
        cpt_code=claim.cpt_code,
        submitted_on="",
        stage="draft",
        place_of_service=claim.place_of_service,
        diagnoses=list(claim.diagnoses or []),
        lines=[dict(line) for line in claim.lines or []],
        frequency="replacement",
        replaces_claim_id=claim.id,
        # The test payer pays the corrected claim (its member prefix would deny it again).
        test_outcome="paid" if claim.test_outcome else None,
    )
    if work is not None and work.appeal_id:
        # Records attached to the appeal travel with the corrected claim (unsolicited 275).
        from careplix_api.claims.models import Appeal

        appeal = await session.get(Appeal, work.appeal_id)
        new.attachments = list(appeal.attachments or []) if appeal else None
    session.add(new)
    await session.flush()
    if work is not None:
        work.resolution = f"Corrected claim {pcn} drafted."
    return new
