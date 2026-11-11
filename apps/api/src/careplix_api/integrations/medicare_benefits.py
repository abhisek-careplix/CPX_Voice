"""Medicare benefits from CMS (HETS 270/271): coverage, what has been used, what remains, and when
each preventive service is next allowed.

Sources, checked Nov 2026: the CMS HETS 270/271 Companion Guide v15-1 (May 2026, §7.4–7.29 and
Appendix B) and Stedi's 2026-06-01 ``plans`` shape (api-reference/post-eligibility-check).

- Benefit groups are ``plans[].benefits.<group>``, keyed on EB01: ``statuses`` (1–8),
  ``coInsurance`` (A), ``coPayment`` (B), ``deductible`` (C), ``benefitDescription`` (D),
  ``exclusions`` (E), ``limitations`` (F), ``nonCovered`` (I), ``reserve`` (K),
  ``otherOrAdditionalPayer`` (R), ``contactFollowingEntityForInformation`` (U) and
  ``healthCareFacility`` (X).
- ``timePeriod`` is EB06 (EPISODE = per Part A spell, CALENDAR_YEAR, SERVICE_YEAR, REMAINING, DAY,
  VISIT, LIFETIME, LIFETIME_REMAINING); ``quantity`` is EB09/EB10. An HSD segment becomes
  ``serviceLimits[].delivery`` with ``quantity`` (HSD01/02) and ``period`` (HSD05/06): CMS sends
  days remaining as ``period {qualifier: REMAINING, value: 56}`` and sessions or blood units
  remaining as ``quantity {value}`` with ``period {qualifier: REMAINING}``.
- DTP qualifiers land in ``dates``: 291 ``plan``, 292 ``benefit``, 348 ``benefit.start`` (next
  eligible date), 472 ``service``, 435 ``admission``, 096 ``discharge``, 193/194 ``period``, 290
  ``coordinationOfBenefits``. Providers (LS/NM1 in 2120C) are ``relatedEntities[]`` with ``npi``.
- Procedure lines carry ``service.system`` "HCPCS". The 26/TC modifier (EB13-3) may only be in
  the raw ``x12``, which the snapshot keeps; :func:`x12_modifiers` reads it back.
- Stedi documents no ``timePeriod`` or ``quantity`` on ``reserve`` and ``exclusions`` lines, so
  lifetime reserve days and blood units are read from the line or from ``serviceLimits``.

Medicare Advantage, secondary payers, Part D and QMB come from the merged eligibility summary
(:func:`careplix_api.integrations.stedi.summarize_eligibility`). Nothing here is advice: it is what
CMS reported, labelled with the field it came from.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from careplix_api.integrations import stedi

# --- what to ask CMS for -------------------------------------------------------------------------

# Call 1: service type codes (HETS §7.5–7.29). No date range: CMS answers for today.
STC_SERVICES = (
    "30",  # plan coverage, Part A/B deductibles and coinsurance
    "10",  # blood deductible
    "42",  # home health episodes
    "45",  # hospice elections and benefit periods
    "47",  # hospital spells, days and lifetime reserve
    "AG",  # skilled nursing facility days
    "AD",  # occupational therapy used
    "AE",  # physical + speech therapy used (combined)
    "AF",  # speech therapy coverage
    "BF",  # pulmonary rehabilitation
    "BG",  # cardiac rehabilitation
    "67",  # smoking cessation
    "RN",  # ESRD
    "BZ",  # annual wellness visit
    "BT",  # screening Pap test
    "71",  # audiology
    "BD",  # cognitive assessment
    "CO",  # flu vaccination
    "80",  # COVID-19 vaccination
)

# Preventive HCPCS codes that return a next eligible date (HETS §7.10.1).
NEXT_ELIGIBLE: dict[str, tuple[str, tuple[str, ...]]] = {
    "alcohol_screening": ("Alcohol misuse screening", ("G0442", "G0443")),
    "depression_screening": ("Depression screening", ("G0444",)),
    "cardiovascular_screening": (
        "Cardiovascular screening blood tests",
        ("80061", "82465", "83718", "84478"),
    ),
    "colorectal_screening": (
        "Colorectal cancer screening",
        ("0464U", "74263", "81528", "G0104", "G0105", "G0121", "G0327"),
    ),
    "ct_bone_density": ("CT bone density study", ("77078",)),
    "lung_cancer_screening": ("Lung cancer screening (low-dose CT)", ("71271",)),
    "diabetes_screening": ("Diabetes screening", ("82947", "82950", "82951", "83036")),
    "dxa_axial": ("Bone density (DXA, axial)", ("77080",)),
    "dxa_appendicular": ("Bone density (DXA, appendicular)", ("77081",)),
    "fobt": ("Fecal occult blood test", ("G0328", "82270")),
    "glaucoma_screening": ("Glaucoma screening", ("G0117", "G0118")),
    "hpv_screening": ("HPV test (cervical cancer screening)", ("G0476",)),
    "obesity_counseling": ("Obesity counseling", ("G0447", "G0473")),
    "cvd_therapy": ("Cardiovascular disease behavioral therapy", ("G0446",)),
    "ippe": ("Welcome to Medicare visit (IPPE)", ("G0402", "G0403", "G0404", "G0405")),
    "prostate_screening": ("Prostate cancer screening", ("G0102", "G0103")),
    "sti_counseling": ("STI screening and counseling", ("G0445",)),
    "mammography": ("Screening mammography", ("77067",)),
    "pelvic_exam": ("Screening pelvic and breast exam", ("G0101",)),
    "single_energy_xray": ("Single energy X-ray bone study", ("G0130",)),
    "ultrasound_bone_density": ("Ultrasound bone density", ("76977",)),
    "aaa_screening": ("Abdominal aortic aneurysm ultrasound", ("76706",)),
}
# Preventive HCPCS codes that return prior service history (HETS §7.10.2).
HISTORY: dict[str, tuple[str, tuple[str, ...]]] = {
    "hbv_screening": ("Hepatitis B screening", ("86704", "86706", "87340", "87341", "G0499")),
    "hcv_screening": ("Hepatitis C screening", ("G0472", "G0567")),
    "hiv_prep": (
        "HIV screening and PrEP",
        ("80081", "G0011", "G0013", "G0432", "G0433", "G0435", "G0475"),
    ),
    "pneumococcal": ("Pneumococcal vaccine", ("90670", "90671", "90677", "90684", "90732")),
}
# Dated services CMS returns for an STC rather than a requested HCPCS code (§7.27–7.29).
STC_DATED: dict[str, tuple[str, tuple[str, ...]]] = {
    "awv": ("Annual wellness visit", ("G0438", "G0439")),
    "pap": (
        "Screening Pap test",
        ("Q0091", "P3000", "G0123", "G0143", "G0144", "G0145", "G0147", "G0148"),
    ),
}
AUDIOLOGY = "audiology"
CODE_LABEL = {
    "G0438": "initial",
    "G0439": "subsequent",
    "G0121": "colonoscopy, not high risk",
    "G0105": "colonoscopy, high risk",
    "G0104": "flexible sigmoidoscopy",
    "81528": "stool DNA test",
    "74263": "CT colonography",
    "G0327": "blood-based biomarker test",
    "0464U": "stool DNA test",
}
HCPCS_SERVICES = tuple(
    c for _, codes in (*NEXT_ELIGIBLE.values(), *HISTORY.values()) for c in codes
)

# Not in HETS at all: these need the practice's or the patient's claims history.
NOT_IN_HETS = (
    {
        "key": "dsmt",
        "label": "Diabetes self-management training (DSMT) hours",
        "note": "Not available from Medicare eligibility; needs claims history.",
    },
    {
        "key": "mnt",
        "label": "Medical nutrition therapy (MNT) hours",
        "note": "Not available from Medicare eligibility; needs claims history.",
    },
)

# Base days per spell when CMS sends no base lines (Medicare Part A; Medicare & You 2026).
HOSPITAL_FULL_DAYS, HOSPITAL_COINSURANCE_DAYS = 60, 30
SNF_FULL_DAYS, SNF_COINSURANCE_DAYS = 20, 80
BLOOD_UNITS = 3  # the first 3 units of blood each calendar year
CARDIAC_REHAB_SESSIONS, INTENSIVE_CARDIAC_REHAB_SESSIONS = 36, 72
BENEFIT_PERIOD_GAP = timedelta(days=60)  # a Part A spell ends 60 days after the last stay


def request_groups(group_size: int = 0) -> list[dict[str, Any]]:
    """Call 1 (STCs) and call 2 (HCPCS), each split into ``group_size`` services when set.
    Stedi suggests one service per request unless the payer is tested; HETS takes up to 99 EQ
    loops, so the default is one request per list and :func:`split` is the fallback."""
    out = []
    for system, codes in (("STC", STC_SERVICES), ("HCPCS", HCPCS_SERVICES)):
        size = group_size if group_size and group_size > 0 else len(codes)
        for i in range(0, len(codes), size):
            out.append({"system": system, "services": list(codes[i : i + size])})
    return out


def split(group: dict[str, Any], size: int) -> list[dict[str, Any]]:
    codes = group["services"]
    size = max(1, size)
    if len(codes) <= size:
        return [group] if len(codes) == 1 else [{**group, "services": [c]} for c in codes]
    return [
        {"system": group["system"], "services": codes[i : i + size]}
        for i in range(0, len(codes), size)
    ]


def group_label(group: dict[str, Any]) -> str:
    codes = group["services"]
    head = ", ".join(codes[:6]) + (f" +{len(codes) - 6}" if len(codes) > 6 else "")
    return f"{group['system']} {head}"


# --- field helpers -------------------------------------------------------------------------------


def _iso(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _span(line: dict[str, Any], key: str) -> tuple[date | None, date | None]:
    span = (line.get("dates") or {}).get(key) or {}
    if not isinstance(span, dict):
        return None, None
    return _iso(span.get("start")), _iso(span.get("end"))


def _covers(
    span: tuple[date | None, date | None], day: date, grace: timedelta = timedelta()
) -> bool:
    start, end = span
    if start is None and end is None:
        return False
    return (start is None or start <= day) and (end is None or day <= end + grace)


def _full_year(span: tuple[date | None, date | None]) -> bool:
    start, end = span
    return bool(
        start and end and start == date(start.year, 1, 1) and end == date(start.year, 12, 31)
    )


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))


def _period(line: dict[str, Any]) -> str | None:
    """EB06 on the line, else the period qualifier of a delivery limit."""
    if line.get("timePeriod"):
        return str(line["timePeriod"])
    for limit in _deliveries(line):
        qualifier = (limit.get("period") or {}).get("qualifier")
        if qualifier:
            return str(qualifier)
    return None


def _deliveries(line: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        limit["delivery"]
        for limit in line.get("serviceLimits") or []
        if isinstance(limit, dict) and isinstance(limit.get("delivery"), dict)
    ]


def _quantity(line: dict[str, Any], qualifiers: tuple[str, ...] = ()) -> float | None:
    """EB10 (``quantity.value``), else a delivery limit's quantity that isn't a remainder."""
    q = line.get("quantity") or {}
    has_value = isinstance(q, dict) and q.get("value") not in (None, "")
    if has_value and (not qualifiers or q.get("qualifier") in qualifiers):
        return _num(q.get("value"))
    for limit in _deliveries(line):
        if (limit.get("period") or {}).get("qualifier") == "REMAINING":
            continue
        dq = limit.get("quantity") or {}
        if dq.get("value") not in (None, "") and (
            not qualifiers or dq.get("qualifier") in qualifiers
        ):
            return _num(dq.get("value"))
    return None


def _remaining(line: dict[str, Any]) -> float | None:
    """What a REMAINING delivery limit says is left: its quantity (sessions, units), else the
    number of periods (days: ``HSD***DA**29*56``)."""
    for limit in _deliveries(line):
        period = limit.get("period") or {}
        if period.get("qualifier") != "REMAINING":
            continue
        dq = (limit.get("quantity") or {}).get("value")
        if dq not in (None, ""):
            return _num(dq)
        if period.get("value") not in (None, ""):
            return _num(period.get("value"))
    return None


def _between(line: dict[str, Any]) -> tuple[float | None, float | None]:
    """From/through day of a base tier (HSD qualifiers EXCEEDED / NOT_EXCEEDED)."""
    low = high = None
    for limit in _deliveries(line):
        period = limit.get("period") or {}
        if period.get("qualifier") == "EXCEEDED":
            low = _num(period.get("value"))
        elif period.get("qualifier") == "NOT_EXCEEDED":
            high = _num(period.get("value"))
    return low, high


def _npi(line: dict[str, Any], kinds: tuple[str, ...] | None = None) -> str | None:
    for entity in line.get("relatedEntities") or []:
        if not isinstance(entity, dict) or not entity.get("npi"):
            continue
        if kinds is None or entity.get("type") in kinds:
            return str(entity["npi"])
    return None


def _entity_name(line: dict[str, Any], kinds: tuple[str, ...]) -> str | None:
    for entity in line.get("relatedEntities") or []:
        if isinstance(entity, dict) and entity.get("type") in kinds:
            name = entity.get("name") or {}
            if isinstance(name, dict):
                return (
                    name.get("organization")
                    or " ".join(
                        x
                        for x in (
                            (name.get("person") or {}).get(k) for k in ("firstName", "lastName")
                        )
                        if x
                    )
                    or None
                )
            return str(name) or None
    return None


def _messages(line: dict[str, Any]) -> list[str]:
    return [str(m) for m in line.get("messages") or [] if m]


def _svc(line: dict[str, Any]) -> str | None:
    return (line.get("service") or {}).get("value")


def _system(line: dict[str, Any]) -> str | None:
    return (line.get("service") or {}).get("system")


def _part(line: dict[str, Any]) -> str | None:
    kind = line.get("insuranceType")
    text = str(line.get("planCoverageDescription") or "").lower()
    if kind == "MEDICARE_PART_A" or (kind == "QUALIFIED_MEDICARE_BENEFICIARY" and "part a" in text):
        return "a"
    if kind == "MEDICARE_PART_B" or (kind == "QUALIFIED_MEDICARE_BENEFICIARY" and "part b" in text):
        return "b"
    return None


def _d(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _range(span: tuple[date | None, date | None]) -> dict[str, str | None]:
    return {"start": _d(span[0]), "end": _d(span[1])}


class _Lines:
    """Benefit lines from every response, each with the field path it came from."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses

    def __call__(self, group: str) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for r, response in enumerate(self.responses):
            for p, plan in enumerate(response.get("plans") or []):
                for i, line in enumerate((plan.get("benefits") or {}).get(group) or []):
                    if isinstance(line, dict):
                        prefix = f"call {r + 1}: " if len(self.responses) > 1 else ""
                        out.append((f"{prefix}plans[{p}].benefits.{group}[{i}]", line))
        return out


