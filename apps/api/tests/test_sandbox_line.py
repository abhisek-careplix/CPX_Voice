"""Shared sandbox line for trials: inbound by workspace code, outbound with daily limits."""

import pytest
from fastapi.testclient import TestClient

from careplix_api.main import create_app
from careplix_api.models import CallAttempt

from .conftest import WORKER, FakeGateway, make_settings
from .test_practice import _db_rows

SANDBOX = "+13127558589"


@pytest.fixture
def sclient(tmp_path):
    settings = make_settings(
        tmp_path,
        run_background=False,
        sandbox_number=SANDBOX,
        sandbox_outbound_trunk_id="ST_sandbox",
        sandbox_daily_calls=2,
        sandbox_max_destinations=2,
    )
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _receptionist(c):
    agent = next(a for a in c.get("/api/agents").json() if a["template"]["direction"] == "inbound")
    c.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "v1"}).raise_for_status()
    return agent


def _inbound(c, code=None, n=1):
    body = {
        "room": f"inb_sandbox_{n}",
        "job_id": f"AJ_{n}",
        "runtime_build": "test",
        "called_number": SANDBOX,
        "caller_number": "+15551230000",
        "workspace_code": code,
    }
    return c.post("/internal/inbound/claim", json=body, headers=WORKER)


def test_each_workspace_gets_a_code_for_the_shared_number(sclient):
    sandbox = sclient.get("/api/connections").json()["sandbox"]
    assert sandbox["available"] and sandbox["number"] == SANDBOX
    assert len(sandbox["code"]) == 6 and sandbox["code"].isdigit()
    assert sclient.get("/api/connections").json()["sandbox"]["code"] == sandbox["code"]  # stable
    fresh = sclient.put("/api/connections/sandbox", json={"new_code": True}).json()
    assert fresh["code"] != sandbox["code"]


def test_a_sandbox_call_needs_the_code_then_reaches_the_workspace_agent(sclient):
    agent = _receptionist(sclient)
    code = sclient.get("/api/connections").json()["sandbox"]["code"]
    assert _inbound(sclient).json()["error"]["code"] == "sandbox_code_required"
    assert _inbound(sclient, "000000").json()["error"]["code"] in (
        "sandbox_code_invalid",
        "sandbox_no_agent",
    )
    assert _inbound(sclient, code).json()["error"]["code"] == "sandbox_no_agent"

    sclient.put("/api/connections/sandbox", json={"agent_id": agent["id"]}).raise_for_status()
    claim = _inbound(sclient, code)
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["brief"]["direction"] == "inbound"
    assert payload["max_duration_s"] <= 300  # the sandbox line caps talk time
    [attempt] = [a for a in _db_rows(sclient, CallAttempt) if a.id == payload["attempt_id"]]
    assert attempt.pins["sandbox_line"] is True and attempt.route_id is None
    assert sclient.get("/api/connections").json()["sandbox"]["calls_today"] == 1


def test_the_sandbox_agent_must_answer_inbound_calls(sclient):
    outbound = next(
        a for a in sclient.get("/api/agents").json() if a["template"]["direction"] == "outbound"
    )
    r = sclient.put("/api/connections/sandbox", json={"agent_id": outbound["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "agent_not_inbound"


def test_daily_allowance_covers_inbound_sandbox_calls(sclient):
    agent = _receptionist(sclient)
    sclient.put("/api/connections/sandbox", json={"agent_id": agent["id"]}).raise_for_status()
    code = sclient.get("/api/connections").json()["sandbox"]["code"]
    for n in (1, 2):
        claim = _inbound(sclient, code, n)
        assert claim.status_code == 200, claim.text
        # end it so the one-call limit doesn't decide the next one
        token = claim.json()["attempt_token"]
        sclient.post(
            f"/internal/attempts/{claim.json()['attempt_id']}/events",
            json={
                "events": [
                    {"event_id": f"e{n}", "type": "worker.done", "payload": {"reason": "done"}}
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    third = _inbound(sclient, code, 3)
    assert third.status_code == 429 and third.json()["error"]["code"] == "sandbox_limit"


def test_outbound_on_the_sandbox_line_uses_its_trunk_and_few_destinations(sclient):
    sclient.put(
        "/api/connections/outbound", json={"approved_destinations": ["+15551230001"]}
    ).raise_for_status()
    on = sclient.put("/api/connections/sandbox", json={"outbound": True}).json()
    assert on["outbound"] is True
    overview = sclient.get("/api/connections").json()
    assert overview["outbound"]["trunk_id"] == "ST_sandbox" and overview["outbound"]["verified"]
    too_many = sclient.put(
        "/api/connections/outbound",
        json={"approved_destinations": ["+15551230001", "+15551230002", "+15551230003"]},
    )
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "sandbox_destinations"


def test_regular_inbound_numbers_are_unchanged(sclient):
    body = {
        "room": "inb_other",
        "job_id": "AJ_x",
        "runtime_build": "test",
        "called_number": "+15559990000",
    }
    r = sclient.post("/internal/inbound/claim", json=body, headers=WORKER)
    assert r.json()["error"]["code"] == "no_route"
