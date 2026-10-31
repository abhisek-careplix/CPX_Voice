"""Da Vinci PAS (Prior Authorization Support) connector, aligned to STU 2 resource shapes.

- ``$inquire``: POST a Bundle (Claim use=preauthorization + Patient + Organizations) to
  ``Claim/$inquire``; the payer answers with a Bundle holding a ClaimResponse.
- Review outcomes come from the ``extension-reviewAction`` extension on item adjudications, coded
  with X12 306 action codes, and are mapped to CarePlix ``reported_status`` values.

Shapes follow the PAS IG; conformance is to be confirmed with the Inferno PAS test kit before any
real payer is connected (docs/solution/HEALTHCARE_INTEGRATION_LAYER.md §2).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any

IG_VERSION = "pas-stu2"
PAS = "http://hl7.org/fhir/us/davinci-pas"
REVIEW_ACTION_URL = f"{PAS}/StructureDefinition/extension-reviewAction"
REVIEW_ACTION_CODE_URL = f"{PAS}/StructureDefinition/extension-reviewActionCode"
X12_306 = "https://codesystem.x12.org/005010/306"
NPI = "http://hl7.org/fhir/sid/us-npi"
MEMBER_ID = "http://terminology.hl7.org/CodeSystem/v2-0203|MB"
CPT = "http://www.ama-assn.org/go/cpt"
TRACKING_SYSTEM = "urn:careplix:pa-tracking"

# X12 306 review action → (CarePlix reported_status, final determination?)
REVIEW_ACTIONS: dict[str, tuple[str, bool]] = {
    "A1": ("approved", True),  # certified in total
    "A2": ("approved", True),  # certified, partial
    "A3": ("denied", True),  # not certified
    "A4": ("pending_review", False),  # pended
    "A6": ("approved", True),  # modified
    "C": ("unknown", False),  # cancelled
    "CT": ("unknown", False),  # contact payer
    "D": ("unknown", False),  # deferred
}
DOCUMENTATION_HINTS = ("documentation", "additional information", "records", "notes")


def _ref(uid: str) -> dict[str, str]:
    return {"reference": f"urn:uuid:{uid}"}


def build_inquiry_bundle(case: dict[str, Any], tracking_id: str) -> dict[str, Any]:
    """PAS inquiry for one synthetic case (``fixtures/synthetic`` shape)."""
    ids = {
        k: str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tracking_id}:{k}"))
        for k in ("claim", "patient", "insurer", "provider")
    }
    patient = case["patient"]
    given, _, family = patient["display_name"].rpartition(" ")
    claim = {
        "resourceType": "Claim",
        "identifier": [{"system": TRACKING_SYSTEM, "value": tracking_id}],
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
        "patient": _ref(ids["patient"]),
        "created": case["request"]["submitted_on"],
        "insurer": _ref(ids["insurer"]),
        "provider": _ref(ids["provider"]),
        "priority": {
            "coding": [{"code": "stat" if case["request"]["urgency"] == "expedited" else "normal"}]
        },
        "item": [
            {
                "sequence": 1,
                "productOrService": {
                    "coding": [{"system": CPT, "code": case["service"]["cpt_code"]}]
                },
                "quantity": {"value": case["service"]["units"]},
            }
        ],
    }
    if known := case["request"].get("known_auth_reference"):
        claim["related"] = [{"reference": {"value": known}}]
    resources = [
        (ids["claim"], claim),
        (
            ids["patient"],
            {
                "resourceType": "Patient",
                "identifier": [
                    {
                        "type": {"coding": [{"system": MEMBER_ID.split("|")[0], "code": "MB"}]},
                        "value": patient["member_id"],
                    }
                ],
                "name": [{"family": family, "given": [given] if given else []}],
                "birthDate": patient["date_of_birth"],
            },
        ),
        (ids["insurer"], {"resourceType": "Organization", "name": case["payer"]["name"]}),
        (
            ids["provider"],
            {
                "resourceType": "Organization",
                "identifier": [{"system": NPI, "value": case["provider"]["npi"]}],
                "name": case["provider"]["name"],
            },
        ),
    ]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "identifier": {"system": TRACKING_SYSTEM, "value": tracking_id},
        "entry": [{"fullUrl": f"urn:uuid:{uid}", "resource": r} for uid, r in resources],
    }


@dataclass
class PasStatus:
    found: bool
    outcome: str | None = None  # ClaimResponse.outcome
    review_action: str | None = None  # X12 306 code
    reported_status: str = "not_found"
    final: bool = False
    authorization_reference: str | None = None
    reason: str | None = None
    synthetic: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_inquiry_response(bundle: dict[str, Any]) -> PasStatus:
    entries = [e.get("resource", {}) for e in bundle.get("entry", [])]
    synthetic = any(t.get("code") == "synthetic" for t in bundle.get("meta", {}).get("tag", []))
    response = next((r for r in entries if r.get("resourceType") == "ClaimResponse"), None)
    if response is None:
        return PasStatus(found=False, synthetic=synthetic)

    action, reason = None, None
    for item in response.get("item", []):
        for adjudication in item.get("adjudication", []):
            for ext in adjudication.get("extension", []):
                if ext.get("url") != REVIEW_ACTION_URL:
                    continue
                for sub in ext.get("extension", []):
                    if sub.get("url") == "number" and "valueString" in sub:
                        continue
                    concept = sub.get("valueCodeableConcept", {})
                    coding = (concept.get("coding") or [{}])[0]
                    if sub.get("url") == "reviewActionCode":
                        action = coding.get("code")
                    elif sub.get("url") == "reasonCode":
                        reason = coding.get("display") or concept.get("text")
    status, final = REVIEW_ACTIONS.get(action or "", ("unknown", False))
    if action == "A4" and reason and any(h in reason.lower() for h in DOCUMENTATION_HINTS):
        status = "pending_additional_information"
    return PasStatus(
        found=True,
        outcome=response.get("outcome"),
        review_action=action,
        reported_status=status,
        final=final,
        authorization_reference=response.get("preAuthRef"),
        reason=reason,
        synthetic=synthetic,
    )


def review_action_extension(code: str, reason: str | None = None) -> dict[str, Any]:
    """Build the reviewAction extension (used by the sandbox and tests)."""
    sub = [
        {
            "url": "reviewActionCode",
            "valueCodeableConcept": {"coding": [{"system": X12_306, "code": code}]},
        }
    ]
    if reason:
        sub.append(
            {
                "url": "reasonCode",
                "valueCodeableConcept": {"text": reason, "coding": [{"display": reason}]},
            }
        )
    return {"url": REVIEW_ACTION_URL, "extension": sub}
