"""OpenEMR adapter against a fake built from OpenEMR 7.0.3's real responses (synthetic people)."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from careplix_api.main import create_app

from .conftest import FakeGateway, make_settings
from .test_practice import FakeStedi

TZ = ZoneInfo("America/New_York")


def _local(days: int, hour: int, minute: int = 0) -> tuple[str, str]:
    day = (datetime.now(TZ) + timedelta(days=days)).date()
    return day.isoformat(), f"{hour:02d}:{minute:02d}:00"


class FakeOpenEmr:
    def __init__(self, connector: bool = True):
        self.connector = connector  # the CarePlix Connector module is installed
        self.calls: list[tuple[str, str]] = []
        self.next_eid = 300
        self.messages: list[dict] = []
        self.patients = [
            {
                "pid": "1",
                "uuid": "p-1",
                "fname": "Alex",
                "lname": "Synth",
                "DOB": "1980-01-02",
                "sex": "Male",
                "phone_cell": "312-555-0101",
            },
            {
                "pid": "2",
                "uuid": "p-2",
                "fname": "Blake",
                "lname": "Mock",
                "DOB": "1979-03-04",
                "sex": "Female",
                "phone_cell": "312-555-0102",
            },
            {
                "pid": "3",
                "uuid": "p-3",
                "fname": "Casey",
                "lname": "Demo",
                "DOB": "1990-05-06",
                "sex": "Female",
                "phone_cell": "",
            },
        ]
        self.insurance = {
            "p-1": [
                {
                    "uuid": "i-1",
                    "id": "1",
                    "provider": "1",
                    "insureruuid": "org-aetna",
                    "type": "primary",
                    "policy_number": "SYN000001",
                    "subscriber_relationship": "self",
                    "date": "2026-01-01",
                    "date_end": None,
                }
            ],
            "p-2": [
                {
                    "uuid": "i-2",
                    "id": "2",
                    "provider": "1",
                    "insureruuid": "org-aetna",
                    "type": "primary",
                    "policy_number": "SYN000002",
                    "subscriber_relationship": "self",
                    "date": "2026-01-01",
                    "date_end": None,
                }
            ],
        }
        self.appts: dict[str, dict] = {}
        self._add("101", "2", 2, 10, "x", "Patient called to cancel")  # the opening
        self._add("102", "1", 9, 11, "-", "ASAP list: call if an earlier slot opens")
        self._add("103", "2", 3, 9, "-", "Routine visit")
        self._add("104", "3", 4, 14, "-", "Routine visit")

    def _add(self, eid, pid, days, hour, status, note, provider="12"):
        day, start = _local(days, hour)
        end = f"{hour:02d}:30:00"
        self.appts[eid] = {
            "pc_eid": eid,
            "pc_pid": pid,
            "pc_aid": provider,
            "pc_catid": "5",
            "pc_title": "Office Visit",
            "pc_eventDate": day,
            "pc_startTime": start,
            "pc_endTime": end,
            "pc_apptstatus": status,
            "pc_facility": "3",
            "pc_hometext": note,
            "pid": pid,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path == "/oauth2/default/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] in ("password", "refresh_token")
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600, "refresh_token": "r"}
            )
        assert request.headers["authorization"] == "Bearer tok"
        api = path.removeprefix("/apis/default/api")
        if path.startswith("/apis/default/fhir/Organization"):
            org = {
                "resourceType": "Organization",
                "id": "org-aetna",
                "name": "Aetna",
                "identifier": [{"system": "http://hl7.org/fhir/v2/0203", "value": "60054"}],
                "type": [{"coding": [{"code": "ins"}]}],
            }
            clinic = {
                "resourceType": "Organization",
                "id": "org-clinic",
                "name": "CarePlix Demo Clinic",
                "type": [{"coding": [{"code": "prov"}]}],
            }
            return httpx.Response(
                200, json={"entry": [{"resource": org}, {"resource": clinic}], "link": []}
            )
        if path.startswith("/apis/default/fhir/"):
            return httpx.Response(200, json={"entry": []})
        if api == "/facility":
            return httpx.Response(200, json={"data": [{"id": "3", "name": "CarePlix Demo Clinic"}]})
        if api == "/practitioner":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "12", "fname": "Jordan", "lname": "Reyes", "npi": "1902345689"}]
                },
            )
        if api == "/patient":
            return httpx.Response(200, json={"data": self.patients})
        if api.endswith("/insurance"):
            return httpx.Response(200, json={"data": self.insurance.get(api.split("/")[2], [])})
        if api in ("/list/apptstat", "/insurance_company"):
            return httpx.Response(200, json={})
        if api == "/appointment":
            listed = [
                {k: v for k, v in a.items() if k != "pc_hometext"} for a in self.appts.values()
            ]
            return httpx.Response(200, json=listed)  # the list omits notes, like 7.0.3
        if api.startswith("/appointment/"):
            appt = self.appts.get(api.split("/")[2])
            return httpx.Response(200, json=[appt] if appt else [])
        parts = api.strip("/").split("/")
        if parts[0] == "patient" and parts[2:3] == ["appointment"] and method == "POST":
            body = json.loads(request.content)
            eid = str(self.next_eid)
            self.next_eid += 1
            self.appts[eid] = {
                "pc_eid": eid,
                "pc_pid": parts[1],
                "pc_aid": body["pc_aid"],
                "pc_catid": body["pc_catid"],
                "pc_title": body["pc_title"],
                "pc_eventDate": body["pc_eventDate"],
                "pc_startTime": body["pc_startTime"] + ":00",
                "pc_endTime": body["pc_startTime"][:3] + "30:00",
                "pc_apptstatus": "-",
                "pc_facility": body["pc_facility"],
                "pc_hometext": body["pc_hometext"],
                "pid": parts[1],
            }
            return httpx.Response(200, json={"id": int(eid)})
        if parts[0] == "patient" and parts[2:3] == ["appointment"] and method == "PUT":
            if not self.connector:
                return httpx.Response(404)  # core OpenEMR has no appointment update route
            headers = {"X-CarePlix-Connector": "1.0.0"}
            body = json.loads(request.content)
            appt = self.appts.get(parts[3])
            if not appt or appt["pc_pid"] != parts[1]:
                return httpx.Response(404, headers=headers, json={"error": "not found"})
            previous = appt["pc_apptstatus"]
            appt["pc_apptstatus"] = body["pc_apptstatus"]
            if body.get("comment"):
                appt["pc_hometext"] += f" — {body['comment']}"
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "pc_eid": int(parts[3]),
                    "pc_apptstatus": appt["pc_apptstatus"],
                    "previous_status": previous,
                },  # fmt: skip
            )
        if parts[0] == "patient" and parts[2:3] == ["appointment"] and method == "DELETE":
            self.appts.pop(parts[3], None)
            return httpx.Response(200, json={"message": "record deleted"})
        if parts[0] == "patient" and parts[2:3] == ["message"]:
            self.messages.append(json.loads(request.content))
            return httpx.Response(201, json={"mid": len(self.messages)})
        return httpx.Response(404, json={})


class Router(httpx.AsyncBaseTransport):
    def __init__(self, connector: bool = True):
        self.openemr, self.stedi = FakeOpenEmr(connector), FakeStedi()
        self._oe, self._st = httpx.MockTransport(self.openemr), httpx.MockTransport(self.stedi)

    async def handle_async_request(self, request):
        target = self._oe if request.url.host == "openemr.test" else self._st
        return await target.handle_async_request(request)


def _client(tmp_path, connector: bool = True):
    settings = make_settings(
        tmp_path,
        openemr_base_url="https://openemr.test",
        openemr_client_id="cid",
        openemr_client_secret="secret",
        openemr_username="admin",
        openemr_password="pass",
        openemr_facility_id="3",
        stedi_api_key="st-test",
        stedi_base_url="https://stedi.test",
        run_background=False,
        payer_sandbox_url="",
    )
    router = Router(connector)
    return TestClient(
        create_app(settings, FakeGateway(settings), integration_transport=router)
    ), router


def _worker(c):
    return c.portal.call(c.app.state.practice_worker.run_once)


def test_openemr_sync_mirrors_the_clinic_in_local_time(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        overview = c.get("/api/practices").json()
        assert (
            overview["ehr"]["kind"] == "openemr"
            and overview["location"]["name"] == "CarePlix Demo Clinic"
        )
        assert overview["mirror"] == {
            "location": 1,
            "provider": 1,
            "appointment_type": 1,
            "insurance_plan": 1,
            "patient": 3,
            "coverage": 2,
            "appointment": 4,
        }
        assert "{pid}" in overview["links"]["patient"]
        rows = {
            r["id"]: r
            for r in c.get("/api/practices/huddle", params={"days": 14}).json()["appointments"]
        }
        day, start = (
            router.openemr.appts["103"]["pc_eventDate"],
            router.openemr.appts["103"]["pc_startTime"],
        )
        expected = datetime.fromisoformat(f"{day}T{start}").replace(tzinfo=TZ)
        assert (
            datetime.fromisoformat(rows["103"]["start"]) == expected
        )  # clinic-local → UTC and back
        assert rows["103"]["provider"] == "Dr. Jordan Reyes"
        assert rows["103"]["coverage"]["payer_id"] == "60054"  # from FHIR Organization
        assert "asap_list" in rows["102"]["flags"] and rows["101"]["status"] == "cancelled"
        assert "no_insurance_on_file" in rows["104"]["flags"]


def test_appointments_deleted_in_openemr_are_marked_deleted(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        router.openemr.appts.pop("104")
        c.post("/api/practices/sync")
        rows = {
            r["id"]: r
            for r in c.get("/api/practices/huddle", params={"days": 14}).json()["appointments"]
        }
        assert rows["104"]["status"] == "deleted"


def test_backfill_books_into_openemr_and_releases_the_later_visit(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        _worker(c)
        [opening] = c.get("/api/practices/backfill").json()["active"]
        assert opening["source_appointment_id"] == "101"
        top = opening["offers"][0]
        assert top["patient_id"] == "1" and top["tier"] == "asap"
        result = c.post(
            f"/api/practices/backfill/offers/{top['id']}", json={"outcome": "accepted"}
        ).json()
        assert result["state"] == "filled" and result["filled_appointment_id"] == "300"
        booked = router.openemr.appts["300"]
        assert booked["pc_pid"] == "1" and booked["pc_aid"] == "12"
        assert (booked["pc_eventDate"], booked["pc_startTime"][:5]) == (
            router.openemr.appts["101"]["pc_eventDate"],
            router.openemr.appts["101"]["pc_startTime"][:5],
        )
        # The ASAP patient's later visit was released through the connector module: it stays on
        # the calendar as "x Canceled", with the reason (no patient details) in its note.
        released = router.openemr.appts["102"]
        assert released["pc_apptstatus"] == "x"
        assert released["pc_hometext"].endswith(
            "Cancelled by CarePlix: Backfill: patient moved to an earlier slot"
        )
        assert ("DELETE", "/apis/default/api/patient/1/appointment/102") not in router.openemr.calls
        writes = {
            (i["kind"], i["state"], i["confirmation"])
            for i in c.get("/api/practices/intents").json()
        }
        # Direct EHR writes are confirmed immediately, unlike the NexHealth sandbox.
        assert writes == {
            ("appointment_book", "written", "ehr_written"),
            ("appointment_cancel", "written", "ehr_written"),
        }


def test_without_the_connector_module_release_falls_back_to_delete(tmp_path):
    c, router = _client(tmp_path, connector=False)
    with c:
        c.post("/api/auth/dev-login")
        assert (
            c.post("/api/practices/verify", params={"kind": "openemr"}).json()["capability"][
                "connector_module"
            ]
            is None
        )
        _worker(c)
        _worker(c)
        [opening] = c.get("/api/practices/backfill").json()["active"]
        top = opening["offers"][0]
        result = c.post(
            f"/api/practices/backfill/offers/{top['id']}", json={"outcome": "accepted"}
        ).json()
        assert result["state"] == "filled"
        assert "102" not in router.openemr.appts
        assert {i["state"] for i in c.get("/api/practices/intents").json()} == {"written"}


def test_a_provider_conflict_means_the_slot_is_not_free(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        cancelled = router.openemr.appts["101"]
        # Staff already rebooked the time for someone else.
        router.openemr.appts["150"] = {
            **cancelled,
            "pc_eid": "150",
            "pc_pid": "3",
            "pc_apptstatus": "-",
        }
        _worker(c)
        board = c.get("/api/practices/backfill").json()
        assert board["active"] == [] and board["recent"][0]["state"] == "taken"


def test_chart_note_is_written_as_an_openemr_patient_message(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        intent = c.post(
            "/api/practices/intents", json={"patient_id": "1", "note": "Bring new card"}
        ).json()
        done = c.post(f"/api/practices/intents/{intent['id']}/approve").json()
        assert done["state"] == "written" and done["remote_id"] == "1"
        assert router.openemr.messages[0]["body"] == "Bring new card"
