"""Verification, proxy and contact rules for person memory (OPERATING_PLAN §5.1, P3).

Pure functions: no database. ``service`` and ``tools`` feed them rows and record the outcome.

- **Identity**: a workspace names the identifiers a caller must match (default: name and date
  of birth). Every listed identifier must match; a missing one fails, never "partly passes".
- **Step-up**: sensitive topics (default: billing, care plan, records) need one more identifier
  from the step-up list before anything about them is shared.
- **Proxy**: a representative may act for someone only through an active, unrevoked
  relationship marked ``proxy`` whose scope covers the topic, inside its start and expiry dates.
- **Contact**: do-not-contact always wins; texts need granted consent; voice stops on revoked
  consent; outreach waits for the person's contact windows.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("patient", "client", "family", "caregiver", "staff", "payer_contact")
KIND_LABEL = {
    "patient": "Patient",
    "client": "Client",
    "family": "Family or representative",
    "caregiver": "Caregiver",
    "staff": "Staff",
    "payer_contact": "Payer contact",
}
CHANNELS = ("voice", "sms", "email")
# What a proxy may act on, and what memory an agent may read: one vocabulary.
TOPICS = ("general", "appointments", "schedule", "billing", "care_plan", "records")
TOPIC_LABEL = {
    "general": "Messages and contact preferences",
    "appointments": "Appointments",
    "schedule": "Home care visits and shifts",
    "billing": "Billing and insurance",
    "care_plan": "Care plan",
    "records": "Medical records",
}
RELATIONS = (
    "parent",
    "child",
    "spouse",
    "sibling",
    "guardian",
    "power_of_attorney",
    "caregiver_family",
    "other",
    "preferred_caregiver",
    "declined_caregiver",
)
FIT_RELATIONS = ("preferred_caregiver", "declined_caregiver")
OBSERVATION_TOPICS = (
    "preference",
    "availability",
    "contact",
    "schedule",
    "care_plan",
    "billing",
    "note",
)
# Memory topics an agent reads under each conversation topic.
READS = {
    "general": ("preference", "contact", "note"),
    "appointments": ("preference", "availability", "contact"),
    "schedule": ("preference", "availability", "schedule", "contact"),
    "billing": ("billing",),
    "care_plan": ("care_plan",),
    "records": (),
}
CREDENTIALS = {
    "rn_license": "RN licence",
    "lpn_license": "LPN licence",
    "cna_certification": "CNA certification",
    "hha_certification": "Home health aide certification",
    "cpr": "CPR",
    "tb_test": "TB test",
    "background_check": "Background check",
    "drivers_license": "Driver's licence",
    "auto_insurance": "Auto insurance",
}
# An expired one of these stops a caregiver being offered any work, required or not.
COMPLIANCE_CREDENTIALS = frozenset(
    {"rn_license", "lpn_license", "cna_certification", "hha_certification", "cpr", "tb_test"}
)
SKILLS = (
    "personal_care",
    "companionship",
    "meal_prep",
    "medication_reminders",
    "dementia_care",
    "hoyer_lift",
    "wound_care",
    "skilled_nursing",
    "pediatric",
    "spanish",
)
IDENTIFIERS = ("name", "date_of_birth", "zip", "phone_last4", "member_id", "caller_phone")
IDENTIFIER_LABEL = {
    "name": "First and last name",
    "date_of_birth": "Date of birth",
    "zip": "Home ZIP code",
    "phone_last4": "Last four digits of the phone on file",
    "member_id": "Member ID",
    "caller_phone": "Calling from the phone on file",
}

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "identifiers": ["name", "date_of_birth"],
    "step_up_identifiers": ["zip", "phone_last4"],
    "step_up_topics": ["billing", "care_plan", "records"],
    "max_attempts": 2,
    # A proxy is verified like anyone else; the patient's identity is checked as well.
    "proxy_subject_identifiers": ["name", "date_of_birth"],
}


class PolicyError(ValueError):
    pass


def clean_policy(rules: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate an edited policy. Basic identity always needs at least two identifiers, and a
    caller ID match alone can never pass anyone (caller ID can be spoofed)."""
    out = {**DEFAULT_POLICY, **{k: v for k, v in rules.items() if k in DEFAULT_POLICY}}
    for key in ("identifiers", "step_up_identifiers", "proxy_subject_identifiers"):
        values = list(dict.fromkeys(out[key]))
        if any(v not in IDENTIFIERS for v in values):
            raise PolicyError(f"Unknown identifier in {key}.")
        out[key] = values
    if len(out["identifiers"]) < 2:
        raise PolicyError("Basic verification needs at least two identifiers.")
    if len([i for i in out["identifiers"] if i != "caller_phone"]) < 2:
        raise PolicyError("Calling from the phone on file can't be one of the two identifiers.")
    if not out["step_up_identifiers"]:
        raise PolicyError("Step-up needs at least one identifier.")
    if set(out["step_up_identifiers"]) <= set(out["identifiers"]):
        raise PolicyError("Step-up must ask for something basic verification doesn't.")
    if any(t not in TOPICS for t in out["step_up_topics"]):
        raise PolicyError("Unknown topic in step_up_topics.")
    if not 1 <= int(out["max_attempts"]) <= 3:
        raise PolicyError("Allow one to three verification attempts per call.")
    out["max_attempts"] = int(out["max_attempts"])
    if not out["proxy_subject_identifiers"]:
        raise PolicyError("Name at least one identifier for the person a proxy calls about.")
    out["version"] = int((previous or DEFAULT_POLICY).get("version", 1)) + 1
    return out


