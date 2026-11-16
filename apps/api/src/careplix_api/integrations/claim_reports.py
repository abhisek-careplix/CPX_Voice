"""The clearinghouse's claim reports, reduced to neutral facts: 277CA acknowledgements and 835
remittances (its ``reports/v2/{transactionId}/277|835`` JSON).

277CA (claim acknowledgement):
``transactions[].payers[].claimStatusTransactions[].claimStatusDetails[]
.patientClaimStatusDetails[].claims[]``, each with ``claimStatus`` (our patient control number in
``referencedTransactionTraceNumber`` or ``patientAccountNumber``, the payer's claim number in
``tradingPartnerClaimNumber``, statuses in ``informationClaimStatuses[].informationStatuses[]``)
and ``serviceLines[].serviceClaimStatuses[]``. Provider-level statuses
(``providerClaimStatuses[].providerStatuses[]``) apply to every claim under that provider.

835 (remittance): ``transactions[]`` with ``financialInformation`` and
``paymentAndRemitReassociationDetails.checkOrEFTTraceNumber``, then
``detailInfo[].paymentInfo[]`` per claim: ``claimPaymentInfo`` (control number, status code,
charge, paid, patient share, payer claim number), ``claimAdjustments[]`` and ``serviceLines[]``
with ``servicePaymentInformation`` and ``serviceAdjustments[]``. Adjustment groups carry up to six
reason/amount pairs (``adjustmentReasonCode1``…``6``, ``adjustmentAmount1``…``6``).

Neither summary keeps names, birth dates or member IDs: only numbers, codes, amounts and the
payer's own status text.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# 277 claim status categories (X12 507) that mean the claim was not accepted.
REJECT_CATEGORIES = frozenset({"A3", "A4", "A6", "A7", "A8"})
ACCEPT_CATEGORIES = frozenset({"A0", "A1", "A2", "A5"})
ACCEPTED, REJECTED, PENDING = "accepted", "rejected", "pending"


def _list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value or [] if isinstance(v, dict)] if isinstance(value, list) else []


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _iso(value: Any) -> str | None:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] or None


# --- 277CA -----------------------------------------------------------------------------------


def _statuses(blocks: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    out = []
    for block in blocks:
        message = block.get("statusMessage")
        for st in _list(block.get(key)):
            out.append(
                {
                    "category": str(st.get("healthCareClaimStatusCategoryCode") or "").upper(),
                    "category_text": st.get("healthCareClaimStatusCategoryCodeValue"),
                    "code": str(st.get("statusCode") or "") or None,
                    "text": st.get("statusCodeValue"),
                    "entity": st.get("entityIdentifierCode"),
                    "entity_text": st.get("entityIdentifierCodeValue"),
                    "message": message,
                }
            )
        if message and not _list(block.get(key)):
            out.append({"category": "", "code": None, "text": None, "message": message})
    return out


def _verdict(statuses: list[dict[str, Any]]) -> str:
    categories = {s["category"] for s in statuses if s.get("category")}
    if categories & REJECT_CATEGORIES:
        return REJECTED
    if categories & ACCEPT_CATEGORIES:
        return ACCEPTED
    return PENDING


def summarize_277ca(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per claim in the acknowledgement: our control number, the verdict and why."""
    out: list[dict[str, Any]] = []
    for tx in _list(report.get("transactions")):
        for payer in _list(tx.get("payers")):
            payer_name = payer.get("organizationName")
            for cst in _list(payer.get("claimStatusTransactions")):
                batch = cst.get("claimTransactionBatchNumber")
                provider_level = _statuses(
                    _list(cst.get("providerClaimStatuses")), "providerStatuses"
                )
                for detail in _list(cst.get("claimStatusDetails")):
                    for patient in _list(detail.get("patientClaimStatusDetails")):
                        for claim in _list(patient.get("claims")):
                            out.append(_ack_claim(claim, payer_name, batch, provider_level))
    return out


def _ack_claim(
    claim: dict[str, Any],
    payer_name: str | None,
    batch: str | None,
    provider_level: list[dict[str, Any]],
) -> dict[str, Any]:
    cs = claim.get("claimStatus") or {}
    statuses = _statuses(_list(cs.get("informationClaimStatuses")), "informationStatuses")
    lines = []
    for line in _list(claim.get("serviceLines")):
        line_statuses = _statuses(_list(line.get("serviceClaimStatuses")), "serviceStatuses")
        service = line.get("service") or {}
        lines.append(
            {
                "line_id": line.get("lineItemControlNumber"),
                "procedure": service.get("procedureCode"),
                "statuses": line_statuses,
                "verdict": _verdict(line_statuses),
            }
        )
    everything = statuses + [s for line in lines for s in line["statuses"]]
    # A provider-level rejection rejects every claim under it.
    if _verdict(provider_level) == REJECTED:
        everything += provider_level
    pcn = cs.get("referencedTransactionTraceNumber") or cs.get("patientAccountNumber")
    return {
        "patient_control_number": str(pcn or "") or None,
        "payer_claim_number": cs.get("tradingPartnerClaimNumber"),
        "payer": payer_name,
        "batch": batch,
        "verdict": _verdict(everything),
        "action": cs.get("statusInformationActionCode"),
        "effective_date": _iso(cs.get("statusInformationEffectiveDate")),
        "charge": cs.get("totalClaimChargeAmount"),
        "statuses": statuses,
        "provider_statuses": provider_level,
        "lines": lines,
    }
