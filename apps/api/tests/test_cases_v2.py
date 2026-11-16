"""Cases v2: the completion contract, playbooks, contact-many and the one review inbox."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from careplix_api.cases import attempts
from careplix_api.cases.models import ContactAttempt
from careplix_api.db import utcnow
from careplix_api.main import create_app
from careplix_api.models import AuditEvent, Task

from .conftest import APPROVED, FakeGateway, make_settings
from .test_cases import _reminder
from .test_practice import _client, _db_rows, _worker


class FakeText:
    """Stands in for workstream C's text channel: records sends and "it's filled" notices."""

    name = "text"

    def __init__(self):
        self.sent: list[tuple[str, str | None]] = []
        self.notices: list[tuple[str, str]] = []

    async def send(self, ctx):
        self.sent.append((ctx.attempt.person_label, ctx.address))
        return attempts.SendResult("sent", message_id=f"SM{len(self.sent)}")

    async def notify(self, ctx, text):
        self.notices.append((ctx.attempt.person_label, text))
        return attempts.SendResult("sent", message_id=f"SN{len(self.notices)}")


@pytest.fixture
def text():
    channel = FakeText()
    attempts.register(channel)
    yield channel
    attempts.unregister("text")


@pytest.fixture
def quiet(tmp_path):
    """A client with no background loops, so each test drives the playbook engine itself."""
    settings = make_settings(tmp_path, run_background=False, playbook_contact_hours=False)
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _engine(c):
    return c.portal.call(c.app.state.playbooks.run_once)


def _case(c, case_id):
    return c.get(f"/api/cases/{case_id}").json()


def _status(c, case_id, status, **extra):
    return c.post(f"/api/cases/{case_id}/status", json={"status": status, **extra})


# --- 1. the completion contract -------------------------------------------------------------------


def test_the_contract_blocks_a_premature_done(client):
    case = _reminder(client)
    early = _status(client, case["id"], "done")
    assert early.status_code == 409 and early.json()["error"]["code"] == "contract_unmet"
    assert early.json()["error"]["detail"]["missing"] == [
        "evidence",
        "system_of_record",
        "informed",
        "next_action",
    ]

    cid = case["id"]
    client.post(
        f"/api/cases/{cid}/evidence",
        json={"kind": "call", "ref": "CA-synthetic-1", "label": "Reminder call recording"},
    ).raise_for_status()
    client.post(
        f"/api/cases/{cid}/ack", json={"system": "nexhealth", "ack": "appt-500-confirmed"}
    ).raise_for_status()
    client.post(
        f"/api/cases/{cid}/informed",
        json={"audience": "patient", "channel": "call", "label": "Told on the call"},
    ).raise_for_status()
    # A failed message doesn't count as telling the staff owner.
    client.post(
        f"/api/cases/{cid}/informed",
        json={"audience": "staff", "channel": "email", "label": "Bounced", "delivery": "failed"},
    ).raise_for_status()
    client.put(f"/api/cases/{cid}/next-action", json={"nothing_left": True}).raise_for_status()
    still = _status(client, cid, "done")
    assert still.status_code == 409
    assert still.json()["error"]["detail"]["missing"] == ["informed"]

    client.post(
        f"/api/cases/{cid}/informed",
        json={"audience": "staff", "channel": "studio", "label": "Front desk told"},
    ).raise_for_status()
    done = _status(client, cid, "done")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "done" and body["contract"]["holds"]
    assert body["system_of_record"] == {"system": "nexhealth", "ack": "appt-500-confirmed"}


