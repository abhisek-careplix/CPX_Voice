"""Healthcare integration layer: PAS client against the CarePlix Payer Sandbox (in-process)."""

import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from careplix_api import payer_sandbox
from careplix_api.integrations import pas
from careplix_api.integrations.fhir import AuthConfig, FhirClient
from careplix_api.main import create_app

from .conftest import APPROVED, WORKER, FakeGateway, make_settings

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "synthetic"


def _case(case_id: str) -> dict:
    return json.loads((FIXTURES / "cases" / f"{case_id}.json").read_text())


ICD = {
    "M54.50": [["M54.50", "Low back pain, unspecified"]],
    "G47.3": [
        ["G47.30", "Sleep apnea, unspecified"],
        ["G47.33", "Obstructive sleep apnea (adult) (pediatric)"],
    ],
}


def _public_apis(request: httpx.Request) -> httpx.Response:
    """Recorded shapes of the public reference APIs (verified live on 31 Oct 2026)."""
    host, path, q = request.url.host, request.url.path, request.url.params
    if host == "clinicaltables.nlm.nih.gov":
        rows = ICD.get(q.get("terms"), [])
        return httpx.Response(200, json=[len(rows), [r[0] for r in rows], None, rows])
    if host == "api.coverage.cms.gov":
        if "national" in path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "document_id": 1,
                            "document_display_id": "240.4",
                            "title": "Continuous Positive Airway Pressure (CPAP) Therapy "
                            "for Obstructive Sleep Apnea",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "document_id": 33718,
                        "document_display_id": "L33718",
                        "title": "Polysomnography and Sleep Testing",
                        "contractor_name_type": "Example MAC",
                        "note": "",
                    },
                    {
                        "document_id": 34049,
                        "document_display_id": "L34049",
                        "title": "Outpatient Physical and Occupational Therapy Services",
                        "contractor_name_type": "Example MAC",
                        "note": "",
                    },
                ]
            },
        )
    if host == "npiregistry.cms.hhs.gov":
        return httpx.Response(200, json={"result_count": 0, "results": []})
    return httpx.Response(404)


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.sandbox = httpx.ASGITransport(app=payer_sandbox.app)
        self.public = httpx.MockTransport(_public_apis)

    async def handle_async_request(self, request):
        target = self.sandbox if request.url.host == "sandbox.test" else self.public
        return await target.handle_async_request(request)


def _client(tmp_path):
    settings = make_settings(tmp_path, payer_sandbox_url="http://sandbox.test/fhir")
    return TestClient(
        create_app(settings, FakeGateway(settings), integration_transport=RoutingTransport())
    )


def _run_worker(c):
    return c.portal.call(c.app.state.integration_worker.run_once)


def _task(c, case_ref):
    agent = next(a for a in c.get("/api/agents").json() if a["template"]["id"] == "auth-status")
    if not agent["current"]:
        c.post(f"/api/agents/{agent['id']}/publish", json={}).raise_for_status()
    case = next(x for x in c.get("/api/cases").json() if x["case_ref"] == case_ref)
    response = c.post(
        "/api/tasks", json={"agent_id": agent["id"], "case_id": case["id"], "destination": APPROVED}
    )
    response.raise_for_status()
    return response.json()


# --- PAS mapping --------------------------------------------------------------------------------


def test_inquiry_bundle_has_pas_shape():
    bundle = pas.build_inquiry_bundle(_case("SYN-CASE-0001"), "SYN-CASE-0001:t1")
    kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert kinds == ["Claim", "Patient", "Organization", "Organization"]
    claim = bundle["entry"][0]["resource"]
    assert claim["use"] == "preauthorization"
    assert claim["item"][0]["productOrService"]["coding"][0] == {"system": pas.CPT, "code": "97110"}
    assert all(e["fullUrl"].startswith("urn:uuid:") for e in bundle["entry"])


