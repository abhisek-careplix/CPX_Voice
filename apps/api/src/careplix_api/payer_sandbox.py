"""CarePlix Payer Sandbox: a synthetic payer exposing Da Vinci PAS operations over FHIR R4.

    uvicorn careplix_api.payer_sandbox:app --port 8091      # make payer-sandbox

Stands in for a payer's CMS-0057-F Prior Authorization API until real endpoints (or the Inferno
PAS simulator) are available. It matches inquiries by member ID + CPT against the synthetic cases
and answers from ``fixtures/synthetic/payer_sandbox.json``. Every response is tagged synthetic.
No real payer data, no authentication: never expose this outside development.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from careplix_api.integrations.pas import (
    CPT,
    IG_VERSION,
    PAS,
    TRACKING_SYSTEM,
    review_action_extension,
)

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "synthetic"
SYNTHETIC_TAG = {"system": "urn:careplix:tag", "code": "synthetic", "display": "Synthetic data"}
FHIR_JSON = "application/fhir+json"


def _load() -> tuple[dict[tuple[str, str], str], dict[str, Any]]:
    by_member_cpt = {}
    for path in sorted((FIXTURES / "cases").glob("*.json")):
        case = json.loads(path.read_text())
        by_member_cpt[(case["patient"]["member_id"], case["service"]["cpt_code"])] = case["case_id"]
    decisions = json.loads((FIXTURES / "payer_sandbox.json").read_text())["decisions"]
    return by_member_cpt, decisions


CASES, DECISIONS = _load()
SUBMISSIONS: dict[str, dict[str, Any]] = {}  # in-memory, keyed by our tracking identifier

app = FastAPI(title="CarePlix Payer Sandbox (synthetic)", version="0.1.0")


def _fhir(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=FHIR_JSON)


def _bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": {"tag": [SYNTHETIC_TAG]},
        "timestamp": datetime.now(UTC).isoformat(),
        "entry": [{"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": r} for r in resources],
    }


def _claim_and_keys(bundle: dict[str, Any]) -> tuple[dict, str | None, str | None, str | None]:
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])]
    claim = next((r for r in resources if r.get("resourceType") == "Claim"), {})
    patient = next((r for r in resources if r.get("resourceType") == "Patient"), {})
    member = next((i.get("value") for i in patient.get("identifier", [])), None)
    cpt = next(
        (
            c.get("code")
            for item in claim.get("item", [])
            for c in item.get("productOrService", {}).get("coding", [])
            if c.get("system") == CPT
        ),
        None,
    )
    tracking = next(
        (i.get("value") for i in claim.get("identifier", []) if i.get("system") == TRACKING_SYSTEM),
        None,
    )
    return claim, member, cpt, tracking


def _claim_response(decision: dict[str, Any], tracking: str | None) -> dict[str, Any]:
    final = decision["action"] in {"A1", "A2", "A3", "A6"}
    response = {
        "resourceType": "ClaimResponse",
        "meta": {"tag": [SYNTHETIC_TAG]},
        "status": "active",
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                    "code": "professional",
                }
            ]
        },
        "use": "preauthorization",
        "created": datetime.now(UTC).isoformat(),
        "outcome": "complete" if final else "queued",
        "item": [
            {
                "itemSequence": 1,
                "adjudication": [
                    {
                        "category": {"coding": [{"code": "submitted"}]},
                        "extension": [
                            review_action_extension(decision["action"], decision.get("reason"))
                        ],
                    }
                ],
            }
        ],
    }
    if decision.get("ref"):
        response["preAuthRef"] = decision["ref"]
    if decision.get("valid_from"):
        response["preAuthPeriod"] = {
            "start": decision["valid_from"],
            "end": decision.get("valid_to"),
        }
    if tracking:
        response["identifier"] = [{"system": TRACKING_SYSTEM, "value": tracking}]
    return response


@app.get("/fhir/metadata")
async def metadata():
    return _fhir(
        {
            "resourceType": "CapabilityStatement",
            "meta": {"tag": [SYNTHETIC_TAG]},
            "status": "active",
            "kind": "instance",
            "date": "2026-10-31",
            "fhirVersion": "4.0.1",
            "format": ["json"],
            "software": {"name": "CarePlix Payer Sandbox (synthetic)", "version": "0.1.0"},
            "implementationGuide": [f"{PAS}/ImplementationGuide/hl7.fhir.us.davinci-pas"],
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": "Claim",
                            "operation": [
                                {
                                    "name": "submit",
                                    "definition": f"{PAS}/OperationDefinition/Claim-submit",
                                },
                                {
                                    "name": "inquire",
                                    "definition": f"{PAS}/OperationDefinition/Claim-inquiry",
                                },
                            ],
                        }
                    ],
                }
            ],
            "extension": [{"url": "urn:careplix:ig-version", "valueString": IG_VERSION}],
        }
    )


@app.post("/fhir/Claim/$inquire")
async def inquire(request: Request):
    _, member, cpt, tracking = _claim_and_keys(await request.json())
    if tracking in SUBMISSIONS:
        return _fhir(_bundle([SUBMISSIONS[tracking]]))
    case_id = CASES.get((member or "", cpt or ""))
    decision = DECISIONS.get(case_id) if case_id else None
    if not decision:
        # PAS: an inquiry that matches nothing returns no ClaimResponse.
        return _fhir(_bundle([]))
    return _fhir(_bundle([_claim_response(decision, tracking)]))


@app.post("/fhir/Claim/$submit")
async def submit(request: Request):
    claim, _, _, tracking = _claim_and_keys(await request.json())
    if not claim or not tracking:
        return _fhir(
            {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "required"}],
            },
            400,
        )
    if tracking not in SUBMISSIONS:  # idempotent on our tracking identifier
        ref = f"SYN-PA-S{int(hashlib.sha256(tracking.encode()).hexdigest(), 16) % 100000:05d}"
        SUBMISSIONS[tracking] = _claim_response(
            {"action": "A4", "reason": "Pended for medical review", "ref": ref}, tracking
        )
    return _fhir(_bundle([SUBMISSIONS[tracking]]))
