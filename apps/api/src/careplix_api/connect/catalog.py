"""The capability catalog: every EHR profile CarePlix can reach and what each one supports.

Sources: NexHealth "Supported health record systems" and "Testing writes, per system"
(read 1 Nov 2026; "Create patient" and "Edit appointment" rechecked 11 Nov 2026); OpenEMR 7.0.3
verified live with the CarePlix Connector module. The table in
docs/solution/NEXHEALTH_EHR_MATRIX.md is the human-readable copy of this data.

The hub enforces this catalog: an operation a profile does not support never leaves the hub; the
write becomes a staff task with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Support = Literal["yes", "no", "verify", "existing_patients"]
Confirm = Literal["immediate", "async", "async_office_hours"]

# Operations, in the order the Studio shows them.
OPERATIONS: dict[str, str] = {
    "schedule.read": "Read schedule, patients and coverage",
    "eligibility.check": "Insurance check (Stedi)",
    "appointment.book": "Book appointments",
    "patient.create": "Add new patients",
    "appointment.cancel": "Cancel appointments",
    "appointment.confirm": "Confirm appointments",
    "appointment.check_in": "Check in",
    "appointment.reschedule": "Change time or notes",
    "chart.alert": "Chart alert or message",
    "document.upload": "Upload documents",
    "payment.post": "Post payments and adjustments",
}

# Write-intent kinds → catalog operation.
INTENT_OPERATION = {
    "patient_alert": "chart.alert",
    "appointment_book": "appointment.book",
    "appointment_cancel": "appointment.cancel",
    "appointment_confirm": "appointment.confirm",
    "appointment_check_in": "appointment.check_in",
    "appointment_reschedule": "appointment.reschedule",
    "patient_create": "patient.create",
}


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    path: Literal["direct", "nexhealth"]
    category: Literal["medical", "dental", "ortho", "general"]
    deployment: Literal["cloud", "server", "self-hosted"]
    connects_by: str
    ops: dict[str, Support]
    confirm: Confirm
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Fields a new patient must have for this EHR to create the chart (beyond NexHealth's own).
    new_patient_fields: tuple[str, ...] = field(default_factory=tuple)

    def support(self, operation: str) -> Support:
        return self.ops.get(operation, "no")

    def view(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "category": self.category,
            "deployment": self.deployment,
            "connects_by": self.connects_by,
            "confirm": self.confirm,
            "ops": {op: self.support(op) for op in OPERATIONS},
            "notes": list(self.notes),
            "new_patient_fields": list(self.new_patient_fields),
        }


def _nexhealth(
    key: str,
    name: str,
    category: Literal["medical", "dental", "ortho"],
    deployment: Literal["cloud", "server"],
    writes: str,
    *,
    existing_patients: bool = False,
    documents: Support = "no",
    notes: tuple[str, ...] = (),
    new_patient_fields: tuple[str, ...] = (),
) -> Profile:
    """``writes`` letters: R cancel, K check-in, T time/note change, D documents, P payments."""
    connects_by = {
        "cloud": "Cloud: dedicated EHR user for NexHealth, plus the Chrome extension at the desk",
        "server": "Server: NexHealth Synchronizer on the practice's server",
    }[deployment]
    if key in {"dentrix_enterprise", "dolphin", "orthotrac", "practiceworks"}:
        connects_by = "Server: NexHealth's team installs the Synchronizer"
    elif key in {"dentrix", "eaglesoft", "open_dental"}:
        connects_by = "Server: the practice runs the Synchronizer with a product key"
    ops: dict[str, Support] = {
        "schedule.read": "yes",
        "eligibility.check": "yes",
        "appointment.book": "existing_patients" if existing_patients else "yes",
        # POST /patients lives in NexHealth until the first booking carries it into the EHR;
        # Curve Hero, Dolphin and PracticeWorks can't take a new patient that way.
        "patient.create": "no" if existing_patients else "yes",
        "appointment.cancel": "yes" if "R" in writes else "no",
        "appointment.confirm": "yes",
        "appointment.check_in": "yes" if "K" in writes else "no",
        "appointment.reschedule": "yes" if "T" in writes else "no",
        # NexHealth publishes no per-system table for patient alerts.
        "chart.alert": "verify",
        "document.upload": "yes" if "D" in writes else documents,
        "payment.post": "yes" if "P" in writes else "no",
    }
    cloud_note = (
        ("Syncs only while someone at the office is signed in to the EHR and Chrome extension.",)
        if deployment == "cloud"
        else ()
    )
    return Profile(
        id=f"nexhealth:{key}",
        name=name,
        path="nexhealth",
        category=category,
        deployment=deployment,
        connects_by=connects_by,
        ops=ops,
        confirm="async_office_hours" if deployment == "cloud" else "async",
        notes=cloud_note + notes,
        new_patient_fields=new_patient_fields,
    )


PROFILES: dict[str, Profile] = {
    p.id: p
    for p in (
        Profile(
            id="openemr",
            name="OpenEMR",
            path="direct",
            category="general",
            deployment="self-hosted",
            connects_by="Direct: OAuth API client + CarePlix Connector module",
            ops={
                "schedule.read": "yes",
                "eligibility.check": "yes",
                "appointment.book": "yes",
                "patient.create": "no",
                "appointment.cancel": "yes",
                "appointment.confirm": "yes",
                "appointment.check_in": "yes",
                "appointment.reschedule": "no",
                "chart.alert": "yes",
                "document.upload": "verify",
                "payment.post": "no",
            },
            confirm="immediate",
            notes=(
                "Cancel, confirm and check-in use the CarePlix Connector module's status update; "
                "without it, cancel deletes the visit.",
            ),
        ),
        _nexhealth("athenahealth", "athenahealth", "medical", "cloud", "R", documents="verify"),
        _nexhealth(
            "eclinicalworks",
            "eClinicalWorks",
            "medical",
            "cloud",
            "R",
            documents="verify",
            notes=("v11.53+ cloud only; on-premises eCW is not supported.",),
        ),
        _nexhealth("modmed", "ModMed", "medical", "cloud", "RK"),
        _nexhealth("nextgen_office", "NextGen Office", "medical", "cloud", "RK"),
        _nexhealth(
            "dentrix",
            "Dentrix",
            "dental",
            "server",
            "RKTDP",
            notes=("G6.2+.", "New patients need a gender to create the chart."),
            new_patient_fields=("gender",),
        ),
        _nexhealth("dentrix_enterprise", "Dentrix Enterprise", "dental", "server", "RKTDP"),
        _nexhealth("dentrix_ascend", "Dentrix Ascend", "dental", "cloud", "RD"),
        _nexhealth(
            "eaglesoft",
            "Eaglesoft",
            "dental",
            "server",
            "RKTDP",
            notes=("18+.", "New patients need a gender to create the chart."),
            new_patient_fields=("gender",),
        ),
        _nexhealth("open_dental", "Open Dental", "dental", "server", "RKTDP", notes=("V17+.",)),
        _nexhealth("denticon", "Denticon", "dental", "cloud", "RKTD"),
        _nexhealth("curve_hero", "Curve Hero", "dental", "cloud", "RD", existing_patients=True),
        _nexhealth(
            "practiceworks", "PracticeWorks", "dental", "server", "", existing_patients=True
        ),
        _nexhealth("cloud9", "Cloud9", "ortho", "cloud", "R"),
        _nexhealth("dolphin", "Dolphin", "ortho", "server", "R", existing_patients=True),
        _nexhealth("orthotrac", "OrthoTrac", "ortho", "server", "", documents="verify"),
    )
}

# NexHealth reports EHRs by display name; match the most specific name first.
_NAMES = sorted(
    [(p.name.lower(), p.id) for p in PROFILES.values() if p.path == "nexhealth"]
    + [
        ("modernizing medicine", "nexhealth:modmed"),
        ("nextgen", "nexhealth:nextgen_office"),
        ("opendental", "nexhealth:open_dental"),
        ("curve", "nexhealth:curve_hero"),
        ("eclinical", "nexhealth:eclinicalworks"),
        ("athena", "nexhealth:athenahealth"),
    ],
    key=lambda item: -len(item[0]),
)


def nexhealth_profile_for(emr_name: str | None) -> Profile | None:
    low = (emr_name or "").lower()
    return next((PROFILES[pid] for name, pid in _NAMES if name in low), None)


def support(profile: Profile | None, operation: str) -> Support:
    """Unknown profiles (e.g. the NexHealth sandbox with no sync) are 'verify' for every write."""
    if profile is None:
        return "yes" if operation in ("schedule.read", "eligibility.check") else "verify"
    return profile.support(operation)
