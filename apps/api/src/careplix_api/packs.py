"""Product-line packs (OPERATING_PLAN §4 D3).

A workspace's product line selects its pack: which Studio sections it shows and which case types
it can create. Everything else (agents, Voice Studio, calls, approvals, connections) is shared.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LINE = "practice"

# Studio sections every line shows; keys match the web app's navigation.
SHARED_SECTIONS = (
    "home",
    "agents",
    "studio",
    "personas",
    "shadow",
    "cases",
    "people",
    "tasks",
    "calls",
    "results",
    "connections",
    "integrations",
    "hub",
    "browser",
    "approvals",
    "inbox",
    "texts",  # text messages and call hand-offs: every lead workflow in both lines needs them
)

PACKS: dict[str, dict[str, Any]] = {
    "practice": {
        "label": "CarePlix Practice",
        "audience": "Ambulatory clinics",
        "sections": (
            *SHARED_SECTIONS,
            "care",
            "practice",
            "backfill",
            "claims",
            "prior_auth",
            "clinical",  # refills, discharge follow-up, pre-visit, approved instructions
            "intake",  # registration forms by secure link
        ),
        "case_types": (
            "prior_authorization",
            "claim_status",
            "claim_denial",
            "eligibility_check",
            "appointment_reminder",
            "care_outreach",
            "slot_backfill",
            "call_back",
            "refill_request",
            "discharge_followup",
            "previsit_prep",
            "patient_intake",
        ),
    },
    "home_health": {
        "label": "CarePlix Home Health",
        "audience": "Home care and home health agencies",
        # Clinic pages (Practice schedule, backfill, care opportunities) stay hidden until the
        # home-care connector and its workflows land (OPERATING_PLAN phase 3).
        "sections": SHARED_SECTIONS,
        "case_types": ("prior_authorization", "claim_status", "eligibility_check", "call_back"),
    },
}

LINES = tuple(PACKS)


def line_of(product_line: str | None) -> str:
    return product_line if product_line in PACKS else DEFAULT_LINE


def pack(product_line: str | None) -> dict[str, Any]:
    line = line_of(product_line)
    p = PACKS[line]
    return {
        "line": line,
        "label": p["label"],
        "audience": p["audience"],
        "sections": list(p["sections"]),
        "case_types": list(p["case_types"]),
    }


def allows_case_type(product_line: str | None, case_type: str) -> bool:
    return case_type in PACKS[line_of(product_line)]["case_types"]
