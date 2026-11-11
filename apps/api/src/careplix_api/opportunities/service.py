"""Care opportunities: import clinical facts, scan them with the rules, and run the approval queue.

Facts arrive from a CSV export of any EHR (or a synthetic sample for demos); a later sync can write
the same rows from OpenEMR (FHIR Condition/Encounter/Procedure). A scan evaluates every patient and
keeps one open opportunity per patient and program. A clinician approves or declines each one
before outreach; nothing here books, bills or writes to the chart.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent
from careplix_api.opportunities import rules
from careplix_api.opportunities.models import CareOpportunity, ClinicalFact
from careplix_api.phi import PhiBox, initials

KINDS = ("patient", "condition", "service", "discharge", "screening")
OPEN = ("proposed", "approved", "contacted")
SETTLED = ("declined", "enrolled", "not_interested", "done")
QUIET_DAYS = 180
DECISIONS = {"approve": "approved", "decline": "declined"}
OUTCOMES = ("contacted", "enrolled", "not_interested", "done")
MAX_ROWS = 20_000
MAX_CSV_CHARS = 2_000_000
COLUMNS = ("patient_ref", "kind", "code", "date", "value", "name", "birth_date", "sex", "coverage")


# --- facts ----------------------------------------------------------------------------------------


def _age(birth: date | None, today: date) -> int | None:
    if birth is None:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _row_payload(row: dict[str, str]) -> dict[str, Any]:
    kind = row["kind"]
    if kind == "patient":
        return {
            "name": row.get("name", "")[:120],
            "birth_date": row.get("birth_date", "")[:10],
            "sex": (row.get("sex", "")[:1] or "").upper() or None,
            "coverage": (row.get("coverage", "") or "").lower().replace(" ", "_") or None,
        }
    if kind == "screening":
        return {
            "kind": row.get("code", "").lower(),
            "score": row.get("value", ""),
            "date": row.get("date", ""),
        }
    if kind == "discharge":
        return {"date": row.get("date", ""), "facility": row.get("value", "")[:120]}
    return {"code": row.get("code", "").upper().strip(), "date": row.get("date", "")}


async def import_csv(
    session: AsyncSession, principal: Principal, box: PhiBox, text: str, *, source: str = "import"
) -> dict[str, int]:
    """Rows: patient_ref, kind, then code/date/value (facts) or name/birth_date/sex/coverage
    (patient). Importing a patient again replaces that patient's facts."""
    if len(text) > MAX_CSV_CHARS:
        raise ApiError(413, "csv_too_large", "Keep the file under 2 MB; split larger exports.")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not {"patient_ref", "kind"} <= {
        f.strip() for f in reader.fieldnames
    }:
        raise ApiError(422, "bad_columns", f"The file needs these columns: {', '.join(COLUMNS)}.")
    rows = []
    for n, raw in enumerate(reader, 2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        row["kind"] = row.get("kind", "").lower()
        if row["kind"] not in KINDS:
            raise ApiError(422, "bad_kind", f"Line {n}: kind must be one of {', '.join(KINDS)}.")
        if not row.get("patient_ref"):
            raise ApiError(422, "no_patient", f"Line {n} has no patient_ref.")
        rows.append(row)
    if len(rows) > MAX_ROWS:
        raise ApiError(422, "too_many_rows", f"Import at most {MAX_ROWS} rows at a time.")
    patients = {r["patient_ref"][:60] for r in rows}
    old = await session.scalars(
        select(ClinicalFact).where(
            ClinicalFact.workspace_id == principal.workspace_id,
            ClinicalFact.patient_ref.in_(patients),
        )
    )
    for fact in old:
        await session.delete(fact)
    for row in rows:
        session.add(
            ClinicalFact(
                workspace_id=principal.workspace_id,
                patient_ref=row["patient_ref"][:60],
                kind=row["kind"],
                payload=box.seal(_row_payload(row)),
                source=source,
            )
        )
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="care.facts_imported",
            target=principal.workspace_id,
            detail={"rows": len(rows), "patients": len(patients), "source": source},
        )
    )
    await session.flush()
    return {"rows": len(rows), "patients": len(patients)}


