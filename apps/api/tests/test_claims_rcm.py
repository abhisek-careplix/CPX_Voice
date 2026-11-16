"""Charge to cash on the clearinghouse (RC3–RC8), in test mode with synthetic data only.

Request and response bodies follow the clearinghouse's documented claim JSON, 277CA and 835 report
shapes. HTTP goes through ``httpx.MockTransport``; nothing leaves the test.
"""

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api.claims import x837
from careplix_api.claims.models import Claim, ClaimCharge
from careplix_api.main import create_app

from .conftest import FakeGateway, make_settings

PROFILE = {
    "organization": "Riverside Family Medicine (synthetic)",
    "npi": "1999999984",
    "tax_id": "912345678",
    "taxonomy": "207Q00000X",
    "address1": "100 Synthetic Way",
    "city": "Chicago",
    "state": "IL",
    "postal_code": "606010001",
    "submitter_name": "CarePlix Billing",
    "contact_phone": "2025550143",
}
PATIENT = {
    "first_name": "Briar",
    "last_name": "Synthetic",
    "date_of_birth": "1961-04-12",
    "member_id": "SYN12345678",
    "gender": "F",
}


def _draft(**kw) -> Claim:
    values = {
        "workspace_id": "ws",
        "source": "charges",
        "source_ref": "CPX0000000001",
        "claim_number": "CPX0000000001",
        "payer_id": "87726",
        "payer_name": "UnitedHealthcare",
        "plan_kind": "commercial",
        "provider_name": "Riverside",
        "provider_npi": "1999999984",
        "date_of_service": "2026-11-07",
        "billed_amount": "206.25",
        "submitted_on": "",
        "stage": "draft",
        "place_of_service": "11",
        "diagnoses": ["R0789", "I10"],
        "lines": [
            {
                "line_id": "L1",
                "cpt": "99213",
                "modifiers": ["25"],
                "units": "1",
                "charge": "142.00",
                "dx": [1, 2],
                "date_of_service": "2026-11-07",
            },
            {
                "line_id": "L2",
                "cpt": "93000",
                "modifiers": [],
                "units": "1",
                "charge": "64.25",
                "dx": [1],
                "date_of_service": "2026-11-07",
            },
        ],
        "frequency": "original",
        "test_outcome": None,
    }
    values.update(kw)
    return Claim(**values)


# --- 837P build ----------------------------------------------------------------------------------


def test_837p_body_has_every_required_part():
    body = x837.build(_draft(), PATIENT, PROFILE, mode="production")
    assert set(body) >= {
        "authorization",
        "billing",
        "encounter",
        "insured",
        "payer",
        "purpose",
        "serviceLines",
        "submitter",
    }
    billing = body["billing"]
    assert billing["patientControlNumber"] == "CPX0000000001"
    assert billing["totalCharge"] == "206.25"
    assert billing["taxId"] == {"ein": "912345678"}
    assert billing["billingProvider"]["identifiers"] == {
        "npi": "1999999984",
        "taxonomyCode": "207Q00000X",
    }
    assert body["encounter"] == {
        "primaryDiagnosisCode": "R0789",
        "primaryPlaceOfService": "11",
        "additionalDiagnosisCodes": ["I10"],
    }
    assert body["payer"] == {"id": "87726", "name": {"organization": "UnitedHealthcare"}}
    assert body["insured"]["memberId"] == "SYN12345678"
    assert body["insured"]["gender"] == "FEMALE"
    assert body["insured"]["dateOfBirth"] == "1961-04-12"
    first, second = body["serviceLines"]
    assert first["procedureCode"] == {"code": "99213", "modifiers": ["25"]}
    assert first["diagnosisCodes"] == ["R0789", "I10"]  # codes, not pointers, in this API
    assert first["datesOfService"] == {"start": "2026-11-07", "end": "2026-11-07"}
    assert first["lineItemChargeAmount"] == "142.00" and first["units"] == "1"
    assert first["lineItemControlNumber"] == "L1"
    assert second["procedureCode"] == {"code": "93000"}  # no empty modifier list
    assert second["diagnosisCodes"] == ["R0789"]


def test_test_outcome_routes_to_the_test_payer_with_its_prefix_in_test_mode_only():
    claim = _draft(test_outcome="denied")
    test_body = x837.build(claim, PATIENT, PROFILE, mode="test")
    assert test_body["payer"]["id"] == "STEDI"
    assert test_body["insured"]["memberId"] == "STEDI_DENIED_SYN12345678"
    assert x837.is_test_claim(test_body)
    prod_body = x837.build(claim, PATIENT, PROFILE, mode="production")
    assert prod_body["payer"]["id"] == "87726" and not x837.is_test_claim(prod_body)
    partial = x837.build(_draft(test_outcome="partial"), PATIENT, PROFILE, mode="test")
    assert partial["insured"]["memberId"].startswith("STEDI_PARTIALLY_PAID_")


def test_replacement_claim_names_the_payers_original_claim():
    body = x837.build(
        _draft(frequency="replacement"),
        PATIENT,
        PROFILE,
        mode="production",
        original_payer_claim_number="PCN-ORIG-1",
    )
    assert body["encounter"]["resubmission"] == {
        "code": "REPLACEMENT_OF_PRIOR_CLAIM",
        "originalReferenceNumber": "PCN-ORIG-1",
    }


def test_control_numbers_are_unique_and_fit_clm01():
    numbers = {x837.new_control_number() for _ in range(500)}
    assert len(numbers) == 500
    assert all(len(n) <= x837.PCN_MAX and n.startswith("CPX") for n in numbers)
    assert x837.icd10(" e11.9 ") == "E119"


# --- the Studio flow -----------------------------------------------------------------------------


