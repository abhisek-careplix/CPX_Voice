"""Case types: what each kind of work is, what an agent needs to do it, and who it calls.

A case is one piece of work (a prior auth to chase, a claim to check, a patient to remind). Its type
is the contract between the case and the agent that works it:

- ``fields``: the facts the case holds, where each lives in the case data, and which are required.
  The facts are the agent's whole context for the call, and ``share`` marks the ones it may tell
  the other side.
- ``counterparty``: who the agent calls (a payer, a patient) and where that number lives in the
  case, so a task is created against the right party without anyone typing a number.
- ``agents``: the agent templates that can work this type. Specialised templates (prior auth) know
  their own objective; the general calling agent (``errand``) gets the type's ``objective``.
- ``checks``: systems asked before and after the call (API first: the payer's API, then the phone).
- ``packs``: guardrail packs every call on this type runs under, whatever the agent's config says.

The types are server-owned, like guardrail packs, so a case can never widen what an agent may do.
"""

from __future__ import annotations

import random
import re
from datetime import date, timedelta
from typing import Any

PATIENT_CALL_PACKS = ("outreach-consent", "patient-privacy")


def _f(key: str, label: str, path: str, kind: str = "text", *, required: bool = True, share=True):
    return {
        "key": key,
        "label": label,
        "path": path,
        "kind": kind,
        "required": required,
        "share": share,
    }


