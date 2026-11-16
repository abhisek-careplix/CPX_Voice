"""A synthetic sandbox home care agency: caregivers, clients, families and staff.

Everyone is made up: "Synthetic", "Sample", "Example" or "Test" names, 555-01xx phone numbers,
coordinates scattered around a fictional service area on Chicago's North Side. Credential and
authorisation dates are relative to today so the sandbox stays meaningful: one caregiver's CPR
has lapsed, one licence is about to, one family authorisation has expired and one was revoked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.people.models import Credential, Observation, Person, Relationship
from careplix_api.phi import PhiBox

AGENCY = "Sunrise Home Care (synthetic)"
MARK = "sample-agency"  # created_by on every sample row: how a second run knows to skip
WEEKDAYS = [0, 1, 2, 3, 4]
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]


def _w(days: list[int], start: str, end: str) -> dict[str, Any]:
    return {"days": days, "start": start, "end": end}


# key, first, last, dob, phone, zip, lat, lon, extras
CAREGIVERS: list[dict[str, Any]] = [
    {
        "key": "ana",
        "pii": ("Ana", "Synthetic-Reyes", "1988-03-14", "+13125550111", "60640", 41.9731, -87.6624),
        "skills": ["personal_care", "dementia_care", "medication_reminders", "spanish"],
        "language": "es",
        "preferred_channel": "sms",
        "availability": [_w(WEEKDAYS, "07:00", "19:00")],
        "reliability": {"offered": 40, "accepted": 34, "completed": 33, "no_shows": 0, "late": 1},
        "credentials": [("hha_certification", 400), ("cpr", 200), ("tb_test", 120)],
        "max_travel_km": 25,
    },
    {
        "key": "ben",
        "pii": ("Ben", "Sample-Okafor", "1979-07-02", "+13125550112", "60613", 41.9543, -87.6566),
        "skills": ["personal_care", "hoyer_lift", "meal_prep"],
        "preferred_channel": "sms",
        "availability": [_w(EVERY_DAY, "06:00", "22:00")],
        "reliability": {"offered": 30, "accepted": 18, "completed": 16, "no_shows": 2, "late": 3},
        "credentials": [("cna_certification", 300), ("cpr", 90), ("tb_test", 200)],
        "max_travel_km": 30,
    },
    {
        "key": "chloe",
        "pii": (
            "Chloe",
            "Example-Nguyen",
            "1992-11-20",
            "+13125550113",
            "60657",
            41.9399,
            -87.6528,
        ),
        "skills": ["skilled_nursing", "wound_care", "medication_reminders"],
        "preferred_channel": "voice",
        "availability": [_w([0, 2, 4], "08:00", "16:00")],
        "reliability": {"offered": 12, "accepted": 11, "completed": 11, "no_shows": 0, "late": 0},
        "credentials": [("rn_license", 20), ("cpr", 300), ("tb_test", 300)],  # licence due soon
        "max_travel_km": 35,
    },
    {
        "key": "dev",
        "pii": ("Dev", "Test-Patel", "1985-01-30", "+13125550114", "60625", 41.9710, -87.7010),
        "skills": ["personal_care", "companionship", "meal_prep", "dementia_care"],
        "preferred_channel": "sms",
        "availability": [_w(WEEKDAYS, "09:00", "17:00")],
        "reliability": {"offered": 20, "accepted": 15, "completed": 15, "no_shows": 1, "late": 0},
        "credentials": [("hha_certification", 200), ("cpr", -10), ("tb_test", 100)],  # CPR lapsed
        "max_travel_km": 20,
    },
    {
        "key": "eve",
        "pii": (
            "Eve",
            "Synthetic-Kowalski",
            "1990-05-09",
            "+13125550115",
            "60618",
            41.9466,
            -87.7036,
        ),
        "skills": ["companionship", "meal_prep", "personal_care"],
        "preferred_channel": "voice",
        "availability": [_w([5, 6], "08:00", "20:00"), _w(WEEKDAYS, "16:00", "21:00")],
        "reliability": {},  # new hire
        "credentials": [("hha_certification", 700), ("cpr", 700), ("tb_test", 330)],
        "max_travel_km": 15,
    },
    {
        "key": "finn",
        "pii": ("Finn", "Sample-Murphy", "1983-09-17", "+13125550116", "60202", 42.0300, -87.6900),
        "skills": ["personal_care", "hoyer_lift", "dementia_care"],
        "preferred_channel": "sms",
        "availability": [_w(EVERY_DAY, "07:00", "19:00")],
        "reliability": {"offered": 25, "accepted": 22, "completed": 21, "no_shows": 0, "late": 2},
        "credentials": [("hha_certification", 150), ("cpr", 150), ("tb_test", 150)],
        "max_travel_km": 10,  # Evanston: rarely travels south
        "do_not_contact": False,
    },
    {
        "key": "gia",
        "pii": ("Gia", "Example-Rossi", "1995-02-25", "+13125550117", "60614", 41.9227, -87.6533),
        "skills": ["personal_care", "companionship"],
        "preferred_channel": "sms",
        "availability": [_w(WEEKDAYS, "07:00", "19:00")],
        "reliability": {"offered": 8, "accepted": 6, "completed": 6, "no_shows": 0, "late": 0},
        "credentials": [("hha_certification", 365), ("cpr", 365), ("tb_test", 365)],
        "max_travel_km": 25,
        "do_not_contact": True,
        "do_not_contact_reason": "On leave until further notice",
    },
]

CLIENTS: list[dict[str, Any]] = [
    {
        "key": "harold",
        "kind": "client",
        "pii": (
            "Harold",
            "Synthetic-Brooks",
            "1938-04-11",
            "+13125550121",
            "60640",
            41.9688,
            -87.6595,
        ),
        "language": "en",
        "availability": [_w(WEEKDAYS, "08:00", "18:00")],
        "contact_windows": [_w(EVERY_DAY, "09:00", "19:00")],
        "notes": [("preference", "Prefers morning visits and a call 30 minutes before arrival.")],
    },
    {
        "key": "rosa",
        "kind": "client",
        "pii": ("Rosa", "Sample-Delgado", "1944-12-03", "+13125550122", "60625", 41.9689, -87.6943),
        "language": "es",
        "availability": [_w(EVERY_DAY, "07:00", "20:00")],
        "contact_windows": [_w(EVERY_DAY, "10:00", "18:00")],
        "notes": [("preference", "Speaks Spanish at home; daughter Lucia handles scheduling.")],
    },
    {
        "key": "walter",
        "kind": "client",
        "pii": ("Walter", "Test-Hughes", "1941-08-28", "+13125550123", "60657", 41.9411, -87.6489),
        "availability": [_w(WEEKDAYS, "08:00", "17:00")],
        "notes": [("care_plan", "Hoyer lift transfers; two-person assist on Mondays.")],
    },
    {
        "key": "june",
        "kind": "patient",
        "pii": ("June", "Example-Park", "1952-06-19", "+13125550124", "60613", 41.9530, -87.6600),
        "availability": [_w(WEEKDAYS, "09:00", "12:00")],
        "notes": [("preference", "Happy to take an earlier appointment at short notice.")],
    },
]

FAMILY: list[dict[str, Any]] = [
    # key, pii, subject, relation, scope, starts (days from today), expires, revoked, evidence
    {
        "key": "lucia",
        "pii": ("Lucia", "Sample-Delgado", "1970-02-14", "+13125550131", "60625", None, None),
        "language": "es",
        "preferred_channel": "sms",
        "subject": "rosa",
        "relation": "child",
        "scope": ["general", "schedule", "billing"],
        "expires": 365,
        "evidence": "Signed representative form on file (synthetic)",
    },
    {
        "key": "martin",
        "pii": ("Martin", "Synthetic-Brooks", "1965-10-01", "+13125550132", "60640", None, None),
        "subject": "harold",
        "relation": "child",
        "scope": ["general", "schedule"],
        "expires": -5,  # lapsed last week
        "evidence": "Representative form expired (synthetic)",
    },
    {
        "key": "nora",
        "pii": ("Nora", "Test-Hughes", "1943-03-30", "+13125550133", "60657", None, None),
        "subject": "walter",
        "relation": "spouse",
        "scope": ["all"],
        "expires": None,
        "revoked": True,
        "evidence": "Power of attorney (synthetic), revoked by the client",
    },
]

OTHERS: list[dict[str, Any]] = [
    {
        "key": "sam",
        "kind": "staff",
        "pii": ("Sam", "Sample-Scheduler", None, "+13125550141", None, None, None),
        "role_title": "Scheduling coordinator",
        "organisation": AGENCY,
    },
    {
        "key": "pat",
        "kind": "payer_contact",
        "pii": ("Pat", "Example-Liaison", None, "+13125550151", None, None, None),
        "role_title": "Provider relations",
        "organisation": "Lakeshore Medicaid Plan (synthetic)",
    },
]

FIT = [("harold", "ana", "preferred_caregiver"), ("rosa", "ana", "preferred_caregiver")]
FIT += [("walter", "ben", "declined_caregiver")]


def _pii(values: tuple) -> dict[str, Any]:
    first, last, dob, phone, zip_code, lat, lon = values
    return {
        "first_name": first,
        "last_name": last,
        "date_of_birth": dob,
        "phone": phone,
        "zip": zip_code,
        "lat": lat,
        "lon": lon,
    }


async def seed_agency(session: AsyncSession, workspace_id: str, box: PhiBox) -> int:
    """Add the sandbox agency to a workspace once. Returns how many people were added."""
    existing = await session.scalar(
        select(func.count())
        .select_from(Person)
        .where(Person.workspace_id == workspace_id, Person.created_by == MARK)
    )
    if existing:
        return 0
    today = utcnow().date()

    def add(spec: dict[str, Any], kind: str) -> Person:
        person = Person(
            workspace_id=workspace_id,
            kind=kind,
            pii=box.seal(_pii(spec["pii"])),
            organisation=spec.get("organisation", AGENCY if kind == "caregiver" else None),
            role_title=spec.get("role_title", "Caregiver" if kind == "caregiver" else None),
            language=spec.get("language", "en"),
            preferred_channel=spec.get("preferred_channel", "voice"),
            do_not_contact=spec.get("do_not_contact", False),
            do_not_contact_reason=spec.get("do_not_contact_reason"),
            contact_windows=spec.get("contact_windows", [_w(EVERY_DAY, "08:00", "20:00")]),
            consent={
                "voice": {"state": "granted", "source": "sample"},
                "sms": {
                    "state": "granted" if spec.get("preferred_channel") == "sms" else "unknown",
                    "source": "sample",
                },
            },
            skills=spec.get("skills", []),
            availability=spec.get("availability", []),
            max_travel_km=spec.get("max_travel_km"),
            reliability=spec.get("reliability", {}),
            created_by=MARK,
        )
        session.add(person)
        return person

    people = [(s, "caregiver") for s in CAREGIVERS]
    people += [(s, s["kind"]) for s in CLIENTS + OTHERS]
    people += [(s, "family") for s in FAMILY]
    made = {s["key"]: (s, add(s, kind)) for s, kind in people}
    await session.flush()
    ids = {key: person.id for key, (_, person) in made.items()}

    for spec in CAREGIVERS:
        for kind, days in spec["credentials"]:
            session.add(
                Credential(
                    workspace_id=workspace_id,
                    person_id=ids[spec["key"]],
                    kind=kind,
                    identifier=f"SYN-{kind[:3].upper()}-{spec['key'].upper()}",
                    issued_on=today + timedelta(days=days - 365),
                    expires_on=today + timedelta(days=days),
                    verified_by=MARK,
                )
            )
    for spec in CLIENTS:
        for topic, text in spec.get("notes", []):
            session.add(
                Observation(
                    workspace_id=workspace_id,
                    person_id=ids[spec["key"]],
                    topic=topic,
                    text=box.seal({"text": text}),
                    visibility="agent",
                    source="sample",
                    confirmed=True,
                    created_by=MARK,
                )
            )
    for spec in FAMILY:
        expires = spec.get("expires")
        session.add(
            Relationship(
                workspace_id=workspace_id,
                subject_id=ids[spec["subject"]],
                related_id=ids[spec["key"]],
                relation=spec["relation"],
                proxy=True,
                scope=spec["scope"],
                starts_on=today - timedelta(days=400),
                expires_on=today + timedelta(days=expires) if expires is not None else None,
                revoked_at=utcnow() - timedelta(days=30) if spec.get("revoked") else None,
                evidence=spec["evidence"],
                created_by=MARK,
            )
        )
    for client, caregiver, relation in FIT:
        session.add(
            Relationship(
                workspace_id=workspace_id,
                subject_id=ids[client],
                related_id=ids[caregiver],
                relation=relation,
                created_by=MARK,
            )
        )
    await session.flush()
    return len(ids)
