"""Charges in, claims out (RC3-lite): the billed services a claim is built from.

- :func:`add_samples`: synthetic visits (one patient each) with their charges and diagnoses. Some
  are deliberately wrong (no diagnosis, a modifier that doesn't fit, past timely filing) so the
  scrub has something to refuse.
- :func:`pull_ehr`: NexHealth ``GET /charges`` for a connected practice, where the sandbox has
  them. NexHealth charges carry procedure code, fee, date, provider and patient, but no
  diagnoses, units or modifiers: the claim built from them is scrubbed as incomplete until a
  person adds the diagnoses. The patient's name, birth date, member ID and payer come from the
  practice mirror.
- :func:`build_claims`: charges not on a claim yet, grouped by patient, provider, payer and date
  of service, become ``draft`` claims with one service line per charge.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims import x837
from careplix_api.claims.models import Claim, ClaimCharge
from careplix_api.claims.samples import FIRST_NAMES, PROVIDER_NAME, PROVIDER_NPI
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import EhrRecord, PayerConnection
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

DRAFT = "draft"


@dataclass(frozen=True)
class Visit:
    key: str
    label: str
    days_ago: int
    payer_id: str
    payer_name: str
    plan_kind: str
    diagnoses: tuple[str, ...]
    # (CPT/HCPCS, modifiers, units, charge, diagnosis pointers, description)
    lines: tuple[tuple[str, tuple[str, ...], str, str, tuple[int, ...], str], ...]
    test_outcome: str | None = "paid"
    place_of_service: str = "11"


VISITS: tuple[Visit, ...] = (
    Visit(
        "office_paid",
        "Office visit · test payer pays in full",
        6,
        "60054",
        "Aetna",
        "commercial",
        ("E119", "I10"),
        (
            ("99214", (), "1", "198.50", (1, 2), "Office visit, moderate"),
            ("83036", (), "1", "42.00", (1,), "Hemoglobin A1c"),
        ),
    ),
    Visit(
        "office_partial",
        "Office visit + ECG · test payer pays part",
        9,
        "87726",
        "UnitedHealthcare",
        "commercial",
        ("R0789", "I10"),
        (
            ("99213", ("25",), "1", "142.00", (1, 2), "Office visit, low"),
            ("93000", (), "1", "64.25", (1,), "ECG with interpretation"),
        ),
        test_outcome="partial",
    ),
    Visit(
        "office_denied",
        "Annual wellness · test payer denies",
        12,
        "62308",
        "Cigna",
        "commercial",
        ("Z0000",),
        (("99396", (), "1", "235.00", (1,), "Preventive visit, 40-64"),),
        test_outcome="denied",
    ),
    Visit(
        "medicare_ack_only",
        "Medicare Part B · acknowledgement only",
        4,
        "06102",
        "Medicare Part B, Illinois (NGS, J6)",
        "medicare",
        ("M545",),
        (("99213", (), "1", "142.00", (1,), "Office visit, low"),),
        test_outcome=None,
    ),
    Visit(
        "missing_diagnosis",
        "Scrub catches: no diagnosis",
        5,
        "60054",
        "Aetna",
        "commercial",
        (),
        (("99213", (), "1", "142.00", (), "Office visit, low"),),
    ),
    Visit(
        "bad_modifier",
        "Scrub catches: modifier 25 on a test",
        7,
        "87726",
        "UnitedHealthcare",
        "commercial",
        ("R0789",),
        (("93000", ("25",), "1", "64.25", (1,), "ECG with interpretation"),),
    ),
    Visit(
        "late",
        "Scrub catches: past timely filing",
        200,
        "62308",
        "Cigna",
        "commercial",
        ("J069",),
        (("99213", (), "1", "142.00", (1,), "Office visit, low"),),
    ),
)
VISITS_BY_KEY = {v.key: v for v in VISITS}


_MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"  # CMS: no S, L, O, I, B or Z


def synthetic_mbi(rng: random.Random) -> str:
    """A random identifier in the Medicare Beneficiary Identifier format (made up)."""
    alnum = _MBI_ALPHA + "0123456789"
    shape = ("19", _MBI_ALPHA, alnum, "09", _MBI_ALPHA, alnum, "09", _MBI_ALPHA, _MBI_ALPHA)
    shape += ("09", "09")
    out = []
    for pool in shape:
        if pool == "19":
            out.append(str(rng.randint(1, 9)))
        elif pool == "09":
            out.append(str(rng.randint(0, 9)))
        else:
            out.append(rng.choice(pool))
    return "".join(out)


def view(charge: ClaimCharge) -> dict[str, Any]:
    return {
        "id": charge.id,
        "source": charge.source,
        "synthetic": charge.synthetic,
        "patient_ref": charge.patient_ref,
        "payer_id": charge.payer_id,
        "payer_name": charge.payer_name,
        "provider_name": charge.provider_name,
        "date_of_service": charge.date_of_service,
        "cpt_code": charge.cpt_code,
        "description": charge.description,
        "modifiers": charge.modifiers or [],
        "units": charge.units,
        "amount": charge.amount,
        "diagnoses": charge.diagnoses or [],
        "claim_id": charge.claim_id,
    }


async def add_samples(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    *,
    today: date,
    keys: list[str] | None = None,
) -> list[ClaimCharge]:
    """One synthetic patient per visit, with the visit's charges."""
    box, rng = PhiBox(settings), random.Random()
    n0 = await session.scalar(
        select(func.count(func.distinct(ClaimCharge.patient_ref))).where(
            ClaimCharge.workspace_id == workspace_id, ClaimCharge.source == "sample"
        )
    )
    made = []
    for i, key in enumerate(keys or [v.key for v in VISITS]):
        visit, n = VISITS_BY_KEY[key], (n0 or 0) + i + 1
        first = FIRST_NAMES[n % len(FIRST_NAMES)]
        patient = box.seal(
            {
                "display_name": f"{first} Synthetic",
                "first_name": first,
                "last_name": "Synthetic",
                "date_of_birth": f"19{rng.randint(45, 89)}-0{rng.randint(1, 9)}-1{n % 10}",
                "member_id": synthetic_mbi(rng)
                if visit.plan_kind == "medicare"
                else f"SYN{rng.randint(10**7, 10**8 - 1)}",
                "gender": "F" if n % 2 else "M",
                "address": {
                    "address1": f"{100 + n} Sample Street",
                    "city": "Chicago",
                    "state": "IL",
                    "postal_code": "60601",
                },
            }
        )
        day = (today - timedelta(days=visit.days_ago)).isoformat()
        for j, (cpt, mods, qty, fee, pointers, label) in enumerate(visit.lines):
            charge = ClaimCharge(
                workspace_id=workspace_id,
                source="sample",
                source_ref=f"SYN-CHG-{n:04d}-{j + 1}",
                synthetic=True,
                patient_ref=f"SYN-PT-{n:04d}",
                patient=patient,
                payer_id=visit.payer_id,
                payer_name=visit.payer_name,
                plan_kind=visit.plan_kind,
                provider_name=PROVIDER_NAME,
                provider_npi=PROVIDER_NPI,
                date_of_service=day,
                cpt_code=cpt,
                description=label,
                modifiers=list(mods),
                units=qty,
                amount=fee,
                diagnoses=[visit.diagnoses[p - 1] for p in pointers],
                place_of_service=visit.place_of_service,
                test_outcome=visit.test_outcome,
            )
            session.add(charge)
            made.append(charge)
    await session.flush()
    return made


