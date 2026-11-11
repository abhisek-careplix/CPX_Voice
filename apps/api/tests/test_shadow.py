"""Shadow mode: CarePlix works an incumbent vendor's worklist again and compares the answers."""

import pytest
from fastapi.testclient import TestClient

from careplix_api.main import create_app
from careplix_api.models import AuditEvent, CallAttempt
from careplix_api.shadow import compare
from careplix_api.shadow import service as shadow_service
from careplix_api.shadow.models import ShadowItem

from .conftest import APPROVED, WORKER, FakeGateway, make_settings
from .test_lifecycle import _claim
from .test_practice import _db_rows

# --- comparing answers ----------------------------------------------------------------------------


def test_answers_compare_without_case_spacing_or_punctuation():
    assert compare.normalize(" PA-48213 ") == compare.normalize("pa 48213") == "pa48213"
    assert compare.normalize("N/A") == compare.normalize(None) == ""
    rows = compare.compare(
        ["ref", "status", "due", "rep"],
        {"ref": "CLM-1", "status": "Paid", "due": "2027-01-05"},
        {"ref": "clm 1", "status": "Denied", "rep": "Dana"},
    )
    assert [r["outcome"] for r in rows] == ["match", "mismatch", "ours_only", "theirs_only"]
    assert compare.resolved(["ref", "status"], {"ref": "x", "status": "y"})
    assert not compare.resolved(["ref", "status"], {"ref": "x", "status": "unknown"})


def test_the_parity_report_counts_finished_rows_only():
    def item(outcomes, ours_resolved, theirs_resolved, ours_s, theirs_s, state="done"):
        return {
            "state": state,
            "comparison": [{"field": f, "outcome": o, "adjudication": None} for f, o in outcomes],
            "our_metrics": {"resolved": ours_resolved, "duration_s": ours_s},
            "incumbent_metrics": {"resolved": theirs_resolved, "duration_s": theirs_s},
        }

    report = compare.summarise(
        ["ref", "status"],
        [
            item([("ref", "match"), ("status", "match")], True, True, 100, 250),
            item([("ref", "mismatch"), ("status", "match")], True, True, 140, 360),
            item([("ref", "match"), ("status", "neither")], False, False, 120, None),
            {"state": "running", "comparison": None, "our_metrics": {}, "incumbent_metrics": {}},
        ],
    )
    assert report["items"] == 4 and report["finished"] == 3 and report["open"] == 1
    assert report["agreement"] == 0.8 and report["disagreements"] == 1
    assert report["resolution"] == {"ours": 0.667, "theirs": 0.667}
    assert report["median_duration_s"] == {"ours": 120, "theirs": 305}
    assert report["adjudicated"]["open"] == 1
    by_field = {f["field"]: f for f in report["fields"]}
    assert by_field["ref"]["agreement"] == 0.667 and by_field["status"]["neither"] == 1


def test_durations_and_flags_read_the_way_vendors_export_them():
    assert shadow_service._seconds("4:10") == 250
    assert shadow_service._seconds("3m 4s") == 184
    assert shadow_service._seconds("184") == 184 and shadow_service._seconds("") is None
    assert shadow_service._flag("Resolved") is True and shadow_service._flag("no") is False
    assert shadow_service._flag("maybe") is None


# --- a replay run, end to end ---------------------------------------------------------------------

CSV = """case_id,member_id,claim_number,phone,status_reported,ref,minutes,done
SV-1,SYN-11,CLM-501,+12025550142,Paid,CLM-90217,4:10,yes
SV-2,SYN-22,CLM-502,+12025550142,Denied,CLM-11111,6:00,yes
SV-3,SYN-33,CLM-503,+12025550142,,CLM-33333,,no
"""

MAPPING = {
    "external_ref": "case_id",
    "destination": "phone",
    "duration_s": "minutes",
    "resolved": "done",
    "facts": {"member_id": "member_id", "claim_number": "claim_number"},
    "values": {"reference_number": "ref", "result_summary": "status_reported"},
}


