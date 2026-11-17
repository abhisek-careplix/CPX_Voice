"""Payer rules: which services need prior authorization with which payer.

A rule matches an appointment by its EHR appointment type (id, or name ignoring case) and the
visit's payer (name or payer id; ``*`` matches any payer). Rules are per workspace and synthetic
until the PHI gate; real payer rule sets (e.g. a PA platform's rules API) plug in here later.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.priorauth.models import PriorAuthRule

# Synthetic starter rules a workspace can add with one click (codes are real CPT/ICD-10 codes,
# the pairing with payers is made up).
SAMPLES: tuple[dict[str, Any], ...] = (
    {
        "payer": "*",
        "appointment_type_name": "MRI",
        "cpt_code": "72148",
        "service_description": "MRI lumbar spine without contrast",
        "icd_codes": ["M54.50"],
        "note": "Synthetic rule: advanced imaging needs prior auth with every payer.",
    },
    {
        "payer": "*",
        "appointment_type_name": "Sleep study",
        "cpt_code": "95810",
        "service_description": "Sleep study, attended",
        "icd_codes": ["G47.33"],
        "note": "Synthetic rule: attended sleep studies need prior auth.",
    },
)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def matches(
    rule: PriorAuthRule, *, type_ref: str | None, type_name: str | None, payer: dict
) -> bool:
    if not rule.enabled:
        return False
    if rule.appointment_type_ref and rule.appointment_type_ref != (type_ref or ""):
        return False
    if rule.appointment_type_name and _norm(rule.appointment_type_name) != _norm(type_name):
        return False
    if not (rule.appointment_type_ref or rule.appointment_type_name):
        return False  # a rule names the service it covers
    if rule.payer.strip() in ("", "*"):
        return True
    wanted = _norm(rule.payer)
    return wanted in (_norm(payer.get("name")), _norm(payer.get("payer_id")))


async def for_workspace(session: AsyncSession, workspace_id: str) -> list[PriorAuthRule]:
    return list(
        await session.scalars(
            select(PriorAuthRule)
            .where(PriorAuthRule.workspace_id == workspace_id)
            .order_by(PriorAuthRule.created_at)
        )
    )


def first_match(
    rules: list[PriorAuthRule], *, type_ref: str | None, type_name: str | None, payer: dict
) -> PriorAuthRule | None:
    return next(
        (r for r in rules if matches(r, type_ref=type_ref, type_name=type_name, payer=payer)), None
    )