async def build_claims(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    *,
    charge_ids: list[str] | None = None,
) -> list[Claim]:
    """Draft claims from charges that aren't on a claim yet (one per patient, provider, payer and
    date of service). A charge without a payer stays unbilled."""
    query = select(ClaimCharge).where(
        ClaimCharge.workspace_id == workspace_id,
        ClaimCharge.claim_id.is_(None),
        ClaimCharge.payer_id.is_not(None),
    )
    if charge_ids is not None:
        query = query.where(ClaimCharge.id.in_(charge_ids))
    groups: dict[tuple[Any, ...], list[ClaimCharge]] = {}
    for charge in await session.scalars(query.order_by(ClaimCharge.source_ref)):
        key = (
            charge.patient_ref,
            charge.provider_npi,
            charge.payer_id,
            charge.date_of_service,
        )
        groups.setdefault(key, []).append(charge)
    made = []
    for charges in groups.values():
        made.append(_claim(workspace_id, charges))
    for claim, charges in zip(made, groups.values(), strict=True):
        session.add(claim)
        await session.flush()
        for charge in charges:
            charge.claim_id = claim.id
    return made


def _claim(workspace_id: str, charges: list[ClaimCharge]) -> Claim:
    first = charges[0]
    diagnoses: list[str] = []
    for charge in charges:
        for code in charge.diagnoses or []:
            code = x837.icd10(code)
            if code and code not in diagnoses:
                diagnoses.append(code)
    lines = []
    for i, charge in enumerate(charges, 1):
        own = [x837.icd10(c) for c in charge.diagnoses or []]
        lines.append(
            {
                "line_id": f"L{i}",
                "cpt": charge.cpt_code,
                "description": charge.description,
                "modifiers": list(charge.modifiers or []),
                "units": charge.units or "1",
                "charge": x837.amount(charge.amount),
                "dx": [diagnoses.index(c) + 1 for c in own if c in diagnoses][: x837.MAX_LINE_DX],
                "date_of_service": charge.date_of_service,
                "charge_id": charge.id,
            }
        )
    pcn = x837.new_control_number()
    return Claim(
        workspace_id=workspace_id,
        source="charges",
        source_ref=pcn,
        ehr_connection_id=first.ehr_connection_id,
        synthetic=first.synthetic,
        claim_number=pcn,
        payer_id=first.payer_id,
        payer_name=first.payer_name or first.payer_id or "",
        # Synthetic payers get a fictional 202-555 number (only approved numbers are dialled).
        payer_phone=f"+1202555{170 + int(pcn[-2:], 36) % 30:04d}" if first.synthetic else None,
        plan_kind=first.plan_kind,
        provider_name=first.provider_name or "",
        provider_npi=first.provider_npi or "",
        patient_ref=first.patient_ref,
        patient=first.patient,
        date_of_service=first.date_of_service,
        billed_amount=x837.total(lines),
        cpt_code=lines[0]["cpt"],
        submitted_on="",
        stage=DRAFT,
        place_of_service=first.place_of_service or "11",
        diagnoses=diagnoses,
        lines=lines,
        frequency="original",
        test_outcome=first.test_outcome,
    )


