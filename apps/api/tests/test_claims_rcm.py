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


# --- scrub (RC5) ---------------------------------------------------------------------------------

from careplix_api.claims import scrub  # noqa: E402

TODAY = date(2026, 11, 16)


def _codes(claim, patient=PATIENT, profile=PROFILE, today=TODAY):
    return {f["code"] for f in scrub.check(claim, patient, profile, today)}


def test_a_clean_claim_has_no_errors():
    findings = scrub.check(_draft(), PATIENT, PROFILE, TODAY)
    assert not scrub.blocking(findings), findings


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"diagnoses": []}, "dx_missing"),
        ({"diagnoses": ["R07.89X", "I10"]}, None),  # dots are dropped; still valid
        ({"diagnoses": ["7890", "I10"]}, "dx_format"),
        ({"payer_id": None}, "payer_missing"),
        ({"place_of_service": "1"}, "place_of_service"),
        ({"date_of_service": "2025-06-01"}, "timely_filing"),
        ({"date_of_service": "2026-08-25"}, "timely_filing_soon"),
    ],
)
def test_claim_level_checks(change, code):
    found = _codes(_draft(**change))
    assert (code in found) if code else not (found & {"dx_format", "dx_missing"})


def _line(**kw):
    base = {
        "line_id": "L1",
        "cpt": "99213",
        "modifiers": [],
        "units": "1",
        "charge": "142.00",
        "dx": [1],
        "date_of_service": "2026-11-07",
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("line", "code", "severity"),
    [
        (_line(cpt="93000", modifiers=["25"]), "mod_25_not_em", "error"),
        (_line(modifiers=["26", "TC"]), "mod_26_tc", "error"),
        (_line(modifiers=["59"]), "mod_distinct_em", "warning"),
        (_line(cpt="27447", modifiers=["50"], units="2"), "mod_50_units", "warning"),
        (_line(modifiers=["A", "25"]), "mod_format", "error"),
        (_line(modifiers=["RT", "RT"]), "mod_duplicate", "error"),
        (_line(cpt="9921"), "cpt_format", "error"),
        (_line(charge="0"), "charge_zero", "error"),
        (_line(units="0"), "units", "error"),
        (_line(dx=[]), "dx_pointer_missing", "error"),
        (_line(dx=[3]), "dx_pointer_range", "error"),
        (_line(date_of_service="2026-12-01"), "dos_future", "error"),
        (_line(modifiers=["25"]), "mod_25_alone", "warning"),
    ],
)
def test_line_checks(line, code, severity):
    findings = scrub.check(_draft(lines=[line]), PATIENT, PROFILE, TODAY)
    match = [f for f in findings if f["code"] == code]
    assert match and match[0]["severity"] == severity and match[0]["line_id"] == "L1"


def test_hcpcs_and_category_codes_are_accepted():
    lines = [_line(cpt=c, line_id=f"L{i}") for i, c in enumerate(["G0438", "3074F", "0042T"])]
    assert not _codes(_draft(lines=lines)) & {"cpt_format"}


def test_patient_and_billing_requirements():
    no_member = {**PATIENT, "member_id": ""}
    assert "member_id_missing" in _codes(_draft(), patient=no_member)
    assert "member_id_mbi" in _codes(_draft(plan_kind="medicare"))  # SYN… isn't an MBI
    assert "member_id_mbi" not in _codes(
        _draft(plan_kind="medicare"), patient={**PATIENT, "member_id": "1EG4-TE5-MK73"}
    )
    assert "patient_dob" in _codes(_draft(), patient={**PATIENT, "date_of_birth": None})
    bad = {**PROFILE, "npi": "1234567890", "tax_id": "12", "postal_code": "6060"}
    assert {"billing_npi", "billing_tax_id", "billing_zip"} <= _codes(_draft(), profile=bad)


# --- submit and acknowledge (RC5) --------------------------------------------------------------

from careplix_api.claims.models import ClaimTransaction, ClaimWork  # noqa: E402


