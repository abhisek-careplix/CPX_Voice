"""Which Stedi payer an EHR insurance plan goes to, and the provider's enrollment with it.

EHRs hold free-text payer names and their own payer IDs. Stedi's payer search (filtered to
payers that take eligibility checks, and by program and state when known) turns them into a
Stedi payer ID, cached per workspace in :class:`PayerMapping`. A match that isn't unique is never
guessed: it waits for a person to pick one in the Studio.

Medicare is special: fee-for-service plans always go to CMS (HETS) and need the patient's MBI;
Medicare Advantage plans are private payers and never go to CMS.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.integrations import stedi
from careplix_api.models import PayerMapping, ProviderEnrollment

REFRESH = timedelta(days=1)  # Stedi: refresh stored payer details at least once a day
MAX_CANDIDATES = 5
SETTLED = ("resolved", "confirmed")

# Plan kinds. A plan name is all the EHR usually tells us, so these are deliberately cautious:
# anything that mentions Advantage, Part C or a managed Medicare product is not fee-for-service.
MEDICARE, MEDICARE_ADVANTAGE, MEDICAID, COMMERCIAL = (
    "medicare",
    "medicare_advantage",
    "medicaid",
    "commercial",
)
_ADVANTAGE = re.compile(
    r"advantage|part\s*c\b|\bma(pd)?\b|\bd-?snp\b|\bc-?snp\b|\bi-?snp\b|\bsnp\b|"
    r"medicare\s*(hmo|ppo|pffs|msa)|(hmo|ppo)\s*medicare|medicare\s*(plus|complete|choice)",
    re.I,
)
_MEDICARE = re.compile(r"\bmedicare\b|\bpart\s*[ab]\b|\bhets\b|\bcms\b", re.I)
_MEDICAID = re.compile(
    r"medicaid|medi-?cal|chip\b|badgercare|masshealth|tenncare|soonercare|ahcccs|"
    r"husky|apple\s*health|healthy\s*(connections|louisiana|blue)|star\s*(kids|plus)",
    re.I,
)
_SUPPLEMENT = re.compile(r"supplement|medigap|med\s*supp|part\s*d\b|prescription", re.I)
_MEDICARE_IDS = {"CMS", "MEDICARE", "HETS"}


def plan_kind(name: str | None, ehr_payer_id: str | None = None) -> str:
    text = f"{name or ''} {ehr_payer_id or ''}"
    if _SUPPLEMENT.search(text):
        return COMMERCIAL  # Medigap and Part D plans are private payers, not CMS
    if _ADVANTAGE.search(text):
        return MEDICARE_ADVANTAGE
    if _MEDICARE.search(text) or str(ehr_payer_id or "").upper() in _MEDICARE_IDS:
        return MEDICARE
    if _MEDICAID.search(text):
        return MEDICAID
    return COMMERCIAL


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def source_key(ehr_payer_id: str | None, name: str | None) -> str:
    return f"{_norm(ehr_payer_id)}|{_norm(name)}"[:160]


def _candidate(payer: dict[str, Any]) -> dict[str, Any]:
    support = (payer.get("transactionSupport") or {}).get("eligibilityCheck")
    return {
        "payer_id": payer.get("primaryPayerId") or payer.get("stediId"),
        "stedi_id": payer.get("stediId"),
        "name": payer.get("displayName"),
        "programs": payer.get("programs") or [],
        "states": payer.get("operatingStates") or [],
        "enrollment_required": support == "ENROLLMENT_REQUIRED",
    }


def _names(payer: dict[str, Any]) -> set[str]:
    return {_norm(n) for n in [payer.get("displayName"), *(payer.get("names") or [])] if n}


def _ids(payer: dict[str, Any]) -> set[str]:
    values = [payer.get("primaryPayerId"), payer.get("stediId"), *(payer.get("aliases") or [])]
    return {str(v).upper() for v in values if v}


def _apply(mapping: PayerMapping, state: str, payer: dict[str, Any] | None = None) -> None:
    mapping.state, mapping.resolved_at, mapping.last_error = state, utcnow(), None
    if payer is not None:
        mapping.stedi_payer_id, mapping.stedi_name = payer["payer_id"], payer["name"]
        mapping.programs = payer["programs"]
        mapping.enrollment_required = payer["enrollment_required"]


async def resolve(
    session: AsyncSession,
    client: stedi.StediClient | None,
    workspace_id: str,
    *,
    ehr_payer_id: str | None,
    name: str | None,
    state: str | None = None,
    kind: str | None = None,
) -> PayerMapping:
    """The workspace's mapping for this EHR payer, searching Stedi when it's new or stale.
    ``kind`` overrides the plan kind guessed from the name (a CMS reply naming an MA plan)."""
    key = source_key(ehr_payer_id, name)
    mapping = await session.scalar(
        select(PayerMapping).where(
            PayerMapping.workspace_id == workspace_id, PayerMapping.source_key == key
        )
    )
    if mapping is None:
        mapping = PayerMapping(
            workspace_id=workspace_id,
            source_key=key,
            ehr_payer_id=(ehr_payer_id or None) and str(ehr_payer_id)[:40],
            ehr_name=(name or None) and str(name)[:160],
            state="new",
        )
        session.add(mapping)
    kind = kind or plan_kind(name, ehr_payer_id)
    if mapping.plan_kind != MEDICARE_ADVANTAGE:  # once known to be MA, it stays MA
        mapping.plan_kind = kind
    kind = mapping.plan_kind
    if mapping.state == "confirmed":
        return mapping  # a person chose this one; never overwrite it
    if mapping.state in ("ambiguous",) and mapping.candidates:
        return mapping  # waiting for a person: searching again would only repeat the question
    fresh = mapping.resolved_at and utcnow() - as_utc(mapping.resolved_at) < REFRESH
    if mapping.state == "resolved" and fresh:
        return mapping
    if kind == MEDICARE:
        # Fee-for-service Medicare is one payer: CMS (HETS), which requires attestation per NPI.
        _apply(
            mapping,
            "resolved",
            {
                "payer_id": stedi.CMS,
                "name": "Medicare (CMS)",
                "programs": ["MEDICARE"],
                "enrollment_required": True,
            },
        )
        return mapping
    if client is None:
        if mapping.state == "new":
            mapping.state = "not_found"
        return mapping
    programs = {MEDICAID: ["MEDICAID"], MEDICARE_ADVANTAGE: ["MEDICARE_ADVANTAGE"]}.get(kind)
    try:
        payer = None
        if ehr_payer_id:
            wanted = str(ehr_payer_id).upper()
            by_id = [p for p in await client.search_payers(wanted) if wanted in _ids(p)]
            if len(by_id) == 1:
                payer = by_id[0]
        found: list[dict[str, Any]] = []
        if payer is None and name:
            found = await client.search_payers(
                name, programs=programs, states=[state] if state else None
            )
            if programs and not found:  # Stedi hasn't classified every payer's programs
                found = await client.search_payers(name, states=[state] if state else None)
            exact = [p for p in found if _norm(name) in _names(p)]
            if len(exact) == 1:
                payer = exact[0]
            elif len(found) == 1:
                payer = found[0]
    except stedi.StediError as err:
        mapping.last_error = err.message[:200]
        if mapping.state == "new":
            mapping.state = "error"
        return mapping
    if payer is not None and _candidate(payer)["payer_id"]:
        chosen = _candidate(payer)
        if kind == MEDICARE_ADVANTAGE and chosen["payer_id"] == stedi.CMS:
            # Never send a Medicare Advantage plan to CMS: a person has to pick the MA payer.
            _apply(mapping, "ambiguous")
            mapping.candidates = []
            return mapping
        _apply(mapping, "resolved", chosen)
        mapping.candidates = None
        if "MEDICARE_ADVANTAGE" in chosen["programs"] and kind == COMMERCIAL:
            mapping.plan_kind = MEDICARE_ADVANTAGE
        return mapping
    candidates = [
        c for c in (_candidate(p) for p in found[:MAX_CANDIDATES]) if c["payer_id"] != stedi.CMS
    ]
    _apply(mapping, "ambiguous" if candidates else "not_found")
    mapping.candidates = candidates
    return mapping


def confirm(mapping: PayerMapping, payer_id: str, user_id: str) -> PayerMapping:
    """A person's choice: one of the candidates, or a payer ID they typed."""
    chosen = next((c for c in mapping.candidates or [] if c["payer_id"] == payer_id), None)
    if mapping.plan_kind == MEDICARE_ADVANTAGE and payer_id.upper() == stedi.CMS:
        raise ValueError("Medicare Advantage plans go to the plan's own payer, never to CMS.")
    _apply(
        mapping,
        "confirmed",
        chosen
        or {
            "payer_id": payer_id,
            "name": mapping.stedi_name if mapping.stedi_payer_id == payer_id else None,
            "programs": mapping.programs or [],
            "enrollment_required": mapping.enrollment_required,
        },
    )
    mapping.confirmed_by = user_id
    return mapping


