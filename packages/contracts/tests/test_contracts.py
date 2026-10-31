import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import careplix_contracts as contracts

ROOT = Path(__file__).resolve().parents[3]
CASES = sorted((ROOT / "fixtures/synthetic/cases").glob("*.json"))
SCRIPTS = sorted((ROOT / "fixtures/synthetic/payer_scripts").glob("*.json"))


@pytest.mark.parametrize("name", contracts.schema_names())
def test_schema_is_valid_and_namespaced(name):
    schema = contracts.load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"{contracts.SCHEMA_URN_PREFIX}{name}"


def test_tool_registry_loads_and_gates_pilot_tools():
    tools = contracts.tool_registry()
    assert set(contracts.demo_tools()) == {
        "end_call",
        "get_assigned_context",
        "record_finding",
        "send_dtmf",
        "set_call_state",
        "verify_caller",
    }
    # Pilot-only tools must never be enabled in the demo (FR13, FR15).
    for name, tool in tools.items():
        if tool["release"] != "D":
            assert not tool["enabled_in_demo"], name
    # The conversational model never holds case write-back authority.
    assert "agent_runtime" not in tools["write_case_result"]["allowed_roles"]


@pytest.mark.parametrize("tool", contracts.tool_registry().values(), ids=lambda t: t["name"])
def test_tool_argument_schemas_are_closed(tool):
    Draft202012Validator.check_schema(tool["arguments_schema"])
    assert tool["arguments_schema"].get("additionalProperties") is False


def test_fixture_counts():
    assert len(CASES) == 10
    assert len(SCRIPTS) == 3


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_case_fixture_valid(path):
    assert contracts.errors("synthetic_case", json.loads(path.read_text())) == []


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_payer_script_valid(path):
    script = json.loads(path.read_text())
    assert contracts.errors("payer_script", script) == []
    nodes = script["ivr"]["nodes"]
    for node in nodes.values():
        for target in [*node.get("options", {}).values(), node.get("next")]:
            assert target is None or target in nodes, f"dangling IVR edge to {target}"


def test_cases_reference_existing_scripts():
    script_ids = {json.loads(p.read_text())["script_id"] for p in SCRIPTS}
    for path in CASES:
        assert json.loads(path.read_text())["payer"]["payer_script_id"] in script_ids


# Phone-number shapes outside the reserved fictional 555-01xx range must not appear in fixtures.
_PHONE = re.compile(r"\+1\d{10}|\b\d{3}[ .-]\d{3}[ .-]\d{4}\b")
_FICTIONAL = re.compile(r"\+1\d{3}55501\d{2}")


@pytest.mark.parametrize("path", CASES + SCRIPTS, ids=lambda p: p.stem)
def test_fixtures_only_use_fictional_phone_numbers(path):
    for match in _PHONE.findall(path.read_text()):
        assert _FICTIONAL.fullmatch(match), f"non-fictional phone number {match!r} in {path.name}"


def test_outcome_requires_null_reason_for_missing_values():
    outcome = {
        "task_id": "7d1f0f0e-2b8e-4a53-9a3c-6c1f3f6f9c01",
        "attempt_id": "1b6c3a52-8e4f-4c55-9d2b-0f6e2f1c7a22",
        "agent_version_id": "3c2a1b0d-7e6f-4a5b-8c9d-0e1f2a3b4c5d",
        "reported_status": {"value": None, "status": "provisional"},
        "missing_requirements": [],
        "unresolved_questions": [],
        "next_action": "Retry",
        "review_status": "needs_review",
    }
    assert any("null_reason" in e for e in contracts.errors("prior_auth_outcome", outcome))
    outcome["reported_status"]["null_reason"] = "Representative could not locate the request"
    assert contracts.errors("prior_auth_outcome", outcome) == []
