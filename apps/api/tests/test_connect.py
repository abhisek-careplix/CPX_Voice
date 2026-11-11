"""CarePlix Connect: the EHR catalog, connector routing and OpenEMR emulating other EHRs."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from careplix_api.connect import connectors
from careplix_api.connect.catalog import PROFILES, nexhealth_profile_for
from careplix_api.models import WriteIntent

from .test_openemr import _client, _worker


def _age_writes(c, seconds: int) -> None:
    """Pretend the EHR has had ``seconds`` to process every sent write."""

    async def run():
        async with c.app.state.db.sessions() as session:
            for intent in await session.scalars(select(WriteIntent)):
                if intent.sent_at:
                    intent.sent_at -= timedelta(seconds=seconds)
            await session.commit()

    c.portal.call(run)


def _accept_top_offer(c) -> dict:
    _worker(c)
    _worker(c)
    [opening] = c.get("/api/practices/backfill").json()["active"]
    top = opening["offers"][0]
    assert top["patient_id"] == "1" and top["current_start"]  # an ASAP patient with a later visit
    return c.post(
        f"/api/practices/backfill/offers/{top['id']}", json={"outcome": "accepted"}
    ).json()


def _writes(c) -> dict[str, tuple[str, str | None]]:
    return {
        i["kind"]: (i["state"], i["confirmation"]) for i in c.get("/api/practices/intents").json()
    }


def test_catalog_covers_openemr_and_every_nexhealth_ehr():
    nexhealth = [p for p in PROFILES.values() if p.path == "nexhealth"]
    assert len(nexhealth) == 15 and "openemr" in PROFILES
    assert {p.category for p in nexhealth} == {"medical", "dental", "ortho"}
    # NexHealth reports EHRs by display name; the most specific name wins.
    assert nexhealth_profile_for("Dentrix Enterprise").id == "nexhealth:dentrix_enterprise"
    assert nexhealth_profile_for("Dentrix").id == "nexhealth:dentrix"
    assert nexhealth_profile_for("Modernizing Medicine").id == "nexhealth:modmed"
    assert nexhealth_profile_for("eClinicalWorks v12").id == "nexhealth:eclinicalworks"
    assert nexhealth_profile_for("Some new EHR") is None
    # Published per-system limits.
    assert PROFILES["nexhealth:orthotrac"].support("appointment.cancel") == "no"
    assert PROFILES["nexhealth:practiceworks"].support("appointment.cancel") == "no"
    assert PROFILES["nexhealth:athenahealth"].support("appointment.reschedule") == "no"
    assert PROFILES["nexhealth:open_dental"].support("payment.post") == "yes"
    assert PROFILES["nexhealth:curve_hero"].support("appointment.book") == "existing_patients"


def test_catalog_api_shows_profiles_and_the_connected_practice(tmp_path):
    c, _ = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        body = c.get("/api/practices/catalog").json()
        assert len(body["profiles"]) == 16 and len(body["operations"]) == 11
        [conn] = body["connections"]
        assert conn["kind"] == "openemr" and conn["profile"] == "openemr"
        assert conn["emulating"] is None and conn["support"]["appointment.cancel"] == "yes"
        overview = c.get("/api/practices").json()
        assert overview["hub"]["profile"] == "openemr" and "{pid}" in overview["links"]["patient"]


def test_emulation_only_accepts_nexhealth_profiles(tmp_path):
    c, _ = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        assert c.put("/api/practices/emulation", json={"profile": "epic"}).status_code == 422
        assert c.put("/api/practices/emulation", json={"profile": "openemr"}).status_code == 422
        on = c.put("/api/practices/emulation", json={"profile": "nexhealth:orthotrac"}).json()
        assert on["emulating"] == "nexhealth:orthotrac" and on["profile_name"] == "OrthoTrac"
        assert on["support"]["appointment.cancel"] == "no"
        off = c.put("/api/practices/emulation", json={"profile": None}).json()
        assert off["emulating"] is None and off["profile"] == "openemr"


def test_emulated_orthotrac_keeps_a_cancel_it_cannot_take_inside_the_hub(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        c.put("/api/practices/emulation", json={"profile": "nexhealth:orthotrac"})
        result = _accept_top_offer(c)
        assert result["state"] == "filled"  # booking is supported everywhere
        # OrthoTrac can't take cancels: the release becomes a staff task, the EHR is untouched.
        assert router.openemr.appts["102"]["pc_apptstatus"] == "-"
        assert ("PUT", "/apis/default/api/patient/1/appointment/102") not in router.openemr.calls
        writes = _writes(c)
        assert writes["appointment_cancel"] == ("manual", None)
        [release] = [
            i for i in c.get("/api/practices/intents").json() if i["kind"] == "appointment_cancel"
        ]
        assert "OrthoTrac" in release["last_error"]
        # OrthoTrac is a server EHR: the booking confirms later, not on the call.
        assert writes["appointment_book"] == ("accepted", "pending")
        _age_writes(c, 90)
        _worker(c)
        assert _writes(c)["appointment_book"] == ("written", "ehr_written")


@pytest.mark.parametrize("office_open", [True, False])
def test_emulated_cloud_ehr_confirms_only_in_office_hours(tmp_path, monkeypatch, office_open):
    monkeypatch.setattr(connectors, "office_open", lambda tz, now: office_open)
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        c.put("/api/practices/emulation", json={"profile": "nexhealth:athenahealth"})
        assert _accept_top_offer(c)["state"] == "filled"
        assert router.openemr.appts["102"]["pc_apptstatus"] == "x"  # athena takes cancels
        _worker(c)
        assert _writes(c)["appointment_book"] == ("accepted", "pending")  # not yet: just sent
        _age_writes(c, 90)
        _worker(c)
        expected = ("written", "ehr_written") if office_open else ("accepted", "pending")
        assert _writes(c)["appointment_book"] == expected
        assert _writes(c)["appointment_cancel"] == expected
