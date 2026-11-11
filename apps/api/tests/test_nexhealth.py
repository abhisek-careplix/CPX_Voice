"""NexHealth integration hardening, against the documented v3.0.0 behaviour (read 11 Nov 2026).

Signed webhooks, safe retries and the per-key throttle, PATCH confirmation from
``appointment_updated``, the live patient lookup and new-patient intake, in-place reschedules,
subscriber details, ended coverage, history backfill, placeholder merges and webhook health.
Everything runs against the in-process fake; all people are synthetic.
"""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from careplix_api.db import utcnow
from careplix_api.integrations import nexhealth
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import EhrRecord, PayerConnection, SyncCursor, WebhookEvent, WriteIntent
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility, intents
from careplix_api.practice.receptionist import spoken
from careplix_api.practice.webhooks import verify_nexhealth

from .test_practice import FakeNexHealth, _client, _db_rows, _deliver, _signed, _worker
from .test_receptionist import JANE, _call

DENTRIX = [{"emr": {"display_name": "Dentrix"}, "read_status": "green", "write_status": "green"}]


def _client_for(fake: FakeNexHealth) -> NexHealthClient:
    return NexHealthClient(
        "https://nexhealth.test", "nh-test", "demo", transport=httpx.MockTransport(fake)
    )


def _as(c, router, syncs):
    """Point the practice at an EHR (its sync status names it) and re-probe."""
    router.nexhealth.sync_status = syncs
    assert c.post("/api/practices/verify").json()["ok"]


def _send_intent(c, kind: str, target: str, payload: dict) -> str:
    async def propose():
        async with c.app.state.db.sessions() as session:
            conn = await session.scalar(
                select(PayerConnection).where(PayerConnection.kind == "nexhealth")
            )
            intent = await intents.propose(
                session,
                conn,
                c.app.state.settings,
                kind=kind,
                target_source_id=target,
                payload=payload,
                reason="test",
                source="test",
                requested_by="t",
            )
            await intents.decide(session, intent, "t", True)
            await session.commit()
            return intent.id

    intent_id = c.portal.call(propose)
    c.portal.call(
        intents.execute_approved,
        c.app.state.db,
        c.app.state.settings,
        c.app.state.integration_transport,
        [intent_id],
    )
    return intent_id


def _intent(c, intent_id: str) -> WriteIntent:
    return next(i for i in _db_rows(c, WriteIntent) if i.id == intent_id)


def _record(c, resource: str, source_id: str) -> EhrRecord:
    return next(
        r for r in _db_rows(c, EhrRecord) if r.resource == resource and r.source_id == source_id
    )


def _registered(c, router) -> str:
    c.post("/api/practices/webhooks/register").raise_for_status()
    return router.last_webhook_token


def _updated(appointment_id: int, outcome: str = "", changed: dict | None = None) -> dict:
    appointment: dict = {"id": appointment_id}
    if changed is not None:
        appointment["previous_attributes"] = changed
    return {
        "event_name": "appointment_updated" + (f".{outcome}" if outcome else ""),
        "event_time": utcnow().isoformat(),
        "resource_type": "Appointment",
        "subdomain": "demo",
        "webhook_subscription_id": 2,
        "data": {"appointment": appointment},
    }


@pytest.fixture
def practice(tmp_path, request):
    overrides = {"webhook_public_url": "https://hooks.test", **getattr(request, "param", {})}
    c, router = _client(tmp_path, **overrides)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)
        yield c, router


# --- client: tokens, headers, retries, throttle, pagination ------------------------------------


@pytest.mark.asyncio
async def test_one_token_per_key_across_clients_and_no_v2_header():
    fake = FakeNexHealth()  # asserts Nex-Api-Version v3.0.0 and no v2 Accept header on every call
    first, second = _client_for(fake), _client_for(fake)
    await first.get("/providers", {"location_id": 100})
    await second.get("/providers", {"location_id": 100})
    await first.aclose()
    await second.aclose()
    assert fake.auths == 1  # the second client reuses the process-wide token for this key


