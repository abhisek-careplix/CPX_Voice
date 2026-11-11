"""Regression suites: an agent version simulated against its scenarios gates publishing."""

import pytest
from fastapi.testclient import TestClient

from careplix_api.evals import rules
from careplix_api.evals import service as evals_service
from careplix_api.judge import service as judge_service
from careplix_api.main import create_app
from careplix_api.models import AuditEvent, CallAttempt

from .conftest import FakeGateway, make_settings
from .test_judge import FakeJudge, _answer
from .test_lifecycle import _claim
from .test_practice import _db_rows

# --- rules ---------------------------------------------------------------------------------------


def _card(state="Ended", refused=(), duration=60, presses=0):
    return {
        "state": state,
        "ended": "done",
        "refused": list(refused),
        "metrics": {"duration_s": duration, "menu_presses": presses},
    }


def test_rules_check_the_record_not_the_agent():
    expect = {
        "outcome": "achieved",
        "findings": [{"field": "reference_number", "pattern": r"^CLM-\d+$"}],
        "max_duration_s": 300,
        "max_menu_presses": 4,
    }
    good = rules.check(expect, _card(), {"outcome": "achieved", "reference_number": "CLM-90217"})
    assert all(r["ok"] for r in good) and len(good) == 6
    bad = rules.check(
        expect,
        _card(state="Failed", refused=["transfer_call"], duration=900, presses=9),
        {"outcome": "partly_achieved", "reference_number": "maybe 90217"},
    )
    assert [r["ok"] for r in bad] == [False] * 6
    # Saved before rules existed: the goal is the judge's call, the safety rules still apply.
    legacy = rules.check(None, _card(), {})
    assert [r["rule"] for r in legacy] == [
        "The call connected and ended normally",
        "No action refused by the platform",
    ]


def test_rules_reject_a_broken_pattern():
    with pytest.raises(Exception, match="regular expression"):
        rules.clean({"findings": [{"field": "ref", "pattern": "(unclosed"}]})


# --- the judge -----------------------------------------------------------------------------------


async def test_the_judge_is_claude_when_an_anthropic_key_is_set(tmp_path):
    # careplix-labs serves no Gemini text model, so the judge never scored a run there.
    settings = make_settings(tmp_path, anthropic_api_key="sk-test")
    target = await judge_service.judge_target(None, "ws", settings)
    assert target.model_id == "claude-opus-5-5" and target.api_key == "sk-test"
    assert "sk-test" not in repr(target)
    schema = judge_service.JSON_SCHEMA
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["properties"]["checks"]["items"]["properties"]["status"]["enum"] == [
        "met",
        "not_met",
        "unclear",
    ]


# --- the runner and the publish gate -------------------------------------------------------------


@pytest.fixture
def judge():
    return FakeJudge({})


@pytest.fixture
def eclient(tmp_path, judge):
    settings = make_settings(
        tmp_path,
        gcp_project="careplix-labs",
        vertex_location="us-central1",
        run_background=False,
        publish_requires_eval=True,
        eval_concurrency=2,
    )
    gateway = FakeGateway(settings)
    with TestClient(create_app(settings, gateway, judge=judge)) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        c.gateway = gateway
        yield c


def _tick(c):
    state = c.app.state
    c.portal.call(evals_service.tick, state.db, state.settings, state.gateway)


def _judge_all(c):
    state = c.app.state
    while c.portal.call(judge_service.judge_pending, state.db, state.settings, state.judge):
        pass


def _setup(c):
    """The starter library, and a draft v2 of its errand agent to evaluate."""
    c.post("/api/scenarios/examples").raise_for_status()
    agent = next(a for a in c.get("/api/agents").json() if a["template"]["id"] == "errand")
    config = c.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["instructions"]["agent_text"] = "Read every reference number back."
    c.put(f"/api/agents/{agent['id']}/draft", json={"config": config}).raise_for_status()
    flight = next(
        s for s in c.get("/api/scenarios").json()["scenarios"] if s["category"] == "travel"
    )
    return agent, flight


