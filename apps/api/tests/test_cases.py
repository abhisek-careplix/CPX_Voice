"""Cases: typed work items, and tasks created from them with the right agent, number and facts."""

import time

from sqlalchemy import select

from careplix_api.cases import types

from .conftest import APPROVED
from .test_lifecycle import _agent, _claim, _publish
from .test_redteam import _errand


def _create(client, case_type, **fields):
    return client.post("/api/cases", json={"case_type": case_type, "fields": fields})


def _reminder(client, **overrides):
    fields = {
        "patient_name": "Synthetic Patient Rho",
        "patient_phone": APPROVED,
        "date_of_birth": "1950-04-02",
        "practice_name": "Riverside Family Medicine (synthetic)",
        "practice_phone": "+12025550160",
        "clinician_name": "Dr. Rowan Synthetic",
        "appointment_at": "2026-11-20 09:00",
        "location": "12 River Road, Suite 200",
        "prep_notes": "Bring your medication list.",
    } | overrides
    response = _create(client, "appointment_reminder", **fields)
    assert response.status_code == 200, response.text
    return response.json()


def _published(client, template_id):
    agent = _errand(client) if template_id == "errand" else _agent(client, template_id)
    if not agent["current"]:
        _publish(client, agent["id"])
    return agent


def test_case_types_say_who_to_call_and_which_agent_works_them(client):
    listed = {t["id"]: t for t in client.get("/api/case-types").json()}
    assert set(listed) == set(types.CASE_TYPES)
    assert listed["prior_authorization"]["agents"] == ["auth-status"]
    assert listed["appointment_reminder"]["counterparty_role"] == "patient"
    assert "outreach-consent" in listed["appointment_reminder"]["packs"]
    assert all(t["fields"] for t in listed.values())


def test_seeded_cases_are_open_prior_auths_with_the_payer_on_file(client):
    rows = client.get("/api/cases").json()
    assert rows and {r["case_type"] for r in rows} == {"prior_authorization"}
    assert {r["status"] for r in rows} == {"open"}
    first = next(r for r in rows if r["case_ref"] == "SYN-CASE-0001")
    detail = client.get(f"/api/cases/{first['id']}").json()
    assert detail["counterparty"]["name"] == "Northwind Health Plan (synthetic)"
    assert detail["plan"]["destination"]["on_file"] == "+12025550150"
    assert detail["plan"]["destination"]["on_file_approved"] is False
    assert APPROVED in detail["plan"]["destination"]["approved"]
    assert detail["plan"]["agents"][0]["template_id"] == "auth-status"


def test_samples_for_every_type_are_complete_and_numbered(client):
    for type_id, t in types.CASE_TYPES.items():
        made = client.post("/api/cases/samples", json={"case_type": type_id, "count": 2})
        assert made.status_code == 200, made.text
        rows = made.json()
        assert [r["missing"] for r in rows] == [[], []]
        assert all(r["case_ref"].startswith(f"SYN-{t['prefix']}-") for r in rows)
        assert all(r["source"] == "sample" and r["synthetic"] for r in rows)
    claims = client.get("/api/cases?case_type=claim_status").json()
    assert [c["case_ref"] for c in claims] == ["SYN-CLM-0001", "SYN-CLM-0002"]


def test_hand_entered_cases_must_be_synthetic_and_complete(client):
    real = _create(client, "appointment_reminder", patient_name="Maria Lopez")
    assert real.status_code == 422 and real.json()["error"]["code"] == "synthetic_only"
    gaps = _create(client, "appointment_reminder", patient_name="Synthetic Patient Mu")
    assert gaps.json()["error"]["code"] == "case_incomplete"
    assert "appointment_at" in gaps.json()["error"]["detail"]["missing"]
    odd = _create(client, "claim_status", patient_name="Synthetic X", favourite_colour="blue")
    assert odd.json()["error"]["code"] == "unknown_field"
    case = _reminder(client)
    assert case["case_ref"] == "SYN-APT-0001" and case["status"] == "open"
    assert case["source"] == "manual"


def test_search_and_filters(client):
    _reminder(client)
    assert [c["case_ref"] for c in client.get("/api/cases?q=rho").json()] == ["SYN-APT-0001"]
    assert client.get("/api/cases?status=done").json() == []
    assert len(client.get("/api/cases?case_type=appointment_reminder").json()) == 1


