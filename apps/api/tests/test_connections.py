"""Connections: LiveKit setup, route qualification and Gemini model test (LiveKit/Google faked)."""

import time

from fastapi.testclient import TestClient

from careplix_api.main import create_app
from careplix_common.gemini import ProbeResult

from .conftest import APPROVED, WORKER, FakeGateway, make_settings

INBOUND = "+12025550100"


async def ok_prober(project, location, model_id, credentials):
    return ProbeResult(
        model_id=model_id, location=location, ok=True, connect_ms=310, first_audio_ms=640
    )


async def failing_prober(project, location, model_id, credentials):
    return ProbeResult(model_id=model_id, location=location, error="PermissionDenied")


def _client(tmp_path, prober=ok_prober, **overrides):
    values = {
        "livekit_outbound_trunk_id": "",
        "approved_test_destinations": "",
        "gemini_live_model": "",
    }
    settings = make_settings(tmp_path, **{**values, **overrides})
    gateway = FakeGateway(settings)
    client = TestClient(create_app(settings, gateway, model_prober=prober))
    return client, gateway


def _checks(c):
    return {x["name"]: x for x in c.get("/api/readiness").json()["checks"]}


def _agent(c, template):
    return next(a for a in c.get("/api/agents").json() if a["template"]["id"] == template)


def test_overview_lists_project_without_secrets(tmp_path):
    client, gw = _client(tmp_path)
    gw.outbound_trunks = [
        {
            "id": "ST_a",
            "name": "carrier",
            "address": "sip.example.com",
            "numbers": [APPROVED],
            "has_auth": True,
        }
    ]
    with client as c:
        c.post("/api/auth/dev-login")
        body = c.get("/api/connections").json()
    assert body["livekit"]["configured"] is True
    assert body["livekit"]["inventory"]["outbound_trunks"][0]["id"] == "ST_a"
    assert "s" * 40 not in str(body)


def test_worker_probe_turns_agent_worker_ready(tmp_path):
    client, gw = _client(tmp_path)
    with client as c:
        c.post("/api/auth/dev-login")
        assert _checks(c)["agent_worker"]["status"] == "unverified"
        probe = c.post("/api/connections/agent-test").json()
        assert probe["status"] == "waiting"
        room = next(call[1] for call in gw.calls if call[0] == "probe")
        bad = c.post(
            f"/internal/probes/{probe['id']}/ack",
            json={"room": "other", "runtime_build": "t"},
            headers=WORKER,
        )
        assert bad.status_code == 404
        c.post(
            f"/internal/probes/{probe['id']}/ack",
            json={"room": room, "runtime_build": "t"},
            headers=WORKER,
        ).raise_for_status()
        assert c.get(f"/api/connections/agent-test/{probe['id']}").json()["status"] == "answered"
        assert _checks(c)["agent_worker"]["status"] == "ready"


def test_model_selection_and_live_test(tmp_path):
    client, _ = _client(tmp_path)
    with client as c:
        c.post("/api/auth/dev-login")
        assert _checks(c)["voice_model"]["status"] == "blocked"
        c.put(
            "/api/connections/model",
            json={
                "model_id": "gemini-live-2.5-flash-native-audio",
                "gcp_project": "careplix-dev",
                "vertex_location": "us-central1",
            },
        ).raise_for_status()
        assert _checks(c)["voice_model"]["status"] == "unverified"
        result = c.post("/api/connections/model/test").json()
        assert result["ok"] and result["first_audio_ms"] == 640
        assert _checks(c)["voice_model"]["status"] == "ready"
        # The Studio-approved model now gates publishing (no env value needed).
        agent = _agent(c, "auth-status")
        cfg = c.get(f"/api/agents/{agent['id']}").json()["draft_config"]
        cfg["model"]["model_id"] = "gemini-live-2.5-flash-native-audio"
        c.put(f"/api/agents/{agent['id']}/draft", json={"config": cfg})
        assert c.post(f"/api/agents/{agent['id']}/publish", json={}).status_code == 200