def test_other_statuses_need_their_reason_or_owner(client):
    case = _reminder(client)
    cid = case["id"]
    assert _status(client, cid, "blocked").json()["error"]["code"] == "reason_required"
    assert _status(client, cid, "failed").json()["error"]["code"] == "reason_required"
    blocked = _status(client, cid, "blocked", reason="Waiting on the payer portal").json()
    assert (
        blocked["status"] == "blocked" and blocked["status_reason"] == "Waiting on the payer portal"
    )
    handed = _status(client, cid, "handed_off", next_step="Call the patient back").json()
    # The person handing off owns it unless they name someone, with a default due date.
    assert handed["status"] == "handed_off" and handed["owner"]["name"] == "Demo Administrator"
    assert handed["next_step"] == "Call the patient back" and handed["due_at"]
    assert handed["contract"]["parts"][3]["met"]  # owned next action


def test_clinical_cases_also_need_a_clinician(client):
    made = client.post("/api/cases/samples", json={"case_type": "care_outreach", "count": 1})
    cid = made.json()[0]["id"]
    for path, body in (
        ("evidence", {"kind": "note", "label": "Patient asked for a call"}),
        ("ack", {"system": "care", "ack": "opp-1"}),
        ("informed", {"audience": "patient", "channel": "call", "label": "Told"}),
        ("informed", {"audience": "staff", "channel": "studio", "label": "Told"}),
    ):
        client.post(f"/api/cases/{cid}/{path}", json=body).raise_for_status()
    client.put(f"/api/cases/{cid}/next-action", json={"nothing_left": True}).raise_for_status()
    assert _status(client, cid, "done").json()["error"]["detail"]["missing"] == ["clinician"]
    signed = client.post(f"/api/cases/{cid}/clinician-signoff").json()
    assert signed["clinician"]["by"] and signed["contract"]["holds"]
    assert _status(client, cid, "done").status_code == 200


def test_overdue_cases_escalate_to_their_owner(quiet):
    c = quiet
    case = _reminder(c)
    me = c.get("/api/me").json()
    c.put(
        f"/api/cases/{case['id']}/next-action",
        json={
            "owner_id": me["user_id"],
            "due_at": (utcnow() - timedelta(hours=1)).isoformat(),
            "next_step": "Call the patient back",
        },
    ).raise_for_status()
    assert _case(c, case["id"])["overdue"]
    assert _engine(c)["escalated"] == 1
    assert _engine(c)["escalated"] == 0  # once per due date
    detail = _case(c, case["id"])
    assert detail["escalated"] and detail["owner"]["id"] == me["user_id"]
    inbox = c.get("/api/inbox?kind=escalation").json()["items"]
    assert [(i["case"]["id"], i["mine"]) for i in inbox] == [(case["id"], True)]
    assert any(e.action == "case.escalated" for e in _db_rows(c, AuditEvent))


# --- 2. a synthetic trigger creates a case and a playbook works it --------------------------------


def test_an_approved_care_opportunity_becomes_a_case_the_playbook_finishes(quiet, text):
    c = quiet
    c.post("/api/care-opportunities/sample").raise_for_status()
    opp = c.get("/api/care-opportunities?status=proposed").json()["items"][0]
    c.post(
        f"/api/care-opportunities/{opp['id']}/decide", json={"decision": "approve"}
    ).raise_for_status()

    report = _engine(c)
    assert report["created"]["care_opportunity"] == 1
    [case] = c.get("/api/cases?case_type=care_outreach").json()
    assert case["source"] == "care_opportunity" and case["playbook"] == "care_outreach"
    detail = _case(c, case["id"])
    # The import had no phone number, so the playbook blocks with the reason and an owner.
    assert detail["status"] == "blocked" and "patient phone" in detail["status_reason"]
    assert detail["owner"]["name"] == "Demo Administrator" and detail["next_step"]
    assert detail["clinician"]["by"]  # the approval is the clinician part
    assert detail["playbook_run"]["state"] == "blocked"

    c.put(
        f"/api/cases/{case['id']}/facts",
        json={"fields": {"patient_phone": APPROVED, "practice_phone": "+12025550160"}},
    ).raise_for_status()
    resumed = c.post(f"/api/cases/{case['id']}/playbook/resume").json()
    assert resumed["status"] == "in_progress"
    [attempt] = resumed["attempts"]
    assert attempt["channel"] == "text" and attempt["state"] == "sent"
    assert attempt["address"] == "…0142" and text.sent[0][1] == APPROVED

    done = c.post(f"/api/attempts/{attempt['id']}/reply", json={"answer": "yes"}).json()
    assert done["status"] == "done", done["playbook_run"]
    assert done["contract"]["holds"] and done["playbook_run"]["state"] == "done"
    assert done["system_of_record"]["ack"].endswith(":enrolled")
    assert done["next_step"] == "Enrol the patient in the program"
    told = {(lk["audience"], lk["channel"]) for lk in done["links"] if lk["part"] == "informed"}
    assert told == {("patient", "text"), ("staff", "studio")}
    statuses = {o["id"]: o["status"] for o in c.get("/api/care-opportunities").json()["items"]}
    assert statuses[opp["id"]] == "enrolled"
    _engine(c)  # nothing more to create for the same opportunity
    assert len(c.get("/api/cases?case_type=care_outreach").json()) == 1