def sample_csv(today: date) -> str:
    """Synthetic patients covering every program, dated relative to today (never real people)."""

    def ago(days: int) -> str:
        return (today - timedelta(days=days)).isoformat()

    def born(years: int) -> str:
        return date(today.year - years, 3, 14).isoformat()

    rows = [
        ("SYN-P001", "patient", "", "", "", "Maria Synthetic", born(72), "F", "medicare"),
        ("SYN-P001", "condition", "E11.9", ago(900), "", "", "", "", ""),
        ("SYN-P001", "condition", "I10", ago(1500), "", "", "", "", ""),
        ("SYN-P001", "condition", "N18.3", ago(400), "", "", "", "", ""),
        ("SYN-P001", "service", "83036", ago(260), "", "", "", "", ""),
        ("SYN-P001", "service", "G0439", ago(400), "", "", "", "", ""),
        ("SYN-P002", "patient", "", "", "", "James Synthetic", born(78), "M", "medicare_advantage"),
        ("SYN-P002", "condition", "I50.22", ago(700), "", "", "", "", ""),
        ("SYN-P002", "discharge", "", ago(1), "Lakeside General", "", "", "", ""),
        ("SYN-P002", "service", "99490", ago(200), "", "", "", "", ""),
        ("SYN-P003", "patient", "", "", "", "Ana Synthetic", born(67), "F", "medicare"),
        ("SYN-P003", "condition", "F33.1", ago(300), "", "", "", "", ""),
        ("SYN-P003", "screening", "phq9", ago(30), "14", "", "", "", ""),
        ("SYN-P003", "service", "77067", ago(1000), "", "", "", "", ""),
        ("SYN-P004", "patient", "", "", "", "Robert Synthetic", born(58), "M", "commercial"),
        ("SYN-P004", "condition", "F17.210", ago(2000), "", "", "", "", ""),
        ("SYN-P004", "condition", "E11.65", ago(500), "", "", "", "", ""),
        ("SYN-P005", "patient", "", "", "", "Linda Synthetic", born(69), "F", "medicare"),
        ("SYN-P005", "condition", "J44.9", ago(1200), "", "", "", "", ""),
        ("SYN-P005", "service", "G0439", ago(90), "", "", "", "", ""),
        ("SYN-P005", "service", "G0444", ago(90), "", "", "", "", ""),
        ("SYN-P005", "service", "G0136", ago(90), "", "", "", "", ""),
        ("SYN-P005", "service", "99497", ago(90), "", "", "", "", ""),
        ("SYN-P005", "service", "45378", ago(1500), "", "", "", "", ""),
        ("SYN-P005", "service", "77067", ago(200), "", "", "", "", ""),
    ]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return out.getvalue()


async def patient_facts(
    session: AsyncSession, workspace_id: str, box: PhiBox, today: date
) -> dict[str, tuple[rules.Facts, dict[str, Any]]]:
    """Every patient's facts for the rules, plus their (sealed-at-rest) demographics."""
    out: dict[str, tuple[rules.Facts, dict[str, Any]]] = {}
    for fact in await session.scalars(
        select(ClinicalFact).where(ClinicalFact.workspace_id == workspace_id)
    ):
        facts, who = out.setdefault(fact.patient_ref, (rules.Facts(fact.patient_ref), {}))
        data = box.open(fact.payload)
        if fact.kind == "patient":
            who.update(data)
            facts.age = _age(rules._d(data.get("birth_date")), today)
            facts.sex = data.get("sex")
            facts.coverage = data.get("coverage")
        elif fact.kind == "condition":
            facts.conditions.append(data)
        elif fact.kind == "service":
            facts.services.append(data)
        elif fact.kind == "discharge":
            facts.discharges.append(data)
        elif fact.kind == "screening":
            facts.screenings.append(data)
    return out


# --- scanning -------------------------------------------------------------------------------------


async def scan(
    session: AsyncSession, principal: Principal, box: PhiBox, today: date
) -> dict[str, int]:
    """Re-run the rules for every patient. New findings become proposals; open proposals the
    rules no longer find become ``resolved``; decided ones are never reopened."""
    patients = await patient_facts(session, principal.workspace_id, box, today)
    existing = list(
        await session.scalars(
            select(CareOpportunity).where(CareOpportunity.workspace_id == principal.workspace_id)
        )
    )
    open_by_key = {(o.patient_ref, o.program): o for o in existing if o.status in OPEN}
    # A decision or an outcome holds for QUIET_DAYS: a declined or enrolled patient isn't asked
    # again until then (by which time the record shows the service, or the rules find it anew).
    quiet_since = utcnow() - timedelta(days=QUIET_DAYS)
    settled = {
        (o.patient_ref, o.program)
        for o in existing
        if o.status in SETTLED and as_utc(o.updated_at) >= quiet_since
    }
    found: set[tuple[str, str]] = set()
    created = 0
    for ref, (facts, _) in patients.items():
        for hit in rules.evaluate(facts, today):
            key = (ref, hit["program"])
            found.add(key)
            row = open_by_key.get(key)
            if row is None and key in settled:
                continue
            if row is None:
                session.add(
                    CareOpportunity(
                        workspace_id=principal.workspace_id,
                        patient_ref=ref,
                        program=hit["program"],
                        priority=hit["priority"],
                        due_by=rules._d(hit["due_by"]),
                        why=box.seal({"why": hit["why"]}),
                    )
                )
                created += 1
            else:
                row.why = box.seal({"why": hit["why"]})
                row.priority, row.due_by = hit["priority"], rules._d(hit["due_by"])
    resolved = 0
    for key, row in open_by_key.items():
        if key not in found and row.status == "proposed":
            row.status, resolved = "resolved", resolved + 1
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="care.scanned",
            target=principal.workspace_id,
            detail={"patients": len(patients), "created": created, "resolved": resolved},
        )
    )
    await session.flush()
    return {"patients": len(patients), "created": created, "resolved": resolved}


