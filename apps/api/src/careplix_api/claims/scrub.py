"""Our claim scrub (RC5, workflow R2): the checks a claim passes before the clearinghouse's own.

:func:`check` is pure: claim, patient, billing profile and today in; findings out. A finding is
``{code, severity, field, line_id, message}``; any ``error`` blocks submission, ``warning`` only
informs. The clearinghouse's validation runs after ours (``claims.submit``).

- Required fields: payer, patient name, birth date, member ID (an MBI for traditional Medicare),
  billing NPI (check digit), tax ID, address, at least one diagnosis and one line.
- Code sanity: ICD-10-CM and CPT/HCPCS shapes, line diagnosis pointers, units, charges, modifier
  shape and pairings (25 on E/M only, 26 with TC, distinct-procedure modifiers on E/M, 50 with
  more than one unit), at most four modifiers, twelve diagnoses and fifty lines.
- Dates: service dates are real and not in the future; timely filing per payer kind, a warning
  in the last :data:`FILING_WARN_DAYS` days and an error once the limit has passed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from careplix_api.claims import x837
from careplix_api.integrations import stedi

ERROR, WARNING = "error", "warning"
# Timely filing in days from the date of service. Payers and contracts vary; these are the
# common floors (Medicare: 12 months, 42 CFR 424.44). A payer-specific limit belongs in the
# payer directory once contracts are loaded.
FILING_LIMIT_DAYS = {
    "medicare": 365,
    "medicare_advantage": 365,
    "medicaid": 180,
    "commercial": 90,
}
DEFAULT_FILING_LIMIT = 90
FILING_WARN_DAYS = 14
_ICD10 = re.compile(r"[A-Z][0-9][0-9A-Z][0-9A-Z]{0,4}")
_CPT = re.compile(r"\d{4}[0-9FTU]")  # Category I, II (F), III (T), PLA (U)
_HCPCS = re.compile(r"[A-V]\d{4}")
_MODIFIER = re.compile(r"[0-9A-Z]{2}")
_POS = re.compile(r"\d{2}")
DISTINCT = {"59", "XE", "XS", "XP", "XU"}


def is_em(code: str) -> bool:
    """Evaluation and management (office, hospital, preventive and consult visits)."""
    return bool(re.fullmatch(r"99\d{3}", code)) and 99202 <= int(code) <= 99499


def filing_limit(plan_kind: str | None) -> int:
    return FILING_LIMIT_DAYS.get(plan_kind or "", DEFAULT_FILING_LIMIT)


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _f(code: str, severity: str, field: str, message: str, line_id: str | None = None):
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "line_id": line_id,
        "message": message,
        "source": "careplix",
    }


def check(
    claim: Any, patient: dict[str, Any], profile: dict[str, Any], today: date
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    out += _required(claim, patient, profile)
    diagnoses = [x837.icd10(d) for d in claim.diagnoses or []]
    out += _diagnoses(diagnoses)
    out += _lines(claim, diagnoses, today)
    out += _timely(claim, today)
    return out


def blocking(findings: list[dict[str, Any]]) -> bool:
    return any(f["severity"] == ERROR for f in findings)


def _required(claim: Any, patient: dict[str, Any], profile: dict[str, Any]):
    out = []
    if not claim.payer_id:
        out.append(_f("payer_missing", ERROR, "payer", "No payer ID: pick the patient's payer."))
    for key, label in (("first_name", "first name"), ("last_name", "last name")):
        if not str(patient.get(key) or "").strip():
            out.append(_f(f"patient_{key}", ERROR, "patient", f"The patient's {label} is missing."))
    dob = _date(patient.get("date_of_birth"))
    if dob is None:
        out.append(_f("patient_dob", ERROR, "patient", "The patient's birth date is missing."))
    member = re.sub(r"[\s-]", "", str(patient.get("member_id") or ""))
    if not member:
        out.append(_f("member_id_missing", ERROR, "patient", "The member ID is missing."))
    elif claim.plan_kind == "medicare":
        try:
            stedi.normalize_mbi(member)
        except stedi.InvalidMbi as err:
            out.append(_f("member_id_mbi", ERROR, "patient", err.message))
    elif len(member) < 2 or len(member) > 80:
        out.append(_f("member_id_length", ERROR, "patient", "The member ID looks wrong."))
    if not stedi.valid_npi(profile.get("npi")):
        out.append(
            _f("billing_npi", ERROR, "billing", "The billing NPI is missing or fails its check.")
        )
    if not re.fullmatch(r"\d{9}", str(profile.get("tax_id") or "")):
        out.append(_f("billing_tax_id", ERROR, "billing", "The billing tax ID needs 9 digits."))
    for key in ("organization", "address1", "city", "state"):
        if not str(profile.get(key) or "").strip():
            out.append(_f(f"billing_{key}", ERROR, "billing", f"Billing {key} is missing."))
    if len(re.sub(r"\D", "", str(profile.get("postal_code") or ""))) not in (5, 9):
        out.append(_f("billing_zip", ERROR, "billing", "The billing ZIP code needs 5 or 9 digits."))
    if not profile.get("taxonomy"):
        out.append(
            _f("billing_taxonomy", WARNING, "billing", "No taxonomy code: some payers need one.")
        )
    if not _POS.fullmatch(str(claim.place_of_service or "")):
        out.append(_f("place_of_service", ERROR, "claim", "Place of service is two digits."))
    return out


def _diagnoses(diagnoses: list[str]):
    out = []
    if not diagnoses:
        out.append(_f("dx_missing", ERROR, "diagnoses", "Add at least one diagnosis (ICD-10-CM)."))
    if len(diagnoses) > x837.MAX_DIAGNOSES:
        out.append(_f("dx_too_many", ERROR, "diagnoses", "A claim carries at most 12 diagnoses."))
    for code in diagnoses:
        if not _ICD10.fullmatch(code):
            out.append(_f("dx_format", ERROR, "diagnoses", f"{code} isn't an ICD-10-CM code."))
    if len(set(diagnoses)) != len(diagnoses):
        out.append(_f("dx_duplicate", ERROR, "diagnoses", "A diagnosis is listed twice."))
    return out


def _lines(claim: Any, diagnoses: list[str], today: date):
    out = []
    lines = claim.lines or []
    if not lines:
        out.append(_f("lines_missing", ERROR, "lines", "The claim has no service lines."))
    if len(lines) > x837.MAX_LINES:
        out.append(_f("lines_too_many", ERROR, "lines", "A claim carries at most 50 lines."))
    codes = [str(line.get("cpt") or "").upper() for line in lines]
    for line, code in zip(lines, codes, strict=True):
        lid = str(line.get("line_id") or "")
        if not (_CPT.fullmatch(code) or _HCPCS.fullmatch(code)):
            out.append(_f("cpt_format", ERROR, "lines", f"{code or '—'} isn't a CPT/HCPCS.", lid))
        if x837.money(line.get("charge")) <= 0:
            out.append(_f("charge_zero", ERROR, "lines", f"{code}: the charge is zero.", lid))
        qty = x837.money(line.get("units"))
        if qty <= 0 or qty > 999:
            out.append(_f("units", ERROR, "lines", f"{code}: units must be 1–999.", lid))
        pointers = [p for p in line.get("dx") or []]
        if not pointers:
            out.append(
                _f("dx_pointer_missing", ERROR, "lines", f"{code}: point it at a diagnosis.", lid)
            )
        if len(pointers) > x837.MAX_LINE_DX:
            out.append(_f("dx_pointer_count", ERROR, "lines", f"{code}: at most 4 pointers.", lid))
        if any(not str(p).isdigit() or not 0 < int(p) <= len(diagnoses) for p in pointers):
            out.append(
                _f("dx_pointer_range", ERROR, "lines", f"{code}: a pointer has no diagnosis.", lid)
            )
        day = _date(line.get("date_of_service") or claim.date_of_service)
        if day is None:
            out.append(_f("dos_missing", ERROR, "lines", f"{code}: no date of service.", lid))
        elif day > today:
            out.append(_f("dos_future", ERROR, "lines", f"{code}: the date is in the future.", lid))
        out += _modifiers(line, code, codes, qty, lid)
    return out


def _modifiers(line: dict[str, Any], code: str, codes: list[str], qty: Any, lid: str):
    out = []
    mods = [str(m).upper() for m in line.get("modifiers") or []]
    if len(mods) > x837.MAX_MODIFIERS:
        out.append(_f("mod_count", ERROR, "lines", f"{code}: at most 4 modifiers.", lid))
    if len(set(mods)) != len(mods):
        out.append(_f("mod_duplicate", ERROR, "lines", f"{code}: a modifier repeats.", lid))
    for m in mods:
        if not _MODIFIER.fullmatch(m):
            out.append(_f("mod_format", ERROR, "lines", f"{code}: {m} isn't a modifier.", lid))
    if "25" in mods and not is_em(code):
        out.append(
            _f("mod_25_not_em", ERROR, "lines", f"{code}: modifier 25 belongs on E/M only.", lid)
        )
    if "25" in mods and is_em(code) and not [c for c in codes if c != code]:
        out.append(
            _f(
                "mod_25_alone",
                WARNING,
                "lines",
                f"{code}: 25 needs a separate procedure the same day on this claim.",
                lid,
            )
        )
    if {"26", "TC"} <= set(mods):
        out.append(_f("mod_26_tc", ERROR, "lines", f"{code}: 26 and TC can't go together.", lid))
    if is_em(code) and DISTINCT & set(mods):
        out.append(
            _f("mod_distinct_em", WARNING, "lines", f"{code}: on E/M use 25, not 59/X.", lid)
        )
    if "50" in mods and x837.money(qty) > 1:
        out.append(_f("mod_50_units", WARNING, "lines", f"{code}: bilateral (50) is 1 unit.", lid))
    return out


def _timely(claim: Any, today: date):
    day = _date(claim.date_of_service)
    if day is None:
        return []
    limit = filing_limit(claim.plan_kind)
    deadline = day + timedelta(days=limit)
    if today > deadline:
        return [
            _f(
                "timely_filing",
                ERROR,
                "claim",
                f"Past timely filing: {limit} days from the service date ended {deadline}.",
            )
        ]
    if (deadline - today).days <= FILING_WARN_DAYS:
        return [
            _f(
                "timely_filing_soon",
                WARNING,
                "claim",
                f"Timely filing ends {deadline}: submit now.",
            )
        ]
    return []
