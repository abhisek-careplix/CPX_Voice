"""Plan suggestions from a Medicare benefits snapshot: what is due now, what is due later, what is
limited, and what needs coordination before the practice plans a service.

Advisory only: nothing here books, bills or writes to the chart. Each suggestion names the
benefit items it came from, so staff can see the CMS field behind it.

Rules that are not in the 271 are dated constants here, with their source:

- Therapy KX threshold: :func:`careplix_api.integrations.medicare_benefits.therapy_threshold`
  (cms.gov "Therapy services"). Warn at 85 % of it.
- Flu vaccine: one per flu season, August through July (Medicare Part B; MLN "Influenza").
- Preventive codes HETS doesn't date follow the Medicare coverage rules; the CMS Coverage API only
  lists NCD/LCD titles, not frequencies, so it is not consulted here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

ADVISORY = (
    "Suggestions are advisory and come from what Medicare reported today. They never book or "
    "bill anything; confirm medical necessity and coverage rules before scheduling."
)
DUE_NOW, DUE_ON, LIMITED, BLOCKED, QMB, INFO = (
    "due_now",
    "due_on",
    "limited",
    "blocked",
    "qmb",
    "info",
)
ORDER = {BLOCKED: 0, QMB: 1, LIMITED: 2, DUE_NOW: 3, DUE_ON: 4, INFO: 5}
KX_WARN = 0.85
LOW_DAYS = 10
# Programs the voice receptionist may mention (caller-safe due dates).
CALLER_SAFE = {
    "awv",
    "ippe",
    "mammography",
    "colorectal_screening",
    "fobt",
    "flu_vaccine",
    "diabetes_screening",
    "cardiovascular_screening",
    "depression_screening",
    "alcohol_screening",
    "glaucoma_screening",
    "prostate_screening",
    "lung_cancer_screening",
    "aaa_screening",
    "pap",
    "pelvic_exam",
    "dxa_axial",
    "dxa_appendicular",
}


def _d(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s(kind: str, title: str, detail: str, category: str, items: list[str], **extra: Any):
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "category": category,
        "items": items,
        **extra,
    }


def flu_season(today: date) -> tuple[date, date]:
    start = date(today.year if today.month >= 8 else today.year - 1, 8, 1)
    return start, date(start.year + 1, 7, 31)


def _blockers(b: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    routing = b.get("routing") or {}
    for s in routing.get("secondary_payers") or []:
        if s.get("active"):
            who = s.get("payer") or "the primary insurer"
            out.append(
                _s(
                    BLOCKED,
                    f"Medicare is secondary: bill {who} first",
                    f"CMS lists a primary payer ({str(s['type']).replace('_', ' ').lower()}). "
                    "Coordinate benefits before planning services.",
                    "routing",
                    [],
                )
            )
    entitlement = b.get("entitlement") or {}
    part_b = entitlement.get("part_b") or {}
    if part_b.get("part_b_id_only"):
        out.append(
            _s(
                BLOCKED,
                "Part B-ID only: immunosuppressive drugs only",
                "No other Part B services can be billed for this patient.",
                "entitlement",
                [],
            )
        )
    elif part_b.get("covered") is False:
        out.append(
            _s(
                BLOCKED,
                "Part B not active",
                "Medicare reports no Part B coverage on the date of service.",
                "entitlement",
                [],
            )
        )
    for item in b.get("items") or []:
        if not item.get("blocker"):
            continue
        npi = f" (NPI {item['other_npi']})" if item.get("other_npi") else ""
        if item["category"] == "hospice":
            title = (
                "Open hospice election"
                if item["key"].startswith("hospice_election")
                else "Current hospice benefit period"
            )
            out.append(
                _s(
                    BLOCKED,
                    f"{title}: coordinate with the hospice{npi}",
                    "Care related to the terminal illness is the hospice's; the attending "
                    "physician's own services are the exception.",
                    "hospice",
                    [item["key"]],
                )
            )
        elif item["category"] == "home_health":
            out.append(
                _s(
                    BLOCKED,
                    f"Open home health episode with another agency{npi}",
                    "Services in the episode are billed by that agency (consolidated billing). "
                    "Coordinate before ordering therapy or supplies.",
                    "home_health",
                    [item["key"]],
                )
            )
    return out


def _limits(b: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in b.get("items") or []:
        key, cat = item["key"], item["category"]
        used, total, left = _f(item.get("used")), _f(item.get("total")), _f(item.get("remaining"))
        if cat == "therapy" and used is not None and total:
            threshold = item.get("threshold") or {}
            review = threshold.get("review") or 3000
            if used >= review:
                out.append(
                    _s(
                        LIMITED,
                        f"{item['label']}: ${used:,.0f} used, past ${review:,} review level",
                        "Add the KX modifier with medical-necessity documentation; claims may "
                        "get targeted medical review.",
                        cat,
                        [key],
                    )
                )
            elif used >= total * KX_WARN:
                past = used >= total
                out.append(
                    _s(
                        LIMITED,
                        f"{item['label']}: ${used:,.0f} of ${total:,.0f} used"
                        + (" (over the threshold)" if past else f" ({used / total:.0%})"),
                        "KX modifier and medical-necessity documentation needed"
                        + (" on every further claim." if past else " once the threshold is met."),
                        cat,
                        [key],
                    )
                )
        elif cat in ("inpatient", "snf") and item.get("unit") == "days" and left is not None:
            if left <= LOW_DAYS:
                out.append(
                    _s(
                        LIMITED,
                        f"{item['label']}: {left:g} left",
                        item.get("note") or "Few covered days left in this benefit period.",
                        cat,
                        [key],
                    )
                )
        elif cat == "smoking_cessation" and left is not None:
            if left <= 0:
                out.append(
                    _s(
                        LIMITED,
                        "Smoking cessation: no sessions left in this 12-month period",
                        item.get("note") or "Eight sessions per 12 months.",
                        cat,
                        [key],
                    )
                )
            else:
                out.append(
                    _s(
                        DUE_NOW,
                        f"Smoking cessation counseling: {left:g} sessions available",
                        "Covered for patients who use tobacco; no cost sharing.",
                        cat,
                        [key],
                    )
                )
        elif cat == "rehab" and left is not None and left <= 0:
            out.append(_s(LIMITED, f"{item['label']}: none left", "", cat, [key]))
        elif cat == "rehab" and used is not None and total and used >= total:
            out.append(
                _s(
                    LIMITED,
                    f"{item['label']}: {used:g} of {total:g} used",
                    item.get("note") or "",
                    cat,
                    [key],
                )
            )
    return out


def _preventive(b: dict[str, Any], today: date, visit: date | None) -> list[dict[str, Any]]:
    out = []
    programs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in b.get("items") or []:
        if item["category"] == "preventive" and item.get("next_eligible") and item.get("covered"):
            programs[item.get("program") or item["key"]].append(item)
    for program, rows in programs.items():
        if program == "awv" and any(r["code"] == "G0439" for r in rows):
            rows = [r for r in rows if r["code"] == "G0439"]  # the initial visit is used once
        dated = sorted(rows, key=lambda r: r["next_eligible"])
        first = dated[0]
        when = _d(first["next_eligible"])
        label = first["label"].split(" (")[0]
        now = [r for r in dated if _d(r["next_eligible"]) == when]
        codes = sorted({r["code"] for r in now})
        mods = sorted({r["modifier"] for r in now if r.get("modifier")})
        detail = f"HCPCS {', '.join(codes)}" + (f" (modifier {', '.join(mods)})" if mods else "")
        keys = [r["key"] for r in dated]
        if when and when <= today:
            out.append(
                _s(
                    DUE_NOW,
                    f"{label}: due now",
                    detail,
                    "preventive",
                    keys,
                    program=program,
                    date=when.isoformat(),
                )
            )
        elif when and visit and when <= visit:
            out.append(
                _s(
                    DUE_NOW,
                    f"{label}: allowed by the visit on {visit.isoformat()}",
                    f"Next eligible {when.isoformat()} · {detail}",
                    "preventive",
                    keys,
                    program=program,
                    date=when.isoformat(),
                )
            )
        elif when:
            out.append(
                _s(
                    DUE_ON,
                    f"{label}: due on {when.isoformat()}",
                    f"Not before that date · {detail}",
                    "preventive",
                    keys,
                    program=program,
                    date=when.isoformat(),
                )
            )
    # Flu: one per season. CMS returns the last 18 months of flu shots it paid for.
    flu = [
        i
        for i in b.get("items") or []
        if i.get("program") == "flu_vaccine" and i.get("last_service")
    ]
    start, _ = flu_season(today)
    this_season = [i for i in flu if (_d(i["last_service"]) or date.min) >= start]
    part_b = (b.get("entitlement") or {}).get("part_b") or {}
    if this_season:
        last = max(i["last_service"] for i in this_season)
        out.append(
            _s(
                INFO,
                f"Flu vaccine: already given this season ({last})",
                "Medicare pays for one flu shot per season (August–July).",
                "vaccine",
                [i["key"] for i in this_season],
                program="flu_vaccine",
            )
        )
    elif part_b.get("covered") and not part_b.get("part_b_id_only"):
        out.append(
            _s(
                DUE_NOW,
                "Flu vaccine: none billed to Medicare this season",
                "One per season with no cost sharing. Shots paid elsewhere (pharmacy plans, "
                "public health) don't show here.",
                "vaccine",
                [i["key"] for i in flu],
                program="flu_vaccine",
            )
        )
    # History-only programs: show the last date and who gave it, never a "due" guess.
    latest: dict[str, dict[str, Any]] = {}
    for item in b.get("items") or []:
        program = item.get("program")
        if not item.get("last_service") or program == "flu_vaccine":
            continue
        if program not in latest or item["last_service"] > latest[program]["last_service"]:
            latest[program] = item
    for program, item in latest.items():
        out.append(
            _s(
                INFO,
                f"{item['label'].split(' (')[0]}: last on {item['last_service']}",
                "From Medicare claims history; check the frequency rules before repeating.",
                item["category"],
                [item["key"]],
                program=program,
            )
        )
    return out


def suggestions(
    b: dict[str, Any] | None, *, today: date, visit: date | None = None
) -> dict[str, Any]:
    """{"advisory", "suggestions": [...]}, most urgent first."""
    if not b:
        return {"advisory": ADVISORY, "suggestions": []}
    out: list[dict[str, Any]] = []
    if b.get("errors"):
        codes = set(b["errors"])
        title = (
            "CMS attestation required"
            if "41" in codes
            else "CMS attestation suspended"
            if "97" in codes
            else "CMS didn't return benefits"
        )
        out.append(_s(BLOCKED, title, b.get("message") or "", "errors", []))
    ma = (b.get("routing") or {}).get("medicare_advantage")
    if ma:
        name = ma.get("name") or "the plan"
        payer = f" (Stedi payer {ma['payer_id']})" if ma.get("payer_id") else ""
        out.append(
            _s(
                BLOCKED,
                f"Medicare Advantage: check benefits with {name}",
                f"The patient's Medicare is managed by {name}{payer}. Original Medicare data "
                "isn't used for planning: check eligibility and benefits with the plan.",
                "routing",
                [],
                route=ma,
            )
        )
        return {"advisory": ADVISORY, "suggestions": out}  # never plan an MA patient from CMS
    out += _blockers(b)
    qmb = (b.get("routing") or {}).get("qmb")
    if qmb and qmb.get("active"):
        out.append(
            _s(
                QMB,
                "QMB: don't bill the patient Medicare cost sharing",
                "Qualified Medicare Beneficiary: no deductibles, coinsurance or copays may be "
                "collected from the patient. Bill Medicaid for cost sharing.",
                "routing",
                [],
            )
        )
    out += _limits(b)
    out += _preventive(b, today, visit)
    for gap in b.get("not_in_hets") or []:
        out.append(_s(INFO, gap["label"], gap["note"], "not_in_hets", [gap["key"]]))
    out.sort(key=lambda s: (ORDER[s["kind"]], s.get("date") or ""))
    return {"advisory": ADVISORY, "suggestions": out}


def caller_facts(b: dict[str, Any], *, today: date, allowed: set[str]) -> dict[str, Any]:
    """What a verified caller may hear: Part A/B covered yes/no and preventive due dates. Never
    another provider's NPI, a claim, a service history date or an amount."""
    out: dict[str, Any] = {}
    ma = (b.get("routing") or {}).get("medicare_advantage")
    if "medicare_coverage" in allowed:
        entitlement = b.get("entitlement") or {}
        out["coverage"] = {
            part: ("covered" if (entitlement.get(part) or {}).get("covered") else "not covered")
            for part in ("part_a", "part_b")
            if (entitlement.get(part) or {}).get("covered") is not None
        }
        if ma:
            out["medicare_advantage"] = ma.get("name") or "a Medicare Advantage plan"
    if "medicare_preventive_due" in allowed and not ma:
        due = []
        for s in suggestions(b, today=today)["suggestions"]:
            if s["kind"] in (DUE_NOW, DUE_ON) and s.get("program") in CALLER_SAFE:
                due.append(
                    {
                        "service": s["title"].split(":")[0],
                        "when": "now" if s["kind"] == DUE_NOW else s.get("date"),
                    }
                )
        out["preventive"] = due
    return out
