"""End-to-end control-plane behaviour with LiveKit faked: doc 08 T01, T08, T09, T10, T11."""

import time

from .conftest import APPROVED, MODEL, WORKER


def _agent(client, template_id):
    return next(a for a in client.get("/api/agents").json() if a["template"]["id"] == template_id)


def _publish(client, agent_id, note="initial"):
    response = client.post(f"/api/agents/{agent_id}/publish", json={"change_note": note})
    assert response.status_code == 200, response.text
    return response.json()


def _claim(client, attempt_id):
    room = f"att_{attempt_id.replace('-', '')}"
    response = client.post(
        f"/internal/attempts/{attempt_id}/claim",
        json={"room": room, "job_id": "AJ_1", "runtime_build": "test"},
        headers=WORKER,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _browser_attempt(client, use="published"):
    agent = _agent(client, "auth-status")
    if use == "published" and not agent["current"]:
        _publish(client, agent["id"])
    response = client.post("/api/browser-sessions", json={"agent_id": agent["id"], "use": use})
    assert response.status_code == 200, response.text
    return response.json()


def test_seeded_agents_start_as_drafts(client):
    agents = client.get("/api/agents").json()
    assert {a["template"]["id"] for a in agents} == {"auth-status", "inbound-enquiry"}
    assert all(a["draft"] and a["current"] is None for a in agents)


def test_publish_change_and_rollback(client):
    """T01: new calls use the selected version; history is preserved."""
    agent = _agent(client, "auth-status")
    v1 = _publish(client, agent["id"])["current"]
    assert v1["number"] == 1 and v1["config_hash"].startswith("sha256:")

    config = client.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["instructions"]["agent_text"] = "Be brief."
    edited = client.put(f"/api/agents/{agent['id']}/draft", json={"config": config}).json()
    assert edited["draft"]["number"] == 2 and edited["current"]["number"] == 1

    preview = client.get(f"/api/agents/{agent['id']}/publish-preview").json()
    assert preview["issues"] == []
    assert [c["path"] for c in preview["changes"]] == ["instructions/agent_text"]

    v2 = _publish(client, agent["id"], "shorter")["current"]
    assert v2["number"] == 2
    rolled = client.post(f"/api/agents/{agent['id']}/rollback", json={"version_id": v1["id"]})
    assert rolled.json()["current"]["number"] == 1
    assert [v["number"] for v in rolled.json()["versions"]] == [2, 1]


def test_prompt_edit_cannot_add_tool_privilege(client):
    """SEC06: the inbound template does not grant DTMF; adding it blocks publication."""
    agent = _agent(client, "inbound-enquiry")
    config = client.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["tools"].append({"name": "send_dtmf", "version": "1.0.0"})
    client.put(f"/api/agents/{agent['id']}/draft", json={"config": config})
    response = client.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "x"})
    assert response.status_code == 422
    issues = response.json()["error"]["detail"]["issues"]
    assert any("send_dtmf" in i["message"] for i in issues)


def test_unapproved_model_blocks_publication(client):
    agent = _agent(client, "auth-status")
    config = client.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["model"]["model_id"] = "some-other-model"
    client.put(f"/api/agents/{agent['id']}/draft", json={"config": config})
    response = client.post(f"/api/agents/{agent['id']}/publish", json={})
    assert response.status_code == 422
    assert MODEL in str(response.json())


