"""The workspace's billing profile: billing provider, tax ID and submitter on every claim.

A workspace without one gets a synthetic profile (the clearinghouse's published test NPI and a
random tax ID), so test claims work out of the box. Test claims that should come back with a
remittance must carry the NPI and tax ID the provider was enrolled with at the clearinghouse's test
payer: the owner enrolls with these values (docs/solution/RCM_TEST_MODE_WALKTHROUGH.md).
"""

from __future__ import annotations

import random
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims.models import BillingProfile
from careplix_api.errors import ApiError
from careplix_api.integrations import stedi

FIELDS = (
    "organization",
    "npi",
    "tax_id",
    "taxonomy",
    "address1",
    "city",
    "state",
    "postal_code",
    "submitter_name",
    "contact_phone",
)


def _synthetic(workspace_id: str) -> dict[str, Any]:
    rng = random.Random(workspace_id)
    return {
        "organization": "Riverside Family Medicine (synthetic)",
        "npi": stedi.MOCK_PROVIDER_NPI,  # passes the NPI check digit; registered to nobody
        "tax_id": f"9{rng.randint(10_000_000, 99_999_999)}",
        "taxonomy": "207Q00000X",  # family medicine
        "address1": "100 Synthetic Way",
        "city": "Chicago",
        "state": "IL",
        "postal_code": "606010001",
        "submitter_name": "CarePlix Billing",
        "contact_phone": "2025550143",
    }


async def get(session: AsyncSession, workspace_id: str) -> BillingProfile:
    """The workspace's profile, made synthetic the first time it's needed."""
    row = await session.scalar(
        select(BillingProfile).where(BillingProfile.workspace_id == workspace_id)
    )
    if row is None:
        row = BillingProfile(workspace_id=workspace_id, **_synthetic(workspace_id))
        session.add(row)
        await session.flush()
    return row


def view(row: BillingProfile) -> dict[str, Any]:
    return {k: getattr(row, k) for k in FIELDS}


def update(row: BillingProfile, values: dict[str, Any]) -> BillingProfile:
    clean = {k: str(v).strip() for k, v in values.items() if k in FIELDS and v is not None}
    for key in ("tax_id", "postal_code", "contact_phone", "npi"):
        if key in clean:
            clean[key] = re.sub(r"\D", "", clean[key])
    if "npi" in clean and not stedi.valid_npi(clean["npi"]):
        raise ApiError(422, "invalid_npi", "That NPI fails the check digit.")
    if "tax_id" in clean and len(clean["tax_id"]) != 9:
        raise ApiError(422, "invalid_tax_id", "The tax ID (EIN) has 9 digits.")
    if "postal_code" in clean and len(clean["postal_code"]) not in (5, 9):
        raise ApiError(422, "invalid_postal_code", "Use a 5- or 9-digit ZIP code.")
    if "state" in clean:
        clean["state"] = clean["state"].upper()[:2]
    for key, value in clean.items():
        setattr(row, key, value)
    return row