def view(mapping: PayerMapping, enrollment: ProviderEnrollment | None = None) -> dict[str, Any]:
    return {
        "id": mapping.id,
        "ehr_payer_id": mapping.ehr_payer_id,
        "ehr_name": mapping.ehr_name,
        "state": mapping.state,
        "plan_kind": mapping.plan_kind,
        "stedi_payer_id": mapping.stedi_payer_id,
        "stedi_name": mapping.stedi_name,
        "programs": mapping.programs or [],
        "enrollment_required": bool(mapping.enrollment_required),
        "enrollment": {"status": enrollment.status, "detail": enrollment.detail}
        if enrollment
        else None,
        "candidates": mapping.candidates or [],
        "has_pin": bool(mapping.pin),
        "last_error": mapping.last_error,
        "resolved_at": mapping.resolved_at,
    }


async def enrollment(
    session: AsyncSession, workspace_id: str, npi: str, payer_id: str
) -> ProviderEnrollment | None:
    return await session.scalar(
        select(ProviderEnrollment).where(
            ProviderEnrollment.workspace_id == workspace_id,
            ProviderEnrollment.npi == npi,
            ProviderEnrollment.payer_id == payer_id,
        )
    )


async def record_enrollment(
    session: AsyncSession,
    workspace_id: str,
    npi: str,
    payer_id: str,
    status: str,
    detail: str | None = None,
) -> ProviderEnrollment:
    row = await enrollment(session, workspace_id, npi, payer_id)
    if row is None:
        row = ProviderEnrollment(workspace_id=workspace_id, npi=npi, payer_id=payer_id, status="")
        session.add(row)
    row.status, row.detail, row.updated_at = status, (detail or None) and detail[:200], utcnow()
    return row


async def note_outcome(
    session: AsyncSession,
    workspace_id: str,
    npi: str,
    payer_id: str,
    errors: list[dict[str, Any]],
    *,
    accepted: bool,
) -> None:
    """Keep the NPI's enrollment status with this payer in step with what the payer said."""
    codes = {e["code"] for e in errors}
    if "41" in codes or (payer_id == stedi.CMS and "97" in codes):
        await record_enrollment(
            session,
            workspace_id,
            npi,
            payer_id,
            "action_required",
            stedi.error_message(errors, payer_id),
        )
    elif accepted:
        await record_enrollment(session, workspace_id, npi, payer_id, "active")
