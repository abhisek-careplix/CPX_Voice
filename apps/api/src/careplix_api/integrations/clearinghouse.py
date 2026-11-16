"""The clearinghouse gateway (RC1): one vendor-neutral front for X12 transactions.

The product says "Clearinghouse" everywhere (RCM_CLEARINGHOUSE_PLAN §1.3). This module is what
claims code talks to: it builds requests from neutral facts, keeps test and production apart, and
turns answers into neutral summaries. The vendor's client (:mod:`.stedi`) stays behind it, and
every message that leaves here has the vendor's name taken out (:func:`white_label`).

- Claim status (276/277): :meth:`Clearinghouse.claim_status`, summarised by
  :func:`summarize_claim_status` into one of :data:`STATUSES`.
- Payer directory: :meth:`Clearinghouse.search_payers`, each payer reduced by :func:`payer_entry`
  to our id, name and which transactions it supports.

Eligibility still calls the vendor client directly from ``practice/``; moving it behind this
gateway is part of RC4.

Test mode: the vendor's test keys answer only its published test members, so a test gateway sends
the published claim status example instead of the claim's patient (no patient identifier leaves
CarePlix in test mode) and labels the answer ``mock``. Production keys are refused for careplix-labs
by ``infra/gcp/deploy-labs.sh``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from careplix_api.integrations import stedi

TEST, PRODUCTION = stedi.TEST, stedi.PRODUCTION
NAME = "Clearinghouse"
white_label = stedi.white_label

# --- claim status categories (X12 507, the 277's STC01-1) ----------------------------------------

PAID, DENIED, NO_PAYMENT, FINALIZED = "paid", "denied", "no_payment", "finalized"
PENDING, ACCEPTED, INFO_REQUESTED = "pending", "accepted", "info_requested"
REJECTED, NOT_FOUND, ERROR, UNAVAILABLE = "rejected", "not_found", "error", "unavailable"
STATUSES = (
    PAID,
    DENIED,
    NO_PAYMENT,
    FINALIZED,
    PENDING,
    ACCEPTED,
    INFO_REQUESTED,
    REJECTED,
    NOT_FOUND,
    ERROR,
    UNAVAILABLE,
)
FINAL = frozenset({PAID, DENIED, NO_PAYMENT, FINALIZED})
STATUS_LABEL = {
    PAID: "Paid",
    DENIED: "Denied",
    NO_PAYMENT: "Finalized, no payment",
    FINALIZED: "Finalized",
    PENDING: "Pending with the payer",
    ACCEPTED: "Accepted by the payer",
    INFO_REQUESTED: "Payer needs more information",
    REJECTED: "Rejected by the payer",
    NOT_FOUND: "Payer can't find the claim",
    ERROR: "Payer couldn't answer",
    UNAVAILABLE: "No electronic status",
}
_BY_CATEGORY = {
    "A0": ACCEPTED,
    "A1": ACCEPTED,
    "A2": ACCEPTED,
    "A3": REJECTED,
    "A4": NOT_FOUND,
    "A5": ACCEPTED,
    "A6": REJECTED,
    "A7": REJECTED,
    "A8": REJECTED,
    "D0": NOT_FOUND,
    "F0": FINALIZED,
    "F1": PAID,
    "F2": DENIED,
    "F3": FINALIZED,
    "F3F": FINALIZED,
    "F3N": FINALIZED,
    "F4": NO_PAYMENT,
    "RQ": INFO_REQUESTED,
}


def status_for_category(code: str | None) -> str:
    """Our status for a 277 category code: P* pending, R* information requested, E* error."""
    code = (code or "").strip().upper()
    if code in _BY_CATEGORY:
        return _BY_CATEGORY[code]
    if code.startswith("P"):
        return PENDING
    if code.startswith("R"):
        return INFO_REQUESTED
    if code.startswith("E"):
        return ERROR
    return ERROR if code else NOT_FOUND


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _iso(value: Any) -> str | None:
    """``YYYYMMDD`` or ``YYYY-MM-DD`` (the first date of a range) as ``YYYY-MM-DD``."""
    text = str(value or "")
    if re.fullmatch(r"\d{8}(-\d{8})?", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10] or None


def _pick_claim(
    claims: list[dict[str, Any]], account: str | None, payer_claim: str | None
) -> dict[str, Any]:
    for c in claims:
        s = c.get("claimStatus") or {}
        if account and s.get("patientAccountNumber") == account:
            return c
        if payer_claim and s.get("tradingPartnerClaimNumber") == payer_claim:
            return c
    return claims[0]


def summarize_claim_status(
    response: dict[str, Any],
    *,
    patient_account_number: str | None = None,
    payer_claim_number: str | None = None,
) -> dict[str, Any]:
    """A 277 answer as outcome fields only: status, codes, dates and amounts (no patient data).

    One inquiry can return several claims (a date range); the one with our account number or the
    payer's claim number wins, else the first. A finalized claim (F0/F3) that paid something is
    ``paid``; one whose every line was denied is ``denied``."""
    claims = [c for c in response.get("claims") or [] if isinstance(c, dict)]
    error_body = response.get("errorResponse") or {}
    errors = stedi.parse_errors(response) + stedi.parse_errors(error_body)
    payer = response.get("payer") or {}
    base: dict[str, Any] = {
        "payer": payer.get("organizationName"),
        "claims_returned": len(claims),
        "errors": [e["code"] for e in errors],
    }
    if not claims:
        status = ERROR if errors else NOT_FOUND
        message = stedi.error_message(errors, None) if errors else None
        return {
            **base,
            "status": status,
            "label": STATUS_LABEL[status],
            "category_code": None,
            "message": white_label(message or error_body.get("description")),
            "service_lines": [],
        }
    claim = _pick_claim(claims, patient_account_number, payer_claim_number)
    s = claim.get("claimStatus") or {}
    category = str(s.get("statusCategoryCode") or "").upper() or None
    status = status_for_category(category)
    lines = []
    for detail in claim.get("serviceDetails") or []:
        service = detail.get("service") or {}
        line_status = next(iter(detail.get("status") or []), {})
        lines.append(
            {
                "procedure": service.get("procedureId"),
                "submitted": service.get("submittedAmount"),
                "paid": service.get("amountPaid"),
                "category_code": line_status.get("statusCategoryCode"),
                "status_code": line_status.get("statusCode"),
                "status": status_for_category(line_status.get("statusCategoryCode")),
            }
        )
    paid = _money(s.get("amountPaid"))
    if status == FINALIZED:
        if paid and paid > 0:
            status = PAID
        elif lines and all(line["status"] == DENIED for line in lines):
            status = DENIED
    return {
        **base,
        "status": status,
        "label": STATUS_LABEL[status],
        "category_code": category,
        "category_text": s.get("statusCategoryCodeValue"),
        "status_code": s.get("statusCode"),
        "status_text": s.get("statusCodeValue"),
        "entity": s.get("entity") or s.get("entityCode"),
        "effective_date": _iso(s.get("effectiveDate")),
        "paid_date": _iso(s.get("paidDate")),
        "amount_paid": s.get("amountPaid"),
        "submitted_amount": s.get("submittedAmount"),
        "check_number": s.get("checkNumber"),
        "check_issue_date": _iso(s.get("checkIssueDate")),
        "payer_claim_number": s.get("tradingPartnerClaimNumber"),
        "service_date": _iso(s.get("claimServiceDate")),
        "service_lines": lines,
        "message": None,
    }


# --- claim status requests -----------------------------------------------------------------------


@dataclass
class ClaimQuery:
    """What the payer needs to find one claim, in our words."""

    payer_id: str
    provider_npi: str
    subscriber: dict[str, str]  # first_name, last_name, member_id, date_of_birth, gender
    service_from: date
    service_to: date | None = None
    dependent: dict[str, str] | None = None
    billed_amount: str | None = None
    patient_account_number: str | None = None  # our claim number (CLM01)
    payer_claim_number: str | None = None
    provider_name: str | None = None


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _x12_person(values: dict[str, str]) -> dict[str, Any]:
    out = {"firstName": values["first_name"], "lastName": values["last_name"]}
    if values.get("date_of_birth"):
        out["dateOfBirth"] = str(values["date_of_birth"])[:10].replace("-", "")
    if values.get("gender") in ("M", "F"):
        out["gender"] = values["gender"]
    return out


def claim_status_request(q: ClaimQuery, today: date) -> dict[str, Any]:
    """A production 276. Dates: the service dates widened by 7 days, never in the future and at
    most 30 days apart (the vendor's guidance). The provider name is left out so the NPI's NPPES
    name is used."""
    start = q.service_from - timedelta(days=7)
    end = min((q.service_to or q.service_from) + timedelta(days=7), today)
    if (end - start).days > 30:
        start = end - timedelta(days=30)
    subscriber = _x12_person(q.subscriber)
    subscriber["memberId"] = q.subscriber["member_id"]
    encounter: dict[str, Any] = {
        "beginningDateOfService": _yyyymmdd(start),
        "endDateOfService": _yyyymmdd(end),
    }
    if q.billed_amount:
        encounter["submittedAmount"] = str(q.billed_amount)
    if q.patient_account_number:
        encounter["patientAccountNumber"] = q.patient_account_number
    if q.payer_claim_number:
        encounter["tradingPartnerClaimNumber"] = q.payer_claim_number
    body: dict[str, Any] = {
        "tradingPartnerServiceId": q.payer_id,
        "providers": [{"npi": q.provider_npi, "providerType": "BillingProvider"}],
        "subscriber": subscriber,
        "encounter": encounter,
    }
    if q.dependent:
        body["dependent"] = _x12_person(q.dependent)
    return body


# --- the gateway ---------------------------------------------------------------------------------


class ClearinghouseError(Exception):
    """A transaction that didn't produce an answer. ``outcome``: retry | correct | not_found."""

    def __init__(self, status: str, message: str, outcome: str, http_status: int | None = None):
        super().__init__(message)
        self.status, self.message, self.outcome = status, white_label(message) or "", outcome
        self.http_status = http_status

    @property
    def retryable(self) -> bool:
        return self.outcome == stedi.RETRY


@dataclass
class ClaimStatusAnswer:
    summary: dict[str, Any]
    raw: dict[str, Any]
    ms: int
    mock: bool
    request: dict[str, Any] = field(repr=False, default_factory=dict)


class Clearinghouse:
    """Vendor-neutral transactions over one vendor client, locked to its mode."""

    def __init__(self, client: stedi.StediClient):
        self._client = client

    @property
    def mode(self) -> str:
        return self._client.mode

    async def aclose(self) -> None:
        await self._client.aclose()

    async def claim_status(self, query: ClaimQuery, *, today: date) -> ClaimStatusAnswer:
        mock = self.mode == TEST
        request = stedi.claim_status_mock_request() if mock else claim_status_request(query, today)
        try:
            body, ms = await self._client.claim_status(request)
        except stedi.StediError as err:
            raise ClearinghouseError(
                err.status, err.message, err.outcome, err.http_status
            ) from None
        summary = summarize_claim_status(
            body,
            patient_account_number=None if mock else query.patient_account_number,
            payer_claim_number=None if mock else query.payer_claim_number,
        )
        return ClaimStatusAnswer(summary, body, ms, mock, request)

    async def search_payers(self, query: str) -> list[dict[str, Any]]:
        try:
            found = await self._client.search_payers(query, eligibility_only=False)
        except stedi.StediError as err:
            raise ClearinghouseError(
                err.status, err.message, err.outcome, err.http_status
            ) from None
        return [payer_entry(p) for p in found if p]


def for_connection(
    conn: Any, settings: Any, transport: httpx.AsyncBaseTransport | None = None
) -> Clearinghouse:
    """The gateway for a workspace's clearinghouse connection (its mode decides the key)."""
    from careplix_api.practice.connections import stedi_client  # local: practice imports us

    return Clearinghouse(stedi_client(conn, settings, transport, timeout_s=65.0))


# --- payer directory -----------------------------------------------------------------------------

SUPPORTED, NOT_SUPPORTED, ENROLLMENT_REQUIRED = "SUPPORTED", "NOT_SUPPORTED", "ENROLLMENT_REQUIRED"
_SUPPORT_KEYS = {
    "eligibility": "eligibilityCheck",
    "claim_status": "claimStatus",
    "claims": "professionalClaimSubmission",
    "era": "claimPayment",
}


def payer_entry(payer: dict[str, Any]) -> dict[str, Any]:
    """Our directory row from a network payer record: ids, name and transaction support."""
    support = payer.get("transactionSupport") or {}
    return {
        "payer_id": str(payer.get("primaryPayerId") or payer.get("stediId") or ""),
        "network_id": payer.get("stediId"),
        "name": payer.get("displayName") or payer.get("name") or "",
        "aliases": [str(a) for a in payer.get("aliases") or []],
        "programs": list(payer.get("programs") or []),
        "support": {ours: support.get(theirs) for ours, theirs in _SUPPORT_KEYS.items()},
    }


def claim_status_route(payer_id: str | None, support: str | None) -> str | None:
    """Why a payer has no electronic claim status, or None when it may answer.

    Traditional Medicare has none (no MAC answers 276/277): its claims are followed by 277CA and
    835 age, and by phone."""
    if (payer_id or "").upper() == stedi.CMS:
        return "Traditional Medicare doesn't answer electronic claim status."
    if support == NOT_SUPPORTED:
        return "This payer doesn't answer electronic claim status."
    return None
