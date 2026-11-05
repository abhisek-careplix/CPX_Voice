"""Supervised browser runs: plan approval, approval cards by risk, take-over and presence."""

import base64
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from careplix_api.browser.adapters import ProposedAction, Turn
from careplix_api.browser.runtime_client import HttpRuntime, RuntimeUnavailable
from careplix_api.main import create_app

from .conftest import FakeGateway, make_settings

PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()

LINK = {"tag": "a", "text": "Claims", "in_form": False, "type": "", "name": "", "role": ""}
FIELD = {"tag": "input", "text": "", "name": "member_id", "in_form": True, "type": "text"}
SUBMIT = {"tag": "button", "text": "Submit request", "in_form": True, "type": "submit"}
PASSWORD = {"tag": "input", "is_password": True, "type": "password", "in_form": True}


class FakeRuntime:
    configured = True

    def __init__(self):
        self.elements: dict[tuple, dict] = {}
        self.acts: list[dict] = []
        self.opened: list[dict] = []
        self.allowed: list[str] = []
        self.closed: list[str] = []
        self.url = "https://portal.example-payer.com/home"

    async def open(self, start_url, allowed_hosts, state):
        self.opened.append({"url": start_url, "hosts": allowed_hosts, "state": state})
        return f"s{len(self.opened)}"

    async def screenshot(self, sid, fmt="png"):
        return {"image": PNG, "url": self.url, "title": "Portal", "width": 1280, "height": 800}

    async def inspect(self, sid, x, y):
        return self.elements.get((x, y))

    async def act(self, sid, action):
        self.acts.append(action)
        return {"ok": True, "url": self.url, "title": "Portal", "left_allowed_sites": False}

    async def allow(self, sid, hosts):
        self.allowed += hosts

    async def storage_state(self, sid):
        return {"cookies": [{"name": "session", "value": "abc"}], "origins": []}

    async def close(self, sid):
        self.closed.append(sid)


class ScriptedAgent:
    model_id = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.received: list[tuple] = []
        self.plan_used: list[str] = []

    async def plan(self, task, shot):
        return ["Open Claims", "Search the member", "Read the status"]

    def use_plan(self, steps):
        self.plan_used = steps

    async def next(self, shot, outcomes, note):
        self.received.append(([(o.ok, o.message, o.acknowledged) for o in outcomes], note))
        return self.turns.pop(0) if self.turns else Turn([], done=True, result="Finished.")


@pytest.fixture
def runtime():
    return FakeRuntime()


@pytest.fixture
def agent_box():
    return {"agent": None, "turns": []}


