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


# --- 835 -------------------------------------------------------------------------------------


def adjustments(groups: list[dict[str, Any]], line_id: str | None = None) -> list[dict[str, Any]]:
    """CAS segments as flat rows: group (CO/PR/OA/PI), reason code, amount, quantity."""
    out = []
    for group in groups:
        code_group = str(group.get("claimAdjustmentGroupCode") or "").upper()
        for i in range(1, 7):
            code = group.get(f"adjustmentReasonCode{i}")
            if not code:
                continue
            out.append(
                {
                    "group": code_group,
                    "code": str(code),
                    "amount": _fmt(_money(group.get(f"adjustmentAmount{i}"))),
                    "quantity": group.get(f"adjustmentQuantity{i}"),
                    "text": group.get(f"adjustmentReason{i}"),
                    "line_id": line_id,
                }
            )
    return out


def _remarks(node: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for key in ("outpatientAdjudication", "inpatientAdjudication"):
        block = node.get(key) or {}
        for i in range(1, 6):
            code = block.get(f"claimPaymentRemarkCode{i}")
            if code:
                codes.append(str(code))
    return codes


def _line_remarks(line: dict[str, Any]) -> list[str]:
    codes = []
    for key in ("healthCareCheckRemarkCodes", "healthCareRemarkCodes"):
        for r in _list(line.get(key)):
            code = r.get("remarkCode") or r.get("code")
            if code:
                codes.append(str(code))
    return codes


CLAIM_STATUS = {
    "1": "processed_primary",
    "2": "processed_secondary",
    "3": "processed_tertiary",
    "4": "denied",
    "19": "processed_primary_forwarded",
    "20": "processed_secondary_forwarded",
    "21": "processed_tertiary_forwarded",
    "22": "reversal",
    "23": "not_our_claim_forwarded",
    "25": "predetermination",
}


def outcome(status_code: str, charge: Decimal, paid: Decimal) -> str:
    """paid | partially_paid | denied | reversed. Status 4 is a denial; otherwise the paid amount
    decides (the payer's status code alone doesn't say whether anything was paid)."""
    if status_code == "22":
        return "reversed"
    if status_code == "4" or paid <= 0:
        return "denied"
    return "paid" if paid >= charge else "partially_paid"


def summarize_835(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per claim payment, with the check/EFT it came in."""
    out: list[dict[str, Any]] = []
    for tx in _list(report.get("transactions")):
        fin = tx.get("financialInformation") or {}
        trace = (tx.get("paymentAndRemitReassociationDetails") or {}).get("checkOrEFTTraceNumber")
        payer = ((tx.get("payer") or {}).get("name")) or (tx.get("payer") or {}).get(
            "organizationName"
        )
        for detail in _list(tx.get("detailInfo")):
            for info in _list(detail.get("paymentInfo")):
                cpi = info.get("claimPaymentInfo") or {}
                charge = _money(cpi.get("totalClaimChargeAmount"))
                paid = _money(cpi.get("claimPaymentAmount"))
                status_code = str(cpi.get("claimStatusCode") or "")
                adj = adjustments(_list(info.get("claimAdjustments")))
                remarks = _remarks(info)
                lines = []
                for line in _list(info.get("serviceLines")):
                    spi = line.get("servicePaymentInformation") or {}
                    line_id = line.get("lineItemControlNumber")
                    line_adj = adjustments(_list(line.get("serviceAdjustments")), line_id)
                    adj += line_adj
                    remarks += [r for r in _line_remarks(line) if r not in remarks]
                    lines.append(
                        {
                            "line_id": line_id,
                            "procedure": spi.get("adjudicatedProcedureCode"),
                            "charge": _fmt(_money(spi.get("lineItemChargeAmount"))),
                            "paid": _fmt(_money(spi.get("lineItemProviderPaymentAmount"))),
                            "allowed": (line.get("serviceSupplementalAmounts") or {}).get(
                                "allowedActual"
                            ),
                        }
                    )
                out.append(
                    {
                        "patient_control_number": str(cpi.get("patientControlNumber") or "")
                        or None,
                        "payer_claim_number": cpi.get("payerClaimControlNumber"),
                        "payer": payer,
                        "status_code": status_code or None,
                        "status": CLAIM_STATUS.get(status_code),
                        "outcome": outcome(status_code, charge, paid),
                        "charge": _fmt(charge),
                        "paid": _fmt(paid),
                        "patient_responsibility": _fmt(
                            _money(cpi.get("patientResponsibilityAmount"))
                        ),
                        "adjustments": adj,
                        "remarks": remarks,
                        "lines": lines,
                        "trace_number": trace,
                        "payment_method": fin.get("paymentMethodCode"),
                        "paid_on": _iso(fin.get("checkIssueOrEFTEffectiveDate")),
                        "total_payment": fin.get("totalActualProviderPaymentAmount"),
                    }
                )
    return out
