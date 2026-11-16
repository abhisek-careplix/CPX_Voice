"""The professional claim (837P) in the clearinghouse's claim JSON (its 2025-03-07 claim API).

:func:`build` turns one claim, its patient and the workspace's billing profile into the request
body the clearinghouse validates and submits. The body follows the CMS-1500 layout of that API:

- ``billing``: billing provider (NPI, taxonomy, address), tax ID, our patient control number and
  the total charge; ``encounter``: diagnoses (ICD-10-CM without the dot, the first is primary),
  place of service and, for a corrected claim, the payer's original claim number;
- ``insured``: the subscriber (patients here are always the subscriber); ``payer``: the payer ID;
- ``serviceLines``: date, CPT/HCPCS with up to four modifiers, units, charge, up to four
  diagnosis codes (codes, not pointers, in this API) and our line control number.

Test mode (the clearinghouse's test key): a claim with a ``test_outcome`` goes to the
clearinghouse's test payer (payer ID ``STEDI``), whose member-ID prefix picks the remittance that
comes back: paid in full, partially paid (60 % paid, 20 % contractual, 20 % patient share) or
denied. Other test claims go to their real payer ID and get a test 277CA only. A production key
never sends to the test payer or with a test prefix, and a test key never sends a claim that isn't
synthetic (:func:`is_test_claim`, enforced by the gateway).
"""

from __future__ import annotations

import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

TEST_PAYER_ID = "STEDI"
TEST_PAYER_NAME = "Clearinghouse test payer"
# The published member-ID prefixes that choose the test payer's remittance.
TEST_PREFIX = {
    "paid": "STEDI_PAID_",
    "partial": "STEDI_PARTIALLY_PAID_",
    "denied": "STEDI_DENIED_",
}
TEST_OUTCOMES = tuple(TEST_PREFIX)
MAX_LINES, MAX_DIAGNOSES, MAX_LINE_DX, MAX_MODIFIERS = 50, 12, 4, 4
PCN_MAX = 17  # CLM01: the payer may truncate longer numbers
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
INSURANCE_TYPE = {"medicare": "MEDICARE", "medicaid": "MEDICAID", "tricare": "TRICARE"}
GENDER = {"F": "FEMALE", "M": "MALE"}


def new_control_number() -> str:
    """Our patient control number: ``CPX`` + 10 random characters (13, unique across workspaces,
    so an acknowledgement or remittance finds its claim without knowing the workspace)."""
    return "CPX" + "".join(secrets.choice(_CROCKFORD) for _ in range(10))


def icd10(code: Any) -> str:
    """ICD-10-CM as claims want it: upper case, no dot or spaces."""
    return re.sub(r"[\s.]", "", str(code or "")).upper()


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def amount(value: Any) -> str:
    return f"{money(value):.2f}"


def units(value: Any) -> str:
    q = money(value)
    return str(int(q)) if q == q.to_integral_value() else f"{q.normalize()}"


def total(lines: list[dict[str, Any]]) -> str:
    return amount(sum((money(line.get("charge")) for line in lines), Decimal("0")))


def line_diagnoses(line: dict[str, Any], diagnoses: list[str]) -> list[str]:
    """A line's diagnosis codes from its 1-based pointers (the primary diagnosis when none)."""
    pointers = [int(p) for p in line.get("dx") or [] if str(p).isdigit()] or [1]
    return [diagnoses[p - 1] for p in pointers[:MAX_LINE_DX] if 0 < p <= len(diagnoses)]


def member_id(claim: Any, patient: dict[str, Any], mode: str) -> str:
    member = str(patient.get("member_id") or "")
    if mode == "test" and claim.test_outcome in TEST_PREFIX:
        return TEST_PREFIX[claim.test_outcome] + re.sub(r"[^A-Z0-9]", "", member.upper())
    return member


def payer(claim: Any, mode: str) -> tuple[str, str]:
    if mode == "test" and claim.test_outcome in TEST_PREFIX:
        return TEST_PAYER_ID, TEST_PAYER_NAME
    return claim.payer_id or "", claim.payer_name


def _address(values: dict[str, Any]) -> dict[str, str]:
    out = {
        "addressLine1": str(values.get("address1") or ""),
        "city": str(values.get("city") or ""),
        "state": str(values.get("state") or ""),
        "postalCode": re.sub(r"\D", "", str(values.get("postal_code") or "")),
    }
    if values.get("address2"):
        out["addressLine2"] = str(values["address2"])
    return out


