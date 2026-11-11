"""Guardrail packs (VS4): server-owned rules, enforced in the prompt and checked by the judge."""

from careplix_api import guardrails

from .test_judge import _answer, _finished_run, jclient, judge  # noqa: F401  (fixtures)
from .test_lifecycle import _agent, _case_id, _claim


def _draft(client, agent):
    return client.get(f"/api/agents/{agent['id']}").json()["draft_config"]


def _save(client, agent, config):
    r = client.put(f"/api/agents/{agent['id']}/draft", json={"config": config})
    assert r.status_code == 200, r.text
    return r.json()


def test_packs_are_listed_with_their_rules(client):
    packs = {p["id"]: p for p in client.get("/api/guardrail-packs").json()}
    assert set(packs) == {
        "clinical-scope",
        "patient-privacy",
        "financial-safety",
        "manipulation-resistance",
        "outreach-consent",
    }
    clinical = packs["clinical-scope"]
    assert "inbound-enquiry" in clinical["required_for"]
    assert any("911" in r["text"] for r in clinical["rules"])
    assert all(r["limit"] for p in packs.values() for r in p["rules"])


def test_a_new_agent_starts_with_its_recommended_packs(client):
    templates = {t["id"]: t for t in client.get("/api/templates").json()}
    created = client.post("/api/agents", json={"name": "Robin", "template_id": "auth-status"})
    assert created.status_code == 200, created.text
    packs = created.json()["draft_config"]["guardrails"]["packs"]
    assert [p["id"] for p in packs] == ["financial-safety", "manipulation-resistance"]
    assert "auth-status" in templates


def test_required_packs_apply_even_when_removed_from_the_config(client):
    agent = _agent(client, "inbound-enquiry")
    config = _draft(client, agent)
    config["guardrails"]["packs"] = []  # an author tries to drop them
    assert _save(client, agent, config)["draft_issues"] == []
    session = client.post(
        "/api/browser-sessions", json={"agent_id": agent["id"], "use": "draft"}
    ).json()
    claim = _claim(client, session["attempt_id"])
    rules = claim["instructions"]["guardrails"]
    assert "Clinical scope:" in rules and "Patient privacy:" in rules
    assert "call 911" in rules and "Financial safety" not in rules


def test_chosen_packs_are_told_to_the_agent_and_pinned_on_the_call(client):
    agent = _agent(client, "auth-status")
    config = _draft(client, agent)
    config["guardrails"]["packs"] = [{"id": "outreach-consent", "version": 1}]
    _save(client, agent, config)
    session = client.post(
        "/api/browser-sessions",
        json={"agent_id": agent["id"], "use": "draft", "case_id": _case_id(client)},
    ).json()
    claim = _claim(client, session["attempt_id"])
    assert "Outreach consent:" in claim["instructions"]["guardrails"]
    assert "Clinical scope" not in claim["instructions"]["guardrails"]  # not required here
    from careplix_api.models import CallAttempt

    from .test_practice import _db_rows

    attempt = next(a for a in _db_rows(client, CallAttempt) if a.id == session["attempt_id"])
    assert attempt.pins["guardrail_packs"] == {"outreach-consent": 1}


def test_unknown_or_stale_packs_block_publishing(client):
    agent = _agent(client, "auth-status")
    config = _draft(client, agent)
    config["guardrails"]["packs"] = [
        {"id": "no-such-pack", "version": 1},
        {"id": "financial-safety", "version": 7},
    ]
    messages = [i["message"] for i in _save(client, agent, config)["draft_issues"]]
    assert "Unknown pack no-such-pack." in messages
    assert any("Financial safety v7 is not current" in m for m in messages)


def test_removing_a_pack_is_called_out_before_publishing(client):
    agent = _agent(client, "auth-status")
    config = _draft(client, agent)
    config["guardrails"]["packs"] = [{"id": "financial-safety", "version": 1}]
    _save(client, agent, config)
    client.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "v1"}).raise_for_status()
    config = _draft(client, agent)  # a new draft starts from the published config
    config["guardrails"]["packs"] = []
    _save(client, agent, config)
    preview = client.get(f"/api/agents/{agent['id']}/publish-preview").json()
    assert preview["privilege_changes"]["packs_removed"] == ["financial-safety"]


def test_a_broken_pack_limit_fails_a_judged_run(jclient, judge):  # noqa: F811
    scenario, attempt_id = _finished_run(jclient)
    url = f"/api/scenarios/{scenario['id']}/runs/{attempt_id}/judge"
    pack_limit = guardrails.judge_limits(["financial-safety"])[0]
    answer = _answer(scenario)
    answer["limits"].append({"limit": pack_limit, "status": "broken", "evidence": "AGENT: covered"})
    judge.answer = answer
    result = jclient.post(url).json()
    assert pack_limit in judge.prompts[-1]  # the judge was told the pack's limits
    assert result["verdict"] == "fail" and result["score"] <= 49
    assert any(row["limit"] == pack_limit and row["status"] == "broken" for row in result["limits"])