def _play(c, attempt_id, outcome="achieved"):
    """The simulated call runs to the end: the agent records its outcome and hangs up."""
    token = _claim(c, attempt_id)["attempt_token"]
    auth = {"Authorization": f"Bearer {token}"}
    items = [
        {"event_id": f"{attempt_id}-j", "type": "participant.joined", "payload": {}},
        {
            "event_id": f"{attempt_id}-t",
            "type": "transcript.segment",
            "payload": {
                "segment_id": f"{attempt_id[:8]}-s1",
                "speaker": "counterpart",
                "text": "Your new confirmation is ZK4M8T.",
                "playout": "played",
            },
        },
    ]
    c.post(f"/internal/attempts/{attempt_id}/events", json={"events": items}, headers=auth)
    c.post(
        f"/internal/attempts/{attempt_id}/tools/record_finding",
        json={
            "call_id": "f1",
            "arguments": {"field": "outcome", "value": outcome, "confirmation": "stated_once"},
            "evidence_segment_ids": [f"{attempt_id[:8]}-s1"],
        },
        headers=auth,
    )
    # Event ids are global dedup keys: each simulated call needs its own.
    done = [{"event_id": f"{attempt_id}-end", "type": "worker.done", "payload": {"reason": "done"}}]
    c.post(f"/internal/attempts/{attempt_id}/events", json={"events": done}, headers=auth)


def _run(c, run_id):
    return c.get(f"/api/suite-runs/{run_id}").json()


def _drive(c, run_id, judge, scenario, verdict="pass"):
    """Run every case of the suite to completion, three ticks at a time."""
    checks = "met" if verdict == "pass" else "not_met"
    judge.answer = _answer(scenario, verdict=verdict, check_status=checks)
    for _ in range(20):
        _tick(c)
        run = _run(c, run_id)
        if run["state"] not in ("queued", "running"):
            return run
        for case in run["cases"]:
            if case["state"] == "running" and case["attempt_id"]:
                attempt = next(a for a in _db_rows(c, CallAttempt) if a.id == case["attempt_id"])
                if attempt.claimed_at is None:
                    _play(c, case["attempt_id"])
        _judge_all(c)
    raise AssertionError("the suite never finished")


def test_a_passing_suite_lets_the_draft_publish(eclient, judge):
    agent, flight = _setup(eclient)
    scenario = eclient.get(f"/api/scenarios/{flight['id']}").json()
    suite = eclient.get(f"/api/agents/{agent['id']}/suite").json()
    assert suite["required_to_publish"] is True and len(suite["scenarios"]) == 14
    assert suite["draft"] == {"status": "not_run"}

    blocked = eclient.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "v2"})
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "suite_required"

    started = eclient.post(
        f"/api/agents/{agent['id']}/suite/runs",
        json={"use": "draft", "scenario_ids": [flight["id"]], "repeats": 3, "compare": True},
    )
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["version"] == {"number": 2, "state": "draft"} and run["baseline_version"] == 1
    assert run["progress"] == {"done": 0, "total": 6} and run["required_passes"] == 2

    _tick(eclient)
    running = [c for c in _run(eclient, run["id"])["cases"] if c["state"] == "running"]
    assert len(running) == 2  # the workspace's suite lane, not its one-call limit
    attempts = {a.id: a for a in _db_rows(eclient, CallAttempt)}
    first = attempts[running[0]["attempt_id"]]
    assert first.pins["simulated"] and first.pins["eval_case_id"] == running[0]["id"]

    done = _drive(eclient, run["id"], judge, scenario)
    assert done["state"] == "passed", done
    [cand, base] = done["summary"]
    assert (cand["role"], cand["passes"], cand["status"]) == ("candidate", 3, "pass")
    assert base["role"] == "baseline" and base["status"] == "pass"
    # The baseline really ran the published version, the candidate the draft.
    versions = {a.pins["agent_version"] for a in _db_rows(eclient, CallAttempt)}
    assert versions == {1, 2}
    assert eclient.get(f"/api/agents/{agent['id']}/suite").json()["draft"]["status"] == "passed"

    published = eclient.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "v2"})
    assert published.status_code == 200, published.text
    assert published.json()["current"]["suite"]["status"] == "passed"


