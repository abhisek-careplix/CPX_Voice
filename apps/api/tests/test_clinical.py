"""Clinical support and front-office completion: the clinician role, the approved instructions
library, refills, discharge follow-up, pre-visit, intake by secure link and the reminder cadence.

The rule under test throughout: the agent never makes a clinical decision. It gathers, prepares,
routes, and delivers only clinician-approved words.
"""

import base64
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api.auth import Principal
from careplix_api.cases import attempts, policy
from careplix_api.cases.models import ContactAttempt, PlaybookRun
from careplix_api.channels import content
from careplix_api.clinical import library, playbook, scripts
from careplix_api.connect import catalog
from careplix_api.db import utcnow
from careplix_api.main import create_app
from careplix_api.models import Case
from careplix_api.practice import intents, reminders

from .conftest import FakeGateway, make_settings

PHONE = "+12025550142"
PATIENT = {
    "patient_name": "Synthetic Patient Kappa",
    "patient_phone": PHONE,
    "date_of_birth": "1955-05-05",
    "practice_name": "Riverside Family Medicine (synthetic)",
    "practice_phone": "+12025550160",
}


class FakeText:
    """The text channel: records offers (send) and one-way notices (notify)."""

    name = "text"

    def __init__(self):
        self.sent: list[str] = []
        self.notices: list[str] = []

    async def send(self, ctx):
        self.sent.append(ctx.attempt.person_label)
        return attempts.SendResult("sent", message_id=f"SM{len(self.sent)}")

    async def notify(self, ctx, text):
        self.notices.append(text)
        return attempts.SendResult("sent", message_id=f"SN{len(self.notices)}")


@pytest.fixture
def text():
    channel = FakeText()
    attempts.register(channel)
    yield channel
    attempts.unregister("text")


@pytest.fixture
def c(tmp_path):
    settings = make_settings(
        tmp_path,
        run_background=False,
        playbook_contact_hours=False,
        studio_url="https://studio.example.test",
    )
    with TestClient(create_app(settings, FakeGateway(settings))) as client:
        client.post("/api/auth/dev-login").raise_for_status()
        yield client


def _engine(c):
    return c.portal.call(c.app.state.playbooks.run_once)


def _advance(c, case_id):
    return c.portal.call(c.app.state.playbooks.advance, case_id)


def _db(c, fn):
    async def run():
        async with c.app.state.db.sessions() as session:
            out = await fn(session)
            await session.commit()
            return out

    return c.portal.call(run)


def _me(c):
    return c.get("/api/me").json()


def _new(c, case_type, **fields):
    r = c.post("/api/clinical/cases", json={"case_type": case_type, "fields": fields})
    assert r.status_code == 200, r.text
    return r.json()


def _approve(c, key):
    r = c.post(f"/api/clinical/instructions/{key}/approve")
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. the clinician role ------------------------------------------------------------------------


def test_only_clinicians_sign_clinical_work():
    reviewer = Principal("u1", "w1", "reviewer", "Rev")
    assert policy.can_decide(reviewer, "staff") and not policy.can_decide(reviewer, "clinician")
    assert policy.can_decide(Principal("u2", "w1", "clinician", "Dr C"), "clinician")
    admin_md = Principal("u3", "w1", "admin", "Dr Owner", clinician=True)
    assert policy.can_decide(admin_md, "clinician") and admin_md.can("clinical_signoff")
    assert not Principal("u4", "w1", "admin", "Admin").can("clinical_signoff")
    assert policy.approver("red_flag") == "clinician"
    assert policy.approver("clinical.refill_decision") == "clinician"


