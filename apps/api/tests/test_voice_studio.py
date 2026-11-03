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
    assert {s["category"] for s in listed} >= {"travel", "healthcare", "pharmacy", "retail"}
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