@pytest.fixture
def bclient(tmp_path, runtime, agent_box):
    settings = make_settings(
        tmp_path,
        gcp_project="careplix-labs",
        vertex_location="us-central1",
        browser_presence_timeout_s=30,
    )

    def factory(model, target, s, task):
        agent_box["agent"] = ScriptedAgent(agent_box["turns"])
        agent_box["model"] = model
        return agent_box["agent"]

    app = create_app(
        settings, FakeGateway(settings), browser_runtime=runtime, browser_agent_factory=factory
    )
    with TestClient(app) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _wait(client, run_id, check, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/browser/runs/{run_id}").json()
        if check(run):
            return run
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state {run['state']} pending={run.get('pending')}")


def _ack(client, host="portal.example-payer.com"):
    r = client.post(
        "/api/browser/sites/acknowledge", json={"host": host, "terms_note": "Read 4 Nov"}
    )
    assert r.status_code == 200, r.text


def _start(client, **extra):
    body = {
        "task": "Check the status of claim 12345 for member W123456789",
        "start_url": "https://portal.example-payer.com/home",
        "supervised": True,
        **extra,
    }
    r = client.post("/api/browser/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _approve_plan(client, run_id):
    _wait(client, run_id, lambda r: r["state"] == "awaiting_plan")
    r = client.post(f"/api/browser/runs/{run_id}/plan", json={"decision": "approve"})
    assert r.status_code == 200, r.text


def _decide(client, run, decision, **extra):
    step = run["pending"]
    return client.post(
        f"/api/browser/runs/{run['id']}/steps/{step['id']}/decision",
        json={"decision": decision, **extra},
    )


def test_runs_need_supervision_and_a_site_acknowledgement(bclient):
    body = {"task": "Check a claim", "start_url": "https://portal.example-payer.com/"}
    r = bclient.post("/api/browser/runs", json={**body, "supervised": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "supervision_required"
    r = bclient.post("/api/browser/runs", json={**body, "supervised": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "site_terms_ack_needed"
    _ack(bclient)
    assert bclient.post("/api/browser/runs", json={**body, "supervised": True}).status_code == 200


def test_plan_then_cards_by_risk_end_to_end(bclient, runtime, agent_box):
    runtime.elements = {(200, 100): LINK, (300, 200): FIELD, (300, 400): SUBMIT}
    agent_box["turns"] = [
        Turn([ProposedAction("a1", "click", {"x": 200, "y": 100})], note="Open Claims"),
        Turn(
            [ProposedAction("a2", "type", {"x": 300, "y": 200, "text": "W12345678"})],
            note="Enter the member ID",
        ),
        Turn([ProposedAction("a3", "click", {"x": 300, "y": 400})], note="Submit the search"),
        Turn([], done=True, result="Claim 12345 is pended for records; reference R-889."),
    ]
    _ack(bclient)
    run_id = _start(bclient, model="claude")
    _approve_plan(bclient, run_id)
    assert agent_box["model"] == "claude"

    # 1. Reading asks while nothing is allowed; "approve similar" covers later reads in this run.
    run = _wait(bclient, run_id, lambda r: r["pending"] is not None)
    assert run["state"] == "awaiting_approval"
    card = run["pending"]
    assert card["risk"] == "read" and card["description"] == "Click “Claims”"
    assert card["why"] == "Open Claims" and card["has_screenshot"]
    shot = bclient.get(f"/api/browser/runs/{run_id}/steps/{card['id']}/screenshot")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg"
    assert _decide(bclient, run, "approve_similar").status_code == 200

    # 2. Entering data shows the value; the supervisor corrects it before it is typed.
    run = _wait(bclient, run_id, lambda r: r["pending"] and r["pending"]["kind"] == "type")
    assert run["pending"]["risk"] == "enter_data" and run["pending"]["text"] == "W12345678"
    assert _decide(bclient, run, "edit", text="W123456789").status_code == 200

    # 3. Submitting always asks and can't be allowed in bulk.
    run = _wait(bclient, run_id, lambda r: r["pending"] and r["pending"]["risk"] == "commit")
    assert _decide(bclient, run, "approve_similar").status_code == 409
    assert _decide(bclient, run, "approve").status_code == 200

    run = _wait(bclient, run_id, lambda r: r["state"] == "done")
    assert run["result"].startswith("Claim 12345 is pended")
    assert [s["status"] for s in run["steps"]] == ["executed", "executed", "executed"]
    assert [a["kind"] for a in runtime.acts] == ["click", "type", "click"]
    assert runtime.acts[1]["text"] == "W123456789"  # the edited value was typed
    oks = [o for outcomes, _ in agent_box["agent"].received[1:] for o in outcomes]
    assert all(ok for ok, _, _ in oks)
    assert runtime.closed == ["s1"]
    assert agent_box["agent"].plan_used == ["Open Claims", "Search the member", "Read the status"]

    # The sign-in is kept for the next run on the same site.
    agent_box["turns"] = [Turn([], done=True, result="ok")]
    second = _start(bclient)
    _wait(bclient, second, lambda r: r["state"] == "awaiting_plan")
    assert runtime.opened[1]["state"]["cookies"][0]["name"] == "session"


def test_deny_feedback_and_person_only_steps(bclient, runtime, agent_box):
    runtime.elements = {(300, 400): SUBMIT, (10, 10): PASSWORD}
    agent_box["turns"] = [
        Turn([ProposedAction("a1", "click", {"x": 300, "y": 400})]),
        Turn([ProposedAction("a2", "type", {"x": 10, "y": 10, "text": "hunter2"})]),
        Turn([], done=True, result="Stopped: sign-in needed."),
    ]
    _ack(bclient)
    run_id = _start(bclient)
    _approve_plan(bclient, run_id)
    run = _wait(bclient, run_id, lambda r: r["pending"] is not None)
    r = _decide(bclient, run, "deny", feedback="Search by claim number instead")
    assert r.status_code == 200

    run = _wait(bclient, run_id, lambda r: r["pending"] and r["pending"]["risk"] == "person_only")
    assert "Passwords" in run["pending"]["needs_approval_reason"]
    assert _decide(bclient, run, "approve").status_code == 409
    assert _decide(bclient, run, "deny").status_code == 200
    run = _wait(bclient, run_id, lambda r: r["state"] == "done")
    assert runtime.acts == []  # nothing denied ever reached the browser
    denied = agent_box["agent"].received[1][0][0]
    assert denied[0] is False and "Search by claim number instead" in denied[1]


def test_away_supervisor_pauses_and_returns(tmp_path, runtime, agent_box):
    settings = make_settings(
        tmp_path, gcp_project="p", vertex_location="us-central1", browser_presence_timeout_s=0.3
    )
    agent_box["turns"] = [Turn([ProposedAction("a1", "scroll", {"direction": "down"})])]

    def factory(model, target, s, task):
        agent_box["agent"] = ScriptedAgent(agent_box["turns"])
        return agent_box["agent"]

    app = create_app(
        settings, FakeGateway(settings), browser_runtime=runtime, browser_agent_factory=factory
    )
    with TestClient(app) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        _ack(c)
        run_id = _start(c, allow_reading=True)
        _approve_plan(c, run_id)
        run = _wait(c, run_id, lambda r: r["state"] == "paused")
        assert run["pause_reason"] == "supervisor_away" and runtime.acts == []
        deadline = time.time() + 5
        while (
            time.time() < deadline
            and c.get(f"/api/browser/runs/{run_id}").json()["state"] != "done"
        ):
            screen = c.get(f"/api/browser/runs/{run_id}/screen")  # watching
            if screen.status_code == 409:  # the run finished between the two requests
                break
            assert screen.status_code == 200
            time.sleep(0.05)
        assert c.get(f"/api/browser/runs/{run_id}").json()["state"] == "done"
        assert runtime.acts == [{"kind": "scroll", "direction": "down"}]


def test_take_over_input_and_hand_back(bclient, runtime, agent_box):
    agent_box["turns"] = [
        Turn([ProposedAction("a1", "click", {"x": 1, "y": 1})]),
        Turn([], done=True, result="done"),
    ]
    _ack(bclient)
    run_id = _start(bclient)
    _approve_plan(bclient, run_id)
    run = _wait(bclient, run_id, lambda r: r["pending"] is not None)
    assert _decide(bclient, run, "take_over").status_code == 200
    run = _wait(bclient, run_id, lambda r: r["control"] == "human")
    assert run["state"] == "paused" and run["pause_reason"] == "human_control"

    typed = bclient.post(
        f"/api/browser/runs/{run_id}/input", json={"kind": "type", "text": "s3cret-code"}
    )
    assert typed.status_code == 200 and typed.json()["ok"]
    run = bclient.get(f"/api/browser/runs/{run_id}").json()
    mine = run["steps"][-1]
    assert mine["status"] == "by_person" and mine["description"] == "Supervisor typed 11 characters"
    assert "s3cret" not in str(run)

    bclient.post(
        f"/api/browser/runs/{run_id}/control", json={"action": "hand_back"}
    ).raise_for_status()
    _wait(bclient, run_id, lambda r: r["state"] == "done")
    outcomes, note = agent_box["agent"].received[-1]
    assert "took control" in note


def test_leaving_the_site_asks_and_extends_the_run(bclient, runtime, agent_box):
    agent_box["turns"] = [
        Turn([ProposedAction("a1", "navigate", {"url": "https://www.availity.com/claims"})]),
    ]
    _ack(bclient)
    run_id = _start(bclient, allow_reading=True)
    _approve_plan(bclient, run_id)
    run = _wait(bclient, run_id, lambda r: r["pending"] is not None)
    assert "outside this run: www.availity.com" in run["pending"]["needs_approval_reason"]
    _decide(bclient, run, "approve").raise_for_status()
    run = _wait(bclient, run_id, lambda r: r["state"] == "done")
    assert "www.availity.com" in run["allowed_hosts"] and runtime.allowed == ["www.availity.com"]


def test_inbox_stop_and_one_run_at_a_time(bclient, runtime, agent_box):
    _ack(bclient)
    run_id = _start(bclient)
    _wait(bclient, run_id, lambda r: r["state"] == "awaiting_plan")
    items = bclient.get("/api/browser/approvals").json()["items"]
    assert items[0]["type"] == "plan" and items[0]["run_id"] == run_id and items[0]["mine"]
    again = bclient.post(
        "/api/browser/runs",
        json={
            "task": "x" * 5,
            "start_url": "https://portal.example-payer.com/",
            "supervised": True,
        },
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_active"
    bclient.post(f"/api/browser/runs/{run_id}/control", json={"action": "stop"}).raise_for_status()
    _wait(bclient, run_id, lambda r: r["state"] == "stopped")
    deadline = time.time() + 3
    while time.time() < deadline and not runtime.closed:
        time.sleep(0.05)
    assert runtime.closed == ["s1"]
    assert bclient.get("/api/browser/approvals").json()["items"] == []


def test_not_configured_runtime_is_reported(client):
    s = client.get("/api/browser/settings").json()
    assert s["configured"] is False and {m["id"] for m in s["models"]} == {"gemini", "claude"}
    r = client.post(
        "/api/browser/runs",
        json={
            "task": "Check",
            "start_url": "https://portal.example-payer.com/",
            "supervised": True,
        },
    )
    assert r.status_code == 503


@pytest.mark.parametrize(
    ("response", "shown"),
    [
        (
            httpx.Response(422, json={"detail": "could not open npiregistry.cms.hhs.gov: x"}),
            "Browser runtime error 422: could not open npiregistry.cms.hhs.gov: x",
        ),
        (httpx.Response(500, text="Internal Server Error"), "error 500: Internal Server Error"),
    ],
)
async def test_runtime_errors_read_as_the_runtime_s_reason(response, shown):
    runtime = HttpRuntime(
        "https://runtime.test", "t0k", transport=httpx.MockTransport(lambda r: response)
    )
    with pytest.raises(RuntimeUnavailable) as caught:
        await runtime.open("https://npiregistry.cms.hhs.gov/", ["npiregistry.cms.hhs.gov"], None)
    assert shown in str(caught.value) and "{" not in str(caught.value)