def _item(
    key: str,
    category: str,
    label: str,
    source: str,
    **values: Any,
) -> dict[str, Any]:
    item = {
        "key": key,
        "category": category,
        "label": label,
        "code": None,
        "modifier": None,
        "covered": None,
        "used": None,
        "remaining": None,
        "total": None,
        "unit": None,
        "amount": None,
        "next_eligible": None,
        "last_service": None,
        "period": None,
        "other_npi": None,
        "blocker": False,
        "note": None,
        "source": source,
    }
    item.update(values)
    return item


# --- raw X12: HCPCS modifiers --------------------------------------------------------------------


def x12_modifiers(x12: str | None) -> dict[tuple[str, str], str]:
    """``(code, date) → modifier`` from 271 EB13 composites (``HC|G0103|26``) and the DTP 348 or
    472 that follows, because the JSON keeps the code but can drop the 26/TC modifier."""
    text = str(x12 or "")
    if not text.startswith("ISA") or len(text) < 106:
        return {}
    element, component, terminator = text[3], text[104], text[105]
    out: dict[tuple[str, str], str] = {}
    pending: tuple[str, str] | None = None
    for segment in text.split(terminator):
        parts = segment.strip().split(element)
        if parts[0] == "EB":
            pending = None
            composite = parts[13] if len(parts) > 13 else ""
            bits = composite.split(component)
            if len(bits) >= 3 and bits[0] == "HC" and bits[2]:
                pending = (bits[1], bits[2])
        elif parts[0] == "DTP" and pending and len(parts) > 3 and parts[1] in ("348", "472"):
            raw = parts[3][:8]
            if re.fullmatch(r"\d{8}", raw):
                out[(pending[0], f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")] = pending[1]
    return out


# --- the parser ----------------------------------------------------------------------------------


def _merged(responses: list[dict[str, Any]]) -> dict[str, Any]:
    first = responses[0] if responses else {}
    return {
        "payerId": stedi.CMS,
        "payer": first.get("payer"),
        "subscriber": first.get("subscriber"),
        "plans": [p for r in responses for p in r.get("plans") or []],
        "errors": [e for r in responses for e in r.get("errors") or []],
    }


_REASON = re.compile(r"^\s*([0-9P])\s*[-–—]")
_REVOCATION = re.compile(r"revocation\s*code\s*[-–—:]?\s*(\d)", re.I)
REASONS = {
    "0": "age",
    "1": "disability",
    "2": "ESRD",
    "3": "disability and ESRD",
    "P": "Part B immunosuppressive drug benefit only",
}


def _entitlement(lines: _Lines, dos: date) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part, kind in (("part_a", "MEDICARE_PART_A"), ("part_b", "MEDICARE_PART_B")):
        rows = [(s, line) for s, line in lines("statuses") if line.get("insuranceType") == kind]
        whole = [(s, line) for s, line in rows if _svc(line) in ("30", None)] or rows
        active = [
            (s, line) for s, line in whole if str(line.get("status") or "").startswith("ACTIVE")
        ]
        inactive = [
            (s, line) for s, line in whole if str(line.get("status") or "").startswith("INACTIVE")
        ]
        if not rows:
            out[part] = {"covered": None, "status": None, "source": None}
            continue
        source, line = (active or whole)[0]
        reason = next(
            (
                m.group(1)
                for _, ln in active or whole
                for msg in _messages(ln)
                if (m := _REASON.match(msg))
            ),
            None,
        )
        start, end = _span(line, "plan")
        out[part] = {
            "covered": bool(active) and _covers((start, end), dos)
            if start or end
            else bool(active),
            "status": line.get("status"),
            "start": _d(start),
            "end": _d(end),
            "reason_code": reason,
            "reason": REASONS.get(reason or ""),
            "esrd": reason in ("2", "3"),
            "part_b_id_only": reason == "P",
            "inactive_periods": [
                {
                    "period": _range(
                        _span(ln, "plan") if any(_span(ln, "plan")) else _span(ln, "eligibility")
                    ),
                    "messages": _messages(ln),
                }
                for _, ln in inactive
            ],
            "source": f"{source}.status",
        }
    return out


def _pick_dated(
    rows: list[tuple[str, dict[str, Any]]], key: str, dos: date, *, spell_first: bool = False
) -> tuple[str, dict[str, Any]] | None:
    """The line whose dates cover the date of service. With ``spell_first``, a spell (anything
    narrower than a calendar year) that covers it, or ended within 60 days, wins over the year."""
    if spell_first:
        for source, line in rows:
            span = _span(line, key)
            if any(span) and not _full_year(span) and _covers(span, dos, BENEFIT_PERIOD_GAP):
                return source, line
    for source, line in rows:
        span = _span(line, key)
        if any(span) and _full_year(span) and _covers(span, dos):
            return source, line
    for source, line in rows:
        if _covers(_span(line, key), dos):
            return source, line
    undated = [(s, ln) for s, ln in rows if not any(_span(ln, key))]
    return undated[0] if undated else None


def _deductibles(lines: _Lines, dos: date) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    rows = lines("deductible")
    for part, base_period, spell in (("a", "EPISODE", True), ("b", "CALENDAR_YEAR", False)):
        mine = [(s, ln) for s, ln in rows if _part(ln) == part and _svc(ln) in ("30", None)]
        # Real Part A/B lines first; QMB lines (EB04 = QM) only when CMS sent those instead.
        real = [
            (s, ln) for s, ln in mine if ln.get("insuranceType") != "QUALIFIED_MEDICARE_BENEFICIARY"
        ]
        mine = real or mine
        base = _pick_dated([(s, ln) for s, ln in mine if _period(ln) == base_period], "plan", dos)
        left = _pick_dated(
            [(s, ln) for s, ln in mine if _period(ln) == "REMAINING"],
            "plan",
            dos,
            spell_first=spell,
        )
        if not (base or left):
            continue
        total = _num((base or ("", {}))[1].get("amount"))
        remaining = _num((left or ("", {}))[1].get("amount"))
        used = total - remaining if total is not None and remaining is not None else None
        span = _span((left or base)[1], "plan")
        label = (
            "Part A deductible (per benefit period)"
            if part == "a"
            else "Part B deductible (calendar year)"
        )
        entry = {
            "total": _fmt(total),
            "remaining": _fmt(remaining),
            "used": _fmt(max(used, 0)) if used is not None else None,
            "period": {"start": _d(span[0]), "end": _d(span[1])},
            "spell": part == "a" and not _full_year(span) and any(span),
            "qmb": not real,
            "source": f"{(left or base)[0]}.amount",
        }
        out[f"part_{part}"] = entry
        items.append(
            _item(
                f"deductible_{part}",
                "deductible",
                label,
                entry["source"],
                covered=True,
                total=entry["total"],
                remaining=entry["remaining"],
                used=entry["used"],
                unit="dollars",
                period=entry["period"],
                note="QMB: the patient owes no Medicare deductible." if not real else None,
            )
        )
    coins = [
        (s, ln)
        for s, ln in lines("coInsurance")
        if _part(ln) == "b"
        and _svc(ln) in ("30", None)
        and (ln.get("timePeriod") in ("VISIT", None))
    ]
    real = [
        (s, ln) for s, ln in coins if ln.get("insuranceType") != "QUALIFIED_MEDICARE_BENEFICIARY"
    ]
    picked = _pick_dated(real or coins, "plan", dos)
    if picked:
        out["part_b_coinsurance_pct"] = stedi._percent(picked[1].get("percent"))
        out["part_b_coinsurance_source"] = f"{picked[0]}.percent"
    return out, items


def _days(
    lines: _Lines, group: str, service: str, dos: date, full_default: int, coins_default: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[tuple[str, dict[str, Any]]]]:
    """Full-coverage days and coinsurance days left in the Part A spell (HETS §7.8)."""
    rows = [(s, ln) for s, ln in lines(group) if _svc(ln) == service and _part(ln) == "a"]
    real = [
        (s, ln) for s, ln in rows if ln.get("insuranceType") != "QUALIFIED_MEDICARE_BENEFICIARY"
    ]
    rows = real or rows
    tiers: list[dict[str, Any] | None] = []
    for period, default in (("EPISODE", full_default), ("DAY", coins_default)):
        tier = [(s, ln) for s, ln in rows if ln.get("timePeriod") == period]
        remaining = [(s, ln) for s, ln in tier if _remaining(ln) is not None]
        bases = [(s, ln) for s, ln in tier if _between(ln) != (None, None)]
        left = _pick_dated(remaining, "admission", dos, spell_first=True)
        base = _pick_dated(bases, "admission", dos)
        if not (left or base):
            tiers.append(None)
            continue
        total = None
        if base:
            low, high = _between(base[1])
            if high is not None:
                total = high - (low or 0)
        total = total if total is not None else float(default)
        span = _span(left[1], "admission") if left else (None, None)
        tiers.append(
            {
                "total": _fmt(total),
                "remaining": _fmt(_remaining(left[1])) if left else None,
                "used": _fmt(total - _remaining(left[1]))
                if left and _remaining(left[1]) is not None
                else None,
                "amount": (left or base)[1].get("amount"),
                "spell": bool(left) and any(span) and not _full_year(span),
                "period": {"start": _d(span[0]), "end": _d(span[1])} if any(span) else None,
                "source": f"{(left or base)[0]}.serviceLimits",
            }
        )
    return tiers[0], tiers[1], rows


def _inpatient(lines: _Lines, dos: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for service, category, name, full, coins in (
        ("30", "inpatient", "Hospital", HOSPITAL_FULL_DAYS, HOSPITAL_COINSURANCE_DAYS),
        ("AG", "snf", "Skilled nursing facility", SNF_FULL_DAYS, SNF_COINSURANCE_DAYS),
    ):
        first, second, _ = _days(lines, "coPayment", service, dos, full, coins)
        for tier, key, label in (
            (first, f"{category}_days", f"{name} days, fully covered"),
            (second, f"{category}_coinsurance_days", f"{name} coinsurance days"),
        ):
            if tier is None:
                continue
            items.append(
                _item(
                    key,
                    category,
                    label,
                    tier["source"],
                    covered=True,
                    total=tier["total"],
                    remaining=tier["remaining"],
                    used=tier["used"],
                    unit="days",
                    amount=tier["amount"],
                    period=tier["period"],
                    note=(f"${tier['amount']} a day" if tier["amount"] not in (None, "0") else None)
                    if key.endswith("coinsurance_days")
                    else None,
                )
            )
    # Lifetime reserve days (EB*K: LIFETIME base, LIFETIME_REMAINING, DAY copayment).
    reserve = [
        (s, ln) for s, ln in lines("reserve") if _svc(ln) in ("30", None) and _part(ln) == "a"
    ]
    base = next(((s, ln) for s, ln in reserve if _period(ln) == "LIFETIME"), None)
    left = next(((s, ln) for s, ln in reserve if _period(ln) == "LIFETIME_REMAINING"), None)
    per_day = _pick_dated([(s, ln) for s, ln in reserve if _period(ln) == "DAY"], "admission", dos)
    if base or left:
        total = _quantity(base[1]) if base else None
        remaining = _quantity(left[1]) if left else None
        items.append(
            _item(
                "lifetime_reserve_days",
                "inpatient",
                "Lifetime reserve days",
                f"{(left or base)[0]}.quantity",
                covered=True,
                total=_fmt(total if total is not None else 60.0),
                remaining=_fmt(remaining),
                used=_fmt(total - remaining)
                if total is not None and remaining is not None
                else None,
                unit="days",
                amount=per_day[1].get("amount") if per_day else None,
                note=f"${per_day[1]['amount']} a day; never renewed"
                if per_day and per_day[1].get("amount")
                else "Never renewed",
            )
        )
    # Spells and stays (EB*D): the spell's DOEBA–DOLBA, each stay and its facility.
    for source, line in lines("benefitDescription"):
        if _part(line) != "a" or _system(line) == "HCPCS":
            continue
        service = _svc(line)
        if service == "30":
            span = _span(line, "benefit")
            if any(span):
                ongoing = _covers(span, dos, BENEFIT_PERIOD_GAP)
                items.append(
                    _item(
                        f"spell_{_d(span[0])}",
                        "inpatient",
                        "Part A benefit period (spell)",
                        f"{source}.dates.benefit",
                        covered=True,
                        period={"start": _d(span[0]), "end": _d(span[1])},
                        note="Ongoing: a new stay continues this benefit period."
                        if ongoing
                        else "Ended: a new stay starts a new benefit period and deductible.",
                    )
                )
        elif service in ("48", "47", "AG", "AH"):
            snf = service in ("AG", "AH")
            span = _span(line, "admission")
            items.append(
                _item(
                    f"stay_{service}_{_d(span[0])}",
                    "snf" if snf else "inpatient",
                    "Skilled nursing stay" if snf else "Hospital stay",
                    f"{source}.dates.admission",
                    covered=True,
                    period={"start": _d(span[0]), "end": _d(span[1])},
                    other_npi=_npi(line),
                )
            )
    return items


def _blood(lines: _Lines, dos: date) -> list[dict[str, Any]]:
    rows = [
        (s, ln)
        for group in ("exclusions", "deductible")
        for s, ln in lines(group)
        if _svc(ln) == "10"
    ]
    picked = _pick_dated(rows, "benefit", dos) or _pick_dated(rows, "plan", dos)
    if not picked:
        return []
    source, line = picked
    total = _quantity(line, ("DEDUCTIBLE_BLOOD_UNITS", "UNITS")) or _quantity(line)
    remaining = _remaining(line)
    total = total if total is not None else float(BLOOD_UNITS)
    return [
        _item(
            "blood_deductible",
            "blood",
            "Blood deductible (units this year)",
            f"{source}.serviceLimits",
            covered=True,
            total=_fmt(total),
            remaining=_fmt(remaining),
            used=_fmt(total - remaining) if remaining is not None else None,
            unit="units",
            note="The patient pays for (or replaces) these units before Medicare covers blood.",
        )
    ]


def _home_health(lines: _Lines, today: date, practice_npi: str | None) -> list[dict[str, Any]]:
    items = []
    for source, line in lines("healthCareFacility"):
        if _svc(line) != "42" or _system(line) == "HCPCS":
            continue
        span = _span(line, "service")
        if not any(span):
            span = _span(line, "period")
        npi = _npi(line, ("PROVIDER", "FACILITY", "OTHER_PHYSICIAN"))
        open_now = _covers(span, today) or (
            span[0] is not None and span[0] <= today and span[1] is None
        )
        other = bool(npi) and npi != practice_npi
        items.append(
            _item(
                f"home_health_{_d(span[0])}",
                "home_health",
                "Home health episode",
                f"{source}.dates.service",
                covered=True,
                period={"start": _d(span[0]), "end": _d(span[1])},
                other_npi=npi,
                blocker=open_now and (other or not npi),
                note=(
                    "Open with another home health agency: coordinate before ordering services."
                    if open_now and (other or not npi)
                    else "Open"
                    if open_now
                    else "Closed"
                ),
                messages=_messages(line),
            )
        )
    return items


def _hospice(lines: _Lines, today: date) -> list[dict[str, Any]]:
    items = []
    for source, line in lines("healthCareFacility"):
        if _svc(line) != "45":
            continue
        code = next((m.group(1) for msg in _messages(line) if (m := _REVOCATION.search(msg))), None)
        span = _span(line, "benefit")
        npi = _npi(line, ("PROVIDER", "FACILITY"))
        if code is not None:
            open_election = code == "0"
            items.append(
                _item(
                    f"hospice_election_{_d(span[0])}",
                    "hospice",
                    "Hospice election",
                    f"{source}.messages",
                    covered=True,
                    period={"start": _d(span[0]), "end": _d(span[1])},
                    other_npi=npi,
                    blocker=open_election,
                    revocation_code=code,
                    note="Open election: the hospice is responsible for care related to the "
                    "terminal illness. Coordinate with the hospice."
                    if open_election
                    else "Revoked",
                )
            )
            continue
        days = None
        for limit in _deliveries(line):
            dq = limit.get("quantity") or {}
            if dq.get("qualifier") in ("DAYS", None) and dq.get("value") not in (None, ""):
                days = _num(dq.get("value"))
        open_period = (
            span[0] is not None and span[0] <= today and (span[1] is None or span[1] >= today)
        )
        items.append(
            _item(
                f"hospice_period_{_d(span[0])}",
                "hospice",
                "Hospice benefit period",
                f"{source}.dates.benefit",
                covered=True,
                used=_fmt(days),
                unit="days",
                period={"start": _d(span[0]), "end": _d(span[1])},
                other_npi=npi,
                blocker=open_period,
                note="Current hospice benefit period: coordinate with the hospice."
                if open_period
                else None,
            )
        )
    return items


def therapy_threshold(year: int) -> dict[str, Any]:
    """The KX modifier threshold (not in the 271). CY2026: $2,480 for OT and $2,480 for PT and
    SLP combined, targeted medical review from $3,000 (cms.gov "Therapy services", Feb 2026)."""
    known = {2026: 2480, 2025: 2410, 2024: 2330}
    amount = known.get(year) or known[max(known)]
    return {
        "year": year,
        "kx": amount,
        "review": 3000,
        "as_of": "2026-02-26",
        "estimated": year not in known,
    }


def _therapy(lines: _Lines, dos: date) -> list[dict[str, Any]]:
    items = []
    names = {
        "AD": ("therapy_ot", "Occupational therapy (KX threshold)"),
        "AE": ("therapy_pt_slp", "Physical + speech therapy (KX threshold, combined)"),
    }
    for service, (key, label) in names.items():
        rows = [
            (s, ln)
            for s, ln in lines("benefitDescription")
            if _svc(ln) == service and _system(ln) != "HCPCS"
        ]
        picked = _pick_dated(rows, "benefit", dos)
        if not picked:
            continue
        source, line = picked
        used = _num(line.get("amount")) or 0.0
        threshold = therapy_threshold(dos.year)
        items.append(
            _item(
                key,
                "therapy",
                label,
                f"{source}.amount",
                covered=True,
                used=_fmt(used),
                total=_fmt(float(threshold["kx"])),
                remaining=_fmt(max(threshold["kx"] - used, 0.0)),
                unit="dollars",
                period=_range(_span(line, "benefit")),
                threshold=threshold,
                note=f"KX threshold ${threshold['kx']:,} in {threshold['year']} (from CMS, not "
                f"the 271); targeted medical review from ${threshold['review']:,}.",
            )
        )
    return items


def _component(line: dict[str, Any]) -> str:
    text = " ".join(_messages(line)).lower()
    kind = "intensive " if "intensive" in text else ""
    if "technical" in text:
        return f"{kind}technical"
    if "professional" in text:
        return f"{kind}professional"
    return kind.strip() or "sessions"


def _rehab(lines: _Lines, dos: date) -> list[dict[str, Any]]:
    items = []
    for source, line in lines("limitations"):
        service = _svc(line)
        if service == "BF":
            part = _component(line)
            remaining = _quantity(line) if _period(line) == "REMAINING" else _remaining(line)
            items.append(
                _item(
                    f"pulmonary_rehab_{part.replace(' ', '_')}",
                    "rehab",
                    f"Pulmonary rehab sessions ({part})",
                    f"{source}.quantity",
                    covered=True,
                    remaining=_fmt(remaining),
                    unit="sessions",
                )
            )
        elif service == "BG":
            part = _component(line)
            intensive = part.startswith("intensive")
            used = _quantity(line)
            total = INTENSIVE_CARDIAC_REHAB_SESSIONS if intensive else CARDIAC_REHAB_SESSIONS
            items.append(
                _item(
                    f"cardiac_rehab_{part.replace(' ', '_')}",
                    "rehab",
                    f"{'Intensive cardiac' if intensive else 'Cardiac'} rehab sessions "
                    f"({part.removeprefix('intensive ')})",
                    f"{source}.quantity",
                    covered=True,
                    used=_fmt(used),
                    total=_fmt(float(total)),
                    remaining=_fmt(max(total - used, 0)) if used is not None else None,
                    unit="sessions",
                    note=f"Medicare covers up to {total} sessions (CMS rule, not from the 271)"
                    + ("; more need the contractor's approval." if not intensive else "."),
                )
            )
        elif service == "67":
            total = _quantity(line, ("VISITS",)) or _quantity(line)
            remaining = _remaining(line)
            initial = _span(line, "benefit")[0]
            items.append(
                _item(
                    "smoking_cessation",
                    "smoking_cessation",
                    "Smoking cessation counseling sessions (12 months)",
                    f"{source}.serviceLimits",
                    covered=True,
                    total=_fmt(total if total is not None else 8.0),
                    remaining=_fmt(remaining),
                    used=_fmt(total - remaining)
                    if total is not None and remaining is not None
                    else None,
                    unit="sessions",
                    last_service=None,
                    period={"start": _d(initial), "end": _d(initial + timedelta(days=365))}
                    if initial
                    else None,
                    note=f"Counting from the first session on {_d(initial)}." if initial else None,
                )
            )
    return items


def _esrd(lines: _Lines, dos: date) -> list[dict[str, Any]]:
    items = []
    for source, line in lines("benefitDescription"):
        if _svc(line) not in ("RN", "CQ") or _system(line) == "HCPCS":
            continue
        span = _span(line, "benefit")
        dialysis = _span(line, "service")
        transplant = _span(line, "discharge")[0]
        notes = []
        if any(dialysis):
            notes.append(f"dialysis {_d(dialysis[0])}–{_d(dialysis[1]) or 'ongoing'}")
        if transplant:
            notes.append(f"transplant {_d(transplant)}")
        items.append(
            _item(
                f"esrd_{_d(span[0])}",
                "esrd",
                "ESRD coverage period",
                f"{source}.dates.benefit",
                covered=_covers(span, dos) if any(span) else True,
                period={"start": _d(span[0]), "end": _d(span[1])},
                note="; ".join(notes).capitalize() if notes else None,
                dialysis={"start": _d(dialysis[0]), "end": _d(dialysis[1])}
                if any(dialysis)
                else None,
                transplant=_d(transplant),
            )
        )
    return items


def _program(code: str) -> tuple[str, str, str]:
    """(key, label, kind) for a procedure code: next_eligible | history | vaccine."""
    for table, kind in (
        (NEXT_ELIGIBLE, "next_eligible"),
        (STC_DATED, "next_eligible"),
        (HISTORY, "history"),
    ):
        for key, (label, codes) in table.items():
            if code in codes:
                return key, label, kind
    if code == "G0009":
        return "pneumococcal", HISTORY["pneumococcal"][0], "history"
    if code == "99483":
        return "cognitive", "Cognitive assessment and care plan", "history"
    if re.fullmatch(r"913\d\d|0\d{3}A|90480|M0201", code):
        return "covid_vaccine", "COVID-19 vaccine", "vaccine"
    if (code.startswith("906") or code in ("90756", "G0008")) or re.fullmatch(r"Q203[5-9]", code):
        return "flu_vaccine", "Flu vaccine", "vaccine"
    if re.fullmatch(r"92[56]\d\d", code):
        return AUDIOLOGY, "Audiology diagnostic test", "next_eligible"
    return f"hcpcs_{code}", f"Procedure {code}", "next_eligible"


def _preventive(
    lines: _Lines, today: date, requested: list[str], modifiers: dict[tuple[str, str], str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in ("benefitDescription", "nonCovered", "statuses"):
        for source, line in lines(group):
            if _system(line) not in ("HCPCS", "CPT"):
                continue
            code = str(_svc(line) or "")
            if not code:
                continue
            key, label, kind = _program(code)
            if group != "benefitDescription":
                inactive = group == "nonCovered" or str(line.get("status") or "").startswith(
                    "INACTIVE"
                )
                if inactive:
                    seen.add(code)
                    items.append(
                        _item(
                            f"{key}_{code}_not_covered",
                            "preventive",
                            label,
                            f"{source}.service",
                            code=code,
                            covered=False,
                            program=key,
                            note="Medicare reports the patient isn't eligible for it now.",
                        )
                    )
                continue
            seen.add(code)
            served = _span(line, "service")[0]
            begins = _span(line, "benefit")[0]
            detail = CODE_LABEL.get(code)
            name = f"{label} ({detail})" if detail else label
            if served:
                items.append(
                    _item(
                        f"{key}_{code}_{_d(served)}",
                        "vaccine" if kind == "vaccine" else "preventive",
                        name,
                        f"{source}.dates.service",
                        code=code,
                        modifier=modifiers.get((code, _d(served))),
                        covered=True,
                        last_service=_d(served),
                        other_npi=_npi(line),
                        program=key,
                    )
                )
            elif begins:
                items.append(
                    _item(
                        f"{key}_{code}_{modifiers.get((code, _d(begins))) or 'all'}",
                        "preventive",
                        name,
                        f"{source}.dates.benefit.start",
                        code=code,
                        modifier=modifiers.get((code, _d(begins))),
                        covered=True,
                        next_eligible=_d(begins),
                        eligible_now=begins <= today,
                        program=key,
                    )
                )
    # Requested codes CMS returned nothing for: follow Medicare's coverage rules for them.
    missing: dict[str, dict[str, Any]] = {}
    for code in requested:
        if code in seen:
            continue
        key, label, _ = _program(code)
        missing.setdefault(key, {"program": key, "label": label, "codes": []})["codes"].append(code)
    return items, list(missing.values())


def _routing(summary: dict[str, Any], lines: _Lines, dos: date) -> dict[str, Any]:
    medicare = summary.get("medicare") or {}
    redirect = summary.get("redirect") or {}
    ma = None
    if medicare.get("advantage") or redirect.get("kind") == "medicare_advantage":
        ma = {
            "name": redirect.get("name"),
            "payer_id": redirect.get("payer_id"),
            "phone": redirect.get("phone"),
            "contract": redirect.get("contract"),
            "plan_benefit_package": redirect.get("plan_benefit_package"),
            "bill_option": redirect.get("bill_option") or medicare.get("bill_option"),
            "source": "plans[].benefits.contactFollowingEntityForInformation[]",
        }
    secondary = []
    qmb = None
    part_d = []
    for source, line in lines("otherOrAdditionalPayer"):
        kind = str(line.get("insuranceType") or "")
        span = _span(line, "coordinationOfBenefits")
        if not any(span):
            span = _span(line, "benefit")
        active = _covers(span, dos) if any(span) else True
        if kind.startswith("MEDICARE_SECONDARY"):
            secondary.append(
                {
                    "type": kind,
                    "payer": _entity_name(line, ("PRIMARY_PAYER", "PAYER")),
                    "period": {"start": _d(span[0]), "end": _d(span[1])},
                    "active": active,
                    "source": source,
                }
            )
        elif kind == "QUALIFIED_MEDICARE_BENEFICIARY":
            if qmb is None or active:
                qmb = {
                    "plan": line.get("planCoverageDescription"),
                    "period": {"start": _d(span[0]), "end": _d(span[1])},
                    "active": active,
                    "source": source,
                }
        elif _svc(line) == "88":
            part_d.append(
                {
                    "name": _entity_name(line, ("PAYER", "PRIMARY_PAYER")),
                    "contract": ((line.get("additionalInformation") or {}).get("plan") or {}).get(
                        "number"
                    ),
                    "period": {"start": _d(span[0]), "end": _d(span[1])},
                    "active": active,
                    "source": source,
                }
            )
    for kind in medicare.get("secondary") or []:
        if not any(s["type"] == kind for s in secondary):
            secondary.append(
                {"type": kind, "payer": None, "period": None, "active": True, "source": None}
            )
    if qmb is None and medicare.get("qmb"):
        qmb = {"plan": None, "period": None, "active": True, "source": None}
    return {"medicare_advantage": ma, "secondary_payers": secondary, "qmb": qmb, "part_d": part_d}


def parse(
    responses: list[dict[str, Any]],
    *,
    date_of_service: date,
    today: date,
    practice_npi: str | None = None,
    requested_hcpcs: list[str] | None = None,
    x12: list[str] | None = None,
) -> dict[str, Any]:
    """Every CMS 271 for one patient → one ``MedicareBenefits`` dict (JSON-ready)."""
    lines = _Lines(responses)
    merged = _merged(responses)
    summary = stedi.summarize_eligibility(merged, payer_id=stedi.CMS)
    modifiers: dict[tuple[str, str], str] = {}
    for raw in x12 or []:
        modifiers.update(x12_modifiers(raw))
    entitlement = _entitlement(lines, date_of_service)
    deductibles, deductible_items = _deductibles(lines, date_of_service)
    preventive, no_data = _preventive(lines, today, list(requested_hcpcs or []), modifiers)
    items = [
        *deductible_items,
        *_inpatient(lines, date_of_service),
        *_blood(lines, date_of_service),
        *_home_health(lines, today, practice_npi),
        *_hospice(lines, today),
        *_therapy(lines, date_of_service),
        *_rehab(lines, date_of_service),
        *_esrd(lines, date_of_service),
        *preventive,
    ]
    part_b = entitlement.get("part_b") or {}
    if part_b.get("part_b_id_only"):
        for item in items:
            if item["category"] in ("preventive", "therapy", "rehab", "smoking_cessation"):
                item["covered"] = False
                item["note"] = "Part B-ID only covers immunosuppressive drugs."
    return {
        "payer": stedi.CMS,
        "date_of_service": date_of_service.isoformat(),
        "as_of": today.isoformat(),
        "coverage": summary["coverage"],
        "entitlement": entitlement,
        "deductibles": deductibles,
        "routing": _routing(summary, lines, date_of_service),
        "items": items,
        "no_data": no_data,
        "not_in_hets": [dict(x) for x in NOT_IN_HETS],
        "errors": summary["errors"],
        "error_details": summary["error_details"],
        "message": stedi.error_message(summary["error_details"], stedi.CMS),
    }
