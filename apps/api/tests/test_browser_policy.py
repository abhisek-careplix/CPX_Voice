"""Risk tiers and the model adapters' action mapping (no network)."""

from types import SimpleNamespace

from careplix_api.browser import policy
from careplix_api.browser.adapters import ClaudeAgent, GeminiAgent, Shot, parse_plan


def test_risk_tiers():
    link = {"tag": "a", "text": "Claims"}
    submit = {"tag": "button", "text": "Submit", "in_form": True, "type": "submit"}
    search = {"tag": "button", "text": "Search", "in_form": True, "type": "submit"}
    pay = {"tag": "button", "text": "Pay now"}
    box = {"tag": "input", "type": "checkbox"}
    assert policy.classify({"kind": "click"}, link).risk == policy.READ
    assert policy.classify({"kind": "click"}, submit).risk == policy.COMMIT
    assert policy.classify({"kind": "click"}, search).risk == policy.READ
    assert policy.classify({"kind": "click"}, pay).risk == policy.PERSON
    assert policy.classify({"kind": "click"}, box).risk == policy.ENTER
    assert policy.classify({"kind": "type", "text": "x"}, {"tag": "input"}).risk == policy.ENTER
    assert (
        policy.classify({"kind": "type", "text": "x"}, {"is_password": True}).risk == policy.PERSON
    )
    field = {"tag": "input", "in_form": True, "name": "member_id"}
    assert policy.classify({"kind": "key", "text": "Return"}, field).risk == policy.COMMIT
    assert policy.classify({"kind": "scroll"}, None).risk == policy.READ


def test_commit_always_asks_and_allowances_cover_only_read_and_entry():
    assert policy.needs_approval(policy.COMMIT, {policy.READ, policy.ENTER, policy.COMMIT})
    assert policy.needs_approval(policy.PERSON, set(policy.TIERS))
    assert not policy.needs_approval(policy.READ, {policy.READ})
    assert policy.needs_approval(policy.READ, set())
    assert policy.needs_approval(policy.READ, {policy.READ}, forced_reason="model asked")


def test_gemini_coordinates_are_scaled_and_confirmations_kept():
    agent = object.__new__(GeminiAgent)
    agent._calls = {}
    shot = Shot("", "https://x", 1280, 800)
    fc = SimpleNamespace(
        id="c1",
        name="type_text_at",
        args={
            "x": 500,
            "y": 250,
            "text": "W1",
            "press_enter": False,
            "safety_decision": {"decision": "require_confirmation", "explanation": "Sends a form"},
        },
    )
    action = agent._to_action(fc, shot)
    assert action.kind == "type" and action.params["x"] == 640 and action.params["y"] == 200
    assert action.params["press_enter"] is False and action.needs_confirmation == "Sends a form"
    nav = agent._to_action(
        SimpleNamespace(id=None, name="navigate", args={"url": "https://a"}), shot
    )
    assert nav.kind == "navigate" and nav.params == {"url": "https://a"}


def test_claude_toolset_members_map_to_actions():
    agent = object.__new__(ClaudeAgent)
    click = SimpleNamespace(
        id="t1",
        name="left_click",
        toolset_name="computer",
        input={"coordinate": [100, 200], "text": "shift"},
    )
    action = agent._to_action(click)
    assert action.kind == "click" and action.params == {"x": 100, "y": 200, "modifiers": ["shift"]}
    scroll = SimpleNamespace(
        id="t2",
        name="scroll",
        toolset_name="computer",
        input={"coordinate": [5, 6], "scroll_direction": "up", "scroll_amount": 2},
    )
    assert agent._to_action(scroll).params == {"x": 5, "y": 6, "direction": "up", "amount": 2}
    nav = SimpleNamespace(id="t3", name="navigate", toolset_name=None, input={"url": "https://a"})
    assert agent._to_action(nav).kind == "navigate"
    look = SimpleNamespace(id="t4", name="screenshot", toolset_name="computer", input={})
    assert agent._to_action(look).kind == "screenshot"


def test_plans_are_parsed_leniently():
    assert parse_plan('{"steps": ["Open Claims", "Search"]}') == ["Open Claims", "Search"]
    assert parse_plan("1. Open Claims\n2. Search") == ["Open Claims", "Search"]
