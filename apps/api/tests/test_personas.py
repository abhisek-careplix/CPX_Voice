"""Voice personas (VS4): a workspace library of how agents sound, applied to agent drafts."""

from careplix_api.models import AuditEvent
from careplix_api.personas import service as personas

from .test_lifecycle import _agent, _case_id, _claim
from .test_practice import _db_rows


def test_starter_personas_are_added_once(client):
    first = client.post("/api/personas/starters").json()
    assert first == {"added": 3}
    assert client.post("/api/personas/starters").json() == {"added": 0}
    library = client.get("/api/personas").json()
    names = [p["name"] for p in library["personas"]]
    assert names == sorted(names) and "Bilingual front desk" in names
    assert "Puck" in library["voices"]
    assert {"code": "es-US", "label": "Spanish"} in library["languages"]


def test_a_persona_is_validated_and_audited(client):
    body = {
        "name": "  Calm   nurse line ",
        "agent_name": "Maya",
        "role": "Care line assistant",
        "voice": "Kore",
        "greeting": "Hi, this is Maya, an automated assistant.",
        "warmth": "warm",
        "pace": "slow",
        "empathy": "high",
        "languages": ["en-US", "es-US", "xx-XX", "es-US"],
        "avoid": ["Calm down", "calm   down", ""],
    }
    created = client.post("/api/personas", json=body)
    assert created.status_code == 200, created.text
    persona = created.json()
    assert persona["name"] == "Calm nurse line"
    assert persona["languages"] == ["en-US", "es-US"]  # unknown and repeated languages dropped
    assert persona["avoid"] == ["Calm down", "calm down"]
    block = persona["config"]
    assert block["identity"] == {
        "name": "Maya",
        "role": "Care line assistant",
        "greeting": "Hi, this is Maya, an automated assistant.",
    }
    assert block["voice"] == "Kore" and block["persona"]["source_id"] == persona["id"]
    assert "automated_identity_disclosure" not in block["identity"]  # never the persona's to set

    bad = client.put(f"/api/personas/{persona['id']}", json={**body, "voice": "Robot"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_voice"
    missing = client.post("/api/personas", json={**body, "greeting": " "})
    assert missing.json()["error"]["code"] == "missing_fields"

    assert client.delete(f"/api/personas/{persona['id']}").json() == {"deleted": True}
    actions = [a.action for a in _db_rows(client, AuditEvent) if a.action.startswith("persona.")]
    assert actions == ["persona.created", "persona.deleted"]


def test_an_applied_persona_shapes_what_the_agent_is_told(client):
    client.post("/api/personas/starters").raise_for_status()
    bilingual = next(
        p
        for p in client.get("/api/personas").json()["personas"]
        if p["name"] == "Bilingual front desk"
    )
    agent = _agent(client, "auth-status")
    config = client.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    block = bilingual["config"]
    config["identity"].update(block["identity"])
    config["conversation"]["voice"] = block["voice"]
    config["persona"] = {**block["persona"], "avoid": ["Calm down"]}
    saved = client.put(f"/api/agents/{agent['id']}/draft", json={"config": config})
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft_issues"] == []  # a valid persona never blocks publishing
    assert saved.json()["name"] == "Sofia"

    session = client.post(
        "/api/browser-sessions",
        json={"agent_id": agent["id"], "use": "draft", "case_id": _case_id(client)},
    ).json()
    claim = _claim(client, session["attempt_id"])
    assert claim["identity"]["name"] == "Sofia" and claim["conversation"]["voice"] == "Leda"
    style = "\n".join(claim["persona_style"])
    assert "switch and stay in that language" in style and "Spanish" in style
    assert 'Never say any of these: "Calm down".' in style
    # The automated-identity disclosure is the template's, whatever the persona says.
    assert claim["identity"]["automated_identity_disclosure"]


def test_a_broken_persona_block_is_a_publish_issue(client):
    agent = _agent(client, "auth-status")
    config = client.get(f"/api/agents/{agent['id']}").json()["draft_config"]
    config["persona"] = {"pace": "warp", "tools": ["transfer_call"]}
    saved = client.put(f"/api/agents/{agent['id']}/draft", json={"config": config}).json()
    fields = {i["field"] for i in saved["draft_issues"]}
    assert any(f.startswith("persona") for f in fields), saved["draft_issues"]


def test_prompt_lines_read_like_instructions():
    lines = personas.prompt_lines(
        {"warmth": "brisk", "formality": "formal", "pace": "fast", "languages": ["en-US"]}
    )
    assert lines[0] == "Tone: brisk and efficient, formal language."
    assert lines[1].startswith("Keep a quick pace")
    assert personas.prompt_lines(None) == [] and personas.prompt_lines({}) == []