def test_a_failing_suite_blocks_publishing_unless_an_admin_overrides(eclient, judge):
    agent, flight = _setup(eclient)
    scenario = eclient.get(f"/api/scenarios/{flight['id']}").json()
    run = eclient.post(
        f"/api/agents/{agent['id']}/suite/runs",
        json={"scenario_ids": [flight["id"]], "repeats": 1, "compare": False},
    ).json()
    done = _drive(eclient, run["id"], judge, scenario, verdict="fail")
    assert done["state"] == "failed" and done["summary"][0]["status"] == "fail"
    case = done["cases"][0]
    assert case["verdict"] == "fail" and case["judge"]["verdict"] == "fail"
    assert all(r["ok"] for r in case["rules"])  # the platform's rules held; the judge failed it

    url = f"/api/agents/{agent['id']}/publish"
    assert eclient.post(url, json={}).json()["error"]["code"] == "suite_required"
    forced = eclient.post(url, json={"override_reason": "Hotfix approved by ops lead"})
    assert forced.status_code == 200, forced.text
    audit = [a for a in _db_rows(eclient, AuditEvent) if a.action == "agent.publish_override"]
    assert audit and audit[0].detail["reason"] == "Hotfix approved by ops lead"


def test_editing_the_draft_mid_run_makes_the_run_stale(eclient, judge):
    agent, flight = _setup(eclient)
    run = eclient.post(
        f"/api/agents/{agent['id']}/suite/runs",
        json={"scenario_ids": [flight["id"]], "repeats": 1, "compare": False},
    ).json()
    _tick(eclient)
    config = eclient.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["instructions"]["agent_text"] = "Something else."
    eclient.put(f"/api/agents/{agent['id']}/draft", json={"config": config}).raise_for_status()
    _tick(eclient)
    stale = _run(eclient, run["id"])
    assert stale["state"] == "stale" and "changed during the run" in stale["error"]
    assert eclient.get(f"/api/agents/{agent['id']}/suite").json()["draft"]["status"] == "not_run"


def test_a_suite_needs_scenarios_and_runs_once_per_version(eclient):
    agent, flight = _setup(eclient)
    url = f"/api/agents/{agent['id']}/suite/runs"
    body = {"scenario_ids": [flight["id"]], "repeats": 1, "compare": False}
    assert eclient.post(url, json=body).status_code == 200
    again = eclient.post(url, json=body)
    assert again.status_code == 409 and again.json()["error"]["code"] == "suite_running"
    assert eclient.post(url, json={**body, "repeats": 9}).status_code in (409, 422)
    receptionist = next(
        a for a in eclient.get("/api/agents").json() if a["template"]["direction"] == "inbound"
    )
    inbound = eclient.post(f"/api/agents/{receptionist['id']}/suite/runs", json={})
    assert inbound.json()["error"]["code"] == "suite_outbound_only"


def test_a_running_suite_never_blocks_a_real_call(eclient):
    agent, flight = _setup(eclient)
    eclient.post(
        f"/api/agents/{agent['id']}/suite/runs",
        json={"scenario_ids": [flight["id"]], "repeats": 1, "compare": False},
    ).raise_for_status()
    _tick(eclient)  # a suite simulation is now running
    rehearse = eclient.post(f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"})
    assert rehearse.status_code == 200, rehearse.text


def test_cancel_stops_queued_cases(eclient):
    agent, flight = _setup(eclient)
    run = eclient.post(
        f"/api/agents/{agent['id']}/suite/runs",
        json={"scenario_ids": [flight["id"]], "repeats": 3, "compare": False},
    ).json()
    cancelled = eclient.post(f"/api/suite-runs/{run['id']}/cancel").json()
    assert cancelled["state"] == "cancelled"
    assert {c["state"] for c in cancelled["cases"]} == {"cancelled"}
