"""The medical EHRs NexHealth connects, each answering the way that EHR does.

Every earlier NexHealth fake answered like Dentrix. Here the same fake is reshaped per medical EHR
(athenahealth, eClinicalWorks cloud, ModMed, NextGen Office): its sync status and institution
name the EHR, records carry that EHR's ``foreign_id_type``, the location is in its own time zone,
and NexHealth refuses the writes that EHR can't take (check-in on athena/eCW, time changes on all
four). Sync, slots, book, reschedule, cancel, confirm, check-in and new patient run on each.
All people are synthetic.
"""

from datetime import timedelta

import httpx
import pytest

from careplix_api.db import utcnow
from careplix_api.models import EhrRecord, WriteIntent

from .test_practice import FakeNexHealth, _client, _db_rows, _deliver, _worker
from .test_receptionist import JANE, _call


class MedicalNexHealth(FakeNexHealth):
    """NexHealth in front of one medical cloud EHR (documented write table, 1 Nov 2026)."""

    EHRS = {
        # key: (display name NexHealth reports, foreign_id_type, writes: R cancel, K check-in)
        "athenahealth": ("athenahealth", "athena", "R"),
        "eclinicalworks": ("eClinicalWorks", "ecw", "R"),
        "modmed": ("ModMed", "modmed", "RK"),
        "nextgen_office": ("NextGen Office", "nextgen", "RK"),
    }

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.display, fid_type, self.writes = self.EHRS[key]
        self.sync_status = [
            {"emr": {"display_name": self.display}, "read_status": "green", "write_status": "green"}
        ]
        self.location = {**self.location, "tz": "America/New_York", "foreign_id_type": fid_type}
        for patient in self.patients.values():
            patient["foreign_id_type"] = fid_type
        # A visit happening now, for check-in.
        self.appointments[507] = self._appt(507, 1, timedelta(0))
        self.refused: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/institutions":
            response = super().__call__(request)
            data = response.json()["data"]
            data[0]["emrs"] = [self.key]
            return httpx.Response(200, json={"data": data})
        parts = request.url.path.strip("/").split("/")
        if request.method == "PATCH" and parts[0] == "appointments" and request.content:
            import json

            fields = set(json.loads(request.content)["appt"])
            if "checkin_at" in fields and "K" not in self.writes:
                self.refused.append("check_in")
                return httpx.Response(400, json={"error": [f"{self.display}: check-in"]})
            if fields & {"start_time", "end_time"}:
                self.refused.append("time_change")
                return httpx.Response(400, json={"error": [f"{self.display}: time change"]})
        return super().__call__(request)


EHRS = list(MedicalNexHealth.EHRS)


@pytest.fixture(params=EHRS)
def ehr(tmp_path, request):
    c, router = _client(tmp_path, webhook_public_url="https://hooks.test")
    fake = MedicalNexHealth(request.param)
    router.nexhealth, router._nh = fake, httpx.MockTransport(fake)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)  # first full sync
        assert c.post("/api/practices/verify").json()["ok"]
        c.post("/api/practices/webhooks/register").raise_for_status()
        yield c, fake, request.param


def _token(fake) -> str:
    return fake.webhook_target.rsplit("/", 1)[-1]


def _updated(appointment_id, changed):
    return {
        "event_name": "appointment_updated",
        "event_time": utcnow().isoformat(),
        "resource_type": "Appointment",
        "subdomain": "demo",
        "webhook_subscription_id": 2,
        "data": {"appointment": {"id": appointment_id, "previous_attributes": changed}},
    }


def _inserted(appointment_id):
    return {
        "event_name": "appointment_insertion.complete",
        "event_time": utcnow().isoformat(),
        "resource_type": "Appointment",
        "subdomain": "demo",
        "data": {"appointment": {"id": appointment_id}},
    }


def _intents(c, **where):
    return [
        i for i in _db_rows(c, WriteIntent) if all(getattr(i, k) == v for k, v in where.items())
    ]


def test_sync_mirrors_the_practice_and_names_its_ehr(ehr):
    c, fake, key = ehr
    overview = c.get("/api/practices").json()
    assert overview["mirror"]["patient"] == 4 and overview["mirror"]["appointment"] == 7
    assert overview["location"]["tz"] == "America/New_York"
    [conn] = c.get("/api/practices/catalog").json()["connections"]
    assert conn["profile"] == f"nexhealth:{key}"
    support = conn["support"]
    assert support["appointment.reschedule"] == "no"  # medical EHRs take no time changes
    assert support["appointment.check_in"] == ("yes" if "K" in fake.writes else "no")


