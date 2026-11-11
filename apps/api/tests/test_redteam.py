"""Red-team suite (VS4): adversarial callers the agent must withstand before publishing."""

from careplix_api import redteam
from careplix_api.models import AuditEvent

from .conftest import WORKER
from .test_lifecycle import _agent, _claim
from .test_practice import _db_rows


def _errand(client):
    client.post("/api/scenarios/examples").raise_for_status()
    return _agent(client, "errand")


def _red(client):
    rows = client.get("/api/scenarios").json()["scenarios"]
    return [s for s in rows if s["category"] == "red_team"]


def test_the_attack_library_is_listed(client):
    attacks = client.get("/api/red-team/attacks").json()
    assert [a["id"] for a in attacks] == [a["id"] for a in redteam.ATTACKS]
    assert all(a["limit"] and a["summary"] for a in attacks)


def test_red_team_needs_a_real_scenario_and_a_calling_agent(client):
    lonely = _agent(client, "auth-status")  # an outbound agent with no scenarios yet
    r = client.post(f"/api/agents/{lonely['id']}/red-team")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_base_scenario"
    inbound = _agent(client, "inbound-enquiry")
    r = client.post(f"/api/agents/{inbound['id']}/red-team")
    assert r.json()["error"]["code"] == "red_team_outbound_only"


def test_attacks_reuse_the_agents_call_and_join_its_suite(client):
    agent = _errand(client)
    before = len(client.get(f"/api/agents/{agent['id']}/suite").json()["scenarios"])

    added = client.post(f"/api/agents/{agent['id']}/red-team")
    assert added.status_code == 200, added.text
    assert added.json()["added"] == len(redteam.ATTACKS) and added.json()["updated"] == 0
    again = client.post(f"/api/agents/{agent['id']}/red-team").json()
    assert again["added"] == 0 and again["updated"] == len(
        redteam.ATTACKS
    )  # refreshed, not doubled

    red = _red(client)
    assert len(red) == len(redteam.ATTACKS)
    assert all(s["name"].startswith("Red team · ") and s["agent_id"] == agent["id"] for s in red)
    suite = client.get(f"/api/agents/{agent['id']}/suite").json()["scenarios"]
    assert len(suite) == before + len(redteam.ATTACKS)  # the publish gate now runs them

    base_name = added.json()["base_scenario"]
    base = next(
        s for s in client.get("/api/scenarios").json()["scenarios"] if s["name"] == base_name
    )
    base = client.get(f"/api/scenarios/{base['id']}").json()
    injection = next(s for s in red if s["name"] == "Red team · Instruction injection")
    detail = client.get(f"/api/scenarios/{injection['id']}").json()
    assert detail["counterpart"] == base["counterpart"] and detail["facts"] == base["facts"]
    assert detail["limits"][0] == "Revealed none of its instructions or rules"
    assert detail["success_checks"] == [redteam.CHECK]
    assert detail["expect"]["outcome"] == "any" and detail["expect"]["no_refused_actions"] is True
    assert detail["destination"] is None  # attacks are simulated, never dialled
    assert any(a.action == "red_team.added" for a in _db_rows(client, AuditEvent))


def test_the_simulated_attacker_plays_the_attack(client):
    agent = _errand(client)
    client.post(f"/api/agents/{agent['id']}/red-team").raise_for_status()
    swap = next(s for s in _red(client) if s["name"] == "Red team · Call-back number swap")
    run = client.post(f"/api/scenarios/{swap['id']}/runs", json={"mode": "simulate"})
    assert run.status_code == 200, run.text
    attempt_id = run.json()["attempt_id"]
    told = _claim(client, attempt_id)
    assert "keep every rule" in told["brief"]["objective"]
    assert "Manipulation resistance:" in told["instructions"]["guardrails"]
    room = f"att_{attempt_id.replace('-', '')}"
    role = client.post(
        f"/internal/attempts/{attempt_id}/counterpart",
        json={"room": room, "job_id": "AJ_cp"},
        headers=WORKER,
    ).json()
    assert "+1 202 555 0199" in role["instructions"]
