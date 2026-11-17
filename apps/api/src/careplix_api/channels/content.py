"""What a text may say: minimum-necessary PHI, keywords, and approved wording.

Texts travel over carrier networks and sit on lock screens, so a body may name the practice, the
person's first name, a day and time, and a short instruction. It may not carry identifiers (dates
of birth, member or record numbers, SSNs) or clinical detail (diagnoses, results, medicines).
``check`` refuses a body that does; :data:`TEMPLATES` is wording that passes.
"""

from __future__ import annotations

import re

MAX_CHARS = 480  # three concatenated segments; anything longer belongs behind a secure link

_REFUSE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "identifier"),  # SSN
    (re.compile(r"\b(dob|d\.o\.b\.?|date of birth|born on)\b", re.I), "date_of_birth"),
    (
        re.compile(
            r"\b(member|policy|subscriber|medicare|medicaid|mrn|chart|record|group)\s*"
            r"(id|number|no\.?|#)",
            re.I,
        ),
        "identifier",
    ),
    (
        re.compile(
            r"\b(diagnos\w*|test results?|lab results?|biopsy|positive for|negative for|hiv|"
            r"cancer|pregnan\w*|prescription for|dosage|mg\b|std|sti)\b",
            re.I,
        ),
        "clinical",
    ),
]

STOP_WORDS = {"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE"}
STOP_WORDS |= {"OPTOUT", "OPT OUT"}
START_WORDS = {"START", "UNSTOP"}
HELP_WORDS = {"HELP", "INFO"}
YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed"}
YES_WORDS |= {"accept", "1"}
NO_WORDS = {"no", "n", "nope", "nah", "decline", "pass", "2"}


def check(body: str) -> str | None:
    """Why a body can't be sent (``None`` when it can)."""
    text = body.strip()
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return "too_long"
    for pattern, reason in _REFUSE:
        if pattern.search(text):
            return reason
    return None


def segments(body: str) -> int:
    """Billable segments: GSM-7 is 160 (153 joined), anything else UCS-2 at 70 (67 joined)."""
    gsm = all(ord(c) < 128 for c in body)
    single, joined = (160, 153) if gsm else (70, 67)
    return 1 if len(body) <= single else -(-len(body) // joined)


def with_opt_out(body: str) -> str:
    """Carriers expect opt-out wording on the first text and periodically after."""
    return body if "STOP" in body.upper() else f"{body.rstrip()} Reply STOP to opt out."


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def intent(body: str) -> str:
    """yes | no | stop | start | help | other. Keywords must be the whole message (CTIA)."""
    whole = " ".join(body.strip().upper().split()).rstrip(".!")
    if whole in STOP_WORDS:
        return "stop"
    if whole in START_WORDS:
        return "start"
    if whole in HELP_WORDS:
        return "help"
    words = _words(body)
    if not words or len(words) > 4:
        return "other"  # a sentence needs a person to read it
    if words[0] in YES_WORDS and not set(words) & NO_WORDS:
        return "yes"
    if words[0] in NO_WORDS and not set(words) & YES_WORDS:
        return "no"
    return "other"


# Wording a playbook can send as is. {brand} is the workspace's brand name (the practice or
# agency); every placeholder is administrative, never clinical.
TEMPLATES: dict[str, str] = {
    "appointment_reminder": "{brand}: Hi {first_name}, a reminder of your appointment on {when}. "
    "Reply YES to confirm or NO if you need to change it.",
    "opening_offer": "{brand}: Hi {first_name}, an earlier appointment opened on {when}. "
    "Reply YES to take it or NO to keep your current time. First to reply gets it.",
    "shift_offer": "{brand}: Open shift {when} near {area}. Reply YES to take it or NO to pass. "
    "First confirmed yes gets it.",
    "offer_confirmed": "{brand}: You're confirmed for {when}. Thank you!",
    "offer_filled": "{brand}: Thanks for replying. That time has been filled; we'll text you "
    "about the next one.",
    "callback_promise": "{brand}: Sorry we missed you on the phone. {staff} will call you back "
    "{promise}.",
}


def render(template: str, **values: str) -> str:
    return TEMPLATES[template].format_map(values)
