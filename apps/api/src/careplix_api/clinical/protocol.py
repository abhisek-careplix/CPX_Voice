"""Refill protocol facts from the EHR mirror. Facts, never a decision.

The clinician decides; this lays out what a refill protocol usually looks at, read from the
practice mirror (appointments) and imported clinical facts (the medicine list), each with where
it came from. A fact that doesn't meet the protocol's usual expectation is marked so the
clinician sees it first; the agent never approves, denies or recommends.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.models import Case, EhrRecord
from careplix_api.opportunities.models import ClinicalFact
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

MAX_MONTHS_SINCE_VISIT = 12
# Schedule II–IV names a refill protocol never routes as routine (illustrative, synthetic labs).
CONTROLLED = (
    "oxycodone",
    "hydrocodone",
    "morphine",
    "fentanyl",
    "tramadol",
    "codeine",
    "alprazolam",
    "lorazepam",
    "clonazepam",
    "diazepam",
    "zolpidem",
    "amphetamine",
    "adderall",
    "methylphenidate",
    "buprenorphine",
)
VISIT_DONE = ("completed", "checked_out", "fulfilled", "booked", "confirmed", "arrived")


def patient_ref(case: Case) -> str | None:
    data = case.data or {}
    return data.get("patient_ref") or (data.get("ehr") or {}).get("patient_id")


def _months(a: datetime, b: datetime) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


async def visits(session: AsyncSession, case: Case) -> tuple[datetime | None, datetime | None]:
    """The patient's last visit and next booked one in the mirror."""
    ref = patient_ref(case)
    if not ref:
        return None, None
    now = utcnow()
    last = nxt = None
    for appt in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.workspace_id == case.workspace_id,
            EhrRecord.resource == "appointment",
            EhrRecord.patient_ref == ref,
        )
    ):
        if appt.starts_at is None or (appt.status or "").lower() == "cancelled":
            continue
        at = as_utc(appt.starts_at)
        if at < now and (last is None or at > last):
            last = at
        elif at >= now and (nxt is None or at < nxt):
            nxt = at
    return last, nxt


async def medicines(
    session: AsyncSession, settings: Settings, workspace_id: str, ref: str | None
) -> list[dict[str, Any]]:
    if not ref:
        return []
    box = PhiBox(settings)
    return [
        box.open(f.payload)
        for f in await session.scalars(
            select(ClinicalFact).where(
                ClinicalFact.workspace_id == workspace_id,
                ClinicalFact.patient_ref == ref,
                ClinicalFact.kind == "medication",
            )
        )
    ]


def _matches(name: str, wanted: str) -> bool:
    a, b = name.lower().split(), wanted.lower().split()
    return bool(a and b and a[0] == b[0])


async def facts(session: AsyncSession, settings: Settings, case: Case) -> list[dict[str, Any]]:
    data = case.data or {}
    refill = data.get("refill") or {}
    wanted = str(refill.get("medication") or "")
    ref = patient_ref(case)
    out: list[dict[str, Any]] = []

    def fact(key: str, label: str, value: str, meets: bool | None, source: str) -> None:
        out.append({"key": key, "label": label, "value": value, "meets": meets, "source": source})

    if not ref:
        fact(
            "chart_link",
            "Chart",
            "No EHR record is linked to this request; check the chart directly",
            None,
            "case",
        )
    last, nxt = await visits(session, case)
    if last is None and refill.get("last_visit"):
        try:
            last = as_utc(datetime.fromisoformat(str(refill["last_visit"])))
        except ValueError:
            last = None
    if last is not None:
        months = _months(last, utcnow())
        fact(
            "last_visit",
            "Last visit",
            f"{last.date().isoformat()} ({months} months ago)",
            months <= MAX_MONTHS_SINCE_VISIT,
            "ehr" if ref else "case",
        )
    else:
        fact("last_visit", "Last visit", "None found in the mirror", False, "ehr")
    fact(
        "next_visit",
        "Next booked visit",
        nxt.date().isoformat() if nxt else "None booked",
        None,
        "ehr",
    )
    meds = await medicines(session, settings, case.workspace_id, ref)
    on_list = next((m for m in meds if wanted and _matches(str(m.get("name") or ""), wanted)), None)
    if on_list:
        fact(
            "on_medicine_list",
            "On the medicine list",
            " · ".join(
                x
                for x in (
                    str(on_list.get("name") or ""),
                    str(on_list.get("dose") or ""),
                    f"since {on_list['date']}" if on_list.get("date") else "",
                )
                if x
            ),
            True,
            "ehr",
        )
        if on_list.get("last_filled"):
            fact("last_filled", "Last filled", str(on_list["last_filled"]), None, "ehr")
    elif ref:
        fact(
            "on_medicine_list",
            "On the medicine list",
            "Not found on the medicine list in the mirror",
            False,
            "ehr",
        )
    controlled = any(c in wanted.lower() for c in CONTROLLED)
    fact(
        "controlled",
        "Controlled substance",
        "Yes: the protocol routes it to the prescriber directly" if controlled else "No",
        not controlled,
        "protocol",
    )
    return out


def today() -> date:
    return utcnow().date()