# --- identity matching ----------------------------------------------------------------------------


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def matches(identifier: str, given: dict[str, Any], on_file: dict[str, Any], caller: str | None):
    """Does one identifier match? ``None`` when it wasn't given or nothing is on file."""
    if identifier == "name":
        want = (norm(given.get("first_name")), norm(given.get("last_name")))
        have = (norm(on_file.get("first_name")), norm(on_file.get("last_name")))
        return None if not all(want) or not all(have) else want == have
    if identifier == "date_of_birth":
        want, have = iso_date(given.get("date_of_birth")), iso_date(on_file.get("date_of_birth"))
        return None if not want or not have else want == have
    if identifier == "zip":
        want, have = digits(given.get("zip"))[:5], digits(on_file.get("zip"))[:5]
        return None if len(want) != 5 or len(have) != 5 else want == have
    if identifier == "phone_last4":
        want, have = digits(given.get("phone_last4"))[-4:], digits(on_file.get("phone"))[-4:]
        return None if len(want) != 4 or len(have) != 4 else want == have
    if identifier == "member_id":
        want, have = norm(given.get("member_id")), norm(on_file.get("member_id"))
        return None if not want or not have else want == have
    if identifier == "caller_phone":
        want, have = digits(caller)[-10:], digits(on_file.get("phone"))[-10:]
        return None if len(want) != 10 or len(have) != 10 else want == have
    return None


def check_identifiers(
    required: list[str], given: dict[str, Any], on_file: dict[str, Any], caller: str | None
) -> tuple[bool, list[str]]:
    """All required identifiers must match. Returns (passed, identifiers checked)."""
    results = [matches(i, given, on_file, caller) for i in required]
    return bool(required) and all(r is True for r in results), list(required)


def step_up_passes(
    policy: dict[str, Any], given: dict[str, Any], on_file: dict[str, Any], caller: str | None
) -> tuple[bool, list[str]]:
    """Any one step-up identifier that matches."""
    checked = [i for i in policy["step_up_identifiers"] if matches(i, given, on_file, caller)]
    return bool(checked), checked


def needs_step_up(policy: dict[str, Any], topic: str) -> bool:
    return topic in policy["step_up_topics"]


# --- proxies -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyDecision:
    authorized: bool
    reason: str  # authorized|no_relationship|revoked|not_started|expired|out_of_scope
    scope: tuple[str, ...] = ()


def proxy_decision(relationships: list[Any], topic: str, today: date) -> ProxyDecision:
    """Is the related person an authorised representative for ``topic`` today?

    ``relationships`` are every row between the subject and the related person. The most
    permissive active one decides; the reason names the closest miss otherwise."""
    rows = [r for r in relationships if r.proxy]
    if not rows:
        return ProxyDecision(False, "no_relationship")
    misses = []
    for r in rows:
        if r.revoked_at is not None:
            misses.append("revoked")
        elif r.starts_on and today < r.starts_on:
            misses.append("not_started")
        elif r.expires_on and today > r.expires_on:
            misses.append("expired")
        elif topic not in r.scope and "all" not in r.scope:
            misses.append("out_of_scope")
        else:
            scope = TOPICS if "all" in r.scope else tuple(t for t in TOPICS if t in r.scope)
            return ProxyDecision(True, "authorized", scope)
    order = ("out_of_scope", "expired", "not_started", "revoked")
    return ProxyDecision(False, next(m for m in order if m in misses))


# --- contact -------------------------------------------------------------------------------------


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Chicago")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Chicago")


def _hm(value: str) -> time:
    hour, minute = (int(p) for p in str(value).split(":")[:2])
    return time(hour % 24, minute % 60)


def clean_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for w in windows or []:
        days = sorted({int(d) for d in w.get("days", []) if 0 <= int(d) <= 6})
        try:
            start, end = _hm(w["start"]), _hm(w["end"])
        except (KeyError, ValueError, TypeError):
            raise PolicyError("Each window needs a start and end time like 09:00.") from None
        if not days or end <= start:
            raise PolicyError("Each window needs at least one day and an end after its start.")
        out.append({"days": days, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")})
    return out


def within(windows: list[dict[str, Any]], start: datetime, end: datetime, tz: ZoneInfo) -> bool:
    """Is the whole of [start, end] inside one window (same local day)?"""
    a, b = start.astimezone(tz), end.astimezone(tz)
    if a.date() != b.date():
        return False  # overnight shifts need an overnight window type (not modelled yet)
    return any(
        a.weekday() in w["days"] and _hm(w["start"]) <= a.time() and b.time() <= _hm(w["end"])
        for w in windows
    )


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str  # ok|do_not_contact|inactive|no_consent|consent_revoked|outside_window|no_channel


def can_contact(person: Any, channel: str, at: datetime) -> ContactDecision:
    """May an agent start contact with this person on ``channel`` at ``at``?"""
    if person.do_not_contact:
        return ContactDecision(False, "do_not_contact")
    if person.status != "active":
        return ContactDecision(False, "inactive")
    if channel not in CHANNELS:
        return ContactDecision(False, "no_channel")
    state = ((person.consent or {}).get(channel) or {}).get("state", "unknown")
    if state == "revoked":
        return ContactDecision(False, "consent_revoked")
    if channel == "sms" and state != "granted":
        return ContactDecision(False, "no_consent")
    windows = person.contact_windows or []
    if windows and not within(windows, at, at, zone(person.timezone)):
        return ContactDecision(False, "outside_window")
    return ContactDecision(True, "ok")


def credential_state(expires_on: date | None, on: date, soon_days: int = 30) -> str:
    if expires_on is None:
        return "current"
    if expires_on < on:
        return "expired"
    return "expiring" if (expires_on - on).days <= soon_days else "current"
