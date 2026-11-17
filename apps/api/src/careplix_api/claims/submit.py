"""Scrub and submit (RC5, workflows R2 and R3).

:func:`scrub` runs our checks (:mod:`.scrub`) and then the clearinghouse's validation; a claim
with an error is ``blocked``, a clean one ``ready``. :func:`submit` scrubs again and sends it.

- Idempotent on the patient control number: the clearinghouse's idempotency key is
  ``<control number>-<attempt>``, so a retry after a timeout returns the first answer instead of
  a second claim, and only a deliberate resubmission (after a rejection) gets a new key.
- A 201 that carries edit failures means the clearinghouse kept the claim as rejected and didn't
  send it: the claim is ``rejected`` and joins the rejection queue (:mod:`.acks`).
- Connectivity errors leave the claim as it was (``ready``) with the message; nothing is lost.

Every validation and submission is kept as a :class:`ClaimTransaction` (the request and answer
sealed: they name the patient).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import acks, billing, scrub, service, x837
from careplix_api.claims.models import Claim, ClaimTransaction
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import clearinghouse
from careplix_api.phi import PhiBox
from careplix_api.practice.connections import audit
from careplix_api.settings import Settings

SUBMITTABLE = ("draft", "ready", "blocked", "rejected")
OUT = ("submitted", "accepted", "paid", "partially_paid", "denied")


def idempotency_key(claim: Claim) -> str:
    return f"{claim.claim_number}-{claim.submit_attempts or 1}"


async def _duplicates(session: AsyncSession, claim: Claim) -> list[dict[str, Any]]:
    """Another claim already out for the same patient, date and codes."""
    others = await session.scalars(
        select(Claim).where(
            Claim.workspace_id == claim.workspace_id,
            Claim.id != claim.id,
            Claim.patient_ref == claim.patient_ref,
            Claim.date_of_service == claim.date_of_service,
            Claim.stage.in_(OUT),
        )
    )
    codes = sorted(str(line.get("cpt")) for line in claim.lines or [])
    for other in others:
        if other.id == claim.replaces_claim_id:
            continue
        if sorted(str(line.get("cpt")) for line in other.lines or []) == codes:
            return [
                {
                    "code": "duplicate",
                    "severity": scrub.WARNING,
                    "field": "claim",
                    "line_id": None,
                    "message": f"Claim {other.claim_number} already went out for this visit.",
                    "source": "careplix",
                }
            ]
    return []


def _record(
    claim: Claim, kind: str, mode: str, *, ok: bool, summary: dict[str, Any], **kw: Any
) -> ClaimTransaction:
    return ClaimTransaction(
        workspace_id=claim.workspace_id,
        claim_id=claim.id,
        kind=kind,
        mode=mode,
        ok=ok,
        summary=summary,
        **kw,
    )


async def scrub_claim(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    gateway: clearinghouse.Clearinghouse | None = None,
) -> dict[str, Any]:
    """Our checks, then (if they pass and the clearinghouse is connected) its validation.
    Sets ``ready`` or ``blocked`` and returns the scrub record."""
    if claim.stage not in SUBMITTABLE:
        raise ApiError(409, "already_out", "This claim has already gone out.")
    now = now or utcnow()
    patient = service.patient(settings, claim)
    profile = billing.view(await billing.get(session, claim.workspace_id))
    findings = scrub.check(claim, patient, profile, now.date())
    findings += await _duplicates(session, claim)
    validated = None
    if not scrub.blocking(findings):
        validated = await _validate(session, settings, claim, patient, profile, transport, gateway)
        findings += validated.get("findings", [])
    clean = not scrub.blocking(findings)
    claim.scrub = {
        "clean": clean,
        "findings": findings,
        "checked_at": now.isoformat(),
        "clearinghouse": validated.get("state") if validated else "skipped",
    }
    if claim.stage != "rejected":
        claim.stage = "ready" if clean else "blocked"
    return claim.scrub


async def _validate(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    patient: dict[str, Any],
    profile: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None,
    gateway: clearinghouse.Clearinghouse | None,
) -> dict[str, Any]:
    conn = await service.clearinghouse_connection(session, claim.workspace_id)
    if conn is None and gateway is None:
        return {"state": "not_connected", "findings": []}
    own = gateway is None
    gw = gateway or clearinghouse.for_connection(conn, settings, transport)
    body = x837.build(claim, patient, profile, mode=gw.mode, **(await _original(session, claim)))
    try:
        problems, raw, ms = await gw.validate_claim(body, synthetic=claim.synthetic)
    except clearinghouse.ClearinghouseError as err:
        session.add(
            _record(claim, "validate", gw.mode, ok=False, summary={}, message=err.message[:300])
        )
        severity = scrub.WARNING if err.retryable else scrub.ERROR
        return {
            "state": "error",
            "findings": [_ch_finding("validation_unavailable", severity, err.message)],
        }
    finally:
        if own:
            await gw.aclose()
    session.add(
        _record(
            claim,
            "validate",
            gw.mode,
            ok=not problems,
            summary={"errors": problems},
            raw=PhiBox(settings).seal({"request": body, "response": raw}),
            duration_ms=ms,
        )
    )
    return {
        "state": "failed" if problems else "passed",
        "findings": [_ch_finding("clearinghouse_edit", scrub.ERROR, p) for p in problems],
    }


def _ch_finding(code: str, severity: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "field": "clearinghouse",
        "line_id": None,
        "message": message[:300],
        "source": "clearinghouse",
    }


async def _original(session: AsyncSession, claim: Claim) -> dict[str, Any]:
    """A corrected claim names the payer's claim number of the one it replaces."""
    if claim.frequency != "replacement" or not claim.replaces_claim_id:
        return {}
    original = await session.get(Claim, claim.replaces_claim_id)
    return {"original_payer_claim_number": original.payer_claim_number if original else None}