def test_the_clinician_flag_gates_sign_off(c, text):
    me = _me(c)
    assert me["clinician"] and "clinical_signoff" in me["permissions"]  # demo admin signs
    members = c.get("/api/workspace/members").json()["members"]
    assert members[0]["clinician"] is True
    c.put(f"/api/workspace/members/{me['user_id']}/clinician", json={"clinician": False})
    assert not _me(c)["clinician"]
    case = _new(c, "refill_request", **PATIENT, medication="Lisinopril", pharmacy="Main St")
    refused = c.post(f"/api/clinical/cases/{case['id']}/decide", json={"outcome": "other"})
    assert refused.status_code == 403
    assert c.post("/api/clinical/instructions/refill_approved/approve").status_code == 403
    c.put(f"/api/workspace/members/{me['user_id']}/clinician", json={"clinician": True})
    again = c.post(f"/api/clinical/cases/{case['id']}/decide", json={"outcome": "other"})
    assert again.status_code == 200


# --- 2. the approved instructions library ---------------------------------------------------------


def test_library_starts_as_drafts_and_only_approved_words_are_usable(c):
    listed = c.get("/api/clinical/instructions").json()
    by_key = {i["key"]: i for i in listed["instructions"]}
    assert {"refill_approved", "refill_needs_visit", "red_flag_response"} <= set(by_key)
    assert all(i["state"] == "draft" for i in by_key.values())
    approved = _approve(c, "refill_approved")
    assert approved["state"] == "approved" and approved["approved_by"] == "Demo Administrator"
    # Editing approved words makes a new draft version: nothing changed is sent unapproved.
    edited = c.put(
        "/api/clinical/instructions/refill_approved",
        json={"body": "Your refill was approved and sent to your pharmacy."},
    ).json()
    assert edited["state"] == "draft" and edited["version"] == 2
    # Texts sit on lock screens: no medicines or doses in a text instruction.
    too_clinical = c.post(
        "/api/clinical/instructions",
        json={
            "key": "take_two",
            "title": "Dose",
            "topic": "refill",
            "channel": "text",
            "body": "Take 20 mg twice a day until we call.",
        },
    )
    assert too_clinical.status_code == 422
    assert too_clinical.json()["error"]["code"] == "not_textable"
    call_only = c.post(
        "/api/clinical/instructions",
        json={
            "key": "take_two",
            "title": "Dose",
            "topic": "refill",
            "channel": "call",
            "body": "Take 20 mg twice a day until we call.",
        },
    )
    assert call_only.status_code == 200
    bad = c.post(
        "/api/clinical/instructions",
        json={"key": "x_name", "title": "Xx", "topic": "general", "body": "Hi {last_name}, call"},
    )
    assert bad.json()["error"]["code"] == "bad_placeholder"
    assert library.render("Hi {first_name}, call {practice_phone}.", first_name="Al") == (
        "Hi Al, call ."
    )


# --- 3. refill requests ---------------------------------------------------------------------------


def test_a_refill_reaches_the_clinician_and_the_patient_hears_approved_words(c, text):
    case = _new(
        c,
        "refill_request",
        **PATIENT,
        medication="Lisinopril (synthetic sample)",
        dose="10 mg once daily",
        pharmacy="Main Street Pharmacy (synthetic)",
    )
    assert case["stage"] == "with_clinician", case["run"]
    assert case["run"]["state"] == "waiting"
    labels = {f["key"]: f for f in case["protocol"]}
    assert labels["last_visit"]["meets"] is False  # no visit in the mirror: shown first
    assert labels["controlled"]["meets"] is True
    assert text.notices and "refill request" in text.notices[0]  # told it's with the team
    # The clinician queue: a decision item that opens the page (no inline approve).
    inbox = c.get("/api/inbox?kind=clinician_signoff").json()["items"]
    item = next(i for i in inbox if i["case"]["id"] == case["id"])
    assert item["decisions"] == [] and item["href"].startswith("/clinical/refills")
    assert item["approver"] == "clinician" and item["can_decide"]
    # Generic sign-off is refused: the decision needs the words the patient will hear.
    assert c.post(f"/api/cases/{case['id']}/clinician-signoff").status_code == 422
    words = c.get("/api/clinical/instructions").json()["instructions"]
    needs_visit = next(i for i in words if i["key"] == "refill_needs_visit")
    unapproved = c.post(
        f"/api/clinical/cases/{case['id']}/decide",
        json={"outcome": "needs_visit", "instruction_id": needs_visit["id"]},
    )
    assert unapproved.json()["error"]["code"] == "not_approved"
    _approve(c, "refill_needs_visit")
    wrong = c.post(
        f"/api/clinical/cases/{case['id']}/decide",
        json={"outcome": "approved", "instruction_id": needs_visit["id"]},
    )
    assert wrong.json()["error"]["code"] == "wrong_instruction"
    decided = c.post(
        f"/api/clinical/cases/{case['id']}/decide",
        json={"outcome": "needs_visit", "instruction_id": needs_visit["id"], "note": "BP check"},
    ).json()
    expected = library.render(needs_visit["body"], practice_phone="+12025550160")
    assert text.notices[-1] == expected  # word for word
    assert decided["status"] == "done", decided["run"]
    contract = decided["case"]["contract"]
    assert contract["holds"] and {p["key"] for p in contract["parts"]} >= {"clinician"}
    assert decided["case"]["system_of_record"]["ack"].startswith("clinician_decision:needs_visit")
    assert decided["case"]["next_step"].startswith("Book the visit")
    again = c.post(f"/api/clinical/cases/{case['id']}/decide", json={"outcome": "other"})
    assert again.status_code == 409


