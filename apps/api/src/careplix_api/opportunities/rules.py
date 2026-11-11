"""The care-opportunity rules: which programs and services a patient's record qualifies them for.

Pure functions over one patient's facts, no database. Every suggestion names the facts it came from
(``why``) so a clinician can check it against the chart before anyone contacts the patient. Nothing
here books, bills or writes to the chart; a clinician approves each suggestion first.

The rules follow Medicare's published program requirements as of the 2026 physician fee schedule:
CMS MLN "Chronic Care Management Services" (MLN909188), "Behavioral Health Integration Services"
(MLN909432), "Transitional Care Management Services" (MLN908628), "Medicare Wellness Visits"
(MLN6775421) and the USPSTF screening intervals. Time-based programs (care management, monitoring,
behavioral health) need clinical staff time: the voice agent may explain, schedule and record
consent, but its own minutes never count toward a billable service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

MEDICARE = {"medicare", "medicare_advantage"}

# ICD-10 prefixes the CCM/PCM rules treat as chronic conditions expected to last 12+ months.
CHRONIC: dict[str, str] = {
    "E11": "Type 2 diabetes",
    "E10": "Type 1 diabetes",
    "I10": "Hypertension",
    "I11": "Hypertensive heart disease",
    "I50": "Heart failure",
    "I48": "Atrial fibrillation",
    "I25": "Coronary artery disease",
    "J44": "COPD",
    "J45": "Asthma",
    "N18": "Chronic kidney disease",
    "E78": "Hyperlipidemia",
    "E66": "Obesity",
    "F32": "Depression",
    "F33": "Recurrent depression",
    "F41": "Anxiety disorder",
    "G30": "Alzheimer's disease",
    "F03": "Dementia",
    "M17": "Knee osteoarthritis",
    "M06": "Rheumatoid arthritis",
    "G20": "Parkinson's disease",
}
# Conditions a single-condition program (PCM) or remote monitoring is built for.
HIGH_RISK = {"I50", "E11", "E10", "J44", "N18", "I48", "G20"}
RPM_CONDITIONS = {"I10", "I11", "I50", "E11", "E10", "J44", "E66"}
BEHAVIORAL = {"F32", "F33", "F41"}
TOBACCO = {"F17", "Z72"}

# Service codes that count as "already done" for each program.
CODES = {
    "awv": {"G0438", "G0439", "G0402"},
    "ccm": {"99490", "99439", "99487", "99489", "99491", "99437", "G0556", "G0557", "G0558"},
    "pcm": {"99424", "99425", "99426", "99427"},
    "rpm": {"99453", "99454", "99457", "99458"},
    "bhi": {"99484", "99492", "99493", "99494", "G0323"},
    "tcm": {"99495", "99496"},
    "acp": {"99497", "99498"},
    "depression_screen": {"G0444"},
    "sdoh": {"G0136"},
    "tobacco": {"99406", "99407"},
    "dsmt_mnt": {"G0108", "G0109", "97802", "97803"},
    "a1c": {"83036"},
    "colonoscopy": {"45378", "45380", "45385", "G0105", "G0121"},
    "fit": {"82274", "G0328"},
    "stool_dna": {"81528"},
    "mammogram": {"77067", "77063"},
}


@dataclass
class Facts:
    patient_ref: str
    age: int | None = None
    sex: str | None = None  # "F" | "M" | None
    coverage: str | None = None  # medicare | medicare_advantage | medicaid | commercial
    conditions: list[dict[str, Any]] = field(default_factory=list)  # {"code", "date"}
    services: list[dict[str, Any]] = field(default_factory=list)  # {"code", "date"}
    discharges: list[dict[str, Any]] = field(default_factory=list)  # {"date", "facility"}
    screenings: list[dict[str, Any]] = field(default_factory=list)  # {"kind", "score", "date"}


@dataclass
class Program:
    key: str
    title: str
    codes: str
    category: (
        str  # care_management | monitoring | transitions | behavioral | prevention | education
    )
    consent: bool  # documented patient consent needed before the service starts
    staff_time: bool  # billed on clinical staff time: the agent's minutes never count
    outreach: str  # what the voice agent may do for it


PROGRAMS: dict[str, Program] = {
    p.key: p
    for p in [
        Program(
            "awv",
            "Annual Wellness Visit",
            "G0438 / G0439",
            "prevention",
            False,
            False,
            "Explain the visit and book it.",
        ),
        Program(
            "ccm",
            "Chronic Care Management",
            "99490, 99439, 99487",
            "care_management",
            True,
            True,
            "Explain the program and cost share, record consent, book the enrolment visit.",
        ),
        Program(
            "pcm",
            "Principal Care Management",
            "99424-99427",
            "care_management",
            True,
            True,
            "Explain the program, record consent, book the first touchpoint.",
        ),
        Program(
            "rpm",
            "Remote Patient Monitoring",
            "99453, 99454, 99457, 99458",
            "monitoring",
            True,
            True,
            "Explain the device and readings, record consent, arrange device setup.",
        ),
        Program(
            "tcm",
            "Transitional Care Management",
            "99495 / 99496",
            "transitions",
            False,
            False,
            "Make the post-discharge contact within 2 business days and book the visit.",
        ),
        Program(
            "bhi",
            "Behavioral Health Integration",
            "99484, 99492-99494",
            "behavioral",
            True,
            True,
            "Explain the program, record consent, book with the care manager.",
        ),
        Program(
            "acp",
            "Advance Care Planning",
            "99497 / 99498",
            "prevention",
            False,
            False,
            "Offer the conversation and book it, ideally with the wellness visit.",
        ),
        Program(
            "depression_screen",
            "Annual depression screening",
            "G0444",
            "prevention",
            False,
            False,
            "Book it with the next visit.",
        ),
        Program(
            "sdoh",
            "Social needs risk assessment",
            "G0136",
            "prevention",
            False,
            False,
            "Book it with the next wellness visit.",
        ),
        Program(
            "tobacco",
            "Tobacco cessation counseling",
            "99406 / 99407",
            "behavioral",
            False,
            False,
            "Offer counseling and book it.",
        ),
        Program(
            "dsmt_mnt",
            "Diabetes education / nutrition therapy",
            "G0108, 97802",
            "education",
            False,
            False,
            "Explain the referral and book the first session.",
        ),
        Program(
            "a1c", "A1c test overdue", "83036", "prevention", False, False, "Book a lab visit."
        ),
        Program(
            "colorectal",
            "Colorectal cancer screening",
            "FIT 82274 / colonoscopy 45378",
            "prevention",
            False,
            False,
            "Offer a FIT kit or book a colonoscopy consult.",
        ),
        Program(
            "mammogram",
            "Screening mammogram",
            "77067",
            "prevention",
            False,
            False,
            "Book the mammogram.",
        ),
    ]
}


def _d(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _prefix(code: str) -> str:
    return str(code).upper().replace(".", "")[:3]


def _last(facts: Facts, codes: set[str]) -> date | None:
    dates = [_d(s.get("date")) for s in facts.services if str(s.get("code", "")).upper() in codes]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def _since(facts: Facts, codes: set[str], today: date, days: int) -> date | None:
    """The last service in ``codes`` within ``days`` of today, if any."""
    last = _last(facts, codes)
    return last if last and (today - last).days < days else None


def _chronic(facts: Facts) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in facts.conditions:
        p = _prefix(c.get("code", ""))
        if p in CHRONIC:
            out[p] = CHRONIC[p]
    return out


def _names(prefixes: set[str]) -> str:
    return ", ".join(sorted(CHRONIC[c] for c in prefixes))


def _add_business_days(start: date, n: int) -> date:
    day = start
    while n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def evaluate(facts: Facts, today: date) -> list[dict[str, Any]]:
    """Every program this patient qualifies for and hasn't had within its interval."""
    out: list[dict[str, Any]] = []
    medicare = facts.coverage in MEDICARE
    chronic = _chronic(facts)
    names = ", ".join(sorted(chronic.values()))

    def add(key: str, why: list[str], priority: int = 2, due: date | None = None) -> None:
        p = PROGRAMS[key]
        out.append(
            {
                "program": key,
                "title": p.title,
                "codes": p.codes,
                "category": p.category,
                "why": why,
                "priority": priority,  # 1 time-sensitive, 2 standard, 3 routine
                "due_by": due.isoformat() if due else None,
                "consent_required": p.consent,
                "staff_time": p.staff_time,
                "outreach": p.outreach,
            }
        )

    # Transitional care: a discharge in the last 14 days, with no TCM billed since.
    recent = [
        d
        for d in (_d(x.get("date")) for x in facts.discharges)
        if d and 0 <= (today - d).days <= 14
    ]
    if recent:
        discharged = max(recent)
        if not (_last(facts, CODES["tcm"]) and _last(facts, CODES["tcm"]) >= discharged):
            contact_by = _add_business_days(discharged, 2)
            add(
                "tcm",
                [
                    f"Discharged {discharged.isoformat()}",
                    f"Interactive contact due by {contact_by.isoformat()}",
                    "Face-to-face visit within 7 days (high complexity) or 14 days",
                ],
                priority=1,
                due=contact_by,
            )

    if medicare:
        if not _since(facts, CODES["awv"], today, 365):
            last = _last(facts, CODES["awv"])
            add(
                "awv",
                [
                    "Medicare coverage",
                    f"Last wellness visit: {last.isoformat() if last else 'none on record'}",
                ],
            )
        if not _since(facts, CODES["depression_screen"], today, 365):
            add(
                "depression_screen",
                ["Medicare coverage", "No depression screening in the last 12 months"],
                3,
            )
        if not _since(facts, CODES["sdoh"], today, 180):
            add("sdoh", ["Medicare coverage", "No social needs assessment in the last 6 months"], 3)
        if (facts.age or 0) >= 65 and not _since(facts, CODES["acp"], today, 365):
            add("acp", [f"Age {facts.age}", "No advance care planning in the last 12 months"], 3)

        enrolled_cm = _since(facts, CODES["ccm"] | CODES["pcm"], today, 60)
        if len(chronic) >= 2 and not enrolled_cm:
            add(
                "ccm",
                [f"{len(chronic)} chronic conditions: {names}", "Not enrolled in care management"],
                1,
            )
        elif len(chronic) == 1 and set(chronic) & HIGH_RISK and not enrolled_cm:
            add(
                "pcm",
                [f"One high-risk chronic condition: {names}", "Not enrolled in care management"],
            )

        rpm = sorted({CHRONIC[c] for c in set(chronic) & RPM_CONDITIONS})
        if rpm and not _since(facts, CODES["rpm"], today, 60):
            add(
                "rpm",
                [
                    f"Condition suited to home monitoring: {', '.join(rpm)}",
                    "No monitoring billed in 60 days",
                ],
            )

        positive = [
            s
            for s in facts.screenings
            if str(s.get("kind", "")).lower() in ("phq9", "gad7")
            and int(s.get("score") or 0) >= 10
            and (_d(s.get("date")) or date.min) >= today - timedelta(days=365)
        ]
        if (set(chronic) & BEHAVIORAL or positive) and not _since(facts, CODES["bhi"], today, 60):
            why = (
                [f"Behavioral health diagnosis: {_names(set(chronic) & BEHAVIORAL)}"]
                if set(chronic) & BEHAVIORAL
                else []
            )
            why += [f"{s['kind'].upper()} score {s['score']} on {s.get('date')}" for s in positive]
            add("bhi", why + ["Not enrolled in behavioral health integration"])

    if any(_prefix(c.get("code", "")) in TOBACCO for c in facts.conditions) and not _since(
        facts, CODES["tobacco"], today, 365
    ):
        add(
            "tobacco",
            ["Tobacco use on the problem list", "No cessation counseling in the last 12 months"],
        )

    diabetic = bool({"E10", "E11"} & set(chronic))
    if (
        (diabetic or "N18" in chronic)
        and medicare
        and not _since(facts, CODES["dsmt_mnt"], today, 365)
    ):
        add(
            "dsmt_mnt",
            [
                f"{'Diabetes' if diabetic else 'Chronic kidney disease'} on the problem list",
                "No diabetes education or nutrition therapy in the last 12 months",
            ],
            3,
        )
    if diabetic and not _since(facts, CODES["a1c"], today, 180):
        last = _last(facts, CODES["a1c"])
        add(
            "a1c",
            [
                "Diabetes on the problem list",
                f"Last A1c: {last.isoformat() if last else 'none on record'}",
            ],
        )

    age = facts.age or 0
    if 45 <= age <= 75 and not (
        _since(facts, CODES["colonoscopy"], today, 3650)
        or _since(facts, CODES["fit"], today, 365)
        or _since(facts, CODES["stool_dna"], today, 1095)
    ):
        add(
            "colorectal",
            [f"Age {age}", "No colonoscopy in 10 years, FIT in 1 year or stool DNA in 3 years"],
        )
    if facts.sex == "F" and 40 <= age <= 74 and not _since(facts, CODES["mammogram"], today, 730):
        add("mammogram", [f"Age {age}", "No screening mammogram in the last 2 years"])

    return sorted(out, key=lambda o: (o["priority"], o["title"]))