@pytest.fixture
def sclient(tmp_path):
    settings = make_settings(
        tmp_path,
        gcp_project="careplix-labs",
        vertex_location="us-central1",
        run_background=False,
        eval_concurrency=2,
    )
    gateway = FakeGateway(settings)
    with TestClient(create_app(settings, gateway)) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        c.post("/api/scenarios/examples").raise_for_status()
        yield c


def _claim_status(c):
    scenarios = c.get("/api/scenarios").json()["scenarios"]
    return next(s for s in scenarios if s["name"].startswith("2a"))


def _start(c, **overrides):
    scenario = _claim_status(c)
    body = {
        "name": "Claim status, October worklist",
        "incumbent": "SuperDial",
        "agent_id": scenario["agent_id"],
        "use": "published",
        "scenario_id": scenario["id"],
        "mode": "replay",
        "csv": CSV,
        "mapping": MAPPING,
        **overrides,
    }
    return c.post("/api/shadow-runs", json=body)


def _tick(c):
    state = c.app.state
    c.portal.call(shadow_service.tick, state.db, state.settings, state.gateway, state.runner)


def _play(c, attempt_id, findings, token=None):
    """The call runs to the end and the agent records what it heard."""
    token = token or _claim(c, attempt_id)["attempt_token"]
    auth = {"Authorization": f"Bearer {token}"}
    segment = f"{attempt_id[:8]}-s1"
    items = [
        {"event_id": f"{attempt_id}-j", "type": "participant.joined", "payload": {}},
        {
            "event_id": f"{attempt_id}-t",
            "type": "transcript.segment",
            "payload": {
                "segment_id": segment,
                "speaker": "counterpart",
                "text": "Here is what I have for that claim.",
                "playout": "played",
            },
        },
    ]
    c.post(f"/internal/attempts/{attempt_id}/events", json={"events": items}, headers=auth)
    for n, (field, value) in enumerate(findings.items()):
        r = c.post(
            f"/internal/attempts/{attempt_id}/tools/record_finding",
            json={
                "call_id": f"f{n}",
                "arguments": {"field": field, "value": value, "confirmation": "stated_once"},
                "evidence_segment_ids": [segment],
            },
            headers=auth,
        )
        assert r.status_code == 200, r.text
    done = [{"event_id": f"{attempt_id}-end", "type": "worker.done", "payload": {"reason": "done"}}]
    c.post(f"/internal/attempts/{attempt_id}/events", json={"events": done}, headers=auth)


def test_preview_reads_the_header_and_counts_rows(sclient):
    r = sclient.post("/api/shadow-runs/preview", json={"csv": CSV})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"][:3] == ["case_id", "member_id", "claim_number"]
    assert body["total"] == 3 and body["sample"][0]["ref"] == "CLM-90217"
    empty = sclient.post("/api/shadow-runs/preview", json={"csv": "a,b\n"})
    assert empty.json()["error"]["code"] == "csv_empty"