def test_a_webhook_trigger_hands_off_when_no_channel_can_reach_anyone(quiet):
    c = quiet
    made = c.post(
        "/api/playbooks/appointment_reminder/trigger",
        json={
            "fields": {
                "patient_name": "Synthetic Patient Tau",
                "patient_phone": "+12025550199",  # not an approved destination
                "date_of_birth": "1951-02-03",
                "practice_name": "Riverside Family Medicine (synthetic)",
                "practice_phone": "+12025550160",
                "clinician_name": "Dr. Rowan Synthetic",
                "appointment_at": "2026-11-20 09:00",
                "location": "12 River Road",
            }
        },
    ).json()
    assert made["source"] == "webhook" and made["playbook"] == "appointment_reminder"
    # No text channel yet, and the call is refused (no published agent / unapproved number).
    assert made["status"] == "handed_off" and "Couldn't reach anyone" in made["status_reason"]
    assert made["owner"]["name"] == "Demo Administrator" and made["due_at"]
    # The hand-off waits for its owner in the review inbox.
    [waiting] = c.get("/api/inbox?kind=hand_off").json()["items"]
    assert waiting["case"]["id"] == made["id"] and waiting["mine"]
    real = c.post(
        "/api/playbooks/appointment_reminder/trigger",
        json={"fields": {"patient_name": "Maria Lopez"}},
    )
    assert real.json()["error"]["code"] == "synthetic_only"


def test_the_ehr_schedule_starts_reminders_and_a_yes_confirms_the_visit(tmp_path, text):
    c, router = _client(
        tmp_path,
        playbook_contact_hours=False,
        sandbox_number="+13127558589",
        backfill_enabled=False,
    )
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        assert _engine(c)["created"]["schedule"] == 2  # appointments 500 (2 days) and 502 (20 h)
        rows = {r["case_ref"]: r for r in c.get("/api/cases?case_type=appointment_reminder").json()}
        by_status = {r["status"] for r in rows.values()}
        assert by_status == {"in_progress", "blocked"}  # patient 2 has no phone on file
        live = next(r for r in rows.values() if r["status"] == "in_progress")
        [attempt] = _case(c, live["id"])["attempts"]
        c.post(f"/api/attempts/{attempt['id']}/reply", json={"answer": "yes"}).raise_for_status()
        waiting = _case(c, live["id"])
        assert waiting["playbook_run"]["steps"][4]["state"] == "current"  # await the EHR's ack
        _worker(c)  # the practice worker sends the auto-approved confirmation
        c.portal.call(c.app.state.playbooks.advance, live["id"])
        done = _case(c, live["id"])
        assert done["status"] == "done", done["playbook_run"]
        assert done["system_of_record"]["ack"].startswith("write_intent:")
        assert router.nexhealth.appointments[500]["confirmed"] is True