def test_a_decision_outside_the_library_goes_to_a_person(c, text):
    case = _new(c, "refill_request", **PATIENT, medication="Metformin", pharmacy="Main St")
    out = c.post(
        f"/api/clinical/cases/{case['id']}/decide",
        json={"outcome": "other", "note": "Switch to the extended-release form"},
    ).json()
    assert out["status"] == "handed_off"
    assert "isn't in the approved instructions library" in out["case"]["status_reason"]
    assert len(text.notices) == 1  # only the "we have your request" notice


def test_refill_protocol_flags_controlled_medicines(c, text):
    case = _new(c, "refill_request", **PATIENT, medication="Oxycodone 5", pharmacy="Main St")
    controlled = next(f for f in case["protocol"] if f["key"] == "controlled")
    assert controlled["meets"] is False and "prescriber" in controlled["value"]


# --- 4. discharge follow-up -----------------------------------------------------------------------


def test_two_business_days_skip_the_weekend():
    friday = date(2026, 11, 13)
    assert playbook.add_business_days(friday, 2) == date(2026, 11, 17)
    assert playbook.add_business_days(date(2026, 11, 16), 2) == date(2026, 11, 18)


def test_discharge_red_flag_goes_to_a_clinician_at_once(c, text):
    out_day = utcnow().date().isoformat()
    case = _new(
        c,
        "discharge_followup",
        **PATIENT,
        facility="Lakeside General (synthetic)",
        discharged_on=out_day,
    )
    # No published clinical-check agent here, so the call can't go out: a nurse owns it.
    assert case["case"]["status"] == "handed_off"
    call_by = date.fromisoformat(case["call_by"])
    assert call_by == playbook.add_business_days(date.fromisoformat(out_day), 2)
    assert "word for word" not in (case["case"]["status_reason"] or "")
    # The nurse calls and records the answers; a yes to chest pain is a red flag.
    recorded = c.post(
        f"/api/clinical/cases/{case['id']}/answers",
        json={
            "answers": [
                {"question": "feeling", "answer": "text", "detail": "Tired"},
                {"question": "chest_pain", "answer": "yes", "detail": "Tight since this morning"},
            ]
        },
    ).json()
    assert recorded["red_flag"] is True and "911" in recorded["say"]
    flags = c.get("/api/inbox?kind=red_flag").json()["items"]
    assert [f["case"]["id"] for f in flags] == [case["id"]]
    assert flags[0]["approver"] == "clinician" and "chest pain" in flags[0]["detail"]
    _advance(c, case["id"])
    detail = c.get(f"/api/clinical/cases/{case['id']}").json()
    assert detail["stage"] == "red_flag" and detail["answers"][0]["level"] == "red_flag"
    # Sign-off waits for the red flag to be acknowledged.
    early = c.post(f"/api/clinical/cases/{case['id']}/sign-off")
    assert early.json()["error"]["code"] == "red_flag_open"
    c.post(
        f"/api/clinical/cases/{case['id']}/red-flag",
        json={"action": "called_patient", "note": "Sent to ED by phone"},
    ).raise_for_status()
    assert c.get("/api/inbox?kind=red_flag").json()["items"] == []
    done = c.post(f"/api/clinical/cases/{case['id']}/sign-off").json()
    assert done["status"] == "done", done["run"]
    assert done["case"]["contract"]["holds"]
    kinds = {lk["label"] for lk in done["case"]["links"] if lk["part"] == "evidence"}
    assert any(label.startswith("Red flag reviewed by") for label in kinds)