def ack_277(pcn, category="A1", code="20", *, payer_claim="PAYER-1", line=None, text=None):
    """A 277CA report in the clearinghouse's JSON shape (one claim)."""
    claim = {
        "claimStatus": {
            "referencedTransactionTraceNumber": pcn,
            "patientAccountNumber": pcn,
            "tradingPartnerClaimNumber": payer_claim,
            "statusInformationEffectiveDate": "20261110",
            "totalClaimChargeAmount": "206.25",
            "informationClaimStatuses": [
                {
                    "informationStatuses": [
                        {
                            "healthCareClaimStatusCategoryCode": category,
                            "statusCode": code,
                            "statusCodeValue": text or "Accepted for processing.",
                            "entityIdentifierCode": "IL" if category == "A7" else None,
                        }
                    ],
                    "statusMessage": text,
                }
            ],
        }
    }
    if line:
        claim["serviceLines"] = [
            {
                "lineItemControlNumber": "L2",
                "service": {"procedureCode": "93000", "chargeAmount": "64.25"},
                "serviceClaimStatuses": [
                    {
                        "statusMessage": line,
                        "serviceStatuses": [
                            {
                                "healthCareClaimStatusCategoryCode": "A7",
                                "statusCode": "454",
                                "statusCodeValue": "Procedure code for services rendered.",
                            }
                        ],
                    }
                ],
            }
        ]
    return {
        "meta": {"transactionId": "tx", "applicationMode": "test"},
        "transactions": [
            {
                "payers": [
                    {
                        "organizationName": "Test payer",
                        "claimStatusTransactions": [
                            {
                                "claimTransactionBatchNumber": "corr-1",
                                "providerClaimStatuses": [
                                    {
                                        "providerStatuses": [
                                            {
                                                "healthCareClaimStatusCategoryCode": "A1",
                                                "statusCode": "19",
                                            }
                                        ]
                                    }
                                ],
                                "claimStatusDetails": [
                                    {"patientClaimStatusDetails": [{"claims": [claim]}]}
                                ],
                            }
                        ],
                    }
                ]
            }
        ],
    }


class Clearinghouse:
    """The clearinghouse's claim API: validate, submit (idempotent), reports, polling."""

    def __init__(self):
        self.validate_errors: list[str] = []
        self.submit_errors: list[str] = []
        self.fail_next: int | None = None
        self.submitted: list[tuple[str, dict]] = []
        self.answers: dict[str, dict] = {}
        self.reports: dict[str, dict] = {}
        self.uploads: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "upload.test":
            self.uploads.append(request.content)
            return httpx.Response(200)
        assert request.url.host == "ch.test", request.url
        if path.endswith("/professional-claim-submissions/validate"):
            errors = [{"description": e} for e in self.validate_errors]
            return httpx.Response(200, json={"errors": errors} if errors else {})
        if path.endswith("/professional-claim-submissions"):
            if self.fail_next:
                status, self.fail_next = self.fail_next, None
                return httpx.Response(status, json={"message": "busy"})
            key = request.headers["Idempotency-Key"]
            if key not in self.answers:
                n = len(self.answers) + 1
                body = {"claimId": f"clm_{n:026d}", "submissionId": f"sbm_{n}"}
                if self.submit_errors:
                    body["errors"] = [{"description": e} for e in self.submit_errors]
                self.answers[key] = body
                self.submitted.append((key, json.loads(request.content)))
            return httpx.Response(201, json=self.answers[key])
        if "/reports/v2/" in path:
            tx = path.split("/")[-2]
            return (
                httpx.Response(200, json=self.reports[tx])
                if tx in self.reports
                else (httpx.Response(404, json={"message": "not found"}))
            )
        if path.endswith("/polling/transactions"):
            items = [
                {
                    "transactionId": tx,
                    "direction": "INBOUND",
                    "x12": {"transactionSetIdentifier": k},
                }
                for tx, k in ((tx, "835" if "era" in tx else "277") for tx in self.reports)
            ]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/claim-attachments/file"):
            return httpx.Response(
                201, json={"attachmentId": "att_1", "uploadUrl": "https://upload.test/put"}
            )
        raise AssertionError(f"unexpected {request.method} {path}")