# --- NexHealth charges (where the practice's EHR has them) ---------------------------------------


async def _mirror(session: AsyncSession, conn: PayerConnection, resource: str, ids: set[str]):
    if not ids:
        return {}
    rows = await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == resource,
            EhrRecord.source_id.in_(ids),
        )
    )
    return {r.source_id: r for r in rows}


async def _coverages(session: AsyncSession, conn: PayerConnection, patients: set[str]):
    if not patients:
        return {}
    rows = await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "coverage",
            EhrRecord.patient_ref.in_(patients),
            EhrRecord.status == "active",
        )
    )
    best: dict[str, EhrRecord] = {}
    for row in rows:
        current = best.get(row.patient_ref or "")
        if current is None or (row.attrs or {}).get("priority") == 1:
            best[row.patient_ref or ""] = row
    return best


async def pull_ehr(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    days: int = 60,
) -> dict[str, Any]:
    """Read the practice's recent charges from NexHealth into the charge list (new ones only)."""
    from careplix_api.db import utcnow
    from careplix_api.practice.connections import is_test, location_id, nexhealth_client

    try:
        client = nexhealth_client(conn, settings, transport)
    except NexHealthError as err:  # e.g. no key for this practice's environment
        return {"ok": False, "read": 0, "added": 0, "error": err.message[:200]}
    since = (utcnow() - timedelta(days=days)).isoformat()
    try:
        rows, _ = await client.list_all(
            "/charges", {"location_id": location_id(conn), "updated_since": since}
        )
    except NexHealthError as err:
        return {"ok": False, "read": 0, "added": 0, "error": err.message[:200]}
    finally:
        await client.aclose()
    rows = [r for r in rows if isinstance(r, dict) and not r.get("deleted_at")]
    patients = {str(r.get("patient_id")) for r in rows if r.get("patient_id")}
    providers = {str(r.get("provider_id")) for r in rows if r.get("provider_id")}
    known = set(
        await session.scalars(
            select(ClaimCharge.source_ref).where(
                ClaimCharge.workspace_id == conn.workspace_id, ClaimCharge.source == "ehr"
            )
        )
    )
    box = PhiBox(settings)
    patient_rows = await _mirror(session, conn, "patient", patients)
    provider_rows = await _mirror(session, conn, "provider", providers)
    coverages = await _coverages(session, conn, patients)
    plans = await _mirror(
        session,
        conn,
        "insurance_plan",
        {str((c.attrs or {}).get("plan_id")) for c in coverages.values()},
    )
    added = 0
    for r in rows:
        ref = f"{conn.id[:8]}:{r.get('id')}"
        if ref in known or not r.get("procedure_code"):
            continue
        pid = str(r.get("patient_id") or "")
        patient = box.open(patient_rows[pid].pii) if pid in patient_rows else {}
        coverage = coverages.get(pid)
        cov_pii = box.open(coverage.pii) if coverage else {}
        plan = plans.get(str((coverage.attrs or {}).get("plan_id"))) if coverage else None
        provider = provider_rows.get(str(r.get("provider_id") or ""))
        fee = (r.get("fee") or {}).get("amount") if isinstance(r.get("fee"), dict) else r.get("fee")
        address = patient.get("address") or {}
        session.add(
            ClaimCharge(
                workspace_id=conn.workspace_id,
                source="ehr",
                source_ref=ref,
                ehr_connection_id=conn.id,
                synthetic=is_test(conn),
                patient_ref=pid or None,
                patient=box.seal(
                    {
                        "display_name": " ".join(
                            x for x in (patient.get("first_name"), patient.get("last_name")) if x
                        ),
                        "first_name": patient.get("first_name"),
                        "last_name": patient.get("last_name"),
                        "date_of_birth": patient.get("date_of_birth"),
                        "gender": {"female": "F", "male": "M"}.get(
                            str(patient.get("gender") or "").lower()
                        ),
                        "member_id": cov_pii.get("member_id"),
                        "address": {
                            "address1": address.get("address_line_1")
                            or address.get("street_address"),
                            "city": address.get("city"),
                            "state": address.get("state"),
                            "postal_code": address.get("zip_code"),
                        },
                    }
                ),
                payer_id=(plan.attrs or {}).get("payer_id") if plan else None,
                payer_name=(plan.attrs or {}).get("name") if plan else None,
                plan_kind="commercial" if plan else None,
                provider_ref=str(r.get("provider_id") or "") or None,
                provider_name=(provider.attrs or {}).get("name") if provider else None,
                provider_npi=(provider.attrs or {}).get("npi") if provider else None,
                date_of_service=str(r.get("charged_at") or "")[:10],
                cpt_code=str(r.get("procedure_code"))[:10],
                description=str(r.get("description") or "")[:160] or None,
                modifiers=[],
                units="1",
                amount=x837.amount(fee),
                diagnoses=[],
            )
        )
        added += 1
    await session.flush()
    return {"ok": True, "read": len(rows), "added": added, "error": None}