@pytest.mark.parametrize(
    ("action", "reason", "expected", "final"),
    [
        ("A1", None, "approved", True),
        ("A3", "Coverage criteria not met", "denied", True),
        ("A4", "Pended for medical review", "pending_review", False),
        ("A4", "Additional documentation required: notes", "pending_additional_information", False),
        ("A6", "Approved with modified units", "approved", True),
        ("CT", "Contact payer", "unknown", False),
    ],
)
def test_review_actions_map_to_statuses(action, reason, expected, final):
    response = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "ClaimResponse",
                    "outcome": "complete",
                    "preAuthRef": "REF1",
                    "item": [
                        {
                            "adjudication": [
                                {"extension": [pas.review_action_extension(action, reason)]}
                            ]
                        }
                    ],
                }
            }
        ],
    }
    status = pas.parse_inquiry_response(response)
    assert (status.reported_status, status.final, status.review_action) == (expected, final, action)


def test_no_claim_response_means_not_found():
    assert (
        pas.parse_inquiry_response({"resourceType": "Bundle", "entry": []}).reported_status
        == "not_found"
    )


# --- SMART Backend Services ---------------------------------------------------------------------


async def test_smart_backend_signs_assertion_and_uses_token(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    monkeypatch.setenv("PAYER_TEST_KEY", pem.decode())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            claims = jwt.decode(
                form["client_assertion"],
                key.public_key(),
                algorithms=["RS384"],
                audience="https://payer.test/token",
            )
            seen["iss"] = claims["iss"]
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 300})
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"resourceType": "CapabilityStatement"})

    auth = AuthConfig(
        type="smart_backend",
        client_id="careplix",
        token_url="https://payer.test/token",
        key_env="PAYER_TEST_KEY",
    )
    client = FhirClient("https://payer.test/fhir", auth, transport=httpx.MockTransport(handler))
    await client.metadata()
    await client.aclose()
    assert seen == {"iss": "careplix", "auth": "Bearer tok-123"}
    assert auth._expires_at > time.time()


# --- End to end with the sandbox ----------------------------------------------------------------