async def submit(
    session: AsyncSession,
    settings: Settings,
    claim: Claim,
    *,
    actor: str,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    gateway: clearinghouse.Clearinghouse | None = None,
) -> dict[str, Any]:
    """Scrub, then submit through the clearinghouse. Returns ``{state, message}`` where state is
    submitted | blocked | rejected | error | not_connected."""
    if claim.stage in OUT:
        return {"state": "submitted", "message": "Already submitted."}
    now = now or utcnow()
    conn = await service.clearinghouse_connection(session, claim.workspace_id)
    if conn is None and gateway is None:
        raise ApiError(409, "clearinghouse_not_connected", "Connect the clearinghouse first.")
    own = gateway is None
    gw = gateway or clearinghouse.for_connection(conn, settings, transport)
    try:
        result = await scrub_claim(session, settings, claim, now=now, gateway=gw)
        if not result["clean"]:
            claim.stage = "blocked"
            return {"state": "blocked", "message": "The scrub found problems to fix first."}
        if claim.submitted_at is not None:
            # The last attempt reached the clearinghouse (and was rejected): a new key. A retry
            # after a connectivity error keeps its key, so the clearinghouse answers it once.
            claim.submit_attempts = (claim.submit_attempts or 1) + 1
            claim.submitted_at = None
        claim.submit_attempts = claim.submit_attempts or 1
        patient = service.patient(settings, claim)
        profile = billing.view(await billing.get(session, claim.workspace_id))
        body = x837.build(
            claim, patient, profile, mode=gw.mode, **(await _original(session, claim))
        )
        try:
            answer = await gw.submit_claim(
                body, idempotency_key=idempotency_key(claim), synthetic=claim.synthetic
            )
        except clearinghouse.ClearinghouseError as err:
            claim.stage = "ready"
            session.add(
                _record(claim, "submit", gw.mode, ok=False, summary={}, message=err.message[:300])
            )
            if conn is not None:
                await audit(
                    session,
                    conn,
                    "clearinghouse.claim_submit",
                    "manual",
                    ok=False,
                    status=err.status,
                    http_status=err.http_status,
                    error=err.message,
                    summary={"claim": claim.claim_number},
                )
            return {"state": "error", "message": err.message}
    finally:
        if own:
            await gw.aclose()
    summary = {
        "claim_id": answer.claim_id,
        "submission_id": answer.submission_id,
        "errors": answer.errors,
        "attempt": claim.submit_attempts,
    }
    session.add(
        _record(
            claim,
            "submit",
            gw.mode,
            ok=not answer.errors,
            summary=summary,
            transaction_id=answer.submission_id,
            raw=PhiBox(settings).seal({"request": body, "response": answer.raw}),
            duration_ms=answer.ms,
        )
    )
    claim.submit_mode = gw.mode
    claim.clearinghouse_claim_id = answer.claim_id
    claim.submission_id = answer.submission_id
    claim.submitted_at = now
    if conn is not None:
        await audit(
            session,
            conn,
            "clearinghouse.claim_submit",
            "manual",
            ok=not answer.errors,
            status="success" if not answer.errors else "rejected",
            duration_ms=answer.ms,
            summary={"claim": claim.claim_number, "mode": gw.mode, "errors": len(answer.errors)},
        )
    if answer.errors:
        await acks.reject(
            session,
            claim,
            source_ref=f"submit:{answer.submission_id or claim.submit_attempts}",
            reasons=[{"kind": "edit", "code": None, "text": e} for e in answer.errors],
            now=now,
        )
        return {"state": "rejected", "message": "The clearinghouse's edits rejected the claim."}
    claim.stage = "submitted"
    claim.submitted_on = now.date().isoformat()
    claim.ack_status, claim.ack_at = None, None
    claim.status, claim.status_label = None, None
    claim.next_check_at = None
    await acks.close_rejections(session, claim, actor=actor, now=now)
    return {"state": "submitted", "message": None}


def evidence(settings: Settings, row: ClaimTransaction) -> dict[str, Any]:
    return PhiBox(settings).open(row.raw)


def transaction_view(row: ClaimTransaction) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "mode": row.mode,
        "transaction_id": row.transaction_id,
        "ok": row.ok,
        "summary": row.summary,
        "message": row.message,
        "duration_ms": row.duration_ms,
        "has_raw": bool(row.raw),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
