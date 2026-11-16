"""Synthetic claims and the synthetic 277 answers they get, until the EHR billing pull (RC3).

Each sample claim has a scenario: how old it is, which payer, and what the payer says when asked.
Sample claims are answered by :func:`response` (shaped like the clearinghouse's 277 JSON, built
from its documentation's examples), the way the payer sandbox answers prior auths: a synthetic
payer can't be asked anything real. Everyone here is synthetic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims.models import Claim
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

PROVIDER_NAME = "Riverside Family Medicine (synthetic)"
PROVIDER_NPI = "1234567893"  # a valid check digit, registered to nobody we know of


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    age_days: int  # days since the payer received the claim
    payer_id: str
    payer_name: str
    plan_kind: str
    # The 277: category, status code, the payer's text, days since the payer set this status.
    category: str | None
    code: str | None
    text: str
    status_age_days: int = 3


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "paid", "Paid", 21, "60054", "Aetna", "commercial", "F1", "65", "Claim/line has been paid."
    ),
    Scenario(
        "pending",
        "Pending, recent",
        16,
        "87726",
        "UnitedHealthcare",
        "commercial",
        "P1",
        "20",
        "Accepted for processing.",
        status_age_days=4,
    ),
    Scenario(
        "stale",
        "Pending for weeks",
        46,
        "62308",
        "Cigna",
        "commercial",
        "P2",
        "20",
        "Accepted for processing.",
        status_age_days=38,
    ),
    Scenario(
        "denied",
        "Denied",
        27,
        "61101",
        "Humana",
        "medicare_advantage",
        "F2",
        "585",
        "Denied Charge or Non-covered Charge.",
    ),
    Scenario(
        "info",
        "Payer needs records",
        24,
        "87726",
        "UnitedHealthcare",
        "commercial",
        "R4",
        "294",
        "Supporting documentation.",
    ),
    Scenario(
        "not_found",
        "Payer can't find it",
        33,
        "68069",
        "Ambetter",
        "commercial",
        "A4",
        "35",
        "Claim/encounter not found.",
    ),
    Scenario(
        "medicare",
        "Traditional Medicare",
        38,
        "06102",
        "Medicare Part B, Illinois (NGS, J6)",
        "medicare",
        None,
        None,
        "",
    ),
    Scenario(
        "young",
        "Just submitted",
        5,
        "60054",
        "Aetna",
        "commercial",
        "A2",
        "20",
        "Accepted for processing.",
    ),
)
BY_KEY = {s.key: s for s in SCENARIOS}
FIRST_NAMES = ("Alder", "Briar", "Cedar", "Dune", "Ember", "Fern", "Gale", "Heath")
CPT = (("99213", "142.00"), ("99214", "198.50"), ("93000", "64.25"), ("80053", "48.00"))


def _phone(n: int) -> str:
    return f"+1202555{(170 + n % 30):04d}"


async def add(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    *,
    today: date,
    scenarios: list[str] | None = None,
) -> list[Claim]:
    """One synthetic claim per scenario (all of them by default), numbered after existing ones."""
    box, rng = PhiBox(settings), random.Random()
    existing = await session.scalar(
        select(func.count()).select_from(Claim).where(Claim.workspace_id == workspace_id)
    )
    made = []
    for i, key in enumerate(scenarios or [s.key for s in SCENARIOS]):
        s, n = BY_KEY[key], (existing or 0) + i + 1
        first = FIRST_NAMES[n % len(FIRST_NAMES)]
        cpt, amount = CPT[n % len(CPT)]
        submitted = today - timedelta(days=s.age_days)
        claim = Claim(
            workspace_id=workspace_id,
            source="sample",
            source_ref=f"SYN-{n:04d}",
            synthetic=True,
            claim_number=f"SYN-CL-{n:05d}",
            payer_id=s.payer_id,
            payer_name=s.payer_name,
            payer_phone=_phone(n),
            plan_kind=s.plan_kind,
            provider_name=PROVIDER_NAME,
            provider_npi=PROVIDER_NPI,
            patient=box.seal(
                {
                    "display_name": f"{first} Synthetic",
                    "first_name": first,
                    "last_name": "Synthetic",
                    "date_of_birth": f"19{rng.randint(40, 89)}-0{rng.randint(1, 9)}-1{n % 10}",
                    "member_id": f"SYN{rng.randint(10**7, 10**8 - 1)}",
                    "gender": "F" if n % 2 else "M",
                }
            ),
            date_of_service=(submitted - timedelta(days=3)).isoformat(),
            billed_amount=amount,
            cpt_code=cpt,
            submitted_on=submitted.isoformat(),
            sample_scenario=s.key,
        )
        session.add(claim)
        made.append(claim)
    await session.flush()
    return made


def response(claim: Claim, today: date) -> dict[str, Any]:
    """The synthetic 277 for a sample claim's scenario, in the clearinghouse's JSON shape."""
    s = BY_KEY.get(claim.sample_scenario or "")
    if s is None or s.category is None:
        return {"claims": [], "errorResponse": {"errors": []}}
    effective = (today - timedelta(days=s.status_age_days)).isoformat()
    paid = claim.billed_amount if s.category == "F1" else "0"
    status = {
        "statusCategoryCode": s.category,
        "statusCode": s.code,
        "statusCodeValue": s.text,
        "effectiveDate": effective,
        "submittedAmount": claim.billed_amount,
        "amountPaid": paid,
        "patientAccountNumber": claim.claim_number,
        "tradingPartnerClaimNumber": f"SYNPAYER{claim.claim_number[-5:]}",
        "claimServiceDate": claim.date_of_service.replace("-", ""),
    }
    if s.category == "F1":
        status |= {
            "paidDate": effective,
            "checkIssueDate": effective,
            "checkNumber": f"SYNEFT{claim.claim_number[-5:]}",
        }
    return {
        "status": "success",
        "payer": {"organizationName": s.payer_name},
        "claims": [
            {
                "claimStatus": status,
                "serviceDetails": [
                    {
                        "service": {
                            "procedureId": claim.cpt_code,
                            "submittedAmount": claim.billed_amount,
                            "amountPaid": paid,
                        },
                        "status": [
                            {
                                "statusCategoryCode": s.category,
                                "statusCode": s.code,
                                "statusCodeValue": s.text,
                            }
                        ],
                    }
                ],
            }
        ],
        "meta": {"applicationMode": "synthetic"},
    }