def test_slots_are_offered_from_the_ehr(ehr):
    c, fake, _ = ehr
    call = _call(c)
    call.ok("find_patient", **JANE)
    [slot] = call.ok("find_slots")["slots"]
    assert slot["slot_id"] == "S1" and fake.slot_queries


def test_book_is_confirmed_by_the_insertion_webhook(ehr):
    c, fake, _ = ehr
    call = _call(c)
    call.ok("find_patient", **JANE)
    call.ok("find_slots")
    assert call.ok("book_appointment", slot_id="S1")["state"] == "booked"
    [booked] = fake.booked
    [intent] = _intents(c, kind="appointment_book")
    assert intent.state == "accepted" and intent.confirmation == "pending"
    _deliver(c, _token(fake), _inserted(booked["id"])).raise_for_status()
    _worker(c)
    [intent] = _intents(c, kind="appointment_book")
    assert intent.state == "written"


def test_reschedule_books_then_releases_never_a_time_change(ehr):
    c, fake, _ = ehr
    call = _call(c)
    call.ok("find_patient", **JANE)
    call.ok("find_slots", appointment_id="501")
    result = call.ok("reschedule_appointment", appointment_id="501", slot_id="S1")
    assert result["state"] == "rescheduled"
    assert len(fake.booked) == 1 and fake.appointments[501]["cancelled"] is True
    assert "time_change" not in fake.refused  # the hub never sent a PATCH start_time


def test_cancel_is_written_when_the_ehr_reports_it(ehr):
    c, fake, _ = ehr
    c.put("/api/practices/automation", json={"receptionist_cancel_mode": "auto"}).raise_for_status()
    call = _call(c)
    call.ok("find_patient", **JANE)
    assert call.ok("cancel_appointment", appointment_id="501", reason="no_longer_needed")[
        "state"
    ] == ("cancelled")
    _deliver(c, _token(fake), _updated(501, {"cancelled": False})).raise_for_status()
    _worker(c)
    [intent] = _intents(c, kind="appointment_cancel")
    assert intent.state == "written"


def test_confirm_from_the_studio_is_written(ehr):
    c, fake, _ = ehr
    proposed = c.post("/api/practices/appointments/500/write", json={"action": "confirm"}).json()
    assert c.post(f"/api/practices/intents/{proposed['id']}/approve").json()["state"] == "accepted"
    _deliver(c, _token(fake), _updated(500, {"confirmed": False})).raise_for_status()
    _worker(c)
    [intent] = _intents(c, kind="appointment_confirm")
    assert intent.state == "written"


def test_check_in_only_where_the_ehr_takes_it(ehr):
    c, fake, key = ehr
    proposed = c.post("/api/practices/appointments/507/write", json={"action": "check_in"})
    assert proposed.status_code == 200, proposed.text
    done = c.post(f"/api/practices/intents/{proposed.json()['id']}/approve").json()
    if "K" in fake.writes:  # ModMed, NextGen Office
        assert done["state"] == "accepted"
        assert any(b[:2] == ("PATCH", "/appointments/507") for b in fake.bodies)
    else:  # athenahealth, eClinicalWorks: stays in the hub as a staff task
        assert done["state"] == "manual" and fake.display in done["last_error"]
        assert not any(b[:2] == ("PATCH", "/appointments/507") for b in fake.bodies)
    assert fake.refused == []


def test_a_new_patient_is_registered_without_extra_fields(ehr):
    c, fake, _ = ehr
    call = _call(c)
    new = {"first_name": "Rory", "last_name": "Vale", "date_of_birth": "1988-09-10"}
    assert call.ok("find_patient", **new) == {"verified": False}
    [slot] = call.ok("find_slots")["slots"]
    result = call.ok(
        "book_new_patient",
        **new,
        email="rv@example.test",
        phone_number="3125550100",
        slot_id=slot["slot_id"],
    )
    assert result["state"] == "booked" and result["patient"] == "registered"
    assert "gender" not in fake.created_patients[0]["patient"]["bio"]  # only Dentrix/Eaglesoft
    patient = next(r for r in _db_rows(c, EhrRecord) if r.source_id == "1000")
    assert patient.attrs["placeholder"] is True
