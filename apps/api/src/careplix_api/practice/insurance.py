"""FO-3 receptionist: "is my insurance active?" and "what's my copay?" on a call.

``verify_insurance`` runs one real-time Stedi check for the patient ``find_patient`` verified,
with the insurer name and member ID (or Medicare number) the caller reads off their card. The
patient's name and date of birth come from the chart, never from the call.

What the caller hears is a short, caller-safe summary: active or not, the office copay, the
deductible left, and whether another plan (a Medicaid managed care plan, a Medicare Advantage
plan) handles their coverage. Each piece is shared only when the agent's published guardrails
allow that disclosure (``coverage_status``, ``plan_name``, ``office_copay``,
``deductible_remaining``, ``coverage_redirect``). Member IDs never appear in results or logs.

A caller is waiting, so the check gets one attempt with a short timeout; a payer that doesn't
answer becomes "the front desk will check", never a guess.
"""

from __future__ import annotations

from typing import Any

from careplix_api import events
from careplix_api.errors import Rejected
from careplix_api.integrations import stedi
from careplix_api.models import CallAttempt
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility, payers
from careplix_api.practice.connections import audit, is_test, stedi_client
from careplix_api.practice.receptionist import ToolEnv, _connection, _count, _records, _verified

TOOL = "verify_insurance"
MAX_CHECKS = 2
DISCLOSURES = (
    "coverage_status",
    "plan_name",
    "office_copay",
    "deductible_remaining",
    "coverage_redirect",
)
CAVEAT = (
    "Tell the caller what the insurer reported, in plain words. It is not a guarantee of payment "
    "or of the final cost; never promise coverage."
)
FRONT_DESK = "Offer to take a message so the front desk can check and call back."


def caller_summary(summary: dict[str, Any], allowed: set[str], mock: bool) -> dict[str, Any]:
    """Only what the agent may disclose. Amounts are what the payer reported, as text."""
    out: dict[str, Any] = {
        "state": "checked",
        "coverage": summary["coverage"] if summary["coverage"] in ("active", "inactive") else None,
    }
    if out["coverage"] is None:
        out["coverage"] = "unknown"
    if "plan_name" in allowed and summary.get("plan"):
        out["plan"] = summary["plan"]
    if "office_copay" in allowed and summary.get("office_copay") is not None:
        out["office_copay"] = f"${summary['office_copay']}"
    remaining = (summary.get("deductible") or {}).get("remaining")
    if "deductible_remaining" in allowed and remaining is not None:
        out["deductible_remaining"] = f"${remaining}"
    redirect = summary.get("redirect")
    message = CAVEAT
    if redirect and "coverage_redirect" in allowed:
        out["other_plan"] = {"kind": redirect["kind"], "name": redirect.get("name")}
        message = (
            f"The insurer says {redirect.get('name') or 'another plan'} handles this coverage. "
            "Tell the caller the front desk will confirm their benefits with that plan before "
            "the visit. " + CAVEAT
        )
    elif out["coverage"] == "inactive":
        message = (
            "The insurer reports this coverage as not active. Ask the caller to check the card "
            "or bring any new insurance details, and say the front desk will confirm. " + CAVEAT
        )
    if mock:
        out["test_data"] = True
        message += " This is test data from a mock insurer, not the caller's real coverage."
    out["message"] = message
    return out


def _unanswered(outcome: str) -> dict[str, Any]:
    if outcome == stedi.NOT_FOUND:
        return {
            "state": "not_found",
            "message": "The insurer couldn't match that member ID to this patient. Ask the caller "
            "to read the member ID from the card again and try once more, or offer to take a "
            "message so the front desk can check and call back.",
        }
    if outcome == stedi.RETRY:
        return {
            "state": "unavailable",
            "message": "The insurance company isn't answering right now. " + FRONT_DESK,
        }
    return {"state": "needs_staff", "message": "I couldn't complete that check. " + FRONT_DESK}


