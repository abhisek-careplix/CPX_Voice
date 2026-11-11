"""Practice layer (FO-0 + FO-1) against in-process NexHealth and Stedi fakes.

The fakes reproduce the shapes verified live on 31 Oct 2026: cursor pagination, bearer tokens,
required filters, the 2026-06-01 eligibility layout. All people in them are synthetic.
"""

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api.db import utcnow
from careplix_api.integrations import stedi
from careplix_api.integrations.nexhealth import USER_AGENT, NexHealthClient
from careplix_api.main import create_app
from careplix_api.models import EhrRecord, IntegrationCall, WriteIntent
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility

from .conftest import FakeGateway, make_settings

PAGE = 2  # force several pages so cursor pagination is exercised


def _iso(delta: timedelta) -> str:
    return (utcnow() + delta).isoformat()


class FakeNexHealth:
    """NexHealth v3.0.0 as documented (docs.nexhealth.com, read 11 Nov 2026), synthetic data."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.bodies: list[tuple[str, str, dict]] = []
        self.auths = 0
        self.sync_status: list[dict] = []
        self.fail_alerts: str | None = None
        self.webhook_target = ""
        self.webhook_active = True
        self.subscriptions: list[dict] = []
        # Queued failures: (method, path, status, headers), consumed by the first matching call.
        self.fail_next: list[tuple[str, str, int, dict]] = []
        self.patients = {
            1: {
                "id": 1,
                "first_name": "Jane",
                "last_name": "Roe",
                "email": "jr@example.test",
                "foreign_id": "P1",
                "foreign_id_type": "dentrix",
                "preferred_language": "es",
                "bio": {
                    "date_of_birth": "1980-01-02",
                    "phone_number": "5550100",
                    "home_phone_number": "5550199",
                    "address_line_1": "1 Synthetic Way",
                    "city": "Testville",
                    "state": "IL",
                    "zip_code": "60601",
                },
                "updated_at": _iso(-timedelta(days=3)),
            },
            2: {
                "id": 2,
                "first_name": "Sam",
                "last_name": "Poe",
                "foreign_id": "P2",
                "bio": {"date_of_birth": "1975-05-06"},
                "updated_at": _iso(-timedelta(days=2)),
            },
            4: {
                "id": 4,
                "first_name": "Max",
                "last_name": "Cole",
                "foreign_id": "P4",
                "bio": {"cell_phone_number": "5550104"},
                "updated_at": _iso(-timedelta(days=1)),
            },
            3: {
                "id": 3,
                "first_name": "Ana",
                "last_name": "Lee",
                "foreign_id": "P3",
                "bio": {},
                "updated_at": _iso(-timedelta(days=1)),
            },
        }
        # Patients the API knows that the mirror hasn't synced yet (charts created since).
        self.unsynced: dict[int, dict] = {}
        self.plans = {50: {"id": 50, "name": "Aetna PPO", "payer_id": "60054", "group_num": "G1"}}
        self.coverages = {
            70: {
                "id": 70,
                "patient_id": 1,
                "plan_id": 50,
                "priority": 0,
                "insurance_type": "medical",
                "subscriber_num": "M-ACTIVE",
                "subscription_relation": "self",
                "active": True,
                "updated_at": _iso(-timedelta(days=3)),
            },
            71: {
                "id": 71,
                "patient_id": 2,
                "plan_id": 50,
                "priority": 0,
                "insurance_type": "medical",
                "subscriber_num": "M-INACTIVE",
                "subscription_relation": "self",
                "active": True,
                "updated_at": _iso(-timedelta(days=3)),
            },
        }
        self.subscribers: dict[int, dict] = {}
        self.appointments = {
            500: self._appt(500, 1, timedelta(days=2), note="knee pain since fall"),
            501: self._appt(501, 1, timedelta(days=10)),
            502: self._appt(502, 2, timedelta(hours=20), sooner=True),
            503: self._appt(503, 3, timedelta(days=5)),
            504: self._appt(504, 2, timedelta(days=3), cancelled=True),
            506: self._appt(506, 4, timedelta(days=8), sooner=True),
        }
        self.alerts = {1: [{"id": 801, "patient_id": 1, "note": "Latex allergy"}]}
        self.recalls = [
            {
                "id": 601,
                "patient_id": 3,
                "date_due": (utcnow() + timedelta(days=30)).date().isoformat(),
                "recall_type": {"id": 9, "name": "Prophy", "interval_num": 6, "interval_unit": "M"},
                "updated_at": _iso(-timedelta(days=1)),
            }
        ]
        self.booked: list[dict] = []
        self.created_patients: list[dict] = []
        self.book_error: str | None = None
        self.patch_error: str | None = None
        self.taken: set[str] = set()  # start times NexHealth no longer reports free
        self.slot_queries: list[dict] = []
        self.type_hours = True  # working hours carry appointment types
        self.next_available: str | None = None  # next_available_date when no slots are found
        self.location = {
            "id": 100,
            "name": "Main",
            "tz": "America/Chicago",
            "insert_appt_client": True,
            "last_sync_time": _iso(-timedelta(minutes=10)),
            "foreign_id_type": "dentrix",
        }

    @staticmethod
    def _appt(i, patient, delta, note=None, sooner=False, cancelled=False):
        return {
            "id": i,
            "patient_id": patient,
            "provider_id": 10,
            "provider_name": "Dr. Avery Test",
            "appointment_type_id": 20,
            "location_id": 100,
            "start_time": _iso(delta),
            "end_time": _iso(delta + timedelta(minutes=30)),
            "timezone": "America/Chicago",
            "confirmed": False,
            "cancelled": cancelled,
            "note": note,
            "foreign_id": f"A{i}",
            "sooner_if_possible": sooner,
            "updated_at": _iso(-timedelta(hours=5)),
        }

    def _list(self, request, rows):
        q = request.url.params
        since = q.get("updated_since")
        if since:
            rows = [r for r in rows if r.get("updated_at", "") >= since]
        start = int(q.get("end_cursor") or 0)
        size = min(int(q.get("per_page") or 5), PAGE)
        page = rows[start : start + size]
        more = start + size < len(rows)
        return httpx.Response(
            200,
            json={
                "code": True,
                "data": page,
                "page_info": {
                    "has_next_page": more,
                    "end_cursor": str(start + size) if more else None,
                },
            },
        )

    def _search(self, q) -> list[dict]:
        rows = list(self.patients.values()) + list(self.unsynced.values())
        if q.get("name"):
            words = q["name"].lower().split()
            rows = [
                p
                for p in rows
                if all(w in f"{p['first_name']} {p['last_name']}".lower() for w in words)
            ]
        if q.get("date_of_birth"):
            rows = [
                p for p in rows if (p.get("bio") or {}).get("date_of_birth") == q["date_of_birth"]
            ]
        return rows

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, request.url.params
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, dict(q)))
        if body:
            self.bodies.append((request.method, path, body))
        assert request.headers["user-agent"] == USER_AGENT  # NexHealth's edge blocks default UAs
        assert request.headers["nex-api-version"] == "v3.0.0"
        assert "accept" not in request.headers or "vnd.Nexhealth" not in request.headers["accept"]
        for i, (method, fail_path, status, headers) in enumerate(self.fail_next):
            if method == request.method and fail_path == path:
                del self.fail_next[i]
                return httpx.Response(status, headers=headers, json={"error": ["fail"]})
        if path == "/authenticates":
            self.auths += 1
            if request.headers.get("authorization") != "nh-test":
                return httpx.Response(401, json={"code": False, "error": ["Invalid Credentials"]})
            return httpx.Response(201, json={"code": True, "data": {"token": "tok"}})
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, json={"code": False, "error": ["Unauthorized"]})
        institution = {"id": 7, "name": "Demo Practice", "subdomain": "demo"}
        if path == "/institutions":
            assert "subdomain" not in q
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            **institution,
                            "emrs": ["dentrix"],
                            "locations": [{"id": 100, "name": "Main"}],
                        }
                    ]
                },
            )
        if path == "/webhook_endpoints" and request.method == "GET":
            assert "subdomain" not in q  # endpoints belong to the API user, not a practice
            endpoint = {"id": 12, "target_url": self.webhook_target, "active": self.webhook_active}
            return httpx.Response(200, json={"data": [endpoint] if self.webhook_target else []})
        if path == "/webhook_endpoints" and request.method == "POST":
            assert "subdomain" not in q
            self.webhook_target = body["target_url"]
            self.subscriptions = []
            return httpx.Response(
                201, json={"data": {"id": 12, "target_url": body["target_url"], "secret_key": "sk"}}
            )
        assert q.get("subdomain") == "demo"
        if path == "/locations":
            return httpx.Response(
                200, json={"data": [{**institution, "locations": [self.location]}]}
            )
        if path == "/sync_status":
            return httpx.Response(200, json={"data": self.sync_status})
        if path == "/providers":
            return self._list(request, [{"id": 10, "name": "Dr. Avery Test", "npi": "1234567893"}])
        if path == "/operatories":
            return self._list(request, [{"id": 30, "name": "Op 1", "active": True}])
        if path == "/working_hours":
            hours = {"id": 40, "provider_id": 10, "operatory_id": 30, "begin_time": "08:00"}
            return self._list(request, [{**hours, "end_time": "17:00", "days": ["Monday"]}])
        if path == "/patient_recalls":
            return self._list(request, self.recalls)
        if path == "/appointment_types":
            return httpx.Response(
                200, json={"data": [{"id": 20, "name": "Follow-up", "minutes": 30}]}
            )
        if path == "/insurance_plans":
            return self._list(request, list(self.plans.values()))
        if path == "/patients" and request.method == "GET":
            if q.get("name") or q.get("date_of_birth"):
                return self._list(request, self._search(q))
            return self._list(request, list(self.patients.values()))
        if path == "/patients" and request.method == "POST":
            patient, bio = body["patient"], body["patient"]["bio"]
            for existing in self._search(
                {"name": f"{patient['first_name']} {patient['last_name']}", **bio}
            ):
                if body.get("return_existing_if_match"):
                    return httpx.Response(200, json={"data": {"user": existing}})
                return httpx.Response(
                    400, json={"error": ["A patient with that information already exists"]}
                )
            new = {
                "id": 1000 + len(self.created_patients),
                "first_name": patient["first_name"],
                "last_name": patient["last_name"],
                "email": patient["email"],
                "foreign_id": None,
                "foreign_id_type": "nex",
                "provider_id": body["provider"]["provider_id"],
                "bio": {**bio, "gender": bio.get("gender", "Female"), "new_patient": True},
                "updated_at": _iso(timedelta(0)),
            }
            self.created_patients.append(body)
            self.unsynced[new["id"]] = new
            return httpx.Response(201, json={"data": {"user": new}})
        if path == "/insurance_coverages":
            wanted = q.get("active", "true") != "false"
            rows = [c for c in self.coverages.values() if (c.get("active") is not False) == wanted]
            return self._list(request, rows)
        if path == "/appointments" and request.method == "GET":
            if not (q.get("start") and q.get("end")):
                return httpx.Response(400, json={"error": ["start and end are required"]})
            return self._list(request, list(self.appointments.values()))
        if path in ("/charges", "/claims", "/payments", "/guarantor_balances"):
            if not q.get("updated_since"):
                return httpx.Response(400, json={"error": ["Must provide at least one filter"]})
            return httpx.Response(200, json={"data": []})
        if path == "/document_types":
            return httpx.Response(200, json={"data": []})
        if path == "/available_slots":
            self.slot_queries.append(dict(q))
            assert int(q["days"]) <= 14  # NexHealth: 14 days or fewer per request
            if q.get("appointment_type_id") and not self.type_hours:
                return httpx.Response(200, json={"data": [{"lid": 100, "pid": 10, "slots": []}]})
            free = [
                a["start_time"]
                for a in self.appointments.values()
                if a["cancelled"] and a["start_time"] not in self.taken
            ]
            slots = [{"time": t, "operatory_id": 30, "provider_id": 10} for t in free]
            entry = {"lid": 100, "pid": 10, "slots": slots}
            if not slots and self.next_available:
                entry["next_available_date"] = self.next_available
            return httpx.Response(200, json={"data": [entry]})
        if path == "/appointments" and request.method == "POST":
            if self.book_error:
                return httpx.Response(400, json={"error": [self.book_error]})
            appt = {**body["appt"], "id": 900 + len(self.booked), "foreign_id": None}
            self.booked.append(appt)
            return httpx.Response(201, json={"data": {"appt": appt}})
        parts = path.strip("/").split("/")
        if parts[0] == "appointments" and len(parts) == 2 and request.method == "PATCH":
            if self.patch_error:
                return httpx.Response(400, json={"error": [self.patch_error]})
            appt = self.appointments[int(parts[1])]
            appt.update(body["appt"])
            return httpx.Response(200, json={"data": {"appt": appt}})
        if parts[0] == "appointments" and len(parts) == 2:
            booked = {b["id"]: b for b in self.booked}
            appt = self.appointments.get(int(parts[1])) or booked.get(int(parts[1]))
            if appt is None:
                return httpx.Response(404, json={"error": ["not found"]})
            return httpx.Response(200, json={"data": appt})
        if parts[0] == "insurance_coverages" and len(parts) == 2:
            coverage = self.coverages.get(int(parts[1]))
            if coverage is None:
                return httpx.Response(404, json={"error": ["not found"]})
            subscriber = self.subscribers.get(coverage["id"]) or {
                "id": coverage["patient_id"],
                "name": "Policy Holder",
                "date_of_birth": None,
            }
            return httpx.Response(200, json={"data": {**coverage, "subscriber": subscriber}})
        if parts[0] == "patients" and len(parts) == 2:
            patient = self.patients.get(int(parts[1])) or self.unsynced.get(int(parts[1]))
            if patient is None:
                return httpx.Response(404, json={"error": ["not found"]})
            return httpx.Response(200, json={"data": patient})
        if parts[0] == "patients" and len(parts) == 3 and parts[2] == "alerts":
            if request.method == "GET":
                return httpx.Response(200, json={"data": self.alerts.get(int(parts[1]), [])})
            if self.fail_alerts == "timeout":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": 901,
                        "patient_id": int(parts[1]),
                        "note": body["patient_alert"]["note"],
                    }
                },
            )
        if parts[0] == "patients" and len(parts) == 4:
            return httpx.Response(200, json={"data": {"id": int(parts[3]), "foreign_id": None}})
        if path.endswith("/webhook_subscriptions") and request.method == "POST":
            sub = {"id": len(self.subscriptions) + 1, **body}
            self.subscriptions.append(sub)
            return httpx.Response(201, json={"data": sub})
        if path.endswith("/webhook_subscriptions"):
            return httpx.Response(200, json={"data": self.subscriptions})
        if parts[0] == "webhook_endpoints" and request.method == "DELETE":
            return httpx.Response(200, json={"data": None})
        return httpx.Response(404, json={"error": ["not found"]})


class FakeStedi:
    def __init__(self):
        self.eligibility_bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Key st-test"
        if request.url.path == "/2024-04-01/payers/search":
            return httpx.Response(
                200, json={"items": [{"payer": {"name": "Aetna"}}], "stats": {"total": 1}}
            )
        body = json.loads(request.content)
        self.eligibility_bodies.append(body)
        if body["subscriber"]["memberId"] == "M-INACTIVE":
            return httpx.Response(
                200,
                json={
                    "id": "ec_2",
                    "plans": [
                        {
                            "benefits": {
                                "statuses": [{"service": {"value": "30"}, "status": "INACTIVE"}]
                            }
                        }
                    ],
                },
            )
        return httpx.Response(200, json=RECORDED_ACTIVE)


# Abridged from the live mock response (Aetna test member, 31 Oct 2026).
RECORDED_ACTIVE = {
    "id": "ec_1",
    "payer": {"name": {"organization": "AETNA INC"}},
    "plans": [
        {
            "benefits": {
                "statuses": [
                    {
                        "service": {"value": "30", "definition": "Health Benefit Plan Coverage"},
                        "network": {"indicator": "IN_AND_OUT_OF_NETWORK"},
                        "insuranceType": "PREFERRED_PROVIDER_ORGANIZATION_PPO",
                        "planCoverageDescription": "Gold Plan",
                        "status": "ACTIVE_COVERAGE",
                    }
                ],
                "coPayment": [
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "33", "definition": "Chiropractic"},
                        "amount": "25",
                    },
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "98", "definition": "Office"},
                        "amount": "30",
                    },
                ],
                "coInsurance": [
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "98"},
                        "percent": "0",
                    }
                ],
                "deductible": [
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "30"},
                        "timePeriod": "CONTRACT",
                        "amount": "500",
                    },
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "30"},
                        "timePeriod": "REMAINING",
                        "amount": "350",
                    },
                ],
                "outOfPocket": [
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "30"},
                        "timePeriod": "CONTRACT",
                        "amount": "7000",
                    },
                    {
                        "network": {"indicator": "IN_NETWORK"},
                        "service": {"value": "30"},
                        "timePeriod": "REMAINING",
                        "amount": "6200",
                    },
                ],
            }
        }
    ],
}


class Router(httpx.AsyncBaseTransport):
    def __init__(self):
        self.nexhealth, self.stedi = FakeNexHealth(), FakeStedi()
        self._nh, self._st = httpx.MockTransport(self.nexhealth), httpx.MockTransport(self.stedi)

    @property
    def last_webhook_token(self) -> str:
        return self.nexhealth.webhook_target.rsplit("/", 1)[-1]

    async def handle_async_request(self, request):
        target = self._nh if request.url.host == "nexhealth.test" else self._st
        return await target.handle_async_request(request)


def _client(tmp_path, **overrides):
    values = {
        "nexhealth_api_key": "nh-test",
        "nexhealth_subdomain": "demo",
        "nexhealth_location_id": "100",
        "nexhealth_institution_id": "7",
        "nexhealth_base_url": "https://nexhealth.test",
        "stedi_api_key": "st-test",
        "stedi_base_url": "https://stedi.test",
        "practice_npi": "1234567893",
        "run_background": False,
        "payer_sandbox_url": "",
    }
    values.update(overrides)
    settings = make_settings(tmp_path, **values)
    router = Router()
    client = TestClient(create_app(settings, FakeGateway(settings), integration_transport=router))
    return client, router


def _worker(c):
    return c.portal.call(c.app.state.practice_worker.run_once)


def _db_rows(c, model):
    async def load():
        async with c.app.state.db.sessions() as session:
            return list(await session.scalars(select(model)))

    return c.portal.call(load)


def _signed(event: dict, secret: str = "sk", at=None) -> tuple[bytes, dict]:
    """A delivery signed the way NexHealth signs it: hex HMAC-SHA256 of ts + "." + b64(body)."""
    body = json.dumps(event).encode()
    stamp = (at or utcnow()).isoformat().replace("+00:00", "Z")
    message = f"{stamp}.{base64.b64encode(body).decode()}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return body, {"timestamp": stamp, "signature": signature, "content-type": "application/json"}


def _deliver(c, token: str, event: dict, **kwargs):
    body, headers = _signed(event, **kwargs)
    return c.post(f"/webhooks/nexhealth/{token}", content=body, headers=headers)


def _huddle(c):
    return c.get("/api/practices/huddle", params={"days": 14}).json()


# --- client ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_follows_cursors_and_reuses_one_token():
    fake = FakeNexHealth()
    client = NexHealthClient(
        "https://nexhealth.test", "nh-test", "demo", transport=httpx.MockTransport(fake)
    )
    rows, pages = await client.list_all("/patients", {"location_id": 100})
    await client.list_all("/insurance_coverages", {"location_id": 100})
    await client.aclose()
    assert sorted(r["id"] for r in rows) == [1, 2, 3, 4] and pages == 2
    assert fake.auths == 1  # cached bearer token
    assert client.calls == 1 + 2 + 1  # auth + two patient pages + one coverage page (cost meter)


# --- FO-0: sync, mirror, probe ----------------------------------------------------------------


def test_first_pass_mirrors_the_practice_and_seals_patient_identifiers(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        overview = c.get("/api/practices").json()
        assert overview["ehr"]["env"] == "sandbox" and overview["clearinghouse"]["env"] == "test"
        assert overview["mirror"] == {
            "location": 1,
            "provider": 1,
            "operatory": 1,
            "working_hour": 1,
            "appointment_type": 1,
            "insurance_plan": 1,
            "patient": 4,
            "coverage": 2,
            "appointment": 6,
            "patient_recall": 1,
            "patient_alert": 1,  # read per patient with a visit in the next week
        }
        location = overview["location"]
        assert location["name"] == "Main" and location["tz"] == "America/Chicago"
        assert location["insert_appt_client"] is True and location["synced"] is True
        records = _db_rows(c, EhrRecord)
        raw = json.dumps([[r.attrs, r.pii] for r in records])
        secrets = ("Jane", "Roe", "1980-01-02", "5550100", "5550199", "Synthetic Way", "60601")
        for secret in (*secrets, "M-ACTIVE", "knee pain", "Latex"):
            assert secret not in raw  # sealed or never mirrored
        box = PhiBox(c.app.state.settings)
        [jane] = [r for r in records if r.resource == "patient" and r.source_id == "1"]
        pii = box.open(jane.pii)
        assert pii["phones"]["home_phone_number"] == "5550199"
        assert pii["address"]["city"] == "Testville" and pii["preferred_language"] == "es"
        assert jane.attrs["placeholder"] is False
        [alert] = [r for r in records if r.resource == "patient_alert"]
        assert alert.patient_ref == "1" and box.open(alert.pii)["note"] == "Latex allergy"
        # Patients are this location's own; lists are read newest-change last.
        patient_reads = [q for m, p, q in router.nexhealth.requests if p == "/patients"]
        assert all(q.get("location_strict") == "true" for q in patient_reads)
        assert all(q.get("sort") == "updated_at" for q in patient_reads)
        assert overview["api_calls_total"] > 0


def test_incremental_sync_asks_only_for_changes(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        router.nexhealth.requests.clear()
        report = c.post("/api/practices/sync").json()
        patient_reads = [q for m, p, q in router.nexhealth.requests if p == "/patients"]
        assert patient_reads and all("updated_since" in q for q in patient_reads)
        # Reference data was refreshed on the first pass and is not re-read within 24 h.
        assert "provider" not in report["resources"]


def test_probe_maps_capabilities_and_flags_unconfirmable_writes(tmp_path):
    c, _ = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        result = c.post("/api/practices/verify").json()
        cap = result["capability"]
        assert result["ok"] and cap["location"]["name"] == "Main"
        assert cap["reads"]["charges"]["ok"] and cap["reads"]["appointments"]["ok"]
        assert cap["writes_confirmable"] is False  # sandbox: no sync behind the demo location
        assert c.post("/api/practices/verify", params={"kind": "stedi"}).json()["ok"]


# --- FO-1: eligibility autopilot --------------------------------------------------------------


def test_eligibility_autopilot_checks_every_upcoming_visit_in_mock_mode(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)  # sync + schedule + run
        _worker(c)
        rows = {r["id"]: r for r in _huddle(c)["appointments"]}
        active = rows["500"]["eligibility"]
        assert active["state"] == "done" and active["mock"] is True
        assert (
            active["summary"]["coverage"] == "active" and active["summary"]["office_copay"] == "30"
        )
        assert active["summary"]["deductible"] == {"total": "500", "remaining": "350"}
        assert (
            rows["501"]["eligibility"]["reused"] is True
        )  # same coverage within 24 h: no new call
        assert "no_insurance_on_file" in rows["503"]["flags"]
        assert rows["504"]["status"] == "cancelled" and rows["504"]["eligibility"] is None
        assert "asap_list" in rows["502"]["flags"]
        assert rows["502"]["eligibility"]["trigger"] == "t24"
        # Test mode never sends a patient identifier: every request is Stedi's mock member.
        assert {b["subscriber"]["memberId"] for b in router.stedi.eligibility_bodies} == {
            "AETNA12345"
        }
        assert len(router.stedi.eligibility_bodies) == 2  # coverages 70 and 71, once each


def test_production_eligibility_flags_inactive_coverage_for_review(tmp_path):
    c, router = _client(tmp_path, stedi_env="production")
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        _worker(c)
        rows = {r["id"]: r for r in _huddle(c)["appointments"]}
        assert rows["502"]["eligibility"]["summary"]["coverage"] == "inactive"
        assert "coverage_inactive" in rows["502"]["flags"]
        sent = {b["subscriber"]["memberId"] for b in router.stedi.eligibility_bodies}
        assert sent == {"M-ACTIVE", "M-INACTIVE"}
        proposals = c.get("/api/practices/intents").json()
        assert [(p["source"], p["state"], p["target_id"]) for p in proposals] == [
            ("eligibility", "proposed", "2")
        ]
        # The audit holds outcomes only.
        audit = json.dumps([x.summary for x in _db_rows(c, IntegrationCall)])
        assert "M-INACTIVE" not in audit and "Poe" not in audit


def test_due_trigger_follows_the_72_and_24_hour_schedule():
    h = timedelta(hours=1)
    assert eligibility.due_trigger(200 * h, set()) == "booking"
    assert eligibility.due_trigger(60 * h, {"booking"}) == "t72"
    assert eligibility.due_trigger(60 * h, {"booking", "t72"}) is None
    assert eligibility.due_trigger(10 * h, {"booking", "t72"}) == "t24"
    assert eligibility.due_trigger(10 * h, set()) == "t24"


def test_summary_reads_the_2026_layout():
    summary = stedi.summarize_eligibility(RECORDED_ACTIVE)
    assert summary["coverage"] == "active" and summary["plan"] == "Gold Plan"
    assert summary["office_copay"] == "30" and summary["out_of_pocket"]["remaining"] == "6200"
    assert summary["payer"] == "AETNA INC"


# --- write intents ------------------------------------------------------------------------------


def test_chart_alert_is_proposed_approved_and_tracked(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        body = {"patient_id": "1", "note": "Bring new insurance card"}
        intent = c.post("/api/practices/intents", json=body).json()
        assert intent["state"] == "proposed" and intent["level"] == "L2"
        assert c.post("/api/practices/intents", json=body).json()["id"] == intent["id"]  # dedupe
        done = c.post(f"/api/practices/intents/{intent['id']}/approve").json()
        assert done["state"] == "accepted" and done["confirmation"] == "not_reported"
        assert done["remote_id"] == "901"
        sends = [r for r in router.nexhealth.requests if r[:2] == ("POST", "/patients/1/alerts")]
        assert len(sends) == 1
        assert c.post(f"/api/practices/intents/{intent['id']}/approve").status_code == 409
        stored = _db_rows(c, WriteIntent)[0]
        assert "insurance card" not in (stored.payload or "")  # sealed


def test_writes_hold_while_ehr_sync_is_offline(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        router.nexhealth.sync_status = [
            {"emr": {"display_name": "athenahealth"}, "read_status": "green", "write_status": "red"}
        ]
        c.post("/api/practices/verify")
        intent = c.post("/api/practices/intents", json={"patient_id": "1", "note": "x"}).json()
        held = c.post(f"/api/practices/intents/{intent['id']}/approve").json()
        assert held["state"] == "approved" and "offline" in held["last_error"]
        assert not [
            r for r in router.nexhealth.requests if r[0] == "POST" and r[1].endswith("/alerts")
        ]


def test_unknown_write_outcome_goes_to_a_person_not_a_retry(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        router.nexhealth.fail_alerts = "timeout"
        intent = c.post("/api/practices/intents", json={"patient_id": "1", "note": "x"}).json()
        result = c.post(f"/api/practices/intents/{intent['id']}/approve").json()
        assert result["state"] == "needs_review" and result["attempts"] == 1
        _worker(c)
        posts = [
            r for r in router.nexhealth.requests if r[0] == "POST" and r[1].endswith("/alerts")
        ]
        assert len(posts) == 1


# --- webhooks -------------------------------------------------------------------------------------


def test_nexhealth_webhooks_register_dedupe_and_refresh_the_mirror(tmp_path):
    c, router = _client(tmp_path, webhook_public_url="https://hooks.test")
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        registered = c.post("/api/practices/webhooks/register").json()
        assert "appointment_updated" in registered["events"]
        assert router.nexhealth.webhook_target.startswith("https://hooks.test/webhooks/nexhealth/")
        token = router.last_webhook_token
        router.nexhealth.appointments[500]["cancelled"] = True
        event = {
            "event_name": "appointment_updated",
            "resource_type": "Appointment",
            "subdomain": "demo",
            "data": {"appointment": {"id": 500, "patient_id": 1}},
        }
        first = _deliver(c, token, event).json()
        again = _deliver(c, token, event).json()
        assert first == {"ok": True, "duplicate": False} and again["duplicate"] is True
        body, headers = _signed(event)
        wrong = c.post("/webhooks/nexhealth/wrong-token", content=body, headers=headers)
        assert wrong.status_code == 404
        _worker(c)
        rows = {r["id"]: r for r in _huddle(c)["appointments"]}
        assert rows["500"]["status"] == "cancelled"


def test_insertion_webhook_marks_a_write_as_in_the_ehr(tmp_path):
    c, _ = _client(tmp_path, webhook_public_url="https://hooks.test")
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        c.post("/api/practices/webhooks/register")
        token = c.app.state.integration_transport.last_webhook_token

        async def seed_intent():
            async with c.app.state.db.sessions() as session:
                conn_id = (await session.scalars(select(EhrRecord.connection_id))).first()
                session.add(
                    WriteIntent(
                        workspace_id=(
                            await session.scalars(select(EhrRecord.workspace_id))
                        ).first(),
                        connection_id=conn_id,
                        kind="patient_alert",
                        level="L2",
                        target_resource="appointment",
                        target_source_id="500",
                        payload_hash="h",
                        reason="test",
                        source="test",
                        requested_by="t",
                        state="accepted",
                        confirmation="pending",
                        remote_id="555",
                        sent_at=utcnow(),
                    )
                )
                await session.commit()

        c.portal.call(seed_intent)
        event = {
            "event_name": "appointment_insertion.complete",
            "resource_type": "Appointment",
            "data": {"appointment": {"id": 555}},
        }
        assert _deliver(c, token, event).json()["ok"]
        _worker(c)
        assert _db_rows(c, WriteIntent)[0].state == "written"


def test_stedi_webhooks_require_a_valid_signature(tmp_path):
    secret = "whsec_" + base64.b64encode(b"k" * 24).decode()
    c, _ = _client(tmp_path, stedi_webhook_secret=secret)
    with c:
        body = json.dumps(
            {"type": "transaction.processed", "resource": {"id": "tx_1", "type": "transaction"}}
        ).encode()
        stamp = str(int(time.time()))
        sig = base64.b64encode(
            hmac.new(b"k" * 24, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()
        ).decode()
        headers = {
            "webhook-id": "msg_1",
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{sig}",
        }
        assert c.post("/webhooks/stedi", content=body, headers=headers).json()["ok"]
        bad = {**headers, "webhook-signature": "v1,AAAA"}
        assert c.post("/webhooks/stedi", content=body, headers=bad).status_code == 401