@pytest.fixture
def ch():
    return Clearinghouse()


@pytest.fixture
def billing_studio(tmp_path, ch):
    with _app(
        tmp_path,
        httpx.MockTransport(ch),
        stedi_api_key="st-test",
        stedi_base_url="https://ch.test",
    ) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _submit(c, claim_id):
    r = c.post(f"/api/claims/{claim_id}/submit")
    assert r.status_code == 200, r.text
    return r.json()


def _ingest(c, tx, report, ch, kind="277"):
    ch.reports[tx] = report
    r = c.post("/api/claims/clearinghouse/fetch", json={"transaction_id": tx, "kind": kind})
    assert r.status_code == 200, r.text
    return r.json()


def test_scrub_refuses_the_broken_sample_visits(billing_studio, ch):
    claims = _drafts(billing_studio, "missing_diagnosis", "bad_modifier", "late", "office_paid")
    by_cpt_dx = {}
    for claim in claims:
        out = billing_studio.post(f"/api/claims/{claim['id']}/scrub").json()
        codes = {f["code"] for f in out["scrub"]["findings"] if f["severity"] == "error"}
        by_cpt_dx[claim["test_outcome"], tuple(claim["diagnoses"]), claim["lines"][0]["cpt"]] = (
            out["stage"],
            codes,
        )
    stages = sorted(by_cpt_dx.values(), key=lambda v: sorted(v[1]))
    assert ("ready", set()) in stages
    blocked = [codes for stage, codes in stages if stage == "blocked"]
    assert {"dx_missing", "dx_pointer_missing"} in [
        c & {"dx_missing", "dx_pointer_missing"} for c in blocked
    ]
    assert any("mod_25_not_em" in c for c in blocked)
    assert any("timely_filing" in c for c in blocked)
    # Our errors stop the claim before the clearinghouse's validation is spent on it.
    assert not ch.submitted


def test_clearinghouse_edits_block_the_claim(billing_studio, ch):
    [claim] = _drafts(billing_studio, "office_paid")
    ch.validate_errors = ["Stedi says: Subscriber primary identifier is required"]
    out = billing_studio.post(f"/api/claims/{claim['id']}/submit").json()
    assert out["result"]["state"] == "blocked" and out["stage"] == "blocked"
    [finding] = [f for f in out["scrub"]["findings"] if f["source"] == "clearinghouse"]
    assert finding["message"].startswith("the clearinghouse says")  # white-labelled
    assert not ch.submitted


def test_submit_is_idempotent_on_the_control_number(billing_studio, ch):
    [claim] = _drafts(billing_studio, "office_partial")
    ch.fail_next = 429  # busy: nothing submitted, the claim stays ready
    first = _submit(billing_studio, claim["id"])
    assert first["result"]["state"] == "error" and first["stage"] == "ready"
    out = _submit(billing_studio, claim["id"])
    assert out["stage"] == "submitted" and out["submit_mode"] == "test"
    assert out["submitted_on"] == date.today().isoformat()
    [(key, body)] = ch.submitted
    assert key == f"{claim['claim_number']}-1"
    assert body["payer"]["id"] == "STEDI"
    assert body["insured"]["memberId"].startswith("STEDI_PARTIALLY_PAID_SYN")
    assert body["billing"]["patientControlNumber"] == claim["claim_number"]
    # Submitting again changes nothing and sends nothing.
    assert _submit(billing_studio, claim["id"])["result"]["state"] == "submitted"
    assert len(ch.submitted) == 1
    kinds = [
        t["kind"] for t in billing_studio.get(f"/api/claims/{claim['id']}/transactions").json()
    ]
    assert kinds.count("submit") == 2 and "validate" in kinds