@pytest.mark.asyncio
async def test_reads_retry_5xx_and_429_honouring_retry_after(monkeypatch):
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(nexhealth, "sleep", record)
    fake = FakeNexHealth()
    fake.fail_next = [
        ("GET", "/providers", 503, {}),
        ("GET", "/providers", 429, {"Retry-After": "2"}),
    ]
    client = _client_for(fake)
    data = await client.get("/providers", {"location_id": 100})
    await client.aclose()
    assert data and [r[1] for r in fake.requests].count("/providers") == 3
    assert 2.0 in waits


@pytest.mark.asyncio
async def test_writes_never_retry_a_5xx_but_retry_a_429():
    fake = FakeNexHealth()
    fake.fail_next = [("POST", "/appointments", 503, {})]
    client = _client_for(fake)
    appt = {"patient_id": 1, "start_time": "2026-12-01T15:00:00Z"}
    with pytest.raises(NexHealthError) as err:
        await client.request("POST", "/appointments", json={"appt": appt})
    assert err.value.http_status == 503
    assert [r[:2] for r in fake.requests].count(("POST", "/appointments")) == 1  # outcome unknown

    fake.fail_next = [("POST", "/appointments", 429, {"Retry-After": "1"})]
    body = await client.request("POST", "/appointments", json={"appt": appt})
    await client.aclose()
    assert body["data"]["appt"]["id"] == 900  # refused once (429: not processed), then accepted
    assert [r[:2] for r in fake.requests].count(("POST", "/appointments")) == 3
    assert len(fake.booked) == 1


