"""Appeals (RC8, workflow R6): letters drafted for staff, deadlines, attachments, sending.

- :func:`draft` writes the appeal letter from the claim (patient, member ID, provider, dates,
  codes, charges), the remittance (denial codes and date) and the evidence we hold (the
  clearinghouse acceptance proves timely filing). The argument depends on the denial category.
  Nothing is sent: staff read, edit and approve the letter.
- Deadline: the denial's appeal window (:func:`careplix_api.claims.denials.deadline`); the Studio
  lists appeals by days left and flags the overdue ones.
- Attachments: visit notes and records go to the payer as unsolicited 275 attachments through
  the clearinghouse (:func:`attach`), referenced from the claim that carries them: a claim before
  it goes out, or an appeal, whose files the corrected claim then carries.
- Sending: fax and payer portals have no vendor yet. :data:`SENDERS` is the hook: a sender
  registered for a channel sends the approved letter; with none, the appeal is ``ready_to_send``
  and staff send it by hand and mark it sent.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import billing, service
from careplix_api.claims.models import Appeal, Claim, ClaimTransaction, ClaimWork
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import clearinghouse
from careplix_api.integrations.stedi import ATTACHMENT_TYPES
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

# Report types the clearinghouse documents for claim attachments.
REPORT_TYPES = {"PROGRESS_REPORT": "Visit notes", "OPERATIVE_NOTE": "Operative note"}
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
# channel → async sender(appeal, letter, settings) → note. Empty until a fax/portal vendor lands.
Sender = Callable[[Appeal, str, Settings], Awaitable[str]]
SENDERS: dict[str, Sender] = {}

ARGUMENTS = {
    "medical_necessity": (
        "The service was medically necessary. The enclosed visit notes document the patient's "
        "presenting problem, the findings that required this service and the plan of care, "
        "consistent with the diagnoses billed ({diagnoses})."
    ),
    "authorization": (
        "The service was authorized (or met the plan's criteria for retroactive authorization). "
        "Please match it to the authorization on file for this member and date of service; the "
        "reference is enclosed."
    ),
    "timely_filing": (
        "The claim was filed on time. The clearinghouse accepted it on {accepted_on}, within the "
        "plan's filing limit for a {date_of_service} date of service; the acceptance report is "
        "enclosed as proof of timely filing."
    ),
    "bundling": (
        "The service on line {lines} was separate and distinct from the other services that day "
        "(a different session, site or problem), as the enclosed notes show; it should be paid "
        "separately."
    ),
}
DEFAULT_ARGUMENT = (
    "We believe the claim was billed correctly and should be paid under the member's plan. "
    "Please reconsider it with the enclosed records."
)


def level_for(plan_kind: str | None) -> str:
    return "redetermination" if plan_kind == "medicare" else "first_level"


def view(row: Appeal, today: date | None = None) -> dict[str, Any]:
    today = today or utcnow().date()
    days_left = (date.fromisoformat(row.due_on) - today).days if row.due_on else None
    return {
        "id": row.id,
        "claim_id": row.claim_id,
        "work_id": row.work_id,
        "level": row.level,
        "state": row.state,
        "due_on": row.due_on,
        "days_left": days_left,
        "overdue": days_left is not None
        and days_left < 0
        and row.state in ("draft", "approved", "ready_to_send"),
        "evidence": row.evidence or [],
        "attachments": row.attachments or [],
        "send_channel": row.send_channel,
        "send_note": row.send_note,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "has_letter": bool(row.letter),
    }


async def _accepted_on(session: AsyncSession, claim: Claim) -> str | None:
    row = await session.scalar(
        select(ClaimTransaction)
        .where(
            ClaimTransaction.claim_id == claim.id,
            ClaimTransaction.kind == "277ca",
            ClaimTransaction.ok.is_(True),
        )
        .order_by(ClaimTransaction.created_at)
    )
    return row.created_at.date().isoformat() if row and row.created_at else None


def letter_text(
    claim: Claim,
    patient: dict[str, Any],
    profile: dict[str, Any],
    work: ClaimWork,
    *,
    accepted_on: str | None,
    today: date,
) -> str:
    codes = [f"{r.get('group')}-{r['code']}" for r in work.reasons if r.get("kind") == "carc"]
    remarks = [r["code"] for r in work.reasons if r.get("kind") == "rarc"]
    lines = sorted({str(r["line_id"]) for r in work.reasons if r.get("line_id")})
    services = "; ".join(
        "-".join([str(line.get("cpt")), *(line.get("modifiers") or [])])
        + f" x{line.get('units')} ${line.get('charge')}"
        for line in claim.lines or []
    )
    name = " ".join(x for x in (patient.get("first_name"), patient.get("last_name")) if x)
    argument = ARGUMENTS.get(work.category, DEFAULT_ARGUMENT).format(
        diagnoses=", ".join(claim.diagnoses or []) or "on the claim",
        accepted_on=accepted_on or "(see the enclosed acceptance report)",
        date_of_service=claim.date_of_service,
        lines=", ".join(lines) or "the denied line",
    )
    kind = (
        "Request for redetermination"
        if level_for(claim.plan_kind) == "redetermination"
        else ("Request for reconsideration (first-level appeal)")
    )
    return "\n".join(
        [
            f"{today.isoformat()}",
            "",
            f"{claim.payer_name}",
            "Appeals department",
            "",
            f"Re: {kind}",
            f"Patient: {name}",
            f"Date of birth: {patient.get('date_of_birth') or ''}",
            f"Member ID: {patient.get('member_id') or ''}",
            f"Claim number: {claim.payer_claim_number or claim.claim_number}",
            f"Our reference: {claim.claim_number}",
            f"Date of service: {claim.date_of_service}",
            f"Services: {services}",
            f"Billed: ${claim.billed_amount}; denied: ${work.amount or ''}",
            f"Denial codes: {', '.join(codes + remarks) or 'none given'}",
            "",
            "To whom it may concern,",
            "",
            f"{profile.get('organization')} (NPI {profile.get('npi')}) asks you to reconsider "
            f"the denial of the claim above.",
            "",
            argument,
            "",
            "Please reprocess the claim for payment. Contact our billing office at "
            f"{profile.get('contact_phone')} with any question.",
            "",
            "Sincerely,",
            "",
            "______________________",
            f"{profile.get('organization')}",
            f"{profile.get('address1')}, {profile.get('city')}, {profile.get('state')} "
            f"{profile.get('postal_code')}",
            "",
            "Enclosures: " + "; ".join(e["label"] for e in evidence_for(work, accepted_on)),
        ]
    )


def evidence_for(work: ClaimWork, accepted_on: str | None) -> list[dict[str, Any]]:
    """What goes with the appeal (labels only: the files are attachments)."""
    out = [
        {"key": "claim", "label": "Copy of the claim", "have": True},
        {"key": "remittance", "label": "Remittance advice (835) for this claim", "have": True},
    ]
    if work.category == "timely_filing":
        out.append(
            {
                "key": "acceptance",
                "label": f"Clearinghouse acceptance report ({accepted_on or 'on file'})",
                "have": bool(accepted_on),
            }
        )
    if work.category in ("medical_necessity", "bundling", "missing_information", "other"):
        out.append({"key": "notes", "label": "Visit notes for the date of service", "have": False})
    if work.category == "authorization":
        out.append({"key": "auth", "label": "Authorization approval", "have": False})
    return out


async def draft(session: AsyncSession, settings: Settings, claim: Claim, work: ClaimWork) -> Appeal:
    """The appeal for a denial, drafted once (an existing one is returned)."""
    if work.appeal_id:
        existing = await session.get(Appeal, work.appeal_id)
        if existing is not None:
            return existing
    today = utcnow().date()
    accepted_on = await _accepted_on(session, claim)
    profile = billing.view(await billing.get(session, claim.workspace_id))
    patient = service.patient(settings, claim)
    from careplix_api.claims import denials

    appeal = Appeal(
        workspace_id=claim.workspace_id,
        claim_id=claim.id,
        work_id=work.id,
        level=level_for(claim.plan_kind),
        state="draft",
        due_on=work.due_on or denials.deadline(claim.plan_kind, denials.APPEAL, None, today),
        letter=PhiBox(settings).seal(
            {
                "text": letter_text(
                    claim, patient, profile, work, accepted_on=accepted_on, today=today
                )
            }
        ),
        evidence=evidence_for(work, accepted_on),
        attachments=[],
    )
    session.add(appeal)
    await session.flush()
    work.appeal_id = appeal.id
    return appeal


def letter(settings: Settings, appeal: Appeal) -> str:
    return str(PhiBox(settings).open(appeal.letter).get("text") or "")


def edit(settings: Settings, appeal: Appeal, text: str) -> Appeal:
    if appeal.state not in ("draft", "approved"):
        raise ApiError(409, "invalid_state", f"This appeal is {appeal.state}.")
    appeal.letter = PhiBox(settings).seal({"text": text})
    appeal.state = "draft"  # an edited letter is approved again
    appeal.approved_by = appeal.approved_at = None
    return appeal


async def approve(settings: Settings, appeal: Appeal, actor: str, channel: str = "fax") -> Appeal:
    """Staff approval, then the send hook: a registered sender sends it; otherwise it's ready to
    send by hand."""
    if appeal.state != "draft":
        raise ApiError(409, "invalid_state", f"This appeal is {appeal.state}.")
    appeal.state, appeal.approved_by, appeal.approved_at = "approved", actor, utcnow()
    appeal.send_channel = channel
    sender = SENDERS.get(channel)
    if sender is None:
        appeal.state = "ready_to_send"
        appeal.send_note = (
            f"No {channel} service is connected yet: print or send the letter and its "
            "attachments by hand, then mark it sent."
        )
        return appeal
    appeal.send_note = (await sender(appeal, letter(settings, appeal), settings))[:200]
    appeal.state, appeal.sent_at = "sent", utcnow()
    return appeal


def mark_sent(appeal: Appeal, note: str | None) -> Appeal:
    if appeal.state not in ("approved", "ready_to_send"):
        raise ApiError(409, "invalid_state", "Approve the appeal before marking it sent.")
    appeal.state, appeal.sent_at = "sent", utcnow()
    appeal.send_note = (note or "Sent by hand.")[:200]
    return appeal


async def attach(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    *,
    label: str,
    content_type: str,
    data_base64: str,
    report_type: str,
    appeal: Appeal | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Upload one file as an unsolicited 275 attachment and reference it from the claim (and the
    appeal). The claim's next submission (a corrected claim) carries it to the payer."""
    if appeal is None and claim.stage not in ("draft", "ready", "blocked", "rejected"):
        raise ApiError(
            409,
            "claim_out",
            "Attach records to a claim before it goes out (a corrected claim), or to its appeal.",
        )
    if content_type not in ATTACHMENT_TYPES:
        raise ApiError(422, "unsupported_type", "Attach a PDF, TIFF, JPEG or PNG.")
    if report_type not in REPORT_TYPES:
        raise ApiError(422, "unknown_report_type", "Pick a listed report type.")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except ValueError:
        raise ApiError(422, "invalid_file", "The file isn't valid base64.") from None
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise ApiError(422, "file_size", "Attachments are up to 6 MB.")
    conn = await service.clearinghouse_connection(session, claim.workspace_id)
    if conn is None:
        raise ApiError(409, "clearinghouse_not_connected", "Connect the clearinghouse first.")
    gateway = clearinghouse.for_connection(conn, settings, transport)
    if gateway.mode == clearinghouse.TEST and not claim.synthetic:
        await gateway.aclose()
        raise ApiError(409, "test_mode", "Test mode only sends files for synthetic claims.")
    try:
        attachment_id, ms = await gateway.upload_attachment(content_type, data)
    except clearinghouse.ClearinghouseError as err:
        session.add(
            ClaimTransaction(
                workspace_id=claim.workspace_id,
                claim_id=claim.id,
                kind="attachment",
                mode=gateway.mode,
                ok=False,
                summary={"label": label[:80]},
                message=err.message[:300],
            )
        )
        raise ApiError(502, "clearinghouse_error", err.message) from None
    finally:
        await gateway.aclose()
    entry = {
        "attachment_id": attachment_id,
        "label": label[:80],
        "content_type": content_type,
        "report_type": report_type,
        "uploaded_at": (now or utcnow()).isoformat(),
    }
    if appeal is None:
        claim.attachments = [*(claim.attachments or []), entry]
    else:
        appeal.attachments = [*(appeal.attachments or []), entry]
        appeal.evidence = [
            {**e, "have": True} if e["key"] == "notes" and report_type == "PROGRESS_REPORT" else e
            for e in appeal.evidence or []
        ]
    session.add(
        ClaimTransaction(
            workspace_id=claim.workspace_id,
            claim_id=claim.id,
            kind="attachment",
            mode=gateway.mode,
            transaction_id=attachment_id,
            ok=True,
            summary={"label": label[:80], "report_type": report_type, "bytes": len(data)},
            duration_ms=ms,
        )
    )
    return entry