async def verify_insurance(session, attempt: CallAttempt, version, args, _evidence, env: ToolEnv):
    patient_ref = _verified(attempt)
    conn = await _connection(session, attempt)
    if await _count(session, attempt, (TOOL,)) >= MAX_CHECKS:
        raise Rejected("insurance_limit", "I've already checked insurance on this call.")
    allowed = set(version.config["guardrails"].get("allowed_disclosures", [])) & set(DISCLOSURES)
    if "coverage_status" not in allowed:
        raise Rejected("not_permitted", "I can't discuss insurance on this line. " + FRONT_DESK)
    settings = env.settings
    stedi_conn = await eligibility.clearinghouse(session, attempt.workspace_id)
    if stedi_conn is None:
        raise Rejected("no_clearinghouse", "Insurance checks aren't set up here. " + FRONT_DESK)
    test = is_test(stedi_conn)
    if attempt.sandbox and not test:
        raise Rejected(
            "test_call_no_payer",
            "This is a test call, so I can't contact a real insurance company.",
        )
    [patient] = await _records(session, conn, "patient", source_id=patient_ref) or [None]
    pii = PhiBox(settings).open(patient.pii if patient else None)
    kind = payers.plan_kind(args["payer_name"])
    client = stedi_client(
        stedi_conn, settings, env.transport, timeout_s=settings.stedi_voice_timeout_s
    )
    try:
        if test:
            # Test keys answer only Stedi's mock members: a tester can say a mock member ID
            # (AETNA12345, 1AA2CC3DD45 …); anything else uses the workspace's test scenario.
            scenario = stedi.mock_for_member(args["member_id"]) or await eligibility.mock_scenario(
                session, attempt.workspace_id, kind
            )
            request, payer_id, npi = (
                stedi.mock_request(scenario),
                stedi.MOCKS[scenario].payer_id,
                None,
            )
        else:
            npi = await eligibility.billing_npi(session, attempt.workspace_id, settings)
            mapping = await payers.resolve(
                session, client, attempt.workspace_id, ehr_payer_id=None, name=args["payer_name"]
            )
            known = all(pii.get(k) for k in ("first_name", "last_name", "date_of_birth"))
            if (
                npi is None
                or not known
                or mapping.state not in payers.SETTLED
                or not mapping.stedi_payer_id
            ):
                await events.append(
                    session, attempt, "insurance.check", "api", {"outcome": "payer_unmapped"}
                )
                return {
                    "state": "needs_staff",
                    "message": "I couldn't match that insurance company exactly. " + FRONT_DESK,
                }
            payer_id = mapping.stedi_payer_id
            try:
                request = stedi.patient_request(
                    payer_id=payer_id,
                    provider_npi=npi,
                    subscriber={
                        "first_name": pii.get("first_name"),
                        "last_name": pii.get("last_name"),
                        "date_of_birth": pii.get("date_of_birth"),
                        "member_id": args["member_id"],
                    },
                    pin=eligibility.provider_pin(mapping, PhiBox(settings)),
                )
            except stedi.InvalidMbi as err:
                return {"state": "invalid_member_id", "message": err.message}
        try:
            response, ms = await client.eligibility(request)
        except stedi.StediError as err:
            await audit(
                session,
                stedi_conn,
                "stedi.eligibility",
                "voice",
                ok=False,
                status=err.status,
                http_status=err.http_status,
                error=err.message,
                summary={"errors": [e["code"] for e in err.errors], "mock": test},
            )
            if npi:
                await payers.note_outcome(
                    session, attempt.workspace_id, npi, payer_id, err.errors, accepted=False
                )
            await events.append(
                session, attempt, "insurance.check", "api", {"outcome": err.outcome}
            )
            return _unanswered(err.outcome)
    finally:
        await client.aclose()
    summary = stedi.summarize_eligibility(response, payer_id=payer_id)
    await audit(
        session,
        stedi_conn,
        "stedi.eligibility",
        "voice",
        ok=summary["coverage"] != "error",
        status="success" if summary["coverage"] != "error" else "error",
        duration_ms=ms,
        summary={"coverage": summary["coverage"], "mock": test, "errors": summary["errors"]},
    )
    if npi:
        await payers.note_outcome(
            session,
            attempt.workspace_id,
            npi,
            payer_id,
            summary["error_details"],
            accepted=summary["coverage"] in eligibility.REUSABLE,
        )
    await events.append(
        session,
        attempt,
        "insurance.check",
        "api",
        {"outcome": summary["coverage"], "mock": test, "redirect": bool(summary["redirect"])},
    )
    if summary["coverage"] == "error":
        return _unanswered(summary["error_class"] or stedi.CORRECT)
    return caller_summary(summary, allowed, test)


HANDLERS = {TOOL: verify_insurance}