def test_a_recent_discharge_fact_opens_a_follow_up(c, text):
    c.post("/api/care-opportunities/sample").raise_for_status()  # SYN-P002 discharged yesterday
    report = _engine(c)
    assert report["created"]["discharge_notice"] == 1
    [row] = c.get("/api/clinical/cases?type=discharge_followup").json()["cases"]
    assert row["source"] == "discharge" and row["facility"] == "Lakeside General"
    assert _engine(c)["created"]["discharge_notice"] == 0  # once per discharge


# --- 5. pre-visit: questionnaire by secure link and the chart-prep brief --------------------------


def _token(notice: str) -> str:
    return re.search(r"/f/([A-Za-z0-9_\-]+)", notice).group(1)


def test_previsit_questionnaire_by_link_and_brief_for_the_clinician(c, text):
    case = _new(
        c,
        "previsit_prep",
        **PATIENT,
        clinician_name="Dr. Rowan Synthetic",
        appointment_at="2026-11-20 09:00",
    )
    assert case["stage"] == "waiting_on_patient"
    token = _token(text.notices[-1])
    assert text.notices[-1].startswith("Please answer a few questions")
    c.cookies.clear()  # the patient has no Studio session
    form = c.get(f"/api/forms/{token}").json()
    assert form["kind"] == "questionnaire" and form["practice"].startswith("Riverside")
    assert [q["key"] for q in form["questions"]][0] == "urgent_now"
    assert "Kappa" not in str(form)  # the page names nobody
    incomplete = c.post(f"/api/forms/{token}", json={"answers": {"urgent_now": {"answer": "no"}}})
    assert incomplete.json()["error"]["code"] == "missing"
    answers = {
        "urgent_now": {"answer": "no"},
        "visit_reason": {"detail": "Knee pain"},
        "new_symptoms": {"answer": "yes"},
        "medicine_changes": {"answer": "no"},
        "outside_care": {"answer": "yes"},
        "refills_needed": {"answer": "no"},
    }
    sent = c.post(f"/api/forms/{token}", json={"answers": answers})
    assert sent.json() == {"state": "received"}
    assert c.post(f"/api/forms/{token}", json={"answers": answers}).status_code == 409
    assert c.get("/api/forms/not-a-real-token").status_code == 404
    c.post("/api/auth/dev-login").raise_for_status()
    _advance(c, case["id"])
    detail = c.get(f"/api/clinical/cases/{case['id']}").json()
    assert detail["stage"] == "with_clinician", detail["run"]
    brief = detail["brief"]
    assert brief["attention"] == ["New symptoms", "Care elsewhere since last visit"]
    assert [line["level"] for line in brief["questionnaire"]][:2] == ["attention", "attention"]
    assert {"problems", "medicines", "care_gaps", "outside_records"} <= set(brief)
    done = c.post(f"/api/clinical/cases/{case['id']}/sign-off").json()
    assert done["status"] == "done", done["run"]
    assert done["case"]["contract"]["holds"]