def test_failed_model_test_blocks(tmp_path):
    client, _ = _client(tmp_path, prober=failing_prober)
    with client as c:
        c.post("/api/auth/dev-login")
        c.put(
            "/api/connections/model",
            json={
                "model_id": "gemini-x",
                "gcp_project": "careplix-dev",
                "vertex_location": "us-central1",
            },
        )
        assert c.post("/api/connections/model/test").json()["error"] == "PermissionDenied"
        check = _checks(c)["voice_model"]
        assert check["status"] == "blocked" and check["detail"]["error"] == "PermissionDenied"


def test_inbound_number_connect_call_and_qualify(tmp_path):
    client, gw = _client(tmp_path, gemini_live_model="gemini-test")
    with client as c:
        c.post("/api/auth/dev-login")
        agent = _agent(c, "inbound-enquiry")
        c.post(f"/api/agents/{agent['id']}/publish", json={}).raise_for_status()
        route = c.post(
            "/api/connections/inbound",
            json={"name": "Auth line", "number": INBOUND, "agent_id": agent["id"]},
        ).json()
        assert route["dispatch_rule_id"] == "SDR_1" and route["reused"] is False
        assert route["scope"] == "project"
        # A second number reuses the project-wide rule instead of adding a conflicting one.
        second = c.post(
            "/api/connections/inbound",
            json={"name": "Second", "number": "+12025550101", "agent_id": agent["id"]},
        ).json()
        assert second["dispatch_rule_id"] == "SDR_1" and second["reused"] is True
        assert len(gw.dispatch_rules) == 1
        assert _checks(c)["inbound_route"]["status"] == "unverified"
        # Qualification needs a real completed call on the route.
        assert c.post(f"/api/connections/inbound/{route['id']}/qualify", json={}).status_code == 409

        claim = c.post(
            "/internal/inbound/claim",
            json={
                "room": "inb_x",
                "job_id": "AJ",
                "runtime_build": "t",
                "called_number": INBOUND,
                "caller_number": "+13125550111",
            },
            headers=WORKER,
        ).json()
        auth = {"Authorization": f"Bearer {claim['attempt_token']}"}
        assert claim["brief"]["direction"] == "inbound" and claim["brief"]["context"] == {}
        verify = c.post(
            f"/internal/attempts/{claim['attempt_id']}/tools/verify_caller",
            json={
                "call_id": "v1",
                "arguments": {"member_id": "000000000", "date_of_birth": "1990-01-01"},
            },
            headers=auth,
        ).json()
        assert verify["result"] == {"verified": False}
        c.post(
            f"/internal/attempts/{claim['attempt_id']}/events",
            json={
                "events": [
                    {
                        "event_id": "d",
                        "type": "worker.done",
                        "payload": {"reason": "objective_complete"},
                    }
                ]
            },
            headers=auth,
        )
        c.post(f"/api/connections/inbound/{route['id']}/qualify", json={}).raise_for_status()
        assert _checks(c)["inbound_route"]["status"] == "ready"


