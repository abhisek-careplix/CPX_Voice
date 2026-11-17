"""The chart-prep brief a clinician reads before a visit.

Assembled, never interpreted: problems and medicines from the imported clinical facts, open care
gaps from care opportunities, outside records received (and hospital discharges), recent and next
visits from the mirror, and the questionnaire answers in reading order.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.clinical import answers, protocol
from careplix_api.models import Case
from careplix_api.opportunities import rules
from careplix_api.opportunities.models import CareOpportunity, ClinicalFact
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

OPEN_GAPS = ("proposed", "approved", "contacted")


async def build(session: AsyncSession, settings: Settings, case: Case) -> dict[str, Any]:
    ref = protocol.patient_ref(case)
    box = PhiBox(settings)
    problems: list[dict[str, Any]] = []
    medicines: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    if ref:
        for fact in await session.scalars(
            select(ClinicalFact).where(
                ClinicalFact.workspace_id == case.workspace_id, ClinicalFact.patient_ref == ref
            )
        ):
            data = box.open(fact.payload)
            if fact.kind == "condition":
                problems.append({"code": data.get("code"), "since": data.get("date")})
            elif fact.kind == "medication":
                medicines.append(
                    {"name": data.get("name"), "dose": data.get("dose"), "since": data.get("date")}
                )
            elif fact.kind == "record":
                records.append(
                    {
                        "kind": "outside_record",
                        "source": data.get("source"),
                        "date": data.get("date"),
                        "summary": data.get("summary"),
                    }
                )
            elif fact.kind == "discharge":
                records.append(
                    {
                        "kind": "discharge",
                        "source": data.get("facility"),
                        "date": data.get("date"),
                        "summary": "Hospital discharge",
                    }
                )
    gaps = []
    if ref:
        for row in await session.scalars(
            select(CareOpportunity).where(
                CareOpportunity.workspace_id == case.workspace_id,
                CareOpportunity.patient_ref == ref,
                CareOpportunity.status.in_(OPEN_GAPS),
            )
        ):
            program = rules.PROGRAMS.get(row.program)
            gaps.append(
                {
                    "program": program.title if program else row.program,
                    "status": row.status,
                    "due_by": row.due_by.isoformat() if row.due_by else None,
                }
            )
    last, nxt = await protocol.visits(session, case)
    lines = answers.summary(case)
    return {
        "linked": bool(ref),
        "problems": sorted(problems, key=lambda p: p.get("since") or "", reverse=True),
        "medicines": medicines,
        "care_gaps": gaps,
        "outside_records": sorted(records, key=lambda r: r.get("date") or "", reverse=True),
        "last_visit": last.date().isoformat() if last else None,
        "next_visit": nxt.date().isoformat() if nxt else None,
        "questionnaire": lines,
        "attention": [line["label"] for line in lines if line["level"] != "info"],
    }