# --- 3. contact many: exactly one winner ----------------------------------------------------------


def test_contact_many_books_exactly_one_winner_and_tells_the_rest(tmp_path, text):
    c, router = _client(tmp_path, playbook_contact_hours=False, sandbox_number="+13127558589")
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        _worker(c)  # eligibility, then the cancelled slot opens with patients 4 and 1 ranked
        report = _engine(c)
        assert report["created"]["backfill_opening"] == 1
        [case] = c.get("/api/cases?case_type=slot_backfill").json()
        detail = _case(c, case["id"])
        assert detail["source"] == "backfill" and detail["status"] == "in_progress"
        assert [a["state"] for a in detail["attempts"]] == ["sent", "sent"]  # both at once
        assert {a["person_ref"] for a in detail["attempts"]} == {"4", "1"}

        ids = [a["id"] for a in detail["attempts"]]

        async def both_say_yes():
            async def one(attempt_id):
                async with c.app.state.db.sessions() as session:
                    attempt = await session.get(ContactAttempt, attempt_id)
                    await attempts.record_reply(
                        session, c.app.state.settings, attempt, "yes", "test"
                    )
                    await session.commit()
                    return attempt.state

            return await asyncio.gather(*(one(i) for i in ids))

        states = c.portal.call(both_say_yes)
        assert sorted(states) == ["confirmed", "too_late"]
        _engine(c)
        done = _case(c, case["id"])
        winner = next(a for a in done["attempts"] if a["state"] == "confirmed")
        loser = next(a for a in done["attempts"] if a["state"] != "confirmed")
        assert done["winner_attempt_id"] == winner["id"]
        assert done["status"] == "done", done["playbook_run"]
        assert len(router.nexhealth.booked) == 1
        assert str(router.nexhealth.booked[0]["patient_id"]) == winner["person_ref"]
        assert loser["notice"] == "sent" and len(text.notices) == 1
        board = c.get("/api/practices/backfill").json()
        assert board["active"] == [] and board["recent"][0]["state"] == "filled"


def test_a_late_yes_never_takes_the_slot(quiet, text):
    c = quiet
    made = c.post(
        "/api/playbooks/slot_backfill/trigger",
        json={
            "fields": {
                "practice_name": "Riverside Family Medicine (synthetic)",
                "practice_phone": "+12025550160",
                "clinician_name": "Dr. Rowan Synthetic",
                "slot_at": "2026-11-20 09:00",
                "minutes": 30,
            },
            "contacts": [
                {"label": "A. S.", "address": "+12025550171"},
                {"label": "B. S.", "address": "+12025550172"},
                {"label": "C. S.", "address": "+12025550173"},
            ],
        },
    ).json()
    first, second, third = made["attempts"]
    c.post(f"/api/attempts/{second['id']}/reply", json={"answer": "no"}).raise_for_status()
    after = c.post(f"/api/attempts/{third['id']}/reply", json={"answer": "yes"}).json()
    states = {a["id"]: a["state"] for a in after["attempts"]}
    assert states == {first["id"]: "superseded", second["id"]: "declined", third["id"]: "confirmed"}
    assert text.notices == [("A. S.", attempts.FILLED_NOTICE)]
    late = c.post(f"/api/attempts/{first['id']}/reply", json={"answer": "yes"}).json()
    assert {a["id"]: a["state"] for a in late["attempts"]}[first["id"]] == "too_late"
    assert late["winner_attempt_id"] == third["id"]
    # A webhook slot has no opening to book, so the playbook hands the booking to a person.
    assert late["status"] == "handed_off" and "offer list" in late["status_reason"]


# --- 4. the one review inbox ----------------------------------------------------------------------