CASE_TYPES: dict[str, dict[str, Any]] = {
    "prior_authorization": {
        "name": "Prior auth status",
        "program": "Prior auth and claims",
        "prefix": "CASE",
        "summary": "Chase a submitted prior authorization with the payer: status, reference, "
        "what's missing.",
        "counterparty": {
            "role": "payer",
            "label": "Payer",
            "name": "payer.name",
            "phone": "payer.phone",
        },
        "on_behalf_of": "provider.name",
        "subject": "patient.display_name",
        "headline": ("service.description", "payer.name"),
        "agents": ["auth-status"],
        "checks": [
            "Provider NPI, diagnosis and procedure codes against public registries",
            "The payer's prior authorization API before the call, and again after it",
        ],
        "packs": [],
        "objective": None,  # the prior-auth template carries its own objective
        "fields": [
            _f("patient_name", "Patient", "patient.display_name"),
            _f("date_of_birth", "Date of birth", "patient.date_of_birth", "date"),
            _f("member_id", "Member ID", "patient.member_id", "code"),
            _f("postal_code", "Postal code", "patient.postal_code", "code"),
            _f("provider_name", "Provider", "provider.name"),
            _f("provider_npi", "Provider NPI", "provider.npi", "code"),
            _f("payer_name", "Payer", "payer.name"),
            _f("payer_phone", "Payer phone", "payer.phone", "phone", required=False, share=False),
            _f("service_description", "Service", "service.description"),
            _f("cpt_code", "CPT code", "service.cpt_code", "code"),
            _f("units", "Units", "service.units", "number"),
            _f("submitted_on", "Submitted on", "request.submitted_on", "date"),
            _f("urgency", "Urgency", "request.urgency"),
            _f(
                "known_auth_reference",
                "Auth reference",
                "request.known_auth_reference",
                "code",
                required=False,
            ),
        ],
    },
    "claim_status": {
        "name": "Claim status",
        "program": "Prior auth and claims",
        "prefix": "CLM",
        "summary": "Ask the payer where a claim stands: paid, pending or denied, and why.",
        "counterparty": {
            "role": "payer",
            "label": "Payer",
            "name": "payer.name",
            "phone": "payer.phone",
        },
        "on_behalf_of": "provider.name",
        "subject": "patient.display_name",
        "headline": ("claim.number", "payer.name"),
        "agents": ["errand"],
        "checks": [],
        "packs": ["financial-safety", "manipulation-resistance"],
        "objective": "Find out the status of claim {claim_number} for {patient_name} (date of "
        "service {date_of_service}, billed {billed_amount}): whether it is paid, pending or "
        "denied, the amount paid or the denial reason, and a reference number for this call.",
        "fields": [
            _f("patient_name", "Patient", "patient.display_name"),
            _f("date_of_birth", "Date of birth", "patient.date_of_birth", "date"),
            _f("member_id", "Member ID", "patient.member_id", "code"),
            _f("provider_name", "Billing provider", "provider.name"),
            _f("provider_npi", "Provider NPI", "provider.npi", "code"),
            _f("payer_name", "Payer", "payer.name"),
            _f("payer_phone", "Payer phone", "payer.phone", "phone", share=False),
            _f("claim_number", "Claim number", "claim.number", "code"),
            _f("date_of_service", "Date of service", "claim.date_of_service", "date"),
            _f("billed_amount", "Billed amount", "claim.billed_amount", "money"),
            _f("cpt_code", "CPT code", "claim.cpt_code", "code", required=False),
        ],
    },
    "eligibility_check": {
        "name": "Benefits check",
        "program": "Benefits and eligibility",
        "prefix": "ELG",
        "summary": "Confirm coverage is active and get the benefits for a planned service.",
        "counterparty": {
            "role": "payer",
            "label": "Payer",
            "name": "payer.name",
            "phone": "payer.phone",
        },
        "on_behalf_of": "provider.name",
        "subject": "patient.display_name",
        "headline": ("service.description", "payer.name"),
        "agents": ["errand"],
        "checks": [],
        "packs": ["financial-safety", "manipulation-resistance"],
        "objective": "Confirm that {patient_name}'s coverage with {payer_name} is active, and find "
        "the benefits for {service_description} (CPT {cpt_code}): copay or coinsurance, how much "
        "of the deductible is met, and whether prior authorization is needed. Get a reference "
        "number for this call.",
        "fields": [
            _f("patient_name", "Patient", "patient.display_name"),
            _f("date_of_birth", "Date of birth", "patient.date_of_birth", "date"),
            _f("member_id", "Member ID", "patient.member_id", "code"),
            _f("provider_name", "Provider", "provider.name"),
            _f("provider_npi", "Provider NPI", "provider.npi", "code"),
            _f("payer_name", "Payer", "payer.name"),
            _f("payer_phone", "Payer phone", "payer.phone", "phone", share=False),
            _f("service_description", "Planned service", "service.description"),
            _f("cpt_code", "CPT code", "service.cpt_code", "code"),
            _f("planned_on", "Planned date", "service.planned_on", "date", required=False),
        ],
    },
    "appointment_reminder": {
        "name": "Appointment reminder",
        "program": "Front desk",
        "prefix": "APT",
        "summary": "Remind a patient of an upcoming visit and confirm they're coming.",
        "counterparty": {
            "role": "patient",
            "label": "Patient",
            "name": "patient.display_name",
            "phone": "patient.phone",
        },
        "on_behalf_of": "practice.name",
        "subject": "patient.display_name",
        "headline": ("appointment.starts_at", "practice.name"),
        "agents": ["errand"],
        "checks": [],
        "packs": list(PATIENT_CALL_PACKS),
        "objective": "Reach {patient_name} and remind them of their appointment with "
        "{clinician_name} at {practice_name}, {location}, on {appointment_at}. Confirm they will "
        "attend, or record that they want to reschedule or cancel so the front desk can follow up. "
        "Share the preparation notes only once you are speaking with {patient_name}.",
        "fields": [
            _f("patient_name", "Patient", "patient.display_name"),
            _f("patient_phone", "Patient phone", "patient.phone", "phone", share=False),
            _f("date_of_birth", "Date of birth", "patient.date_of_birth", "date", share=False),
            _f("practice_name", "Practice", "practice.name"),
            _f("practice_phone", "Practice call-back number", "practice.phone", "phone"),
            _f("clinician_name", "Clinician", "appointment.clinician"),
            _f("appointment_at", "Appointment", "appointment.starts_at", "datetime"),
            _f("location", "Location", "appointment.location"),
            _f("prep_notes", "Preparation notes", "appointment.prep_notes", required=False),
        ],
    },
    "care_outreach": {
        "name": "Care program outreach",
        "program": "Care outreach",
        "prefix": "OUT",
        "summary": "Invite a patient a clinician approved to a care program, and record their "
        "answer.",
        "counterparty": {
            "role": "patient",
            "label": "Patient",
            "name": "patient.display_name",
            "phone": "patient.phone",
        },
        "on_behalf_of": "practice.name",
        "subject": "patient.display_name",
        "headline": ("program.name", "practice.name"),
        "agents": ["errand"],
        "checks": [],
        "packs": [*PATIENT_CALL_PACKS, "clinical-scope"],
        "objective": "Reach {patient_name} and, once you are speaking with them, tell them that "
        "{practice_name} offers {program_name} and that {approved_by} thought it could help them. "
        "Explain what it involves using only the program description, answer only what it covers, "
        "and ask whether they would like to enrol or have the care team call them. Record their "
        "answer.",
        "fields": [
            _f("patient_name", "Patient", "patient.display_name"),
            _f("patient_phone", "Patient phone", "patient.phone", "phone", share=False),
            _f("date_of_birth", "Date of birth", "patient.date_of_birth", "date", share=False),
            _f("practice_name", "Practice", "practice.name"),
            _f("practice_phone", "Practice call-back number", "practice.phone", "phone"),
            _f("program_name", "Program", "program.name"),
            _f("program_description", "What the program involves", "program.description"),
            _f("approved_by", "Approved by", "program.approved_by"),
        ],
    },
}