def test_browser_session_claim_tools_and_events(client, gateway):
    session = _browser_attempt(client)
    attempt_id = session["attempt_id"]
    assert session["token"].startswith("token-for-att_")
    assert gateway.calls[0][0] == "dispatch"

    payload = _claim(client, attempt_id)
    assert payload["brief"]["sandbox"] is True
    assert payload["brief"]["context"]["member_id"].startswith("99")
    assert "platform_policy" in payload["instructions"]
    record = next(t for t in payload["tools"] if t["name"] == "record_finding")
    assert "evidence_segment_ids" not in record["parameters"]["properties"]

    # A second claim is refused (no hijack or replay of the attempt).
    room = f"att_{attempt_id.replace('-', '')}"
    again = client.post(
        f"/internal/attempts/{attempt_id}/claim",
        json={"room": room, "job_id": "AJ_2", "runtime_build": "test"},
        headers=WORKER,
    )
    assert again.status_code == 409

    auth = {"Authorization": f"Bearer {payload['attempt_token']}"}
    base = f"/internal/attempts/{attempt_id}"
    events = [
        {"event_id": "e1", "type": "participant.joined", "payload": {"kind": "standard"}},
        {
            "event_id": "e2",
            "type": "transcript.segment",
            "payload": {
                "segment_id": "seg-1",
                "speaker": "counterpart",
                "text": "Reference R4472.",
            },
        },
        {"event_id": "e2", "type": "transcript.segment", "payload": {"segment_id": "seg-1"}},
    ]
    accepted = client.post(f"{base}/events", json={"events": events}, headers=auth).json()
    assert accepted == {"accepted": 2, "state": "Connected"}  # duplicate e2 ignored

    # T08: a tool not granted to this agent is rejected and audited.
    denied = client.post(
        f"{base}/tools/request_handoff",
        json={"call_id": "c1", "arguments": {"reason": "x", "destination_alias": "desk"}},
        headers=auth,
    ).json()
    assert denied["state"] == "rejected" and denied["result"]["error"] == "tool_not_granted"

    unknown = client.post(
        f"{base}/tools/record_finding",
        json={
            "call_id": "c2",
            "arguments": {
                "field": "favourite_colour",
                "value": "blue",
                "confirmation": "stated_once",
            },
        },
        headers=auth,
    ).json()
    assert unknown["result"]["error"] == "unknown_field"

    finding_call = {
        "call_id": "c3",
        "arguments": {
            "field": "authorization_reference",
            "value": "R4472",
            "confirmation": "read_back_confirmed",
        },
        "evidence_segment_ids": ["seg-1", "seg-not-mine"],
    }
    ok = client.post(f"{base}/tools/record_finding", json=finding_call, headers=auth).json()
    assert ok["state"] == "succeeded" and ok["result"]["status"] == "confirmed_by_counterpart"
    # Retrying the same model tool call returns the same result without a second finding (FR11).
    assert client.post(f"{base}/tools/record_finding", json=finding_call, headers=auth).json() == ok

    detail = client.get(f"/api/calls/{attempt_id}").json()
    assert len(detail["findings"]) == 1
    assert detail["findings"][0]["evidence_segment_ids"] == ["seg-1"]
    assert detail["transcript"][0]["text"] == "Reference R4472."

    dtmf = {"call_id": "c4", "arguments": {"digits": "2", "reason": "auth menu"}}
    assert (
        client.post(f"{base}/tools/send_dtmf", json=dtmf, headers=auth).json()["state"]
        == "succeeded"
    )
    dtmf2 = {"call_id": "c5", "arguments": {"digits": "1", "reason": "status"}}
    too_fast = client.post(f"{base}/tools/send_dtmf", json=dtmf2, headers=auth).json()
    assert too_fast["result"]["error"] == "dtmf_cooldown"
    bad = {"call_id": "c6", "arguments": {"digits": "12a", "reason": "x"}}
    assert (
        client.post(f"{base}/tools/send_dtmf", json=bad, headers=auth).json()["result"]["error"]
        == "invalid_arguments"
    )

    done = [{"event_id": "e9", "type": "worker.done", "payload": {"reason": "objective_complete"}}]
    assert (
        client.post(f"{base}/events", json={"events": done}, headers=auth).json()["state"]
        == "Ended"
    )

    # T11: the event stream replays from a cursor and terminates once the call has ended.
    stream = client.get(f"/api/calls/{attempt_id}/events?cursor=0").text
    assert "Reference R4472." in stream and "event: end" in stream
    later = client.get(f"/api/calls/{attempt_id}/events?cursor=9999").text
    assert "Reference R4472." not in later


def test_attempt_token_is_bound_to_its_attempt(client):
    first = _browser_attempt(client)
    token = _claim(client, first["attempt_id"])["attempt_token"]
    client.post(f"/api/calls/{first['attempt_id']}/end")
    second = _browser_attempt(client)
    response = client.post(
        f"/internal/attempts/{second['attempt_id']}/events",
        json={"events": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_one_active_call_per_workspace(client):
    _browser_attempt(client)
    agent = _agent(client, "auth-status")
    response = client.post(
        "/api/browser-sessions", json={"agent_id": agent["id"], "use": "published"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capacity_exhausted"


def _task(client, destination=APPROVED):
    agent = _agent(client, "auth-status")
    if not agent["current"]:
        _publish(client, agent["id"])
    case = client.get("/api/cases?q=Alpha").json()[0]
    return client.post(
        "/api/tasks",
        json={"agent_id": agent["id"], "case_id": case["id"], "destination": destination},
    )


def test_destination_policy_blocks_unapproved_numbers(client):
    response = _task(client, "+1 (415) 555-0100")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "destination_not_approved"
    assert _task(client, "202-555-0142").json()["destination"] == APPROVED


def test_repeated_start_call_creates_one_attempt_and_dials_after_claim(client, gateway):
    """T09 + doc 03 step 3: idempotent start; no dialing before a worker claims."""
    task = _task(client).json()
    headers = {"Idempotency-Key": "start-1"}
    first = client.post(f"/api/tasks/{task['id']}/calls", headers=headers).json()
    second = client.post(f"/api/tasks/{task['id']}/calls", headers=headers).json()
    assert first["id"] == second["id"]
    assert not [c for c in gateway.calls if c[0] == "dial"]

    for _ in range(40):
        if client.get(f"/api/calls/{first['id']}").json()["state"] == "Queued":
            break
        time.sleep(0.05)
    _claim(client, first["id"])
    for _ in range(60):
        if client.get(f"/api/calls/{first['id']}").json()["state"] == "Connected":
            break
        time.sleep(0.05)
    assert client.get(f"/api/calls/{first['id']}").json()["state"] == "Connected"
    assert [c for c in gateway.calls if c[0] == "dial"] == [
        ("dial", f"att_{first['id'].replace('-', '')}", APPROVED)
    ]


def test_reviewer_correction_preserves_original(client):
    session = _browser_attempt(client)
    payload = _claim(client, session["attempt_id"])
    auth = {"Authorization": f"Bearer {payload['attempt_token']}"}
    result = client.post(
        f"/internal/attempts/{session['attempt_id']}/tools/record_finding",
        json={
            "call_id": "c1",
            "arguments": {
                "field": "call_reference",
                "value": "NW-552",
                "confirmation": "stated_once",
            },
        },
        headers=auth,
    ).json()
    finding_id = result["result"]["finding_id"]
    revised = client.post(
        f"/api/findings/{finding_id}/revisions",
        json={"value": "NW-55210", "reason": "Digits truncated in transcript; confirmed by audio."},
    ).json()
    assert revised["value"] == "NW-55210" and revised["status"] == "reviewed"
    assert revised["revisions"][0]["prior_value"] == "NW-552"
