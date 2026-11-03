"""Voice Studio VS1: scenarios, the errand agent, rehearsals, live runs and scorecards."""

from careplix_api.models import Scenario

from .conftest import APPROVED, WORKER
from .test_lifecycle import _claim
from .test_practice import _db_rows


def _examples(client):
    added = client.post("/api/scenarios/examples")
    assert added.status_code == 200, added.text
    return client.get("/api/scenarios").json()["scenarios"]


def _flight(client):
    return next(s for s in _examples(client) if s["category"] == "travel")


def test_example_library_creates_a_published_errand_agent(client):
    listed = _examples(client)
    assert len(listed) == 14  # the Head-of-RCM top 5 as 13 scenarios, plus the airline showcase
    assert {s["category"] for s in listed} == {
        "eligibility",
        "claim_status",
        "denials",
        "prior_auth",
        "patient_billing",
        "travel",
    }
    assert all(s["facts"] is None for s in listed)  # the list never carries facts
    agent = next(a for a in client.get("/api/agents").json() if a["template"]["id"] == "errand")
    assert agent["current"] is not None  # ready to run
    assert client.post("/api/scenarios/examples").json()["added"] == 0  # idempotent
    # Facts are sealed at rest.
    [row] = [s for s in _db_rows(client, Scenario) if s.category == "travel"]
    assert "QX7P2R" not in (row.facts or "")


def test_rehearsal_brief_is_the_scenario(client):
    flight = _flight(client)
    run = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"})
    assert run.status_code == 200, run.text
    payload = _claim(client, run.json()["attempt_id"])
    brief = payload["brief"]
    assert "earliest flight the next day" in brief["objective"]
    assert "Do not pay or accept any change fee" in brief["objective"]
    assert brief["context"]["confirmation_code"] == "QX7P2R"
    assert brief["authority"]["on_behalf_of"].startswith("Sam Example")
    assert "confirmation_code" in brief["authority"]["disclose_allowlist"]
    assert {t["name"] for t in payload["tools"]} == {
        "set_call_state",
        "send_dtmf",
        "record_finding",
        "end_call",
    }


def test_live_run_needs_an_approved_number(client):
    flight = _flight(client)
    refused = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "live"})
    assert refused.json()["error"]["code"] == "no_destination"
    detail = client.get(f"/api/scenarios/{flight['id']}").json()
    body = {k: detail[k] for k in ("name", "category", "agent_id", "counterpart", "on_behalf_of")}
    body |= {k: detail[k] for k in ("goal", "facts", "limits", "success_checks")}
    client.put(
        f"/api/scenarios/{flight['id']}", json={**body, "destination": "+1 312 555 0100"}
    ).raise_for_status()
    refused = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "live"})
    assert refused.json()["error"]["code"] == "destination_not_approved"
    client.put(
        f"/api/scenarios/{flight['id']}", json={**body, "destination": APPROVED}
    ).raise_for_status()
    live = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "live"})
    assert live.status_code == 200, live.text
    runs = client.get(f"/api/scenarios/{flight['id']}").json()["runs"]
    assert runs[0]["attempt_id"] == live.json()["attempt_id"] and runs[0]["mode"] == "live"