def test_an_emergency_answer_on_the_form_shows_the_red_flag_words(c, text):
    case = _new(c, "previsit_prep", **PATIENT, clinician_name="Dr. A", appointment_at="2026-11-20")
    token = _token(text.notices[-1])
    answers = {k: {"answer": "no"} for k in ("new_symptoms", "medicine_changes", "outside_care")}
    answers |= {"urgent_now": {"answer": "yes"}, "refills_needed": {"answer": "no"}}
    answers |= {"visit_reason": {"detail": "Check-up"}}
    out = c.post(f"/api/forms/{token}", json={"answers": answers}).json()
    assert "911" in out["say"]
    assert [i["case"]["id"] for i in c.get("/api/inbox?kind=red_flag").json()["items"]] == [
        case["id"]
    ]


def test_scripts_are_word_for_word_and_keyed():
    spoken = scripts.spoken("discharge_followup")
    assert '[chest_pain] "Have you had any chest pain' in spoken
    red = [q["key"] for q in scripts.questions("discharge_followup") if q["red_flag"]]
    assert red == ["chest_pain", "breathing", "fever", "bleeding_or_wound", "fall_or_confusion"]
    q = scripts.by_key("discharge_followup")["breathing"]
    assert scripts.is_red_flag(q, "unsure") and not scripts.is_red_flag(q, "no")


# --- 6. intake and registration -------------------------------------------------------------------

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


def test_intake_by_secure_link_with_card_capture(c, text):
    made = c.post("/api/intake", json={"fields": PATIENT}).json()
    assert made["run"]["state"] == "waiting"
    token = _token(text.notices[-1])
    form = c.get(f"/api/forms/{token}").json()
    assert form["kind"] == "intake" and any(f["kind"] == "image" for f in form["fields"])
    fields = {
        "first_name": "Synthetic",
        "last_name": "Kappa",
        "date_of_birth": "1955-05-05",
        "phone": PHONE,
        "address_line1": "1 Synthetic Way",
        "city": "Chicago",
        "state": "IL",
        "postal_code": "60601",
        "confirm": True,
    }
    no_cover = c.post(f"/api/forms/{token}", json={"fields": fields})
    assert no_cover.json()["error"]["code"] == "coverage_missing"
    bad = c.post(f"/api/forms/{token}", json={"fields": {**fields, "card_front": "nope"}})
    assert bad.json()["error"]["code"] == "bad_image"
    fields |= {"payer_name": "Northwind (synthetic)", "member_id": "99123", "card_front": PNG}
    assert c.post(f"/api/forms/{token}", json={"fields": fields}).json()["state"] == "received"
    _advance(c, made["id"])
    detail = c.get(f"/api/intake/{made['id']}").json()
    assert detail["has_card_photo"] and detail["card"]["front"] == PNG
    assert detail["answers"]["member_id"] == "99123"
    # No EHR chart linked: the front desk enters it, with the photos on the case.
    assert detail["case"]["status"] == "handed_off"
    assert "isn't linked to an EHR chart" in detail["case"]["status_reason"]

    async def sealed(session):
        from careplix_api.forms.models import FormLink

        return [r.answers for r in await session.scalars(select(FormLink))]

    assert all("99123" not in (a or "") for a in _db(c, sealed))


def test_registration_writes_wait_for_the_nexhealth_beta():
    assert {"patient_update", "coverage_create"} <= set(intents.KINDS)
    assert catalog.support(None, "patient.update") == "no"
    assert catalog.support(catalog.PROFILES[next(iter(catalog.PROFILES))], "coverage.create") == (
        "no"
    )
    assert policy.approver("playbook.ehr_write.patient_update") == "admin"


# --- 7. reminders: 7 days, 72 hours, 24 hours, and the replies ------------------------------------


def test_reminder_replies():
    assert reminders.reply_kind("R") == "reschedule"
    assert reminders.reply_kind(" c. ") == "cancel"
    assert reminders.reply_kind("CANCEL") is None and content.intent("CANCEL") == "stop"
    stages = [s["stage"] for s in reminders.schedule(utcnow())]
    assert stages == ["7d", "72h", "24h"]