def _profile_dict(profile: Any) -> dict[str, Any]:
    if isinstance(profile, dict):
        return profile
    return {
        k: getattr(profile, k)
        for k in (
            "organization",
            "npi",
            "tax_id",
            "taxonomy",
            "address1",
            "city",
            "state",
            "postal_code",
            "submitter_name",
            "contact_phone",
        )
    }


def build(
    claim: Any,
    patient: dict[str, Any],
    profile: Any,
    *,
    mode: str,
    original_payer_claim_number: str | None = None,
) -> dict[str, Any]:
    """The claim request body. ``mode`` is the gateway's (test or production)."""
    bp = _profile_dict(profile)
    diagnoses = [icd10(d) for d in claim.diagnoses or [] if icd10(d)]
    lines = claim.lines or []
    pos = claim.place_of_service or "11"
    payer_id, payer_name = payer(claim, mode)
    identifiers = {"npi": str(bp["npi"])}
    if bp.get("taxonomy"):
        identifiers["taxonomyCode"] = str(bp["taxonomy"])
    encounter: dict[str, Any] = {
        "primaryDiagnosisCode": diagnoses[0] if diagnoses else "",
        "primaryPlaceOfService": pos,
    }
    if diagnoses[1:]:
        encounter["additionalDiagnosisCodes"] = diagnoses[1:MAX_DIAGNOSES]
    if claim.frequency == "replacement" and original_payer_claim_number:
        encounter["resubmission"] = {
            "code": "REPLACEMENT_OF_PRIOR_CLAIM",
            "originalReferenceNumber": original_payer_claim_number,
        }
    insured: dict[str, Any] = {
        "insuranceType": INSURANCE_TYPE.get(claim.plan_kind or "", "OTHER"),
        "memberId": member_id(claim, patient, mode),
        "name": {
            "person": {
                "firstName": str(patient.get("first_name") or ""),
                "lastName": str(patient.get("last_name") or ""),
            }
        },
        "paymentResponsibilityLevelCode": "PRIMARY",
    }
    if patient.get("date_of_birth"):
        insured["dateOfBirth"] = str(patient["date_of_birth"])[:10]
    insured["gender"] = GENDER.get(str(patient.get("gender") or "").upper()[:1], "UNKNOWN")
    if isinstance(patient.get("address"), dict) and patient["address"].get("address1"):
        insured["address"] = _address(patient["address"])
    service_lines = []
    for line in lines:
        code: dict[str, Any] = {"code": str(line.get("cpt") or "").upper()}
        modifiers = [str(m).upper() for m in line.get("modifiers") or [] if str(m).strip()]
        if modifiers:
            code["modifiers"] = modifiers[:MAX_MODIFIERS]
        day = str(line.get("date_of_service") or claim.date_of_service)
        service_lines.append(
            {
                "datesOfService": {"start": day, "end": day},
                "diagnosisCodes": line_diagnoses(line, diagnoses),
                "lineItemChargeAmount": amount(line.get("charge")),
                "placeOfService": pos,
                "procedureCode": code,
                "units": units(line.get("units") or 1),
                "lineItemControlNumber": str(line.get("line_id") or ""),
            }
        )
    return {
        "authorization": {
            "insuredAuthorizesAssignment": "YES",
            "patientReleasesMedicalInfo": "YES",
            "providerAcceptsAssignment": "ASSIGNED",
            "providerSignature": "ON_FILE",
        },
        "billing": {
            "billingProvider": {
                "address": _address(bp),
                "identifiers": identifiers,
                "name": {"organization": str(bp["organization"])},
            },
            "patientControlNumber": claim.claim_number,
            "taxId": {"ein": re.sub(r"\D", "", str(bp["tax_id"]))},
            "totalCharge": total(lines),
        },
        "encounter": encounter,
        "insured": insured,
        "payer": {"id": payer_id, "name": {"organization": payer_name}},
        "purpose": "CHARGEABLE",
        "serviceLines": service_lines,
        "submitter": {
            "name": {"organization": str(bp["submitter_name"])},
            "contact": {"phoneNumber": re.sub(r"\D", "", str(bp["contact_phone"]))},
        },
    }


def is_test_claim(body: dict[str, Any]) -> bool:
    """Any trace of the test payer or its member-ID prefixes."""
    member = str((body.get("insured") or {}).get("memberId") or "").upper()
    return str((body.get("payer") or {}).get("id") or "").upper() == TEST_PAYER_ID or any(
        member.startswith(p) for p in TEST_PREFIX.values()
    )