async def get(session: AsyncSession, principal: Principal, opportunity_id: str) -> CareOpportunity:
    row = await session.get(CareOpportunity, opportunity_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Care opportunity not found.")
    return row


async def decide(
    session: AsyncSession, principal: Principal, row: CareOpportunity, decision: str, note: str
) -> None:
    if decision not in DECISIONS:
        raise ApiError(422, "bad_decision", "Approve or decline.")
    if row.status != "proposed":
        raise ApiError(409, "already_decided", "This opportunity has already been decided.")
    if decision == "decline" and not note.strip():
        raise ApiError(422, "reason_required", "Say why it doesn't apply, for the record.")
    row.status = DECISIONS[decision]
    row.decided_by, row.decided_at, row.note = (
        principal.user_id,
        utcnow(),
        note.strip()[:300] or None,
    )
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=f"care.{row.status}",
            target=row.id,
            detail={"program": row.program},
        )
    )


async def record_outcome(
    session: AsyncSession, principal: Principal, row: CareOpportunity, outcome: str, note: str
) -> None:
    if outcome not in OUTCOMES:
        raise ApiError(422, "bad_outcome", f"Choose one of {', '.join(OUTCOMES)}.")
    if row.status not in ("approved", "contacted"):
        raise ApiError(409, "not_approved", "A clinician has to approve this before outreach.")
    row.status = outcome
    if note.strip():
        row.note = note.strip()[:300]
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="care.outcome",
            target=row.id,
            detail={"program": row.program, "outcome": outcome},
        )
    )


# --- views ----------------------------------------------------------------------------------------


def view(
    row: CareOpportunity, who: dict[str, Any], box: PhiBox, principal: Principal
) -> dict[str, Any]:
    p = rules.PROGRAMS[row.program]
    clinical = principal.can("review") or principal.can("edit_draft")
    name = who.get("name") or ""
    parts = name.split()
    return {
        "id": row.id,
        "patient_ref": row.patient_ref,
        "patient": initials(
            {
                "first_name": parts[0] if parts else "",
                "last_name": parts[-1] if len(parts) > 1 else "",
            }
        ),
        "coverage": who.get("coverage"),
        "program": row.program,
        "title": p.title,
        "codes": p.codes,
        "category": p.category,
        "consent_required": p.consent,
        "staff_time": p.staff_time,
        "outreach": p.outreach,
        "why": box.open(row.why).get("why", []) if clinical else ["•••"],
        "status": row.status,
        "priority": row.priority,
        "due_by": row.due_by.isoformat() if row.due_by else None,
        "note": row.note,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
    }


async def listing(
    session: AsyncSession, principal: Principal, box: PhiBox, today: date, status: str | None
) -> dict[str, Any]:
    patients = await patient_facts(session, principal.workspace_id, box, today)
    query = select(CareOpportunity).where(CareOpportunity.workspace_id == principal.workspace_id)
    rows = list(
        await session.scalars(query.order_by(CareOpportunity.priority, CareOpportunity.created_at))
    )
    counts: dict[str, int] = {}
    by_program: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in OPEN:
            by_program[r.program] = by_program.get(r.program, 0) + 1
    shown = [r for r in rows if (status is None or r.status == status)]
    return {
        "patients": len(patients),
        "counts": counts,
        "open_by_program": [
            {"program": k, "title": rules.PROGRAMS[k].title, "count": v}
            for k, v in sorted(by_program.items(), key=lambda kv: -kv[1])
        ],
        "items": [
            view(r, patients.get(r.patient_ref, (None, {}))[1], box, principal) for r in shown[:500]
        ],
        "programs": [
            {
                "key": p.key,
                "title": p.title,
                "codes": p.codes,
                "consent": p.consent,
                "staff_time": p.staff_time,
            }
            for p in rules.PROGRAMS.values()
        ],
    }