def test_dev_connection_verifies_against_sandbox(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        conn = next(
            x for x in c.get("/api/integrations").json()["connections"] if x["kind"] == "pas"
        )
        assert conn["synthetic"] and conn["verified_at"] is None
        result = c.post(f"/api/integrations/payers/{conn['id']}/verify").json()
        assert result["ok"], result
        assert result["capability"]["operations"] == ["inquire", "submit"]
        assert result["test_inquiry"]["synthetic"] is True
        calls = c.get("/api/integrations").json()["recent_calls"]
        assert {x["operation"] for x in calls} == {"metadata", "pas.inquire"}
        # The audit keeps outcomes, never patient details.
        assert "Synthetic Patient" not in json.dumps(calls)


def test_pre_call_check_runs_api_first(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        task = _task(c, "SYN-CASE-0002")
        assert task["electronic_check"]["pre_call"]["state"] == "pending"
        assert _run_worker(c) == 2  # readiness + payer pre-call check
        result = c.get(f"/api/tasks/{task['id']}/result").json()["task"]["electronic_check"][
            "pre_call"
        ]
        assert result["state"] == "done"
        assert result["status"]["reported_status"] == "approved"
        assert result["status"]["final"] is True
        assert result["status"]["authorization_reference"] == "SYN-PA-73988"


def test_unknown_case_is_not_found(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        task = _task(c, "SYN-CASE-0008")
        _run_worker(c)
        status = c.get(f"/api/tasks/{task['id']}/result").json()["task"]["electronic_check"][
            "pre_call"
        ]["status"]
        assert status["found"] is False and status["reported_status"] == "not_found"


def _call_and_record(c, task, findings):
    attempt = c.post(
        f"/api/tasks/{task['id']}/calls", headers={"Idempotency-Key": task["id"]}
    ).json()
    for _ in range(40):
        if c.get(f"/api/calls/{attempt['id']}").json()["state"] == "Queued":
            break
        time.sleep(0.05)
    claim = c.post(
        f"/internal/attempts/{attempt['id']}/claim",
        json={
            "room": f"att_{attempt['id'].replace('-', '')}",
            "job_id": "AJ",
            "runtime_build": "t",
        },
        headers=WORKER,
    ).json()
    for _ in range(60):
        if c.get(f"/api/calls/{attempt['id']}").json()["state"] == "Connected":
            break
        time.sleep(0.05)
    auth = {"Authorization": f"Bearer {claim['attempt_token']}"}
    base = f"/internal/attempts/{attempt['id']}"
    for i, (field, value) in enumerate(findings.items()):
        c.post(
            f"{base}/tools/record_finding",
            json={
                "call_id": f"f{i}",
                "arguments": {
                    "field": field,
                    "value": value,
                    "confirmation": "read_back_confirmed",
                },
            },
            headers=auth,
        ).raise_for_status()
    done = [
        {"event_id": "done", "type": "worker.done", "payload": {"reason": "objective_complete"}}
    ]
    c.post(f"{base}/events", json={"events": done}, headers=auth).raise_for_status()


def test_post_call_validation_confirms_matching_findings(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        task = _task(c, "SYN-CASE-0001")
        _run_worker(c)
        _call_and_record(
            c,
            task,
            {
                "reported_status": "pending_additional_information",
                "authorization_reference": "SYN PA 73915",
            },
        )
        _run_worker(c)
        result = c.get(f"/api/tasks/{task['id']}/result").json()
        assert result["task"]["electronic_check"]["post_call"]["comparison"] == {
            "reported_status": "match",
            "authorization_reference": "match",
        }
        system = [f for f in result["findings"] if f["source"] == "system"]
        assert {f["field"]: f["status"] for f in system} == {
            "reported_status": "system_verified",
            "authorization_reference": "system_verified",
        }
        assert all(f["integration_call_id"] for f in system)


def test_post_call_validation_flags_conflicts_for_review(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        task = _task(c, "SYN-CASE-0001")
        _run_worker(c)
        _call_and_record(
            c, task, {"reported_status": "approved", "authorization_reference": "SYN-PA-11111"}
        )
        _run_worker(c)
        result = c.get(f"/api/tasks/{task['id']}/result").json()
        assert (
            result["task"]["electronic_check"]["post_call"]["comparison"]["reported_status"]
            == "conflict"
        )
        heard = next(
            f
            for f in result["findings"]
            if f["source"] == "call" and f["field"] == "reported_status"
        )
        assert heard["status"] == "conflicting"
        assert heard["revisions"][0]["actor"] == "system:pas"
        assert "payer system disagrees" in result["task"]["next_action"].lower()


def test_readiness_validates_case_against_public_systems(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        kinds = {x["kind"] for x in c.get("/api/integrations").json()["connections"]}
        assert {"npi", "icd10", "hcpcs", "cms_coverage", "pas"} <= kinds
        task = _task(c, "SYN-CASE-0004")
        _run_worker(c)
        result = c.get(f"/api/tasks/{task['id']}/result").json()["task"]
        checks = {x["capability"]: x for x in result["electronic_check"]["readiness"]["checks"]}
        assert checks["provider.npi.validate"]["status"] == "skipped"  # synthetic NPI range
        dx = checks["code.icd10.validate"]
        assert dx["status"] == "invalid" and "G47.33" in dx["detail"]["suggestions"]
        assert checks["code.hcpcs.validate"]["status"] == "skipped"  # CPT is AMA-licensed
        policies = checks["coverage.policy.lookup"]["detail"]["policies"]
        assert {p["id"] for p in policies} == {"240.4", "L33718"}  # CPAP NCD + sleep testing LCD
        assert "G47.3" in result["next_action"]


def test_reference_connections_verify_live_shapes(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/auth/dev-login")
        conns = {x["kind"]: x for x in c.get("/api/integrations").json()["connections"]}
        icd = c.post(f"/api/integrations/payers/{conns['icd10']['id']}/verify").json()
        assert icd["ok"] and icd["test"]["detail"]["display"] == "Low back pain, unspecified"