def test_a_rejected_submission_joins_the_queue(billing_studio, ch):
    [claim] = _drafts(billing_studio, "office_paid")
    ch.submit_errors = ["Billing provider tax ID is invalid"]
    out = _submit(billing_studio, claim["id"])
    assert out["result"]["state"] == "rejected" and out["stage"] == "rejected"
    [work] = billing_studio.get("/api/claims/work").json()
    assert work["kind"] == "rejection" and work["category"] == "provider_identity"
    assert work["route"] == "fix_resubmit" and "billing profile" in work["suggestion"]


def test_277ca_acceptance_and_rejection(billing_studio, ch):
    accepted, rejected = _drafts(billing_studio, "office_paid", "office_partial")
    for c in (accepted, rejected):
        _submit(billing_studio, c["id"])
    out = _ingest(billing_studio, "tx_ack_1", ack_277(accepted["claim_number"]), ch)
    assert out["accepted"] == 1
    claim = billing_studio.get(f"/api/claims/{accepted['id']}").json()
    assert claim["stage"] == "accepted" and claim["payer_claim_number"] == "PAYER-1"
    assert claim["status"] == "accepted"

    report = ack_277(
        rejected["claim_number"].lower(),  # payers may change case
        "A7",
        "21",
        text="Missing or invalid information.",
        line="Procedure code is invalid for the date of service",
    )
    assert _ingest(billing_studio, "tx_ack_2", report, ch)["rejected"] == 1
    assert _ingest(billing_studio, "tx_ack_2", report, ch)["claims"] == 0  # seen already
    claim = billing_studio.get(f"/api/claims/{rejected['id']}").json()
    assert claim["stage"] == "rejected"
    [work] = billing_studio.get("/api/claims/work?kind=rejection").json()
    assert work["claim_id"] == rejected["id"]
    assert work["category"] == "patient_identity"  # IL entity + "invalid information"
    assert {r["level"] for r in work["reasons"]} == {"claim", "line L2"}
    assert {r["code"] for r in work["reasons"]} == {"21", "454"}

    # Fix and resubmit: a new idempotency key, and the rejection closes.
    assert (
        billing_studio.patch(
            f"/api/claims/{rejected['id']}", json={"place_of_service": "11"}
        ).status_code
        == 200
    )
    again = _submit(billing_studio, rejected["id"])
    assert again["stage"] == "submitted"
    assert [k for k, _ in ch.submitted][-1] == f"{rejected['claim_number']}-2"
    assert billing_studio.get("/api/claims/work").json() == []
    [done] = billing_studio.get("/api/claims/work?state=done").json()
    assert done["resolution"] == "Corrected and resubmitted."


def test_277_categories():
    from careplix_api.claims import acks

    assert acks.categorize([{"text": "Procedure code invalid"}])[0] == "coding"
    assert acks.categorize([{"text": "Duplicate claim"}])[0] == "duplicate"
    assert acks.categorize([{"entity": "85", "text": "Missing"}])[0] == "provider_identity"
    assert acks.categorize([{"text": "Provider not enrolled with payer"}])[0] == "enrollment"
    assert acks.categorize([{"text": "Something else"}])[0] == "other"