def _reminder_case(c, starts_in):
    from .test_cases import _reminder

    case = _reminder(c)

    async def place(session):
        row = await session.get(Case, case["id"])
        row.data = {**row.data, "appointment_starts_at": (utcnow() + starts_in).isoformat()}

    _db(c, place)
    c.post(f"/api/cases/{case['id']}/playbook").raise_for_status()
    return case


def _shift(c, case_id, starts_in):
    async def move(session):
        row = await session.get(Case, case_id)
        row.data = {**row.data, "appointment_starts_at": (utcnow() + starts_in).isoformat()}
        run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case_id))
        run.next_run_at = utcnow()

    _db(c, move)
    _advance(c, case_id)


def test_reminders_repeat_at_72_and_24_hours_until_answered(c, text):
    case = _reminder_case(c, timedelta(days=5))
    assert len(text.sent) == 1  # the 7-day reminder, at once
    run = c.get(f"/api/cases/{case['id']}").json()["playbook_run"]
    assert run["state"] == "waiting" and "72h" in run["log"][-1]["detail"]
    _shift(c, case["id"], timedelta(hours=60))  # now inside 72 hours
    assert len(text.sent) == 2
    _shift(c, case["id"], timedelta(hours=20))  # now inside 24 hours
    assert len(text.sent) == 3
    _shift(c, case["id"], timedelta(hours=1))  # nobody answered
    after = c.get(f"/api/cases/{case['id']}").json()
    assert after["status"] == "handed_off" and "3 reminders" in after["status_reason"]


def test_a_reschedule_reply_hands_off_and_tells_the_patient(c, text):
    case = _reminder_case(c, timedelta(days=2))
    [attempt] = c.get(f"/api/cases/{case['id']}").json()["attempts"]
    ws = _me(c)["workspace_id"]

    async def reply(session):
        return await reminders.take_reply(
            session,
            SimpleNamespace(text="R", reply_ref=f"attempt:{attempt['id']}", workspace_id=ws),
        )

    assert _db(c, reply) is True
    _advance(c, case["id"])
    after = c.get(f"/api/cases/{case['id']}").json()
    assert after["status"] == "handed_off" and after["next_step"].startswith("Call the patient")
    assert text.notices[-1] == reminders.RESCHEDULE_NOTICE
    rows = _db(c, lambda s: s.scalars(select(ContactAttempt)))
    assert [r.state for r in rows] == ["declined", "notified"]  # the R, then our notice


def test_a_cancel_reply_proposes_the_cancellation(c, text):
    case = _reminder_case(c, timedelta(days=2))
    [attempt] = c.get(f"/api/cases/{case['id']}").json()["attempts"]
    ws = _me(c)["workspace_id"]

    async def reply(session):
        return await reminders.take_reply(
            session,
            SimpleNamespace(text="C", reply_ref=f"attempt:{attempt['id']}", workspace_id=ws),
        )

    _db(c, reply)
    _advance(c, case["id"])
    after = c.get(f"/api/cases/{case['id']}").json()
    # No EHR appointment on a hand-made case: the front desk cancels it.
    assert after["status"] == "handed_off" and "cancel" in after["status_reason"]
    assert text.notices[-1] == reminders.CANCEL_NOTICE


# --- 8. Voice Studio suite ------------------------------------------------------------------------


def test_clinical_suite_adds_scenarios_and_clinical_red_team(c):
    out = c.post("/api/scenarios/examples/clinical").json()
    assert out["added"] == out["total"] == 12
    listed = c.get("/api/scenarios").json()["scenarios"]
    clinical = [s for s in listed if s["agent_id"] == out["agent_id"]]
    assert {s["category"] for s in clinical} == {"clinical", "red_team"}
    assert sum(s["name"].startswith("Clinical red team") for s in clinical) == 5
    agent = next(a for a in c.get("/api/agents").json() if a["id"] == out["agent_id"])
    assert agent["template"]["id"] == "clinical-check"
    rt = c.post(f"/api/agents/{out['agent_id']}/red-team").json()
    assert rt["added"] == 9  # six general attacks, three clinical
    assert c.post("/api/scenarios/examples/clinical").json()["added"] == 0