def _app(tmp_path, transport=None, **overrides):
    settings = make_settings(tmp_path, run_background=False, payer_sandbox_url="", **overrides)
    return TestClient(create_app(settings, FakeGateway(settings), integration_transport=transport))


@pytest.fixture
def studio(tmp_path):
    with _app(tmp_path) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _rows(c, model, *where):
    async def load():
        async with c.app.state.db.sessions() as session:
            return list(await session.scalars(select(model).where(*where)))

    return c.portal.call(load)


def _drafts(c, *visits):
    r = c.post("/api/claims/charges/samples", json={"visits": list(visits) or None})
    assert r.status_code == 200, r.text
    r = c.post("/api/claims/build", json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_charges_become_one_draft_claim_per_visit(studio):
    claims = _drafts(studio, "office_partial", "office_denied")
    assert len(claims) == 2
    by_outcome = {c["test_outcome"]: c for c in claims}
    partial = by_outcome["partial"]
    assert partial["stage"] == "draft" and partial["submitted_on"] == ""
    assert [line["cpt"] for line in partial["lines"]] == ["99213", "93000"]
    assert partial["billed_amount"] == "206.25"
    assert partial["diagnoses"] == ["R0789", "I10"]
    assert partial["lines"][1]["dx"] == [1]
    charges = _rows(studio, ClaimCharge)
    assert len(charges) == 3 and all(c.claim_id for c in charges)
    # Building again finds nothing new; drafts are never followed up with the payer.
    assert studio.post("/api/claims/build", json={}).json() == []
    assert studio.post("/api/claims/follow-up").json() == {"checked": 0, "cases": 0, "errors": 0}


def test_a_draft_can_be_fixed_before_it_goes_out(studio):
    [claim] = _drafts(studio, "missing_diagnosis")
    assert claim["diagnoses"] == []
    r = studio.patch(
        f"/api/claims/{claim['id']}",
        json={"diagnoses": ["j06.9"], "lines": [{"line_id": "L1", "dx": [1], "units": "2"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["diagnoses"] == ["J069"] and r.json()["lines"][0]["units"] == "2"


def test_billing_profile_defaults_to_synthetic_and_validates(studio):
    profile = studio.get("/api/clearinghouse/billing-profile").json()
    assert profile["npi"] == "1999999984" and len(profile["tax_id"]) == 9
    bad = studio.put("/api/clearinghouse/billing-profile", json={"npi": "1234567890"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_npi"
    r = studio.put("/api/clearinghouse/billing-profile", json={"tax_id": "98-7654321"})
    assert r.json()["tax_id"] == "987654321"


def test_nexhealth_charges_are_read_with_patient_and_payer_from_the_mirror(tmp_path):
    from careplix_api.models import EhrRecord, PayerConnection
    from careplix_api.phi import PhiBox

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticates"):
            return httpx.Response(200, json={"data": {"token": "t"}})
        assert request.url.path.endswith("/charges")
        return httpx.Response(
            200,
            json={
                "code": True,
                "data": [
                    {
                        "id": 113,
                        "patient_id": 3,
                        "provider_id": 15,
                        "procedure_code": "99213",
                        "fee": {"amount": "142.00", "currency": "USD"},
                        "charged_at": "2026-11-02",
                        "deleted_at": None,
                    },
                    {"id": 114, "patient_id": 3, "procedure_code": "99213", "deleted_at": "x"},
                ],
                "page_info": {"has_next_page": False},
            },
        )

    with _app(tmp_path, httpx.MockTransport(handler)) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        settings = c.app.state.settings
        ws = c.get("/api/me").json()["workspace_id"]

        async def seed():
            box = PhiBox(settings)
            async with c.app.state.db.sessions() as session:
                conn = PayerConnection(
                    workspace_id=ws,
                    kind="nexhealth",
                    name="Sandbox practice",
                    base_url="https://nex.test",
                    auth_ref={"subdomain": "s", "location_id": "1", "env": "sandbox"},
                    enabled=True,
                )
                session.add(conn)
                await session.flush()
                rows = [
                    ("patient", "3", {}, {"first_name": "Cedar", "last_name": "Synthetic"}, "3"),
                    ("provider", "15", {"name": "Dr Synthetic", "npi": "1999999984"}, None, None),
                    ("coverage", "77", {"plan_id": "9", "priority": 1}, {"member_id": "M1"}, "3"),
                    ("insurance_plan", "9", {"name": "Aetna", "payer_id": "60054"}, None, None),
                ]
                for resource, sid, attrs, pii, pref in rows:
                    session.add(
                        EhrRecord(
                            workspace_id=ws,
                            connection_id=conn.id,
                            resource=resource,
                            source_id=sid,
                            attrs=attrs,
                            pii=box.seal(pii) if pii else None,
                            patient_ref=pref,
                            status="active",
                        )
                    )
                await session.commit()

        c.portal.call(seed)
        out = c.post("/api/claims/charges/pull").json()
        assert out[0]["ok"] is True, out
        assert out[0]["added"] == 1 and out[0]["read"] == 1
        [charge] = _rows(c, ClaimCharge)
        assert (charge.payer_id, charge.provider_npi, charge.amount) == (
            "60054",
            "1999999984",
            "142.00",
        )
        assert charge.diagnoses == [] and charge.synthetic is True
        assert PhiBox(settings).open(charge.patient)["member_id"] == "M1"


def test_timely_filing_dates_are_relative_to_today():
    # Sanity for the sample visit used by the scrub tests.
    from careplix_api.claims.charges import VISITS_BY_KEY

    late = VISITS_BY_KEY["late"]
    assert date.today() - timedelta(days=late.days_ago) < date.today() - timedelta(days=90)
    assert json.dumps(x837.TEST_PREFIX)  # serialisable constants