@pytest.mark.asyncio
async def test_throttle_keeps_a_key_under_its_budgets(monkeypatch):
    now, waits = [0.0], []

    async def advance(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(nexhealth, "clock", lambda: now[0])
    monkeypatch.setattr(nexhealth, "sleep", advance)
    monkeypatch.setattr(nexhealth, "LIMIT_PER_SECOND", 2)
    monkeypatch.setitem(nexhealth.ROUTE_LIMITS, ("GET", "/patients"), 3)
    throttle = nexhealth.Throttle()
    for _ in range(3):
        await throttle.acquire("GET", "/providers")
    assert waits == [1.0]  # the third call waits for the per-second window
    waits.clear()
    for _ in range(4):
        now[0] += 1.0  # well inside the per-second budget
        await throttle.acquire("GET", "/patients")
    assert waits and waits[0] > 50  # GET /patients has its own per-minute budget


@pytest.mark.asyncio
async def test_list_all_never_returns_a_partial_list():
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticates":
            return httpx.Response(201, json={"data": {"token": "tok"}})
        return httpx.Response(200, json={"data": [{"id": 1}], "page_info": {"has_next_page": True}})

    client = NexHealthClient(
        "https://nexhealth.test", "nh-test", "demo", transport=httpx.MockTransport(broken)
    )
    with pytest.raises(NexHealthError) as err:
        await client.list_all("/patients", {"location_id": 100})
    assert err.value.status == "incomplete"
    fake = FakeNexHealth()
    paged = _client_for(fake)
    with pytest.raises(NexHealthError):
        await paged.list_all("/patients", {"location_id": 100}, max_pages=1)
    rows, pages = await paged.list_all("/patients", {"location_id": 100})
    await client.aclose()
    await paged.aclose()
    assert len(rows) == 4 and pages == 2  # read to has_next_page false, not to a page cap


def test_a_failed_list_leaves_the_cursor_where_it_was(practice):
    c, router = practice
    before = next(x for x in _db_rows(c, SyncCursor) if x.resource == "patient").high_water
    router.nexhealth.patients[2]["updated_at"] = utcnow().isoformat()
    router.nexhealth.fail_next = [("GET", "/patients", 500, {})] * 3
    report = c.post("/api/practices/sync").json()
    assert "error" in report["resources"]["patient"]
    cursor = next(x for x in _db_rows(c, SyncCursor) if x.resource == "patient")
    assert cursor.high_water == before and cursor.last_error


# --- webhooks: signatures, identity, health ---------------------------------------------------


def test_a_subscriber_name_splits_when_their_chart_is_not_mirrored():
    from careplix_api.practice.records import map_coverage, split_name

    assert split_name("Poe, Sam") == ("Sam", "Poe") and split_name(" Ana  de la Cruz ") == (
        "Ana",
        "de la Cruz",
    )
    coverage = map_coverage(
        {
            "id": 9,
            "patient_id": 3,
            "subscription_relation": "spouse",
            "subscriber": {"id": 77, "name": "Lee Park", "date_of_birth": "1970-01-01"},
        }
    )
    assert coverage.pii["subscriber_first_name"] == "Lee"
    assert coverage.attrs["subscriber_id"] == "77"


def test_signature_matches_the_documented_scheme():
    # Fixture built the way NexHealth's docs describe: hex HMAC-SHA256(secret_key,
    # timestamp + "." + base64(raw body)).
    body = b'{"event_name":"appointment_insertion.complete","data":{"appointment":{"id":1}}}'
    stamp = "2026-11-11T10:00:00.000Z"
    expected = hmac.new(
        b"whsec_fixture",
        f"{stamp}.{base64.b64encode(body).decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    at = utcnow().replace(year=2026, month=11, day=11, hour=10, minute=1, second=0)
    headers = {"timestamp": stamp, "signature": expected}
    assert verify_nexhealth("whsec_fixture", headers, body, now=at)
    assert not verify_nexhealth("other", headers, body, now=at)
    assert not verify_nexhealth("whsec_fixture", headers, body + b" ", now=at)  # raw bytes
    assert not verify_nexhealth("whsec_fixture", headers, body, now=at + timedelta(minutes=10))
    assert not verify_nexhealth("whsec_fixture", {"timestamp": stamp}, body, now=at)
    assert not verify_nexhealth(None, headers, body, now=at)


def test_unsigned_stale_or_forged_deliveries_are_refused(practice):
    c, router = practice
    token = _registered(c, router)
    event = _updated(500)
    unsigned = c.post(f"/webhooks/nexhealth/{token}", content=json.dumps(event))
    assert unsigned.status_code == 401
    assert _deliver(c, token, event, secret="not-the-key").status_code == 401
    stale = utcnow() - timedelta(minutes=10)
    assert _deliver(c, token, event, at=stale).status_code == 401
    body, headers = _signed(event)
    # The URL token still matters: a valid signature on an unknown endpoint is not accepted.
    assert c.post("/webhooks/nexhealth/nope", content=body, headers=headers).status_code == 404
    assert _deliver(c, token, event).status_code == 200
    assert len(_db_rows(c, WebhookEvent)) == 1


def test_a_retried_delivery_dedupes_on_event_identity(practice):
    c, router = practice
    token = _registered(c, router)
    event = _updated(500, changed={"confirmed": False})
    assert _deliver(c, token, event).json()["duplicate"] is False
    retry = {**event, "delivery_errors": [{"code": "500", "attempts": 1}]}
    assert _deliver(c, token, retry).json()["duplicate"] is True  # body differs, event is the same
    later = {**event, "event_time": (utcnow() + timedelta(seconds=5)).isoformat()}
    assert _deliver(c, token, later).json()["duplicate"] is False
    [first, *_] = _db_rows(c, WebhookEvent)
    assert first.detail == {"changed": ["confirmed"], "delivery_errors": 0}


def test_registration_subscribes_insertion_events_for_every_documented_write(practice):
    c, router = practice
    result = c.post("/api/practices/webhooks/register").json()
    assert {"appointment_insertion", "appointment_updated", "payment_insertion"} <= set(
        result["events"]
    )
    assert "adjustment_insertion" in result["events"]
    # subdomain is in the query string, never in the JSON body.
    subs = [b for m, p, b in router.nexhealth.bodies if p.endswith("/webhook_subscriptions")]
    assert subs and all("subdomain" not in b for b in subs)
    assert c.get("/api/practices").json()["ehr"]["webhook"]["signed"] is True


def test_webhook_health_surfaces_a_deactivated_endpoint_and_inactive_subscriptions(practice):
    c, router = practice
    token = _registered(c, router)
    assert c.post("/api/practices/webhooks/check").json()["status"] == "ok"
    router.nexhealth.subscriptions[0]["active"] = False
    health = c.post("/api/practices/webhooks/check").json()
    assert health["status"] == "subscriptions_inactive"
    assert health["inactive_subscriptions"] == ["appointment_created"]
    router.nexhealth.webhook_active = False
    assert c.post("/api/practices/webhooks/check").json()["status"] == "deactivated"
    view = c.get("/api/practices").json()["ehr"]["webhook"]["health"]
    assert view["status"] == "deactivated" and "Register" in view["message"]

    router.nexhealth.webhook_active = True
    router.nexhealth.subscriptions[0]["active"] = True
    retried = {**_updated(501), "delivery_errors": [{"code": "500", "attempts": 3}]}
    _deliver(c, token, retried).raise_for_status()
    health = c.post("/api/practices/webhooks/check").json()
    assert health["status"] == "retrying" and "pauses" in health["message"]


# --- PATCH writes: confirmation only from appointment_updated ---------------------------------


def test_a_cancel_is_written_only_when_appointment_updated_names_cancelled(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    token = _registered(c, router)
    intent_id = _send_intent(c, "appointment_cancel", "500", {"patient_id": 1})
    assert ("PATCH", "/appointments/500", {"appt": {"cancelled": True}}) in router.nexhealth.bodies
    accepted = _intent(c, intent_id)
    # The appointment had a foreign_id before the cancel: that proves nothing.
    assert (accepted.state, accepted.confirmation) == ("accepted", "pending")
    _deliver(c, token, _updated(500)).raise_for_status()  # no previous_attributes
    _deliver(c, token, _updated(500, changed={"confirmed": False})).raise_for_status()
    _worker(c)
    assert _intent(c, intent_id).state == "accepted"
    _deliver(c, token, _updated(500, changed={"cancelled": False})).raise_for_status()
    _worker(c)
    written = _intent(c, intent_id)
    assert (written.state, written.confirmation) == ("written", "ehr_written")
    assert _record(c, "appointment", "500").status == "cancelled"


@pytest.mark.parametrize("outcome", ["failed", "timed_out", "incomplete"])
def test_a_failed_update_sends_the_write_to_a_person(practice, outcome):
    c, router = practice
    _as(c, router, DENTRIX)
    token = _registered(c, router)
    intent_id = _send_intent(c, "appointment_confirm", "501", {"confirmed": True})
    assert ("PATCH", "/appointments/501", {"appt": {"confirmed": True}}) in router.nexhealth.bodies
    _deliver(c, token, _updated(501, outcome, {"confirmed": False})).raise_for_status()
    _worker(c)
    intent = _intent(c, intent_id)
    assert intent.state == "needs_review" and outcome.replace("_", " ") in intent.last_error


def test_a_read_back_never_confirms_a_cancel(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    intent_id = _send_intent(c, "appointment_cancel", "503", {"patient_id": 3})

    async def age():
        async with c.app.state.db.sessions() as session:
            intent = await session.get(WriteIntent, intent_id)
            intent.sent_at -= timedelta(hours=3)
            await session.commit()

    c.portal.call(age)
    _worker(c)
    # cancelled=true plus a foreign_id used to count as written; now only the webhook does.
    intent = _intent(c, intent_id)
    assert intent.state == "needs_review" and "confirmation" in intent.last_error


def test_insertion_rereads_the_appointment_for_its_foreign_id(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    token = _registered(c, router)
    start = utcnow() + timedelta(days=4)
    payload = {
        "location_id": "100",
        "patient_id": 1,
        "provider_id": 10,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=30)).isoformat(),
    }
    intent_id = _send_intent(c, "appointment_book", "1", payload)
    assert _intent(c, intent_id).remote_id == "900"
    router.nexhealth.booked[0]["foreign_id"] = "D-900"
    event = {
        "event_name": "appointment_insertion.complete",
        "event_time": utcnow().isoformat(),
        "resource_type": "Appointment",
        "data": {"appointment": {"id": 900}},
    }
    _deliver(c, token, event).raise_for_status()
    _worker(c)
    intent = _intent(c, intent_id)
    assert (intent.state, intent.foreign_id) == ("written", "D-900")
    assert _record(c, "appointment", "900").attrs["foreign_id"] == "D-900"


def test_an_incomplete_insertion_needs_review(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    token = _registered(c, router)
    start = utcnow() + timedelta(days=4)
    intent_id = _send_intent(
        c,
        "appointment_book",
        "2",
        {"location_id": "100", "patient_id": 2, "start_time": start.isoformat()},
    )
    event = {
        "event_name": "appointment_insertion.incomplete",
        "event_time": utcnow().isoformat(),
        "resource_type": "Appointment",
        "data": {"appointment": {"id": 900}},
    }
    _deliver(c, token, event).raise_for_status()
    _worker(c)
    assert _intent(c, intent_id).state == "needs_review"


def test_studio_confirm_and_check_in_join_the_write_queue(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    proposed = c.post("/api/practices/appointments/500/write", json={"action": "confirm"}).json()
    assert proposed["kind"] == "appointment_confirm" and proposed["state"] == "proposed"
    done = c.post(f"/api/practices/intents/{proposed['id']}/approve").json()
    assert (done["state"], done["confirmation"]) == ("accepted", "pending")
    assert ("PATCH", "/appointments/500", {"appt": {"confirmed": True}}) in router.nexhealth.bodies
    # A visit two days out can't be checked in (NexHealth refuses a future check-in time).
    later = c.post("/api/practices/appointments/500/write", json={"action": "check_in"})
    assert later.status_code == 409


def test_check_in_never_leaves_an_ehr_that_cannot_take_it(practice):
    c, router = practice
    athena = [{**DENTRIX[0], "emr": {"display_name": "athenahealth"}}]
    _as(c, router, athena)
    intent_id = _send_intent(c, "appointment_check_in", "502", {"checkin_at": "x"})
    intent = _intent(c, intent_id)
    assert intent.state == "manual" and "athenahealth" in intent.last_error
    assert not [b for b in router.nexhealth.bodies if b[1] == "/appointments/502"]


# --- sync: subscribers, ended coverage, history, placeholders, more data ----------------------


def test_coverage_subscriber_is_read_and_sealed_and_ended_coverage_turns_inactive(tmp_path):
    c, router = _client(tmp_path)
    fake = router.nexhealth
    fake.coverages[72] = {
        **fake.coverages[70],
        "id": 72,
        "patient_id": 3,
        "subscriber_num": "M-CHILD",
        "subscription_relation": "child",
    }
    fake.subscribers[72] = {"id": 2, "name": "Sam Poe", "date_of_birth": "1975-05-06"}
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        box = PhiBox(c.app.state.settings)
        child = _record(c, "coverage", "72")
        assert child.attrs["subscriber_details"] == "loaded"
        assert child.attrs["subscriber_id"] == "2" and child.attrs["subscriber_is_patient"] is False
        pii = box.open(child.pii)
        assert pii["subscriber_name"] == "Sam Poe"
        assert pii["subscriber_date_of_birth"] == "1975-05-06"
        # The names eligibility reads (from Sam's own chart, patient 2).
        assert (pii["subscriber_first_name"], pii["subscriber_last_name"]) == ("Sam", "Poe")
        holder = eligibility.subscriber_identity(pii)
        assert holder == {
            "first_name": "Sam",
            "last_name": "Poe",
            "date_of_birth": "1975-05-06",
            "member_id": "M-CHILD",
        }
        assert "Sam Poe" not in json.dumps(child.attrs) and "Sam Poe" not in (child.pii or "")
        own = _record(c, "coverage", "70")
        assert own.attrs["subscriber_details"] == "self" and own.attrs["subscriber_id"] == "1"
        # Self coverages need no detail read (each read is billable).
        reads = [p for _, p, _ in fake.requests if p.startswith("/insurance_coverages/")]
        assert reads == ["/insurance_coverages/72"]

        # The office ends coverage 71: the list of active coverages simply drops it.
        fake.coverages[71].update(active=False, updated_at=utcnow().isoformat())
        fake.requests.clear()
        c.post("/api/practices/sync").raise_for_status()
        assert _record(c, "coverage", "71").status == "inactive"
        ended = [q for _, p, q in fake.requests if p == "/insurance_coverages"]
        assert any(q.get("active") == "false" and q.get("updated_since") for q in ended)
        # A later list pass without the subscriber keeps the one already read.
        assert box.open(_record(c, "coverage", "72").pii)["subscriber_name"] == "Sam Poe"


def test_appointments_load_the_forward_window_and_history_in_monthly_chunks(tmp_path):
    c, router = _client(tmp_path, nexhealth_history_months=2, nexhealth_window_days=90)
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        reads = [q for m, p, q in router.nexhealth.requests if p == "/appointments" and m == "GET"]
        windows = {(q["start"], q["end"]) for q in reads}
        now = utcnow()
        forward = sorted(w for w in windows if w[0] >= (now - timedelta(days=2)).isoformat())
        # 91 days (yesterday to 90 days out) in 30-day pieces on the first (full) pass.
        assert len(forward) == 4
        assert all(b == c for (_, b), (c, _) in zip(forward, forward[1:], strict=False))
        history = sorted(w for w in windows if w not in forward)
        assert len(history) == 3  # this month so far, then two whole calendar months
        assert all(start[8:10] == "01" for start, _ in history)
        assert all(
            q.get("sort") == "updated_at" for q in reads if (q["start"], q["end"]) in forward
        )
        cursor = next(x for x in _db_rows(c, SyncCursor) if x.resource == "appointment_history")
        assert cursor.last_full_at is not None
        router.nexhealth.requests.clear()
        c.post("/api/practices/sync").raise_for_status()
        again = [q for m, p, q in router.nexhealth.requests if p == "/appointments" and m == "GET"]
        # History is done: one window (read page by page), changes only.
        assert len({(q["start"], q["end"]) for q in again}) == 1
        assert all(q.get("updated_since") for q in again)


def test_a_placeholder_merged_into_the_real_chart_moves_its_visits(tmp_path):
    c, router = _client(tmp_path)
    fake = router.nexhealth
    fake.patients[1000] = {
        "id": 1000,
        "first_name": "Pat",
        "last_name": "New",
        "foreign_id": None,
        "foreign_id_type": "nex",
        "bio": {"date_of_birth": "1990-03-04", "new_patient": True},
        "updated_at": (utcnow() - timedelta(days=1)).isoformat(),
    }
    fake.appointments[510] = fake._appt(510, 1000, timedelta(days=6))
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        assert _record(c, "patient", "1000").attrs["placeholder"] is True
        # The office creates the real chart; NexHealth links it and deletes the placeholder.
        del fake.patients[1000]
        fake.patients[5] = {
            "id": 5,
            "first_name": "Pat",
            "last_name": "New",
            "foreign_id": "P5",
            "bio": {"date_of_birth": "1990-03-04"},
            "updated_at": utcnow().isoformat(),
        }
        report = c.post("/api/practices/sync").json()
        assert report["placeholders_merged"] == 1
        placeholder = _record(c, "patient", "1000")
        assert placeholder.status == "deleted" and placeholder.attrs["merged_into"] == "5"
        assert _record(c, "appointment", "510").patient_ref == "5"


def test_a_record_the_source_deleted_is_marked_deleted(practice):
    c, router = practice
    token = _registered(c, router)
    del router.nexhealth.patients[4]
    event = {
        "event_name": "patient_updated",
        "event_time": utcnow().isoformat(),
        "resource_type": "Patient",
        "data": {"patients": [{"id": 4}]},
    }
    _deliver(c, token, event).raise_for_status()
    _worker(c)
    assert _record(c, "patient", "4").status == "deleted"


def test_location_time_zone_is_used_for_spoken_times(tmp_path):
    c, router = _client(tmp_path)
    router.nexhealth.location["tz"] = "America/Los_Angeles"  # appointments say Chicago
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        call = _call(c)
        assert call.ok("find_patient", **JANE) == {"verified": True}
        [first, _] = call.ok("list_appointments")["appointments"]
        start = _record(c, "appointment", "500").starts_at
        assert first["when"] == spoken(start, ZoneInfo("America/Los_Angeles"))


# --- available slots ----------------------------------------------------------------------------


def test_slots_ask_by_visit_type_and_follow_next_available_date(practice):
    c, router = practice
    fake = router.nexhealth
    call = _call(c)
    assert call.ok("find_slots")["slots"]
    query = fake.slot_queries[-1]
    assert query["appointment_type_id"] == "20" and int(query["days"]) <= 14

    fake.appointments[504]["cancelled"] = False  # nothing free now
    jump = (utcnow() + timedelta(days=9)).date().isoformat()
    fake.next_available = jump
    fake.slot_queries.clear()
    call.ok("find_slots")
    assert fake.slot_queries[1]["start_date"] == jump


def test_slots_fall_back_to_slot_length_when_no_hours_carry_the_type(practice):
    c, router = practice
    router.nexhealth.type_hours = False
    found = _call(c).ok("find_slots")
    assert found["slots"]
    first, second = router.nexhealth.slot_queries[-2:]
    assert "appointment_type_id" in first and "appointment_type_id" not in second
    assert second["slot_length"] == "30"


def test_no_times_are_offered_while_the_sync_is_red(practice):
    c, router = practice
    _as(c, router, [{**DENTRIX[0], "read_status": "red"}])
    router.nexhealth.slot_queries.clear()
    result = _call(c).ok("find_slots")
    assert result["slots"] == [] and "offline" in result["message"]
    assert router.nexhealth.slot_queries == []


# --- receptionist: live lookup, new patients, in-place reschedule -----------------------------


def test_find_patient_searches_the_ehr_when_the_mirror_misses(practice):
    c, router = practice
    fake = router.nexhealth
    fake.unsynced[7] = {
        "id": 7,
        "first_name": "Lia",
        "last_name": "Park",
        "foreign_id": "P7",
        "bio": {"date_of_birth": "1992-07-08"},
    }
    fake.unsynced[8] = {
        "id": 8,
        "first_name": "Liam",
        "last_name": "Parker",
        "foreign_id": "P8",
        "bio": {"date_of_birth": "1992-07-08"},
    }
    call = _call(c)
    lia = {"first_name": "Lia", "last_name": "Park", "date_of_birth": "1992-07-08"}
    assert call.ok("find_patient", **lia) == {"verified": True}  # the fuzzy Liam doesn't count
    [search] = [q for m, p, q in fake.requests if p == "/patients" and q.get("name")]
    assert search["location_strict"] == "true" and search["date_of_birth"] == "1992-07-08"
    assert search["name"] == "Lia Park"
    assert _record(c, "patient", "7").patient_ref == "7"  # in the mirror for the rest of the call
    assert "8" not in {r.source_id for r in _db_rows(c, EhrRecord) if r.resource == "patient"}


def test_a_new_patient_is_registered_and_booked_in_one_step(practice):
    c, router = practice
    fake = router.nexhealth
    call = _call(c)
    new = {"first_name": "Rory", "last_name": "Vale", "date_of_birth": "1988-09-10"}
    early = call.tool("book_new_patient", **new, email="rv@example.test", slot_id="S1")
    assert early["result"]["error"] == "lookup_first"
    assert call.ok("find_patient", **new) == {"verified": False}
    [slot] = call.ok("find_slots")["slots"]
    result = call.ok(
        "book_new_patient",
        **new,
        email="rv@example.test",
        phone_number="(312) 555-0100",
        slot_id=slot["slot_id"],
    )
    assert result["state"] == "booked" and result["patient"] == "registered"
    [created] = fake.created_patients
    assert created["return_existing_if_match"] is True
    assert created["provider"] == {"provider_id": 10}
    assert created["patient"]["bio"] == {
        "date_of_birth": "1988-09-10",
        "phone_number": "3125550100",
    }
    [booked] = fake.booked
    assert booked["patient_id"] == 1000
    kinds = {(i.kind, i.state) for i in _db_rows(c, WriteIntent) if i.source == "receptionist"}
    assert kinds == {("patient_create", "accepted"), ("appointment_book", "accepted")}
    # The call is now bound to the new patient, whose details are sealed in the mirror.
    assert "900" in [a["appointment_id"] for a in call.ok("list_appointments")["appointments"]]
    stored = _record(c, "patient", "1000")
    assert stored.attrs["placeholder"] is True and "Rory" not in (stored.pii or "")


@pytest.mark.parametrize(
    "emr,error",
    [("Curve Hero", "new_patients_not_supported"), ("Dolphin", "new_patients_not_supported")],
)
def test_ehrs_that_cannot_take_new_patients_never_get_one(practice, emr, error):
    c, router = practice
    _as(c, router, [{**DENTRIX[0], "emr": {"display_name": emr}}])
    call = _call(c)
    new = {"first_name": "Rory", "last_name": "Vale", "date_of_birth": "1988-09-10"}
    call.ok("find_patient", **new)
    call.ok("find_slots")
    out = call.tool("book_new_patient", **new, email="rv@example.test", slot_id="S1")
    assert out["result"]["error"] == error and "message" in out["result"]["message"]
    assert router.nexhealth.created_patients == [] and router.nexhealth.booked == []


def test_dentrix_needs_the_patients_gender_for_a_new_chart(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    call = _call(c)
    new = {"first_name": "Rory", "last_name": "Vale", "date_of_birth": "1988-09-10"}
    call.ok("find_patient", **new)
    call.ok("find_slots")
    args = {**new, "email": "rv@example.test", "phone_number": "3125550100", "slot_id": "S1"}
    assert call.tool("book_new_patient", **args)["result"]["error"] == "gender_required"
    assert router.nexhealth.created_patients == []
    assert call.ok("book_new_patient", **args, gender="Male")["state"] == "booked"
    assert router.nexhealth.created_patients[0]["patient"]["bio"]["gender"] == "Male"


def test_an_existing_match_is_returned_not_duplicated(practice):
    c, router = practice
    fake = router.nexhealth
    # Known to NexHealth (say, registered on an earlier call) but not yet in the mirror.
    fake.unsynced[1000] = {
        "id": 1000,
        "first_name": "Rory",
        "last_name": "Vale",
        "foreign_id": None,
        "bio": {"date_of_birth": "1988-09-10", "new_patient": True},
    }
    call = _call(c)
    new = {"first_name": "Rory", "last_name": "Vale", "date_of_birth": "1988-09-10"}
    # The live lookup finds the placeholder by name and birth date.
    assert call.ok("find_patient", **new) == {"verified": True}
    assert call.tool("book_new_patient", **new, email="x@example.test", slot_id="S1")["result"][
        "error"
    ] == ("already_verified")


def test_dentrix_moves_a_visit_in_place(practice):
    c, router = practice
    _as(c, router, DENTRIX)
    token = _registered(c, router)
    call = _call(c)
    call.ok("find_patient", **JANE)
    call.ok("find_slots", appointment_id="501")
    result = call.ok("reschedule_appointment", appointment_id="501", slot_id="S1")
    assert result["state"] == "rescheduled"
    assert router.nexhealth.booked == []  # no second visit, nothing to release
    [(_, _, body)] = [b for b in router.nexhealth.bodies if b[:2] == ("PATCH", "/appointments/501")]
    assert set(body["appt"]) == {"start_time", "end_time", "operatory_id"}
    assert router.nexhealth.appointments[501]["cancelled"] is False
    [move] = [i for i in _db_rows(c, WriteIntent) if i.source == "receptionist"]
    assert (move.kind, move.state, move.confirmation) == (
        "appointment_reschedule",
        "accepted",
        "pending",
    )
    moved_to = router.nexhealth.appointments[504]["start_time"]
    assert _record(c, "appointment", "501").starts_at.isoformat()[:16] == moved_to[:16]
    _deliver(c, token, _updated(501, changed={"start_time": "x", "end_time": "y"}))
    _worker(c)
    assert _intent(c, move.id).state == "written"


def test_a_visit_not_yet_in_the_ehr_is_moved_by_book_then_cancel(tmp_path):
    c, router = _client(tmp_path)
    router.nexhealth.appointments[501]["foreign_id"] = None  # NexHealth hasn't written it yet
    with c:
        c.post("/api/auth/dev-login")
        _worker(c)
        _as(c, router, DENTRIX)
        call = _call(c)
        call.ok("find_patient", **JANE)
        call.ok("find_slots", appointment_id="501")
        assert call.ok("reschedule_appointment", appointment_id="501", slot_id="S1")["state"] == (
            "rescheduled"
        )
        assert len(router.nexhealth.booked) == 1
        assert router.nexhealth.appointments[501]["cancelled"] is True