def test_the_inbox_lists_results_ehr_writes_browser_approvals_and_signoffs(tmp_path):
    from careplix_api.browser.models import BrowserRun, BrowserStep

    from .test_cases import _published

    c, _ = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        # An EHR write waiting for approval.
        c.post(
            "/api/practices/intents", json={"patient_id": "1", "note": "Synthetic alert"}
        ).raise_for_status()
        # A call result waiting for review.
        case = _reminder(c)
        errand = _published(c, "errand")
        task = c.post(
            "/api/tasks",
            json={"agent_id": errand["id"], "case_id": case["id"], "destination": APPROVED},
        ).json()
        # A clinical case waiting for a clinician.
        c.post("/api/cases/samples", json={"case_type": "care_outreach", "count": 1})

        me = c.get("/api/me").json()

        async def seed():
            async with c.app.state.db.sessions() as session:
                row = await session.get(Task, task["id"])
                row.state = "Review"
                run = BrowserRun(
                    workspace_id=me["workspace_id"],
                    supervisor_id=me["user_id"],
                    task="Check a claim on the synthetic payer portal",
                    start_url="https://portal.example.test/claims",
                    model="gemini",
                    model_id="test",
                    state="awaiting_approval",
                    plan=["Open claims", "Search"],
                )
                session.add(run)
                await session.flush()
                session.add(
                    BrowserStep(
                        workspace_id=me["workspace_id"],
                        run_id=run.id,
                        seq=1,
                        kind="click",
                        risk="commit",
                        description="Click 'Submit request'",
                    )
                )
                await session.commit()

        c.portal.call(seed)
        inbox = c.get("/api/inbox").json()
        kinds = {i["kind"] for i in inbox["items"]}
        assert {"result_review", "ehr_write", "browser_step", "clinician_signoff"} <= kinds
        assert inbox["counts"]["ehr_write"] == 1 and inbox["counts"]["result_review"] == 1
        result = next(i for i in inbox["items"] if i["kind"] == "result_review")
        assert result["href"] == f"/results/{task['id']}" and result["approver"] == "staff"
        write = next(i for i in inbox["items"] if i["kind"] == "ehr_write")
        assert write["approver"] == "admin" and write["decisions"] == ["approve", "reject"]
        step = next(i for i in inbox["items"] if i["kind"] == "browser_step")
        assert step["approver"] == "supervisor" and step["href"].startswith("/browser/")
        signoff = next(i for i in inbox["items"] if i["kind"] == "clinician_signoff")
        assert signoff["approver"] == "clinician" and signoff["can_decide"]

        # Decide two kinds from the inbox itself; the others open their own page.
        after = c.post(
            "/api/inbox/decide",
            json={"kind": "ehr_write", "id": write["id"], "decision": "approve"},
        ).json()
        assert "ehr_write" not in after["counts"]
        assert c.get("/api/practices/intents").json()[0]["state"] in ("accepted", "written")
        after = c.post(
            "/api/inbox/decide",
            json={"kind": "clinician_signoff", "id": signoff["id"], "decision": "approve"},
        ).json()
        assert "clinician_signoff" not in after["counts"]
        refused = c.post(
            "/api/inbox/decide",
            json={"kind": "result_review", "id": result["id"], "decision": "approve"},
        )
        assert refused.json()["error"]["code"] == "open_item"


def test_old_rows_and_case_types_still_work(client):
    """Rows from before Cases v2 have none of the new columns; they still list and open."""
    rows = client.get("/api/cases").json()
    first = _case(client, rows[0]["id"])
    assert first["contract"]["holds"] is False and first["attempts"] == []
    assert first["playbook_run"] is None and first["owner"] is None
    playbooks = client.get("/api/playbooks").json()["playbooks"]
    assert {p["id"] for p in playbooks} == {
        "care_outreach",
        "appointment_reminder",
        "slot_backfill",
    }
    assert {t["kind"] for p in playbooks for t in p["triggers"]} >= {
        "care_opportunity",
        "backfill_opening",
        "schedule",
    }
