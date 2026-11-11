"""FO-3 receptionist: "what am I covered for?" and "when is my wellness visit due?" on a call.

``medicare_benefits`` reads the latest Medicare benefits snapshot the front desk took for the
patient ``find_patient`` verified (Practice → Medicare benefits). It never asks CMS itself: HETS
checks are for patients being scheduled or treated, made by staff, one a day.

What the caller hears is caller-safe and gated by the agent's published guardrails:
``medicare_coverage`` (Part A/B covered or not, and whether a Medicare Advantage plan handles
their benefits) and ``medicare_preventive_due`` (when common preventive services are due). Never
another provider's NPI, a claim, a service history date or a dollar amount.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from careplix_api import events
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import Rejected
from careplix_api.models import CallAttempt
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility, medicare, medicare_planner
from careplix_api.practice.connections import is_test
from careplix_api.practice.receptionist import ToolEnv, _connection, _verified

TOOL = "medicare_benefits"
DISCLOSURES = ("medicare_coverage", "medicare_preventive_due")
MAX_AGE = timedelta(days=30)
CAVEAT = (
    "Say what Medicare reported, in plain words. It is not a guarantee of payment; never promise "
    "coverage or quote a cost, and don't give medical advice about which services they need."
)
FRONT_DESK = "Offer to take a message so the front desk can check Medicare benefits and call back."


async def medicare_benefits(session, attempt: CallAttempt, version, args, _evidence, env: ToolEnv):
    patient_ref = _verified(attempt)
    conn = await _connection(session, attempt)
    allowed = set(version.config["guardrails"].get("allowed_disclosures", [])) & set(DISCLOSURES)
    if not allowed:
        raise Rejected("not_permitted", "I can't discuss Medicare benefits on this line.")
    clearinghouse = await eligibility.clearinghouse(session, attempt.workspace_id)
    mode = None
    if clearinghouse is not None:
        mode = "test" if is_test(clearinghouse) else "production"
    snapshot = await medicare.latest(session, conn, patient_ref, mode)
    fresh = (
        snapshot is not None
        and snapshot.state == "done"
        and utcnow() - as_utc(snapshot.created_at) <= MAX_AGE
    )
    if not fresh:
        await events.append(session, attempt, "medicare.benefits", "api", {"outcome": "none"})
        return {
            "state": "not_checked",
            "message": "The front desk hasn't checked this caller's Medicare benefits recently. "
            + FRONT_DESK,
        }
    benefits = PhiBox(env.settings).open(snapshot.benefits).get("benefits") or {}
    facts = medicare_planner.caller_facts(benefits, today=utcnow().date(), allowed=allowed)
    out: dict[str, Any] = {
        "state": "checked",
        "checked_on": as_utc(snapshot.created_at).date().isoformat(),
        **facts,
    }
    message = CAVEAT
    if facts.get("medicare_advantage"):
        message = (
            f"Medicare says {facts['medicare_advantage']} handles this caller's benefits. Tell "
            "them to check their benefits with that plan; the front desk will confirm. " + CAVEAT
        )
    elif "preventive" in facts and not facts["preventive"]:
        message = "Medicare listed nothing due right now. " + CAVEAT
    if snapshot.mode == "test":
        out["test_data"] = True
        message += " This is test data, not the caller's real Medicare record."
    out["message"] = message
    await events.append(
        session,
        attempt,
        "medicare.benefits",
        "api",
        {
            "outcome": "checked",
            "items": len(facts.get("preventive") or []),
            "mock": snapshot.mode == "test",
        },
    )
    return out


HANDLERS = {TOOL: medicare_benefits}