def test_a_replay_run_compares_answers_and_people_settle_disagreements(sclient):
    started = _start(sclient)
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["state"] == "queued" and run["fields"] == ["reference_number", "result_summary"]
    assert [i["external_ref"] for i in run["items"]] == ["SV-1", "SV-2", "SV-3"]
    assert run["items"][0]["theirs"] == {"reference_number": "CLM-90217", "result_summary": "Paid"}

    _tick(sclient)
    run = sclient.get(f"/api/shadow-runs/{run['id']}").json()
    running = [i for i in run["items"] if i["state"] == "running"]
    assert len(running) == 2  # simulations use the suite lane, not the one-call limit
    attempts = {a.id: a for a in _db_rows(sclient, CallAttempt)}
    first = attempts[running[0]["attempt_id"]]
    assert first.pins["simulated"] and first.pins["shadow_item_id"] == running[0]["id"]

    # The agent is told the row's facts, not the scenario's.
    claimed = _claim(sclient, first.id)
    tokens = {first.id: claimed["attempt_token"]}
    brief = claimed["brief"]
    assert brief["context"] == {"claim_number": "CLM-501", "member_id": "SYN-11"}
    # The simulated other side answers from what the incumbent recorded.
    room = f"att_{first.id.replace('-', '')}"
    role = sclient.post(
        f"/internal/attempts/{first.id}/counterpart",
        json={"room": room, "job_id": "AJ_cp"},
        headers=WORKER,
    ).json()
    assert "reference number: CLM-90217" in role["instructions"]
    assert "SYN-11" in role["instructions"]

    answers = {
        "SV-1": {"reference_number": "CLM-90217", "result_summary": "paid"},
        "SV-2": {"reference_number": "CLM-22222", "result_summary": "Denied"},
        "SV-3": {"reference_number": "clm 33333"},
    }
    for _ in range(10):
        run = sclient.get(f"/api/shadow-runs/{run['id']}").json()
        if run["state"] == "done":
            break
        for item in run["items"]:
            if item["state"] == "running":
                attempt_id = item["attempt_id"]
                if attempt_id not in tokens or tokens[attempt_id] != "played":
                    _play(
                        sclient, attempt_id, answers[item["external_ref"]], tokens.get(attempt_id)
                    )
                    tokens[attempt_id] = "played"
        _tick(sclient)
    assert run["state"] == "done", run

    by_ref = {i["external_ref"]: i for i in run["items"]}
    assert [r["outcome"] for r in by_ref["SV-1"]["comparison"]] == ["match", "match"]
    assert [r["outcome"] for r in by_ref["SV-2"]["comparison"]] == ["mismatch", "match"]
    assert [r["outcome"] for r in by_ref["SV-3"]["comparison"]] == ["match", "neither"]
    assert by_ref["SV-2"]["ours"]["reference_number"] == "CLM-22222"
    report = run["summary"]
    assert report["finished"] == 3 and report["agreement"] == 0.8
    assert report["disagreements"] == 1 and report["adjudicated"]["open"] == 1
    assert report["resolution"] == {"ours": 0.667, "theirs": 0.667}
    assert report["median_duration_s"]["theirs"] == 305

    settled = sclient.post(
        f"/api/shadow-runs/{run['id']}/items/{by_ref['SV-2']['id']}/adjudicate",
        json={"field": "reference_number", "verdict": "ours"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["summary"]["adjudicated"] == {
        "ours": 1,
        "theirs": 0,
        "both": 0,
        "neither": 0,
        "open": 0,
    }
    nothing = sclient.post(
        f"/api/shadow-runs/{run['id']}/items/{by_ref['SV-1']['id']}/adjudicate",
        json={"field": "reference_number", "verdict": "ours"},
    )
    assert nothing.json()["error"]["code"] == "nothing_to_settle"
    actions = {a.action for a in _db_rows(sclient, AuditEvent)}
    assert {"shadow.created", "shadow.adjudicated"} <= actions
    # Both sides' answers and the row's facts are sealed at rest.
    stored = _db_rows(sclient, ShadowItem)[0]
    assert "CLM-90217" not in (stored.incumbent_values or "")
    assert "SYN-11" not in (stored.facts or "")


def test_a_run_rejects_fields_the_agent_never_records(sclient):
    bad = _start(sclient, mapping={**MAPPING, "values": {"favourite_colour": "ref"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_field"
    missing = _start(sclient, mapping={**MAPPING, "values": {"reference_number": "nope"}})
    assert missing.json()["error"]["code"] == "unknown_column"


def test_live_shadow_calls_only_approved_numbers(sclient):
    other = CSV.replace(APPROVED, "+13125550199", 1)
    refused = _start(sclient, mode="live", csv=other)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "destination_not_approved"


def test_cancelling_stops_open_rows(sclient):
    run = _start(sclient).json()
    _tick(sclient)
    cancelled = sclient.post(f"/api/shadow-runs/{run['id']}/cancel").json()
    assert cancelled["state"] == "cancelled"
    assert {i["state"] for i in cancelled["items"]} == {"cancelled"}
