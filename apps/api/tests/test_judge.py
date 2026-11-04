"""VS3 AI judge: scores a finished Voice Studio run from its record, with guard rails."""

import pytest
from fastapi.testclient import TestClient

from careplix_api.judge import service as judge_service
from careplix_api.main import create_app

from .conftest import FakeGateway, make_settings
from .test_lifecycle import _claim


class FakeJudge:
    def __init__(self, answer):
        self.answer = answer
        self.prompts: list[str] = []
        self.targets: list = []

    async def __call__(self, prompt, target):
        self.prompts.append(prompt)
        self.targets.append(target)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def judge():
    return FakeJudge({})


@pytest.fixture
def jclient(tmp_path, judge):
    settings = make_settings(
        tmp_path, gcp_project="careplix-labs", vertex_location="us-central1", run_background=False
    )
    with TestClient(create_app(settings, FakeGateway(settings), judge=judge)) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _finished_run(client, end=True):
    client.post("/api/scenarios/examples").raise_for_status()
    flight = next(
        s for s in client.get("/api/scenarios").json()["scenarios"] if s["category"] == "travel"
    )
    scenario = client.get(f"/api/scenarios/{flight['id']}").json()
    attempt_id = client.post(
        f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"}
    ).json()["attempt_id"]
    token = _claim(client, attempt_id)["attempt_token"]
    auth = {"Authorization": f"Bearer {token}"}
    said = [
        ("agent", "Hi, I'm calling for Jordan Lee to move flight UA 1488 to the next day."),
        ("counterpart", "Sure. I can move it to the 8 a.m. flight. Your confirmation is ZK4M8T."),
        ("agent", "Thank you. ZK4M8T. Is there a change fee?"),
        ("counterpart", "No fee today."),
    ]
    items = [{"event_id": "j1", "type": "participant.joined", "payload": {}}]
    items += [
        {
            "event_id": f"t{i}",
            "type": "transcript.segment",
            "payload": {"segment_id": f"seg{i}", "speaker": who, "text": text, "playout": "played"},
        }
        for i, (who, text) in enumerate(said)
    ]
    if end:
        items.append({"event_id": "end", "type": "worker.done", "payload": {"reason": "done"}})
    client.post(
        f"/internal/attempts/{attempt_id}/events", json={"events": items}, headers=auth
    ).raise_for_status()
    return scenario, attempt_id


def _answer(scenario, check_status="met", limit_status="kept", verdict="pass", score=92):
    return {
        "verdict": verdict,
        "score": score,
        "summary": "Moved the flight and read back the confirmation.",
        "checks": [
            {
                "check": c,
                "status": check_status,
                "evidence": 'OTHER SIDE: "Your confirmation is ZK4M8T."',
            }
            for c in scenario["success_checks"]
        ],
        "limits": [
            {"limit": c, "status": limit_status, "evidence": "No card details were asked for."}
            for c in scenario["limits"]
        ],
        "coaching": ["Confirm the new departure time out loud."],
    }


def test_a_finished_run_is_judged_with_evidence(jclient, judge):
    scenario, attempt_id = _finished_run(jclient)
    judge.answer = _answer(scenario)
    r = jclient.post(f"/api/scenarios/{scenario['id']}/runs/{attempt_id}/judge")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["state"] == "done" and result["verdict"] == "pass" and result["score"] == 92
    assert [c["status"] for c in result["checks"]] == ["met"] * len(scenario["success_checks"])
    assert result["coaching"] == ["Confirm the new departure time out loud."]

    # The judge saw the scenario and the record, but never sealed fact values.
    prompt = judge.prompts[0]
    assert scenario["goal"] in prompt and "ZK4M8T" in prompt and "OTHER SIDE:" in prompt
    facts = scenario.get("facts") or {}
    assert facts and facts["confirmation_code"] not in prompt and facts["flight"] not in prompt
    assert judge.targets[0].project == "careplix-labs"
    assert judge.targets[0].model_id == "gemini-2.5-flash"

    detail = jclient.get(f"/api/scenarios/{scenario['id']}").json()
    assert detail["runs"][0]["judge"]["verdict"] == "pass"
    assert detail["judge_summary"] == {"runs": 1, "average": 92, "passed": 1}


def test_guard_rails_override_a_generous_model(jclient, judge):
    scenario, attempt_id = _finished_run(jclient)
    url = f"/api/scenarios/{scenario['id']}/runs/{attempt_id}/judge"
    judge.answer = _answer(scenario, limit_status="broken", verdict="pass", score=95)
    broken = jclient.post(url).json()
    assert broken["verdict"] == "fail" and broken["score"] <= 49

    judge.answer = _answer(scenario, check_status="unclear", verdict="pass")
    assert jclient.post(url).json()["verdict"] == "partial"

    judge.answer = {"verdict": "great", "score": "lots", "checks": [], "limits": []}
    odd = jclient.post(url).json()
    assert odd["verdict"] == "fail" and odd["score"] == 0
    assert all(c["status"] == "unclear" for c in odd["checks"])
    assert len(odd["checks"]) == len(scenario["success_checks"])


def test_judge_failure_is_recorded_not_raised(jclient, judge):
    scenario, attempt_id = _finished_run(jclient)
    judge.answer = RuntimeError("model unavailable")
    result = jclient.post(f"/api/scenarios/{scenario['id']}/runs/{attempt_id}/judge").json()
    assert result["state"] == "failed" and "model unavailable" in result["error"]


def test_a_running_call_cannot_be_judged(jclient):
    scenario, attempt_id = _finished_run(jclient, end=False)
    r = jclient.post(f"/api/scenarios/{scenario['id']}/runs/{attempt_id}/judge")
    assert r.status_code == 409


def test_finished_runs_are_scored_automatically(jclient, judge):
    scenario, attempt_id = _finished_run(jclient)
    judge.answer = _answer(scenario)
    app = jclient.app
    done = jclient.portal.call(judge_service.judge_pending, app.state.db, app.state.settings, judge)
    assert done == 1
    assert jclient.get(f"/api/scenarios/{scenario['id']}").json()["runs"][0]["judge"]["score"] == 92


def test_judge_needs_a_project(client):
    client.post("/api/scenarios/examples").raise_for_status()
    flight = next(
        s for s in client.get("/api/scenarios").json()["scenarios"] if s["category"] == "travel"
    )
    attempt_id = client.post(
        f"/api/scenarios/{flight['id']}/runs", json={"mode": "rehearse"}
    ).json()["attempt_id"]
    token = _claim(client, attempt_id)["attempt_token"]
    client.post(
        f"/internal/attempts/{attempt_id}/events",
        json={"events": [{"event_id": "e", "type": "worker.done", "payload": {}}]},
        headers={"Authorization": f"Bearer {token}"},
    ).raise_for_status()
    result = client.post(f"/api/scenarios/{flight['id']}/runs/{attempt_id}/judge").json()
    assert result["state"] == "failed" and "Connections" in result["error"]