def test_an_agent_must_be_built_for_the_case(client):
    case = _reminder(client)
    auth = _published(client, "auth-status")
    wrong = client.post(
        "/api/tasks", json={"agent_id": auth["id"], "case_id": case["id"], "destination": APPROVED}
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "agent_cannot_work_case"


def test_a_task_from_a_case_gets_its_objective_facts_and_packs(client):
    case = _reminder(client)
    errand = _published(client, "errand")
    plan = client.get(f"/api/cases/{case['id']}").json()["plan"]
    assert plan["suggested_agent_id"] == errand["id"]
    assert plan["destination"]["on_file"] == APPROVED and plan["destination"]["on_file_approved"]

    task = client.post(
        "/api/tasks",
        json={"agent_id": errand["id"], "case_id": case["id"], "destination": APPROVED},
    ).json()
    assert "Synthetic Patient Rho" in task["objective"] and "2026-11-20 09:00" in task["objective"]
    assert task["case"]["type_name"] == "Appointment reminder"
    assert client.get(f"/api/cases/{case['id']}").json()["status"] == "in_progress"

    attempt = client.post(
        f"/api/tasks/{task['id']}/calls", headers={"Idempotency-Key": "case-1"}
    ).json()
    for _ in range(40):
        if client.get(f"/api/calls/{attempt['id']}").json()["state"] == "Queued":
            break
        time.sleep(0.05)
    claim = _claim(client, attempt["id"])
    brief = claim["brief"]
    assert brief["context"]["clinician_name"] == "Dr. Rowan Synthetic"
    assert brief["authority"]["on_behalf_of"] == "Riverside Family Medicine (synthetic)"
    assert "patient_phone" not in brief["authority"]["disclose_allowlist"]
    assert "prep_notes" in brief["authority"]["disclose_allowlist"]
    assert "Outreach consent:" in claim["instructions"]["guardrails"]
    assert "Patient privacy:" in claim["instructions"]["guardrails"]


def test_only_prior_auth_cases_ask_the_payer_api(client):
    case = _reminder(client)
    errand = _published(client, "errand")
    task = client.post(
        "/api/tasks",
        json={"agent_id": errand["id"], "case_id": case["id"], "destination": APPROVED},
    ).json()
    from careplix_api.models import Task

    from .test_practice import _db_rows

    row = next(t for t in _db_rows(client, Task) if t.id == task["id"])
    assert row.electronic_check is None


def test_reviewed_outcomes_move_the_case(client):
    case = _reminder(client)
    errand = _published(client, "errand")

    def task():
        return client.post(
            "/api/tasks",
            json={"agent_id": errand["id"], "case_id": case["id"], "destination": APPROVED},
        ).json()

    first = task()
    client.post(
        f"/api/tasks/{first['id']}/outcome",
        json={"outcome": "Unsuccessful", "reason": "No answer twice."},
    ).raise_for_status()
    assert client.get(f"/api/cases/{case['id']}").json()["status"] == "open"
    second = task()
    client.post(
        f"/api/tasks/{second['id']}/outcome",
        json={"outcome": "Completed", "reason": "Patient confirmed."},
    ).raise_for_status()
    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["status"] == "done" and len(detail["tasks"]) == 2
    closed = client.post(f"/api/cases/{case['id']}/status", json={"status": "closed"}).json()
    assert closed["status"] == "closed"


def test_rows_from_before_case_types_are_brought_forward(client):
    from careplix_api.cases import service
    from careplix_api.models import Case

    async def age_and_backfill():
        async with client.app.state.db.sessions() as session:
            case = (
                await session.scalars(select(Case).where(Case.case_ref == "SYN-CASE-0002"))
            ).one()
            payer = {k: v for k, v in case.data["payer"].items() if k != "phone"}
            case.data = {**case.data, "payer": payer}
            case.case_type = case.status = case.source = None
            await session.commit()
        async with client.app.state.db.sessions() as session:
            await service.backfill(session)
        async with client.app.state.db.sessions() as session:
            return (
                await session.scalars(select(Case).where(Case.case_ref == "SYN-CASE-0002"))
            ).one()

    case = client.portal.call(age_and_backfill)
    assert (case.case_type, case.status, case.source) == ("prior_authorization", "open", "sample")
    assert case.data["payer"]["phone"] == "+12025550150"
