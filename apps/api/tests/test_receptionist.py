"""FO-3 AI receptionist: a published front-desk agent on a call, against the NexHealth fake.

Drives the same internal API the worker uses (claim, then tool calls keyed by the model's
call id), so this is the whole path a real call takes, minus LiveKit and the voice model.
All people are synthetic.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from careplix_api.models import AuditEvent, EhrRecord, FrontDeskMessage, WriteIntent
from careplix_api.practice.openemr import OpenEmrClient

from .conftest import WORKER
from .test_practice import _client, _db_rows, _worker

JANE = {"first_name": "Jane", "last_name": "Roe", "date_of_birth": "1980-01-02"}


class Call:
    def __init__(self, c, claim):
        self.c, self.claim = c, claim
        self.auth = {"Authorization": f"Bearer {claim['attempt_token']}"}
        self.n = 0

    def tool(self, name, call_id=None, **arguments):
        self.n += 1
        response = self.c.post(
            f"/internal/attempts/{self.claim['attempt_id']}/tools/{name}",
            json={"call_id": call_id or f"{name}-{self.n}", "arguments": arguments},
            headers=self.auth,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def ok(self, name, **arguments):
        out = self.tool(name, **arguments)
        assert out["state"] == "succeeded", out
        return out["result"]


@pytest.fixture
def practice(tmp_path, request):
    overrides = getattr(request, "param", {})
    c, router = _client(tmp_path, **overrides)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)  # mirror the practice
        yield c, router


def _call(c) -> Call:
    agent = c.post(
        "/api/agents", json={"name": "Riley", "template_id": "front-desk-receptionist"}
    ).json()
    published = c.post(f"/api/agents/{agent['id']}/publish", json={"change_note": "FO-3"})
    assert published.status_code == 200, published.text
    session = c.post("/api/browser-sessions", json={"agent_id": agent["id"], "use": "published"})
    assert session.status_code == 200, session.text
    attempt_id = session.json()["attempt_id"]
    claim = c.post(
        f"/internal/attempts/{attempt_id}/claim",
        json={"room": f"att_{attempt_id.replace('-', '')}", "job_id": "AJ", "runtime_build": "t"},
        headers=WORKER,
    )
    assert claim.status_code == 200, claim.text
    return Call(c, claim.json())


def _verified_call(c) -> Call:
    call = _call(c)
    assert call.ok("find_patient", **JANE) == {"verified": True}
    return call


def test_claim_carries_the_front_desk_tools_with_their_timeouts(practice):
    c, _ = practice
    call = _call(c)
    tools = {t["name"]: t for t in call.claim["tools"]}
    assert set(tools) == {
        "find_patient",
        "list_appointments",
        "find_slots",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "leave_message",
        "record_finding",
        "end_call",
    }
    assert tools["book_appointment"]["timeout_ms"] == 20000
    assert call.claim["speak_first"] is True


def test_nothing_about_appointments_before_verification(practice):
    c, _ = practice
    call = _call(c)
    refused = call.tool("list_appointments")
    assert refused["state"] == "rejected"
    assert refused["result"]["error"] == "not_verified"
    # A wrong birth date gives the same answer as a stranger; then the right one passes.
    assert call.ok("find_patient", **{**JANE, "date_of_birth": "1980-02-01"}) == {"verified": False}
    assert call.ok("find_patient", **{**JANE, "first_name": " jane "}) == {"verified": True}
    third = call.tool("find_patient", **JANE)
    assert third["result"]["error"] == "verification_limit"

    listed = call.ok("list_appointments")["appointments"]
    assert [a["appointment_id"] for a in listed] == ["500", "501"]
    assert listed[0]["provider"] == "Dr. Avery Test" and listed[0]["visit_type"] == "Follow-up"
    assert " at " in listed[0]["when"]


def test_book_an_offered_time_once_even_when_the_call_is_retried(practice):
    c, router = practice
    call = _verified_call(c)
    found = call.ok("find_slots", part_of_day="any")
    assert found["visit_type"] == "Follow-up" and found["minutes"] == 30
    [slot] = found["slots"]  # the fake schedule has one free time: cancelled visit 504
    assert slot["slot_id"] == "S1" and slot["provider"] == "Dr. Avery Test"

    # The model can only book ids the schedule offered on this call.
    assert call.tool("book_appointment", slot_id="S9")["result"]["error"] == "unknown_slot"

    first = call.tool("book_appointment", call_id="bk-1", slot_id="S1")
    assert first["state"] == "succeeded" and first["result"]["state"] == "booked"
    again = call.tool("book_appointment", call_id="bk-1", slot_id="S1")
    assert again == first
    [booked] = router.nexhealth.booked
    assert booked["patient_id"] == 1 and booked["provider_id"] == 10
    assert booked["start_time"].startswith(router.nexhealth.appointments[504]["start_time"][:16])
    [intent] = [i for i in _db_rows(c, WriteIntent) if i.source == "receptionist"]
    assert intent.kind == "appointment_book" and intent.state == "accepted"
    assert intent.approved_by == "policy:receptionist-L2"
    # The new visit shows straight away, before the next sync.
    assert "900" in [a["appointment_id"] for a in call.ok("list_appointments")["appointments"]]


def test_a_time_taken_since_it_was_offered_is_not_booked(practice):
    c, router = practice
    call = _verified_call(c)
    call.ok("find_slots")
    router.nexhealth.taken.add(router.nexhealth.appointments[504]["start_time"])
    result = call.ok("book_appointment", slot_id="S1")
    assert result["state"] == "slot_taken" and "another" in result["message"]
    assert router.nexhealth.booked == []


def test_reschedule_books_the_new_time_then_releases_the_old_one(practice):
    c, router = practice
    call = _verified_call(c)
    found = call.ok("find_slots", appointment_id="501")
    assert found["visit_type"] == "Follow-up"
    result = call.ok("reschedule_appointment", appointment_id="501", slot_id="S1")
    assert result["state"] == "rescheduled"
    assert len(router.nexhealth.booked) == 1
    assert router.nexhealth.appointments[501]["cancelled"] is True
    kinds = [(i.kind, i.state) for i in _db_rows(c, WriteIntent) if i.source == "receptionist"]
    assert sorted(kinds) == [("appointment_book", "accepted"), ("appointment_cancel", "accepted")]


def test_a_failed_booking_never_releases_the_old_visit(practice):
    c, router = practice
    call = _verified_call(c)
    call.ok("find_slots", appointment_id="501")
    router.nexhealth.book_error = "Provider not available"
    result = call.ok("reschedule_appointment", appointment_id="501", slot_id="S1")
    assert result["state"] == "failed"
    assert router.nexhealth.appointments[501]["cancelled"] is False


def test_cancel_waits_for_the_front_desk_by_default(practice):
    c, router = practice
    call = _verified_call(c)
    result = call.ok("cancel_appointment", appointment_id="500", reason="schedule_conflict")
    assert result["state"] == "requested" and "not cancelled" in result["message"]
    assert router.nexhealth.appointments[500]["cancelled"] is False
    [intent] = [i for i in _db_rows(c, WriteIntent) if i.source == "receptionist"]
    assert intent.state == "proposed" and "schedule conflict" in intent.reason
    approved = c.post(f"/api/practices/intents/{intent.id}/approve").json()
    assert approved["state"] == "accepted"
    assert router.nexhealth.appointments[500]["cancelled"] is True


@pytest.mark.parametrize("practice", [{"receptionist_cancel_mode": "auto"}], indirect=True)
def test_auto_cancel_frees_the_slot_for_backfill(practice):
    c, router = practice
    call = _verified_call(c)
    result = call.ok("cancel_appointment", appointment_id="501", reason="no_longer_needed")
    assert result["state"] == "cancelled"
    assert router.nexhealth.appointments[501]["cancelled"] is True
    [mirror] = [
        r for r in _db_rows(c, EhrRecord) if r.resource == "appointment" and r.source_id == "501"
    ]
    assert mirror.status == "cancelled"
    _worker(c)  # the next pass opens the freed time for backfill
    board = c.get("/api/practices/backfill").json()
    assert "501" in [o["source_appointment_id"] for o in board["active"] + board["recent"]]


def test_another_patients_visit_cannot_be_touched(practice):
    c, router = practice
    call = _verified_call(c)
    for name, args in (
        ("cancel_appointment", {"appointment_id": "502", "reason": "other"}),
        ("find_slots", {"appointment_id": "502"}),
    ):
        out = call.tool(name, **args)
        assert out["result"]["error"] == "unknown_appointment"
    assert router.nexhealth.appointments[502]["cancelled"] is False


@pytest.mark.parametrize("practice", [{"nexhealth_env": "production"}], indirect=True)
def test_a_browser_test_never_writes_to_a_real_practice(practice):
    c, router = practice
    call = _verified_call(c)
    call.ok("find_slots")
    out = call.tool("book_appointment", slot_id="S1")
    assert out["result"]["error"] == "test_call_no_writes"
    assert router.nexhealth.booked == []


def test_messages_reach_the_front_desk_sealed(practice):
    c, _ = practice
    call = _call(c)  # not verified: a new patient
    left = call.ok(
        "leave_message",
        topic="new_patient",
        message="Would like to register and book a first visit next week.",
        callback_requested=True,
        caller_name="Pat Example",
    )
    assert left["state"] == "recorded" and len(left["reference"]) == 8
    [stored] = _db_rows(c, FrontDeskMessage)
    assert "register" not in (stored.body or "")  # sealed at rest
    [row] = c.get("/api/practices/messages").json()
    assert row["topic"] == "new_patient" and row["callback_requested"] is True
    assert row["message"] is None and row["verified"] is False
    opened = c.get(f"/api/practices/messages/{row['id']}").json()
    assert opened["caller_name"] == "Pat Example" and "register" in opened["message"]
    assert any(e.action == "phi.message_viewed" for e in _db_rows(c, AuditEvent))
    c.post(f"/api/practices/messages/{row['id']}/done").raise_for_status()
    assert c.get("/api/practices/messages").json() == []
    for n in range(2):
        call.ok("leave_message", topic="other", message=f"m{n}", callback_requested=False)
    assert (
        call.tool("leave_message", topic="other", message="x", callback_requested=False)["result"][
            "error"
        ]
        == "message_limit"
    )


@pytest.mark.asyncio
async def test_openemr_free_slots_skip_booked_times_weekends_and_closed_hours():
    client = OpenEmrClient(
        "https://emr.test",
        client_id="c",
        client_secret="s",
        username="u",
        password="p",
        timezone="America/Chicago",
    )
    tz = ZoneInfo("America/Chicago")
    monday = datetime(2026, 11, 2, tzinfo=tz)

    async def appointments():
        return [
            {
                "pc_aid": "1",
                "pc_apptstatus": "-",
                "pc_eventDate": "2026-11-02",
                "pc_startTime": "08:00:00",
                "pc_endTime": "09:00:00",
            },
            {
                "pc_aid": "1",
                "pc_apptstatus": "x",
                "pc_eventDate": "2026-11-02",
                "pc_startTime": "09:00:00",
                "pc_endTime": "09:30:00",
            },
        ]

    client.appointments = appointments
    slots = await client.free_slots(
        (monday - timedelta(days=2)).astimezone(UTC),
        (monday + timedelta(days=1)).astimezone(UTC),
        30,
        ["1"],
    )
    local = [s[0].astimezone(tz) for s in slots]
    assert all(t.weekday() < 5 for t in local)  # Saturday and Sunday skipped
    assert local[0] == monday.replace(hour=9)  # 8–9 booked; the cancelled 9:00 is free again
    assert local[-1] <= monday.replace(hour=16, minute=30) + timedelta(days=1)
    assert all(8 <= t.hour < 17 for t in local)
    await client.aclose()