KINDS = ("text", "date", "datetime", "phone", "code", "number", "money")


def get(type_id: str | None) -> dict[str, Any]:
    return CASE_TYPES.get(type_id or "prior_authorization") or CASE_TYPES["prior_authorization"]


def value(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _put(data: dict[str, Any], path: str, val: Any) -> None:
    *parents, leaf = path.split(".")
    node = data
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = val


def flatten(type_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
    """The case's facts by field key: what tools, the brief and the Studio read."""
    out: dict[str, Any] = {"case_ref": data.get("case_id")}
    for field in get(type_id)["fields"]:
        out[field["key"]] = value(data, field["path"])
    return out


def missing(type_id: str | None, data: dict[str, Any]) -> list[str]:
    return [
        f["key"]
        for f in get(type_id)["fields"]
        if f["required"] and value(data, f["path"]) in (None, "")
    ]


def objective(type_id: str | None, data: dict[str, Any]) -> str | None:
    text = get(type_id)["objective"]
    if not text:
        return None
    facts = {k: ("not given" if v in (None, "") else v) for k, v in flatten(type_id, data).items()}
    return text.format_map(facts)


def build(type_id: str, case_ref: str, values: dict[str, Any]) -> dict[str, Any]:
    """Case data from field values, shaped by the type's paths."""
    data: dict[str, Any] = {"case_id": case_ref, "synthetic": True, "case_type": type_id}
    for field in get(type_id)["fields"]:
        val = values.get(field["key"])
        if isinstance(val, str):
            val = val.strip()
        if val in (None, ""):
            val = None
        elif field["kind"] == "number":
            val = int(val)
        _put(data, field["path"], val)
    return data


def view() -> list[dict[str, Any]]:
    return [
        {
            "id": tid,
            "name": t["name"],
            "program": t["program"],
            "summary": t["summary"],
            "counterparty": t["counterparty"]["label"],
            "counterparty_role": t["counterparty"]["role"],
            "agents": list(t["agents"]),
            "checks": list(t["checks"]),
            "packs": list(t["packs"]),
            "objective": t["objective"],
            "fields": [
                {k: f[k] for k in ("key", "label", "kind", "required", "share")}
                for f in t["fields"]
            ],
        }
        for tid, t in CASE_TYPES.items()
    ]


# --- Synthetic samples ---------------------------------------------------------------------------
# Clearly fictional: "Synthetic" names, "(synthetic)" organisations, 555-01xx numbers, 91xxx NPIs.

_GREEK = "Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa Lambda Mu Nu Xi Omicron Pi Rho"
_PAYERS = (
    ("Northwind Health Plan (synthetic)", "+12025550150"),
    ("Harbor Mutual Health (synthetic)", "+12025550151"),
    ("Summit Care Alliance (synthetic)", "+12025550152"),
)
_PRACTICES = (
    ("Riverside Family Medicine (synthetic)", "+12025550160", "12 River Road, Suite 200"),
    ("Lakeview Internal Medicine (synthetic)", "+12025550161", "400 Lake Street"),
)
_CLINICIANS = ("Dr. Rowan Synthetic", "Dr. Casey Example", "Nurse Practitioner Avery Sample")
_SERVICES = (
    ("MRI lumbar spine without contrast", "72148"),
    ("Outpatient physical therapy evaluation", "97161"),
    ("Sleep study, attended", "95810"),
    ("Echocardiogram, complete", "93306"),
)
_PROGRAMS = (
    (
        "Chronic care management",
        "A nurse calls once a month to check in, help with medicines and appointments, and answer "
        "questions between visits. There may be a monthly copay depending on the plan.",
    ),
    (
        "Remote blood pressure monitoring",
        "The practice sends a connected blood pressure cuff. Readings go to the care team, who "
        "call if anything needs attention.",
    ),
)


def _phone(rng: random.Random) -> str:
    return f"+1202555{rng.randint(170, 199):04d}"


def sample_values(type_id: str, rng: random.Random, n: int) -> dict[str, Any]:
    name = f"Synthetic Patient {_GREEK.split()[n % 17]} {n:03d}"
    dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 55))
    payer, payer_phone = rng.choice(_PAYERS)
    practice, practice_phone, address = rng.choice(_PRACTICES)
    service, cpt = rng.choice(_SERVICES)
    soon = date.today() + timedelta(days=rng.randint(2, 9))
    base = {
        "patient_name": name,
        "date_of_birth": dob.isoformat(),
        "member_id": f"99{rng.randint(1000000, 9999999)}",
        "postal_code": f"{rng.randint(10001, 99950)}",
        "patient_phone": _phone(rng),
        "provider_name": practice,
        "provider_npi": f"91000{rng.randint(10000, 99999)}",
        "payer_name": payer,
        "payer_phone": payer_phone,
        "practice_name": practice,
        "practice_phone": practice_phone,
        "service_description": service,
        "cpt_code": cpt,
    }
    if type_id == "prior_authorization":
        base |= {
            "units": 1,
            "submitted_on": (date.today() - timedelta(days=rng.randint(5, 20))).isoformat(),
            "urgency": "standard",
        }
    elif type_id == "claim_status":
        base |= {
            "claim_number": f"CLM{rng.randint(10000000, 99999999)}",
            "date_of_service": (date.today() - timedelta(days=rng.randint(30, 75))).isoformat(),
            "billed_amount": f"${rng.randint(120, 2400)}.00",
        }
    elif type_id == "eligibility_check":
        base |= {"planned_on": soon.isoformat()}
    elif type_id == "appointment_reminder":
        base |= {
            "clinician_name": rng.choice(_CLINICIANS),
            "appointment_at": f"{soon.isoformat()} {rng.choice(['09:00', '10:30', '14:15'])}",
            "location": address,
            "prep_notes": rng.choice(
                [None, "Bring your medication list and insurance card.", "Arrive 15 minutes early."]
            ),
        }
    elif type_id == "care_outreach":
        program, description = rng.choice(_PROGRAMS)
        base |= {
            "program_name": program,
            "program_description": description,
            "approved_by": rng.choice(_CLINICIANS),
        }
    return base


def is_synthetic_text(values: dict[str, Any]) -> bool:
    """A loose check that hand-entered people and organisations are marked as made up."""
    name = str(values.get("patient_name") or "")
    return bool(re.search(r"synthetic|sample|example|test", name, re.IGNORECASE))