def test_scorecard_counts_what_the_call_recorded(client):
    flight = _flight(client)
    run = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"})
    attempt_id = run.json()["attempt_id"]
    payload = _claim(client, attempt_id)
    auth = {"Authorization": f"Bearer {payload['attempt_token']}"}
    base = f"/internal/attempts/{attempt_id}"

    def tool(name, call_id, **arguments):
        r = client.post(
            f"{base}/tools/{name}", json={"call_id": call_id, "arguments": arguments}, headers=auth
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "succeeded", r.json()
        return r.json()

    tool("set_call_state", "s1", substate="ivr", reason="test")
    tool("send_dtmf", "d1", digits="2", reason="changes to an existing booking")
    tool("set_call_state", "s2", substate="hold", reason="test")
    client.post(
        f"{base}/events",
        json={
            "events": [
                {"event_id": "h1", "type": "hold.started", "payload": {"hold": 1}},
                {"event_id": "h2", "type": "hold.ended", "payload": {"hold_s": 95}},
            ]
        },
        headers=auth,
    ).raise_for_status()
    tool("set_call_state", "s3", substate="conversation", reason="test")
    tool(
        "record_finding",
        "f1",
        field="outcome",
        value="achieved",
        confirmation="read_back_confirmed",
    )
    tool(
        "record_finding",
        "f2",
        field="reference_number",
        value="QX7P2R",
        confirmation="read_back_confirmed",
    )
    [card] = client.get(f"/api/scenarios/{flight['id']}").json()["runs"]
    assert card["verdict"] == "achieved" and card["mode"] == "rehearsal"
    assert card["reference_number"] == "QX7P2R"
    metrics = card["metrics"]
    assert metrics["menu_presses"] == 1 and metrics["holds"] == 1 and metrics["hold_s"] == 95
    assert metrics["findings_recorded"] == 2 and metrics["refused_actions"] == 0
    assert [c["status"] for c in card["checks"]] == ["to_review", "to_review"]


def test_a_scenario_of_another_workspace_is_not_found(client):
    assert client.get("/api/scenarios/does-not-exist").status_code == 404
    assert WORKER  # imported for parity with other suites


# --- VS2: simulated counterpart --------------------------------------------------------------


def _by_name(client, prefix):
    return next(s for s in _examples(client) if s["name"].startswith(prefix))


def test_simulation_dispatches_agent_and_counterpart_into_one_room(client, gateway):
    claim_status = _by_name(client, "2a")
    run = client.post(f"/api/scenarios/{claim_status['id']}/runs", json={"mode": "simulate"})
    assert run.status_code == 200, run.text
    attempt_id = run.json()["attempt_id"]
    kinds = [c[0] for c in gateway.calls]
    assert kinds[-2:] == ["dispatch", "counterpart"]
    assert gateway.calls[-1][1] == gateway.calls[-2][1]  # same room

    agent = _claim(client, attempt_id)
    assert agent["speak_first"] is False  # the counterpart answers, like a real callee
    assert agent["counterpart_kind"] == "agent"

    room = f"att_{attempt_id.replace('-', '')}"
    body = {"room": room, "job_id": "AJ_cp"}
    role = client.post(f"/internal/attempts/{attempt_id}/counterpart", json=body, headers=WORKER)
    assert role.status_code == 200, role.text
    role = role.json()
    assert role["counterpart"] == "Health plan claims line" and role["hold_s"] == 45
    assert "press 1" in role["instructions"] and "put_on_hold" in role["instructions"]
    assert "CLM-90217" in role["instructions"]  # the script's answer
    assert "Never mention that this is a simulation" in role["instructions"]
    again = client.post(f"/internal/attempts/{attempt_id}/counterpart", json=body, headers=WORKER)
    assert again.status_code == 409

    [card] = client.get(f"/api/scenarios/{claim_status['id']}").json()["runs"]
    assert card["mode"] == "simulation"


def test_counterpart_cannot_claim_a_real_call(client):
    flight = _by_name(client, "Showcase")
    run = client.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"})
    attempt_id = run.json()["attempt_id"]
    room = f"att_{attempt_id.replace('-', '')}"
    refused = client.post(
        f"/internal/attempts/{attempt_id}/counterpart",
        json={"room": room, "job_id": "AJ_cp"},
        headers=WORKER,
    )
    assert refused.status_code == 404  # only simulated runs have a counterpart
    unauthorised = client.post(
        f"/internal/attempts/{attempt_id}/counterpart", json={"room": room, "job_id": "x"}
    )
    assert unauthorised.status_code == 401


def test_simulation_settings_are_validated(client):
    flight = _by_name(client, "Showcase")
    detail = client.get(f"/api/scenarios/{flight['id']}").json()
    assert detail["simulation"]["hold_s"] == 30 and detail["simulation"]["voice"] == "Zephyr"
    body = {k: detail[k] for k in ("name", "category", "agent_id", "counterpart", "on_behalf_of")}
    body |= {k: detail[k] for k in ("goal", "facts", "limits", "success_checks")}
    bad = client.put(
        f"/api/scenarios/{flight['id']}", json={**body, "simulation": {"voice": "Nobody"}}
    )
    assert bad.json()["error"]["code"] == "unknown_voice"
    capped = client.put(
        f"/api/scenarios/{flight['id']}", json={**body, "simulation": {"hold_s": 99999}}
    ).json()
    assert capped["simulation"]["hold_s"] == 600