def test_webhook_event_fetches_and_applies_the_277(tmp_path, ch):
    import base64
    import hashlib
    import hmac
    import time

    from careplix_api.claims import inbound
    from careplix_api.models import WebhookEvent

    secret = "whsec_" + base64.b64encode(b"k" * 24).decode()
    with _app(
        tmp_path,
        httpx.MockTransport(ch),
        stedi_api_key="st-test",
        stedi_base_url="https://ch.test",
        stedi_webhook_secret=secret,
    ) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        [claim] = _drafts(c, "office_paid")
        _submit(c, claim["id"])
        ch.reports["tx_hook"] = ack_277(claim["claim_number"])
        event = {
            "id": "evt_1",
            "object": "v1.event",
            "environment": "TEST",
            "resource": {"id": "tx_hook", "type": "transaction"},
            "relatedResources": [
                {"id": "tx_hook", "type": "transaction.x12.277"},
                {"id": "exe_1", "type": "execution"},
            ],
            "type": "transaction.processed",
        }
        body = json.dumps(event).encode()
        stamp = str(int(time.time()))
        sig = base64.b64encode(
            hmac.new(b"k" * 24, f"msg_9.{stamp}.".encode() + body, hashlib.sha256).digest()
        ).decode()
        headers = {
            "webhook-id": "msg_9",
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{sig}",
        }
        assert c.post("/webhooks/stedi", content=body, headers=headers).json()["ok"]
        [row] = _rows(c, WebhookEvent)
        assert (row.resource_type, row.resource_ids, row.detail) == (
            "transaction.x12.277",
            ["tx_hook"],
            {"mode": "test"},
        )
        out = c.portal.call(
            lambda: inbound.process_events(
                c.app.state.db, c.app.state.settings, httpx.MockTransport(ch)
            )
        )
        assert out == {"processed": 1, "failed": 0}
        assert c.get(f"/api/claims/{claim['id']}").json()["stage"] == "accepted"
        [tx] = _rows(c, ClaimTransaction, ClaimTransaction.kind == "277ca")
        assert tx.transaction_id == "tx_hook" and tx.summary["verdict"] == "accepted"


def test_poll_applies_recent_acknowledgements(billing_studio, ch):
    [claim] = _drafts(billing_studio, "office_paid")
    _submit(billing_studio, claim["id"])
    ch.reports["tx_poll"] = ack_277(claim["claim_number"])
    out = billing_studio.post("/api/claims/clearinghouse/poll").json()
    assert out == [
        {
            "transaction_id": "tx_poll",
            "kind": "277",
            "claims": 1,
            "accepted": 1,
            "rejected": 0,
            "pending": 0,
            "unmatched": 0,
        }
    ]


async def test_production_refuses_synthetic_and_test_claims():
    from careplix_api.integrations import clearinghouse, stedi

    def handler(request):  # pragma: no cover - never reached
        raise AssertionError("nothing may be sent")

    prod = clearinghouse.Clearinghouse(
        stedi.StediClient(
            "https://ch.test", "k", mode=stedi.PRODUCTION, transport=httpx.MockTransport(handler)
        )
    )
    test = clearinghouse.Clearinghouse(
        stedi.StediClient(
            "https://ch.test", "k", mode=stedi.TEST, transport=httpx.MockTransport(handler)
        )
    )
    real = x837.build(_draft(), PATIENT, PROFILE, mode="production")
    test_payer = x837.build(_draft(test_outcome="paid"), PATIENT, PROFILE, mode="test")
    for gw, body, synthetic in ((prod, real, True), (prod, test_payer, False), (test, real, False)):
        with pytest.raises(clearinghouse.ClearinghouseError) as err:
            await gw.submit_claim(body, idempotency_key="k", synthetic=synthetic)
        assert err.value.status == "refused"
    for gw in (prod, test):
        await gw.aclose()


def test_claims_hosts_sit_next_to_the_healthcare_host():
    from careplix_api.integrations import stedi

    assert (
        stedi.sibling("https://healthcare.us.stedi.com", "claims.") == "https://claims.us.stedi.com"
    )
    assert stedi.sibling("https://ch.test", "claims.") == "https://ch.test"


def test_claim_work_is_scoped_to_the_workspace(billing_studio, ch):
    from .test_tenancy import CLINIC_ADMIN, _dev_login, _email_sign_in, _new_customer

    [claim] = _drafts(billing_studio, "office_paid")
    ch.submit_errors = ["Billing provider tax ID is invalid"]
    _submit(billing_studio, claim["id"])
    assert len(billing_studio.get("/api/claims/work").json()) == 1
    _new_customer(billing_studio, "practice")
    billing_studio.cookies.clear()
    _email_sign_in(billing_studio, CLINIC_ADMIN)
    assert billing_studio.get("/api/claims/work").json() == []
    assert billing_studio.get("/api/claims/charges").json() == []
    assert billing_studio.post(f"/api/claims/{claim['id']}/submit").status_code == 404
    _dev_login(billing_studio)
    assert _rows(billing_studio, ClaimWork)