def test_outbound_trunk_test_call_and_qualify(tmp_path):
    client, gw = _client(tmp_path, gemini_live_model="gemini-test")
    with client as c:
        c.post("/api/auth/dev-login")
        assert "inbound-only" in _checks(c)["outbound_route"]["message"]
        trunk = c.post(
            "/api/connections/outbound/trunks",
            json={
                "name": "Carrier",
                "address": "careplix.pstn.example.com",
                "number": "+1 202 555 0199",
                "username": "u",
                "password": "sip-secret",
            },
        ).json()
        assert trunk["trunk_id"] == "ST_new"
        # The SIP password went to LiveKit and nowhere else.
        assert "sip-secret" not in str(c.get("/api/connections").json())
        assert _checks(c)["outbound_route"]["status"] == "unverified"

        agent = _agent(c, "auth-status")
        c.post(f"/api/agents/{agent['id']}/publish", json={}).raise_for_status()
        denied = c.post(
            "/api/connections/outbound/test-call",
            json={"agent_id": agent["id"], "destination": APPROVED},
        )
        assert denied.json()["error"]["code"] == "destination_not_approved"
        c.put(
            "/api/connections/outbound", json={"approved_destinations": [APPROVED]}
        ).raise_for_status()
        attempt_id = c.post(
            "/api/connections/outbound/test-call",
            json={"agent_id": agent["id"], "destination": APPROVED},
        ).json()["attempt_id"]

        for _ in range(40):
            if c.get(f"/api/calls/{attempt_id}").json()["state"] == "Queued":
                break
            time.sleep(0.05)
        claim = c.post(
            f"/internal/attempts/{attempt_id}/claim",
            json={
                "room": f"att_{attempt_id.replace('-', '')}",
                "job_id": "AJ",
                "runtime_build": "t",
            },
            headers=WORKER,
        ).json()
        for _ in range(60):
            if c.get(f"/api/calls/{attempt_id}").json()["state"] == "Connected":
                break
            time.sleep(0.05)
        assert ("dial", f"att_{attempt_id.replace('-', '')}", APPROVED) in gw.calls
        auth = {"Authorization": f"Bearer {claim['attempt_token']}"}
        assert (
            c.post("/api/connections/outbound/qualify", json={"attempt_id": attempt_id}).status_code
            == 409
        )
        c.post(
            f"/internal/attempts/{attempt_id}/events",
            json={
                "events": [
                    {
                        "event_id": "d",
                        "type": "worker.done",
                        "payload": {"reason": "objective_complete"},
                    }
                ]
            },
            headers=auth,
        )
        c.post(
            "/api/connections/outbound/qualify", json={"attempt_id": attempt_id}
        ).raise_for_status()
        assert _checks(c)["outbound_route"]["status"] == "ready"
        # Choosing another trunk drops the qualification.
        gw.outbound_trunks.append(
            {"id": "ST_other", "name": "x", "address": "y", "numbers": [], "has_auth": False}
        )
        c.put("/api/connections/outbound", json={"trunk_id": "ST_other"}).raise_for_status()
        assert _checks(c)["outbound_route"]["status"] == "unverified"


def _inbound_setup(c):
    agent = _agent(c, "inbound-enquiry")
    c.post(f"/api/agents/{agent['id']}/publish", json={}).raise_for_status()
    return c.post(
        "/api/connections/inbound",
        json={"name": "Auth line", "number": INBOUND, "agent_id": agent["id"]},
    ).json()


def _claim_inbound(c, room, **signals):
    body = {"room": room, "job_id": "AJ", "runtime_build": "t", **signals}
    return c.post("/internal/inbound/claim", json=body, headers=WORKER)


def _end(c, claim):
    auth = {"Authorization": f"Bearer {claim['attempt_token']}"}
    done = [{"event_id": "d", "type": "worker.done", "payload": {"reason": "x"}}]
    c.post(f"/internal/attempts/{claim['attempt_id']}/events", json={"events": done}, headers=auth)


def test_inbound_routes_by_rule_when_livekit_omits_called_number(tmp_path):
    """Regression: LiveKit numbers may not report sip.trunkPhoneNumber (call rejected as busy)."""
    client, _ = _client(tmp_path, gemini_live_model="gemini-test")
    with client as c:
        c.post("/api/auth/dev-login")
        route = _inbound_setup(c)
        ok = _claim_inbound(c, "inb_1", route_id=route["id"], rule_id=route["dispatch_rule_id"])
        assert ok.status_code == 200, ok.text
        _end(c, ok.json())


def test_inbound_number_matches_in_any_format(tmp_path):
    client, _ = _client(tmp_path, gemini_live_model="gemini-test")
    with client as c:
        c.post("/api/auth/dev-login")
        _inbound_setup(c)
        ok = _claim_inbound(c, "inb_2", called_number="12025550100")
        assert ok.status_code == 200, ok.text


def test_inbound_rejects_forged_or_conflicting_signals(tmp_path):
    client, _ = _client(tmp_path, gemini_live_model="gemini-test")
    with client as c:
        c.post("/api/auth/dev-login")
        route = _inbound_setup(c)
        # A route ID only counts with the dispatch rule LiveKit actually matched.
        forged = _claim_inbound(c, "inb_3", route_id=route["id"], rule_id="SDR_other")
        assert forged.json()["error"]["code"] == "no_route"
        assert _claim_inbound(c, "inb_4").json()["error"]["code"] == "no_route"
        # A call to another number in the project hits the project-wide rule, but the reported
        # called number decides: no route, so it is not answered.
        other = _claim_inbound(
            c,
            "inb_5",
            called_number="+13125550000",
            route_id=route["id"],
            rule_id=route["dispatch_rule_id"],
        )
        assert other.json()["error"]["code"] == "no_route"
