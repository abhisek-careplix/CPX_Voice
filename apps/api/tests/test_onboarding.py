"""Onboarding a clinic: add a practice, keys per env, go-live checklist, workspace automation,
office-hours confirmation for cloud EHRs. All against in-process fakes; all people synthetic."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from careplix_api.connect.connectors import connector_for, office_elapsed
from careplix_api.db import utcnow
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import PayerConnection, WriteIntent
from careplix_api.practice.connections import nexhealth_client, stedi_client
from careplix_api.settings import Settings

from .conftest import make_settings
from .test_medical_ehrs import MedicalNexHealth, _inserted, _token, _updated
from .test_practice import _client, _db_rows, _deliver, _worker
from .test_receptionist import JANE, _call
from .test_tenancy import CLINIC_ADMIN, _email_sign_in, _new_customer

ATHENA = "nexhealth:athenahealth"
SANDBOX = {"profile": ATHENA, "subdomain": "demo", "location_id": "100", "env": "sandbox"}
PRODUCTION = {**SANDBOX, "env": "production"}


def _app(tmp_path, fake=None, **overrides):
    """No env practice: every practice here is added in the Studio."""
    values = {"nexhealth_subdomain": "", "webhook_public_url": "https://hooks.test", **overrides}
    c, router = _client(tmp_path, **values)
    fake = fake or MedicalNexHealth("athenahealth")
    fake.patients[9] = {
        "id": 9,
        "first_name": "Pat",
        "last_name": "Test",
        "foreign_id": "P9",
        "bio": {"date_of_birth": "1990-01-01"},
        "updated_at": utcnow().isoformat(),
    }
    router.nexhealth, router._nh = fake, httpx.MockTransport(fake)
    return c, fake


@pytest.fixture
def production_deployment(monkeypatch):
    """Behave as a production deployment for the cap only (the full CAREPLIX_ENV=production
    startup rules are environment.py's and its tests')."""
    monkeypatch.setattr(Settings, "production_allowed", property(lambda self: True))


def _steps(view) -> dict[str, bool | None]:
    return {s["key"]: s["ok"] for s in view["onboarding"]["steps"]}


def _items(view) -> dict[str, bool | None]:
    return {i["key"]: i["ok"] for i in view["golive"]["items"]}


# --- add a practice -------------------------------------------------------------------------------


def test_a_clinic_admin_adds_a_sandbox_practice_and_it_syncs(tmp_path):
    c, fake = _app(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _new_customer(c, "practice")
        c.cookies.clear()
        _email_sign_in(c, CLINIC_ADMIN)  # a customer workspace admin, not CarePlix staff
        assert c.get("/api/practices").json()["ehr"] is None  # no lab practice here
        options = c.get("/api/practices/setup/options").json()
        assert {p["id"] for p in options["profiles"]} >= {ATHENA, "nexhealth:modmed"}
        envs = {e["env"]: e["available"] for e in options["envs"]}
        assert envs == {"sandbox": True, "production": False}

        r = c.post("/api/practices/setup", json=SANDBOX)
        assert r.status_code == 200, r.text
        view = r.json()
        assert _steps(view) == {
            "created": True,
            "verified": True,
            "webhooks": True,
            "first_sync": None,
        }
        assert view["connection"]["enabled"] and fake.webhook_target
        assert view["golive"]["required"] is False  # sandbox: synthetic data only
        _worker(c)  # the first full sync runs in the background
        view = c.get(f"/api/practices/setup/{view['connection']['connection_id']}").json()
        assert _steps(view)["first_sync"] is True and view["onboarding"]["done"]
        assert c.get("/api/practices").json()["mirror"]["patient"] == 5
        # The same location can't be added twice.
        assert c.post("/api/practices/setup", json=SANDBOX).status_code == 409
        # Never production in a sandbox deployment.
        r = c.post("/api/practices/setup", json=PRODUCTION)
        assert r.status_code == 409 and r.json()["error"]["code"] == "production_not_allowed"


def test_a_wrong_location_or_subdomain_stays_disabled(tmp_path):
    c, fake = _app(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        wrong = c.post("/api/practices/setup", json={**SANDBOX, "location_id": "999"}).json()
        assert _steps(wrong)["verified"] is False and wrong["connection"]["enabled"] is False
        other = c.post("/api/practices/setup", json={**SANDBOX, "subdomain": "elsewhere"}).json()
        assert _steps(other)["verified"] is False
        fake.requests.clear()
        _worker(c)
        assert not fake.requests  # disabled practices are never synced
        bad = c.post("/api/practices/setup", json={**SANDBOX, "subdomain": "Bad Name!"})
        assert bad.status_code == 422


def test_without_a_webhook_url_the_step_says_so(tmp_path):
    c, _ = _app(tmp_path, webhook_public_url="")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        view = c.post("/api/practices/setup", json=SANDBOX).json()
        assert _steps(view)["webhooks"] is False
        assert "WEBHOOK_PUBLIC_URL" in view["onboarding"]["steps"][2]["detail"]
        assert _items(view)["webhooks"] is False


# --- keys per env -------------------------------------------------------------------------------


def _conn(env: str, subdomain: str = "demo") -> PayerConnection:
    return PayerConnection(
        workspace_id="w",
        name="p",
        kind="nexhealth",
        base_url="https://nexhealth.test",
        auth_ref={"env": env, "subdomain": subdomain, "location_id": "100"},
    )


def test_each_env_reads_only_its_own_key(tmp_path):
    both = make_settings(tmp_path, nexhealth_api_key="sand", nexhealth_prod_api_key="prod")
    assert nexhealth_client(_conn("sandbox"), both, None)._key == "sand"
    assert nexhealth_client(_conn("production"), both, None)._key == "prod"
    assert nexhealth_client(_conn("anything else"), both, None)._key == "sand"
    only_prod = make_settings(tmp_path, nexhealth_prod_api_key="prod")
    with pytest.raises(NexHealthError):
        nexhealth_client(_conn("sandbox"), only_prod, None)  # never falls back to production
    only_sandbox = make_settings(tmp_path, nexhealth_api_key="sand")
    with pytest.raises(NexHealthError):
        nexhealth_client(_conn("production"), only_sandbox, None)
    assert only_sandbox.nexhealth_envs == ["sandbox"]


@pytest.mark.asyncio
async def test_clinics_on_one_key_share_its_token_and_throttle(tmp_path):
    settings = make_settings(tmp_path, nexhealth_api_key="nh-test", nexhealth_prod_api_key="nh-p")
    fake = MedicalNexHealth("modmed")
    transport = httpx.MockTransport(fake)
    a = nexhealth_client(_conn("sandbox"), settings, transport)
    b = nexhealth_client(_conn("sandbox", "other"), settings, transport)
    p = nexhealth_client(_conn("production"), settings, transport)
    assert a.throttle is b.throttle and a.throttle is not p.throttle
    await a.get("/providers", {"location_id": 100})
    with pytest.raises(NexHealthError):
        await b.get("/providers", {"location_id": 100})  # 403: subdomain not authorised
    assert fake.auths == 1  # one token for the key, whichever clinic asks
    for client in (a, b, p):
        await client.aclose()


def test_a_workspace_switches_its_clearinghouse_to_production(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, stedi_prod_api_key="st-prod")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        r = c.put("/api/practices/automation", json={"clearinghouse_env": "production"})
        # A sandbox deployment (dev, labs, staging) never runs production mode, key or not.
        assert r.status_code == 409 and r.json()["error"]["code"] == "production_not_allowed"
    monkeypatch.setattr(Settings, "production_allowed", property(lambda self: True))
    (tmp_path / "nokey").mkdir()
    c, _ = _app(tmp_path / "nokey")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        r = c.put("/api/practices/automation", json={"clearinghouse_env": "production"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "production_unavailable"
    (tmp_path / "prod").mkdir()
    c, _ = _app(tmp_path / "prod", stedi_prod_api_key="st-prod")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        assert c.get("/api/practices").json()["clearinghouse"]["env"] == "test"
        r = c.put("/api/practices/automation", json={"clearinghouse_env": "production"})
        assert r.status_code == 200 and r.json()["effective"]["clearinghouse_env"] == "production"
        [ch] = [x for x in _db_rows(c, PayerConnection) if x.kind == "stedi"]
        assert ch.auth_ref["env"] == "production" and not ch.synthetic
        client = stedi_client(ch, c.app.state.settings, None)
        assert (
            client._http.headers["authorization"] == "Key st-prod" and client.mode == "production"
        )
        # Back to the env default (test mode, the test key).
        c.put("/api/practices/automation", json={"clearinghouse_env": None}).raise_for_status()
        [ch] = [x for x in _db_rows(c, PayerConnection) if x.kind == "stedi"]
        assert ch.auth_ref["env"] == "test"


# --- go-live -----------------------------------------------------------------------------------


def _production_practice(c) -> str:
    view = c.post("/api/practices/setup", json=PRODUCTION)
    assert view.status_code == 200, view.text
    _worker(c)
    return view.json()["connection"]["connection_id"]


def test_a_real_clinic_runs_no_automation_until_go_live_passes(tmp_path, production_deployment):
    c, fake = _app(tmp_path, nexhealth_api_key="", nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()  # CarePlix staff
        conn_id = _production_practice(c)
        view = c.get(f"/api/practices/setup/{conn_id}").json()
        assert view["golive"]["required"] and not view["golive"]["automations_allowed"]
        items = _items(view)
        assert items["insert_appt_client"] and items["read_sync"] and items["write_sync"]
        assert items["webhooks"] and items["first_sync"] and items["test_writes"] is False

        # Blocked: the receptionist offers no times, autopilots stay off.
        call = _call(c)
        call.ok("find_patient", **JANE)
        found = call.ok("find_slots")
        assert found["slots"] == [] and "go-live" in found["message"]
        out = call.tool("cancel_appointment", appointment_id="501", reason="no_longer_needed")
        assert out["result"]["error"] == "practice_not_live"
        overview = c.get("/api/practices").json()
        assert overview["eligibility"]["autopilot"] is False
        assert c.get("/api/practices/backfill").json()["settings"]["enabled"] is False

        # The test must use the practice's test patient.
        test_url = f"/api/practices/setup/{conn_id}/golive-test"
        assert c.post(test_url, json={"patient_id": "1"}).status_code == 422
        view = c.post(test_url, json={"patient_id": "9"}).json()
        assert view["golive"]["test"]["step"] == "book"
        [booked] = fake.booked
        assert booked["patient_id"] == 9 and "go-live" in booked["note"]
        assert c.post(test_url, json={"patient_id": "9"}).status_code == 409  # already running

        token = _token(fake)
        _deliver(c, token, _inserted(booked["id"])).raise_for_status()
        _worker(c)  # book confirmed → confirm sent
        assert ("PATCH", f"/appointments/{booked['id']}") in [b[:2] for b in fake.bodies]
        _deliver(c, token, _updated(booked["id"], {"confirmed": False})).raise_for_status()
        _worker(c)  # confirm confirmed → cancel sent
        _deliver(c, token, _updated(booked["id"], {"cancelled": False})).raise_for_status()
        _worker(c)  # cancel confirmed → passed
        view = c.get(f"/api/practices/setup/{conn_id}").json()
        assert view["golive"]["test"]["step"] == "done", view["golive"]["test"]
        assert view["golive"]["passed"] and view["golive"]["automations_allowed"], view["golive"]
        kinds = {(i.kind, i.state) for i in _db_rows(c, WriteIntent) if i.source == "golive"}
        assert kinds == {
            ("appointment_book", "written"),
            ("appointment_confirm", "written"),
            ("appointment_cancel", "written"),
        }
        assert call.ok("find_slots")["slots"]  # live now (same call: one call at a time)


def test_go_live_lists_what_blocks_it(tmp_path, production_deployment):
    fake = MedicalNexHealth("eclinicalworks")
    fake.location["insert_appt_client"] = False
    fake.sync_status[0]["write_status"] = "red"
    c, _ = _app(tmp_path, fake, nexhealth_api_key="", nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        conn_id = _production_practice(c)
        items = _items(c.get(f"/api/practices/setup/{conn_id}").json())
        assert items["insert_appt_client"] is False and items["write_sync"] is False
        r = c.post(f"/api/practices/setup/{conn_id}/golive-test", json={"patient_id": "9"})
        assert r.status_code == 409 and "Appointment insertion" in r.json()["error"]["message"]
        assert fake.booked == []


def test_a_mismatched_ehr_is_flagged(tmp_path):
    c, _ = _app(tmp_path, MedicalNexHealth("modmed"))
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        view = c.post("/api/practices/setup", json=SANDBOX).json()  # chose athenahealth
        assert _items(view)["ehr_match"] is False


def test_only_carePlix_staff_add_production_practices(tmp_path, production_deployment):
    c, _ = _app(tmp_path, nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _new_customer(c, "practice")
        c.cookies.clear()
        _email_sign_in(c, CLINIC_ADMIN)
        assert c.post("/api/practices/setup", json=PRODUCTION).status_code == 403
        envs = {e["env"]: e for e in c.get("/api/practices/setup/options").json()["envs"]}
        assert envs["production"]["available"] is False
        assert "staff" in envs["production"]["reason"]


# --- workspace automation settings ---------------------------------------------------------------


def test_workspace_settings_override_the_env_defaults(tmp_path):
    c, fake = _app(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        conn_id = c.post("/api/practices/setup", json=SANDBOX).json()["connection"]["connection_id"]
        _worker(c)
        got = c.get("/api/practices/automation").json()
        assert got["effective"] == got["defaults"] and got["chosen"] == {}
        body = {"backfill_enabled": False, "eligibility_autopilot": False, "sync_interval_s": 300}
        assert c.put("/api/practices/automation", json=body).status_code == 200
        assert c.get("/api/practices/backfill").json()["settings"]["enabled"] is False
        assert c.get("/api/practices").json()["eligibility"]["autopilot"] is False
        view = c.get(f"/api/practices/setup/{conn_id}").json()
        assert view["sync_interval_s"]["effective"] == 300
        own = c.put(f"/api/practices/setup/{conn_id}/sync-interval", json={"sync_interval_s": 120})
        assert own.json()["sync_interval_s"] == {"own": 120, "effective": 120}
        too_fast = {"sync_interval_s": 5}
        assert (
            c.put(f"/api/practices/setup/{conn_id}/sync-interval", json=too_fast).status_code == 422
        )
        assert (
            c.put("/api/practices/automation", json={"receptionist_cancel_mode": "x"}).status_code
            == 422
        )
        # Cancel mode is the workspace's: auto cancels on the call.
        c.put("/api/practices/automation", json={"receptionist_cancel_mode": "auto"})
        call = _call(c)
        call.ok("find_patient", **JANE)
        assert call.ok("cancel_appointment", appointment_id="501", reason="no_longer_needed")[
            "state"
        ] == ("cancelled")


def test_one_broken_clinic_never_stops_the_others(tmp_path, monkeypatch):
    c, fake = _app(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        c.post("/api/practices/setup", json=SANDBOX).raise_for_status()
        from careplix_api.practice import sync

        real = sync.sync_practice
        calls = []

        async def flaky(session, conn, *args, **kwargs):
            calls.append(conn.id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return await real(session, conn, *args, **kwargs)

        monkeypatch.setattr(sync, "sync_practice", flaky)
        report = _worker(c)
        assert report["errors"] == 1
        [conn] = [x for x in _db_rows(c, PayerConnection) if x.kind == "nexhealth"]
        assert conn.last_error == "boom"
        assert _worker(c)["errors"] == 0  # retried next pass


# --- office-hours confirmation for cloud EHRs --------------------------------------------------

NY = ZoneInfo("America/New_York")


def test_office_elapsed_counts_weekday_office_hours_only():
    friday = datetime(2026, 11, 13, 17, 30, tzinfo=NY)  # a Friday
    monday = datetime(2026, 11, 16, 9, 0, tzinfo=NY)
    assert office_elapsed("America/New_York", friday, monday) == timedelta(minutes=90)
    assert office_elapsed("Not/AZone", friday, monday) == timedelta(minutes=90)


@pytest.mark.parametrize(
    "emr,now,overdue",
    [
        ("athenahealth", datetime(2026, 11, 16, 9, 0, tzinfo=NY), False),  # 1.5 office hours
        ("athenahealth", datetime(2026, 11, 16, 10, 31, tzinfo=NY), True),
        ("NextGen Office", datetime(2026, 11, 16, 9, 0, tzinfo=NY), False),
        ("Dentrix", datetime(2026, 11, 13, 19, 31, tzinfo=NY), True),  # server EHR: wall clock
    ],
)
def test_cloud_writes_wait_through_the_weekend(emr, now, overdue):
    conn = _conn("production")
    conn.capability = {"sync": [{"emr": emr}], "location_tz": "America/New_York"}
    intent = SimpleNamespace(sent_at=datetime(2026, 11, 13, 17, 30, tzinfo=NY).astimezone(UTC))
    connector = connector_for(conn, Settings(_env_file=None))
    assert connector.confirm_overdue(intent, now.astimezone(UTC)) is overdue
    assert connector.confirm_due(intent, now.astimezone(UTC)) is overdue


def test_a_sandbox_practice_runs_the_whole_go_live_flow_for_demos(tmp_path):
    fake = MedicalNexHealth("athenahealth")
    fake.sync_status = []  # NexHealth's sandbox: no EHR behind the location
    c, _ = _app(tmp_path, fake)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        conn_id = c.post("/api/practices/setup", json=SANDBOX).json()["connection"]["connection_id"]
        _worker(c)
        view = c.get(f"/api/practices/setup/{conn_id}").json()
        items = _items(view)
        assert items["read_sync"] is True and items["write_sync"] is True  # not applicable
        assert (
            "Not applicable"
            in next(i for i in view["golive"]["items"] if i["key"] == "read_sync")["detail"]
        )
        c.post(
            f"/api/practices/setup/{conn_id}/golive-test", json={"patient_id": "9"}
        ).raise_for_status()
        for _ in range(3):  # each step is accepted with no EHR to confirm it: one per pass
            _worker(c)
        golive = c.get(f"/api/practices/setup/{conn_id}").json()["golive"]
        assert golive["test"]["step"] == "done" and golive["passed"] and golive["passed_at"]


def test_each_deployment_adds_only_its_own_kind_of_practice(tmp_path, monkeypatch):
    # A sandbox deployment holding a production key still refuses production practices.
    c, _ = _app(tmp_path, nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        envs = {e["env"]: e for e in c.get("/api/practices/setup/options").json()["envs"]}
        assert envs["sandbox"]["available"] and not envs["production"]["available"]
        assert "sandbox deployment" in envs["production"]["reason"]
        r = c.post("/api/practices/setup", json=PRODUCTION)
        assert r.status_code == 409 and r.json()["error"]["code"] == "production_not_allowed"
    assert make_settings(tmp_path, nexhealth_prod_api_key="p").nexhealth_envs == []
    # A production deployment refuses sandbox practices, even with a sandbox key present.
    monkeypatch.setattr(Settings, "production_allowed", property(lambda self: True))
    monkeypatch.setattr("careplix_api.environment.is_production", lambda settings=None: True)
    (tmp_path / "prod").mkdir()
    c, _ = _app(tmp_path / "prod", nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        envs = {e["env"]: e for e in c.get("/api/practices/setup/options").json()["envs"]}
        assert envs["production"]["available"] and not envs["sandbox"]["available"]
        r = c.post("/api/practices/setup", json=SANDBOX)
        assert r.status_code == 409 and r.json()["error"]["code"] == "not_in_production"


def test_labs_refuses_a_production_nexhealth_key(tmp_path):
    from careplix_api import environment

    labs = make_settings(tmp_path, careplix_env="labs", nexhealth_prod_api_key="p")
    assert any("NEXHEALTH_PROD_API_KEY" in p for p in environment.problems(labs))
    assert not labs.production_allowed


def test_demo_workspaces_share_the_sandbox_location_but_a_clinic_is_one_workspaces(
    tmp_path, monkeypatch
):
    c, _ = _app(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        assert c.post("/api/practices/setup", json=SANDBOX).status_code == 200
        _new_customer(c, "practice")
        c.cookies.clear()
        _email_sign_in(c, CLINIC_ADMIN)
        # A trial workspace adds NexHealth's sandbox practice too (synthetic patients).
        r = c.post("/api/practices/setup", json=SANDBOX)
        assert r.status_code == 200 and _steps(r.json())["verified"] is True
        again = c.post("/api/practices/setup", json=SANDBOX)  # but not twice in one workspace
        assert again.status_code == 409 and "here" in again.json()["error"]["message"]
    monkeypatch.setattr(Settings, "production_allowed", property(lambda self: True))
    (tmp_path / "prod").mkdir()
    c, _ = _app(tmp_path / "prod", nexhealth_api_key="", nexhealth_prod_api_key="nh-test")
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        assert c.post("/api/practices/setup", json=PRODUCTION).status_code == 200
        ws_b = _new_customer(c, "practice")
        assert c.post(f"/api/platform/workspaces/{ws_b}/join").status_code == 200
        c.post("/api/auth/workspace", json={"workspace_id": ws_b}).raise_for_status()
        r = c.post("/api/practices/setup", json=PRODUCTION)  # a real clinic: one workspace only
        assert r.status_code == 409 and "another workspace" in r.json()["error"]["message"]
