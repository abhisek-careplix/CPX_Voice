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


# --- ERA 835 posting (RC7) -----------------------------------------------------------------------

from careplix_api.claims import era  # noqa: E402
from careplix_api.integrations import claim_reports  # noqa: E402


def era_835(
    pcn, outcome="paid", *, charge="206.25", lines=(("L1", "142.00"), ("L2", "64.25")), remarks=()
):
    """An 835 report in the clearinghouse's JSON shape, the way its test payer pays: in full,
    60 % with CO-45 and PR-2 at 20 % each, or denied with CO-31."""
    total = Decimal(charge)
    pct = {"paid": Decimal("1"), "partial": Decimal("0.6"), "denied": Decimal("0")}[outcome]
    paid = (total * pct).quantize(Decimal("0.01"))
    adj = []
    if outcome == "partial":
        fifth = (total * Decimal("0.2")).quantize(Decimal("0.01"))
        adj = [
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "45",
                "adjustmentAmount1": str(fifth),
            },
            {
                "claimAdjustmentGroupCode": "PR",
                "adjustmentReasonCode1": "2",
                "adjustmentAmount1": str(fifth),
            },
        ]
    if outcome == "denied":
        adj = [
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "31",
                "adjustmentAmount1": str(total),
            }
        ]
    service_lines = [
        {
            "lineItemControlNumber": lid,
            "servicePaymentInformation": {
                "adjudicatedProcedureCode": "99213",
                "lineItemChargeAmount": amt,
                "lineItemProviderPaymentAmount": str(
                    (Decimal(amt) * pct).quantize(Decimal("0.01"))
                ),
            },
        }
        for lid, amt in lines
    ]
    return {
        "transactions": [
            {
                "controlNumber": "0001",
                "financialInformation": {
                    "totalActualProviderPaymentAmount": str(paid),
                    "paymentMethodCode": "NON" if outcome == "denied" else "ACH",
                    "checkIssueOrEFTEffectiveDate": "20261114",
                },
                "paymentAndRemitReassociationDetails": {"checkOrEFTTraceNumber": "EFT12345"},
                "payer": {"name": "Test payer"},
                "detailInfo": [
                    {
                        "paymentInfo": [
                            {
                                "claimPaymentInfo": {
                                    "patientControlNumber": pcn,
                                    "claimStatusCode": "4" if outcome == "denied" else "1",
                                    "totalClaimChargeAmount": charge,
                                    "claimPaymentAmount": str(paid),
                                    "patientResponsibilityAmount": adj[1]["adjustmentAmount1"]
                                    if outcome == "partial"
                                    else "0",
                                    "payerClaimControlNumber": f"PAY-{pcn}",
                                },
                                "claimAdjustments": adj,
                                "outpatientAdjudication": {
                                    f"claimPaymentRemarkCode{n}": r
                                    for n, r in enumerate(remarks, 1)
                                },
                                "serviceLines": service_lines,
                            }
                        ]
                    }
                ],
            }
        ]
    }


from decimal import Decimal  # noqa: E402


def test_835_summary_shapes_paid_partial_denied():
    [paid] = claim_reports.summarize_835(era_835("CPX1"))
    assert (paid["outcome"], paid["paid"], paid["trace_number"]) == ("paid", "206.25", "EFT12345")
    assert paid["paid_on"] == "2026-11-14" and paid["payment_method"] == "ACH"
    [partial] = claim_reports.summarize_835(era_835("CPX1", "partial"))
    assert partial["outcome"] == "partially_paid" and partial["paid"] == "123.75"
    assert [(a["group"], a["code"], a["amount"]) for a in partial["adjustments"]] == [
        ("CO", "45", "41.25"),
        ("PR", "2", "41.25"),
    ]
    assert partial["patient_responsibility"] == "41.25"
    [denied] = claim_reports.summarize_835(era_835("CPX1", "denied", remarks=["N130"]))
    assert denied["outcome"] == "denied" and denied["remarks"] == ["N130"]


def test_posting_proposals_post_payment_and_write_offs_never_patient_share():
    claim = _draft()
    [partial] = claim_reports.summarize_835(era_835(claim.claim_number, "partial"))
    writes = era.proposals(claim, partial)
    assert [(w["kind"], w["amount"]) for w in writes] == [
        ("payment_post", "123.75"),
        ("adjustment_post", "41.25"),
    ]
    assert writes[0]["payload"]["amount"] == -123.75  # payments reduce the balance
    assert writes[0]["payload"]["transaction_id"].startswith("ERA-P-CPX0000000001")
    assert len(writes[1]["payload"]["transaction_id"]) <= 50
    assert "CO-45" in writes[1]["reason"]


def test_remittances_settle_claims_and_propose_postings(billing_studio, ch):
    paid, partial, denied = _drafts(
        billing_studio, "office_paid", "office_partial", "office_denied"
    )
    for c in (paid, partial, denied):
        _submit(billing_studio, c["id"])
    report = era_835(
        paid["claim_number"],
        charge=paid["billed_amount"],
        lines=(("L1", "198.50"), ("L2", "42.00")),
    )
    report["transactions"] += era_835(partial["claim_number"], "partial")["transactions"]
    report["transactions"] += era_835(
        denied["claim_number"], "denied", charge="235.00", lines=(("L1", "235.00"),)
    )["transactions"]
    out = _ingest(billing_studio, "tx_era_1", report, ch, kind="835")
    assert (out["claims"], out["paid"], out["partially_paid"], out["denied"]) == (3, 1, 1, 1)
    assert _ingest(billing_studio, "tx_era_1", report, ch, kind="835")["claims"] == 0

    claims = {c["id"]: c for c in billing_studio.get("/api/claims").json()}
    assert claims[paid["id"]]["stage"] == "paid" and claims[paid["id"]]["paid_total"] == "240.50"
    assert claims[partial["id"]]["stage"] == "partially_paid"
    assert claims[partial["id"]]["patient_responsibility"] == "41.25"
    assert claims[denied["id"]]["stage"] == "denied" and claims[denied["id"]]["status"] == "denied"
    postings = {p["claim_id"]: p for p in billing_studio.get("/api/claims/era").json()}
    # Synthetic samples have no EHR patient: posted by hand.
    assert postings[paid["id"]]["state"] == "manual"
    assert postings[denied["id"]]["state"] == "review"
    r = billing_studio.post(f"/api/claims/era/{postings[paid['id']]['id']}/posted")
    assert r.json()["state"] == "posted"


def test_ehr_claims_post_through_write_intents(tmp_path, ch):
    """A claim built from NexHealth charges: its remittance becomes L3 write intents that a person
    approves; approval sends POST /payments and POST /adjustments."""
    from careplix_api.models import EhrRecord, PayerConnection, WriteIntent
    from careplix_api.phi import PhiBox

    nex_writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nex.test":
            if request.url.path.endswith("/authenticates"):
                return httpx.Response(200, json={"data": {"token": "t"}})
            if request.method == "POST":
                nex_writes.append((request.url.path, json.loads(request.content)))
                return httpx.Response(202)
            if request.url.path.endswith("/charges"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": 501,
                                "patient_id": 3,
                                "provider_id": 15,
                                "procedure_code": "99213",
                                "fee": {"amount": "142.00"},
                                "charged_at": date.today().isoformat(),
                            }
                        ],
                        "page_info": {"has_next_page": False},
                    },
                )
            return httpx.Response(200, json={"data": []})
        return ch(request)

    with _app(
        tmp_path,
        httpx.MockTransport(handler),
        stedi_api_key="st-test",
        stedi_base_url="https://ch.test",
    ) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        ws = c.get("/api/me").json()["workspace_id"]
        settings = c.app.state.settings

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
                    (
                        "patient",
                        "3",
                        {},
                        {
                            "first_name": "Cedar",
                            "last_name": "Synthetic",
                            "date_of_birth": "1970-02-03",
                            "gender": "Female",
                        },
                        "3",
                    ),
                    ("provider", "15", {"name": "Dr Synthetic", "npi": "1999999984"}, None, None),
                    ("coverage", "77", {"plan_id": "9", "priority": 1}, {"member_id": "M100"}, "3"),
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
        assert c.post("/api/claims/charges/pull").json()[0]["added"] == 1
        [claim] = c.post("/api/claims/build", json={}).json()
        assert claim["diagnoses"] == []  # NexHealth charges carry none
        c.patch(
            f"/api/claims/{claim['id']}",
            json={"diagnoses": ["J06.9"], "lines": [{"line_id": "L1", "dx": [1]}]},
        )
        claim = _submit(c, claim["id"])
        assert claim["stage"] == "submitted", claim
        _ingest(
            c,
            "tx_era_ehr",
            era_835(claim["claim_number"], "partial", charge="142.00", lines=(("L1", "142.00"),)),
            ch,
            kind="835",
        )
        [posting] = c.get("/api/claims/era").json()
        assert posting["state"] == "proposed" and len(posting["intent_ids"]) == 2
        intents = {i.kind: i for i in _rows(c, WriteIntent)}
        assert {i.level for i in intents.values()} == {"L3"}
        assert intents["payment_post"].state == "proposed"
        assert intents["payment_post"].target_source_id == "3"
        out = c.post(f"/api/claims/era/{posting['id']}/approve").json()
        assert out["state"] == "posted", [(i.state, i.last_error) for i in _rows(c, WriteIntent)]
        (pay_path, pay), (adj_path, adj) = nex_writes
        assert pay_path.endswith("/payments") and adj_path.endswith("/adjustments")
        assert pay["payment"]["patient_id"] == 3 and pay["payment"]["amount"] == -85.2
        assert pay["payment"]["charge_splits"] == {"501": -85.2}
        assert adj["adjustment"]["amount"] == 28.4
        assert all(i.state == "accepted" for i in _rows(c, WriteIntent))


# --- denials (RC8) -------------------------------------------------------------------------------

from careplix_api.claims import denials  # noqa: E402


@pytest.mark.parametrize(
    ("codes", "remarks", "category", "route"),
    [
        (["31"], [], "eligibility", "fix_resubmit"),
        (["27"], [], "eligibility", "fix_resubmit"),
        (["22"], [], "coordination", "fix_resubmit"),
        (["4"], [], "coding", "fix_resubmit"),
        (["16"], ["M127"], "missing_information", "fix_resubmit"),
        (["197"], [], "authorization", "appeal"),
        (["50"], [], "medical_necessity", "appeal"),
        (["29"], [], "timely_filing", "appeal"),
        (["97"], [], "bundling", "appeal"),
        (["18"], [], "duplicate", "payer_call"),
        (["96"], ["N130"], "non_covered", "write_off"),
        (["B7"], [], "provider", "payer_call"),
        (["999"], [], "other", "payer_call"),
        (["16"], ["N54"], "authorization", "appeal"),  # the remark is more specific
    ],
)
def test_denial_classification(codes, remarks, category, route):
    assert denials.classify(codes, remarks) == category
    assert denials.CATEGORIES[category][0] == route


def test_contractual_and_patient_share_are_not_denials():
    rows = denials.denial_rows(
        {
            "adjustments": [
                {"group": "CO", "code": "45", "amount": "41.25"},
                {"group": "PR", "code": "2", "amount": "41.25"},
                {"group": "CO", "code": "253", "amount": "1.10"},
                {"group": "CO", "code": "97", "amount": "64.25"},
            ]
        }
    )
    assert [(r["group"], r["code"]) for r in rows] == [("CO", "97")]


def test_appeal_deadlines():
    today = date(2026, 11, 16)
    assert denials.deadline("medicare", "appeal", "2026-11-14", today) == "2027-03-14"
    assert denials.deadline("commercial", "appeal", None, today) == "2027-05-15"
    assert denials.deadline("commercial", "fix_resubmit", "2026-11-14", today) == "2027-02-12"


def _denied(c, ch, visit="office_denied", **era_kw):
    [claim] = _drafts(c, visit)
    claim = _submit(c, claim["id"])
    _ingest(c, f"tx_ack_{claim['id'][:6]}", ack_277(claim["claim_number"]), ch)
    lines = tuple((line["line_id"], line["charge"]) for line in claim["lines"])
    report = era_835(claim["claim_number"], "denied", charge=claim["billed_amount"], lines=lines)
    for k, v in era_kw.items():
        report["transactions"][0]["detailInfo"][0]["paymentInfo"][0][k] = v
    _ingest(c, f"tx_era_{claim['id'][:6]}", report, ch, kind="835")
    return claim


def test_test_payer_denial_routes_to_a_corrected_claim_that_gets_paid(billing_studio, ch):
    claim = _denied(billing_studio, ch)
    [work] = billing_studio.get("/api/claims/work?kind=denial").json()
    assert work["category"] == "eligibility" and work["route"] == "fix_resubmit"
    assert work["amount"] == "235.00" and work["due_on"] == "2027-02-12"
    assert {r["code"] for r in work["reasons"]} == {"31", "N130"} or work["reasons"][0][
        "code"
    ] == "31"

    fixed = billing_studio.post(f"/api/claims/work/{work['id']}/correct").json()
    assert fixed["frequency"] == "replacement" and fixed["replaces_claim_id"] == claim["id"]
    assert fixed["claim_number"] != claim["claim_number"] and fixed["test_outcome"] == "paid"
    # Asking twice returns the same draft.
    assert billing_studio.post(f"/api/claims/work/{work['id']}/correct").json()["id"] == fixed["id"]
    out = _submit(billing_studio, fixed["id"])
    _, body = ch.submitted[-1]
    assert body["encounter"]["resubmission"] == {
        "code": "REPLACEMENT_OF_PRIOR_CLAIM",
        "originalReferenceNumber": f"PAY-{claim['claim_number']}",
    }
    assert body["insured"]["memberId"].startswith("STEDI_PAID_")
    report = era_835(out["claim_number"], charge="235.00", lines=(("L1", "235.00"),))
    _ingest(billing_studio, "tx_era_fix", report, ch, kind="835")
    assert billing_studio.get("/api/claims/work?kind=denial").json() == []
    [done] = billing_studio.get("/api/claims/work?kind=denial&state=done").json()
    assert done["resolution"].startswith(f"Corrected claim {out['claim_number']} paid")


def test_payer_call_route_opens_one_denial_case(billing_studio, ch):
    from careplix_api.models import Case

    _denied(
        billing_studio,
        ch,
        claimAdjustments=[
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "18",
                "adjustmentAmount1": "235.00",
            }
        ],
        outpatientAdjudication={},
    )
    [work] = billing_studio.get("/api/claims/work?kind=denial").json()
    assert work["category"] == "duplicate" and work["route"] == "payer_call", work
    assert work["case_id"], work["resolution"]
    [case] = _rows(billing_studio, Case, Case.source == "claim_denial")
    assert case.id == work["case_id"] and case.data["denial"]["reason_codes"] == "CO-18"
    # Rerouting to a payer call again never opens a second case.
    billing_studio.post(f"/api/claims/work/{work['id']}/route", json={"route": "payer_call"})
    assert len(_rows(billing_studio, Case, Case.source == "claim_denial")) == 1


def test_write_off_route_waits_for_staff(billing_studio, ch):
    _denied(
        billing_studio,
        ch,
        claimAdjustments=[
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "96",
                "adjustmentAmount1": "235.00",
            }
        ],
    )
    [work] = billing_studio.get("/api/claims/work?kind=denial").json()
    assert work["route"] == "write_off" and work["category"] == "non_covered"
    # Synthetic sample: no EHR patient, so nothing is written; staff do it by hand.
    assert work["intent_ids"] == [] and "by hand" in work["resolution"]


# --- appeals (RC8) -------------------------------------------------------------------------------

import base64  # noqa: E402

from careplix_api.claims import appeals  # noqa: E402

PDF = base64.b64encode(b"%PDF-1.4 synthetic visit note").decode()


def _necessity_denial(c, ch):
    claim = _denied(
        c,
        ch,
        claimAdjustments=[
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "50",
                "adjustmentAmount1": "235.00",
            }
        ],
    )
    [work] = c.get("/api/claims/work?kind=denial").json()
    return claim, work


def test_appeal_route_drafts_a_letter_for_staff(billing_studio, ch):
    claim, work = _necessity_denial(billing_studio, ch)
    assert work["category"] == "medical_necessity" and work["route"] == "appeal"
    assert work["appeal_id"] and work["due_on"] == "2027-05-13"  # 180 days from 2026-11-14
    [listed] = billing_studio.get("/api/claims/appeals").json()
    assert listed["state"] == "draft" and listed["level"] == "first_level"
    assert listed["claim_number"] == claim["claim_number"] and not listed["overdue"]
    assert {e["key"] for e in listed["evidence"]} == {"claim", "remittance", "notes"}
    full = billing_studio.get(f"/api/claims/appeals/{work['appeal_id']}").json()
    letter = full["letter"]
    assert "medically necessary" in letter and "CO-50" in letter
    assert f"PAY-{claim['claim_number']}" in letter and "Synthetic" in letter  # the patient
    assert "Stedi" not in letter


def test_appeal_approval_without_a_sender_is_ready_to_send_by_hand(billing_studio, ch):
    _, work = _necessity_denial(billing_studio, ch)
    aid = work["appeal_id"]
    edited = billing_studio.put(
        f"/api/claims/appeals/{aid}/letter", json={"text": "Edited letter for the payer. " * 3}
    )
    assert edited.json()["letter"].startswith("Edited letter")
    out = billing_studio.post(f"/api/claims/appeals/{aid}/approve", json={"channel": "fax"}).json()
    assert out["state"] == "ready_to_send" and "No fax service" in out["send_note"]
    assert (
        billing_studio.put(f"/api/claims/appeals/{aid}/letter", json={"text": "x" * 30}).status_code
        == 409
    )
    sent = billing_studio.post(f"/api/claims/appeals/{aid}/sent", json={"note": "Faxed"}).json()
    assert sent["state"] == "sent" and sent["sent_at"]
    out = billing_studio.post(
        f"/api/claims/appeals/{aid}/decided", json={"outcome": "overturned"}
    ).json()
    assert out["state"] == "decided"
    assert billing_studio.get("/api/claims/work?kind=denial").json() == []


async def test_a_registered_sender_sends_the_approved_letter(tmp_path):
    from careplix_api.claims.models import Appeal

    sent = []

    async def fake_fax(appeal, text, settings):
        sent.append(text)
        return "Fax queued"

    appeals.SENDERS["fax"] = fake_fax
    try:
        settings = make_settings(tmp_path)
        from careplix_api.phi import PhiBox

        row = Appeal(
            workspace_id="ws",
            claim_id="c",
            level="first_level",
            state="draft",
            due_on="2027-01-01",
            letter=PhiBox(settings).seal({"text": "Dear payer"}),
        )
        out = await appeals.approve(settings, row, "u1", "fax")
        assert out.state == "sent" and out.send_note == "Fax queued" and sent == ["Dear payer"]
    finally:
        appeals.SENDERS.pop("fax")


def test_timely_filing_appeal_cites_the_acceptance(billing_studio, ch):
    _denied(
        billing_studio,
        ch,
        claimAdjustments=[
            {
                "claimAdjustmentGroupCode": "CO",
                "adjustmentReasonCode1": "29",
                "adjustmentAmount1": "235.00",
            }
        ],
    )
    [work] = billing_studio.get("/api/claims/work?kind=denial").json()
    full = billing_studio.get(f"/api/claims/appeals/{work['appeal_id']}").json()
    accepted = date.today().isoformat()
    assert f"accepted it on {accepted}" in full["letter"]
    assert any(e["key"] == "acceptance" and e["have"] for e in full["evidence"])


def test_appeal_records_go_as_275_attachments_with_the_corrected_claim(billing_studio, ch):
    _, work = _necessity_denial(billing_studio, ch)
    r = billing_studio.post(
        f"/api/claims/appeals/{work['appeal_id']}/attachments",
        json={"label": "Visit note", "content_type": "application/pdf", "data_base64": PDF},
    )
    assert r.status_code == 200, r.text
    assert r.json()["attachment_id"] == "att_1" and ch.uploads == [b"%PDF-1.4 synthetic visit note"]
    listed = billing_studio.get("/api/claims/appeals").json()[0]
    assert next(e for e in listed["evidence"] if e["key"] == "notes")["have"] is True
    fixed = billing_studio.post(f"/api/claims/work/{work['id']}/correct").json()
    _submit(billing_studio, fixed["id"])
    _, body = ch.submitted[-1]
    assert body["encounter"]["attachments"] == [
        {
            "attachmentId": "att_1",
            "reportTypeCode": "PROGRESS_REPORT",
            "transmissionCode": "ELECTRONICALLY_ONLY",
        }
    ]
    # A claim that already went out takes no new attachment of its own.
    denied_claim = work["claim_id"]
    bad = billing_studio.post(
        f"/api/claims/{denied_claim}/attachments",
        json={"label": "Note", "content_type": "application/pdf", "data_base64": PDF},
    )
    assert bad.status_code == 409


def test_overdue_appeals_are_flagged():
    from careplix_api.claims.models import Appeal

    row = Appeal(level="first_level", state="draft", due_on="2026-11-01")
    v = appeals.view(row, date(2026, 11, 16))
    assert v["days_left"] == -15 and v["overdue"] is True
    row.state = "sent"
    assert appeals.view(row, date(2026, 11, 16))["overdue"] is False


# --- claim status follow-up for claims we submitted (RC6) -------------------------------------

from datetime import UTC, datetime  # noqa: E402

from careplix_api.claims import follow_up  # noqa: E402
from careplix_api.models import Case  # noqa: E402


def _sweep(c, days):
    now = datetime.now(UTC) + timedelta(days=days)
    return c.portal.call(
        lambda: follow_up.run_due(c.app.state.db, c.app.state.settings, None, now=now)
    )


def test_submitted_claims_follow_277ca_and_835_age(billing_studio, ch):
    silent, accepted, paid, rejected = _drafts(
        billing_studio, "office_paid", "office_partial", "office_denied", "medicare_ack_only"
    )
    for c in (silent, accepted, paid, rejected):
        _submit(billing_studio, c["id"])
    _ingest(billing_studio, "tx_a", ack_277(accepted["claim_number"]), ch)
    _ingest(billing_studio, "tx_p", ack_277(paid["claim_number"]), ch)
    _ingest(
        billing_studio,
        "tx_r",
        ack_277(rejected["claim_number"], "A7", "21", text="Missing or invalid information."),
        ch,
    )
    _ingest(
        billing_studio,
        "tx_e",
        era_835(paid["claim_number"], charge="235.00", lines=(("L1", "235.00"),)),
        ch,
        kind="835",
    )

    assert _sweep(billing_studio, 1)["cases"] == 0
    # Day 4: no acknowledgement for the silent claim → one payer call.
    assert _sweep(billing_studio, 4)["cases"] == 1
    # Day 31: the accepted claim has no remittance → its call; the others never get one.
    assert _sweep(billing_studio, 31)["cases"] == 1
    assert _sweep(billing_studio, 60)["cases"] == 0
    refs = {c.source_ref for c in _rows(billing_studio, Case, Case.source == follow_up.SOURCE)}
    assert refs == {f"claim:{silent['id']}", f"claim:{accepted['id']}"}
    detail = billing_studio.get(f"/api/claims/{accepted['id']}").json()
    assert detail["follow_up_reason"].startswith("Accepted, but no remittance (835)")
    # Test mode never sends a 276 for our claims; their status stays what the 277CA said.
    assert detail["status"] == "accepted" and detail["no_route"].startswith("Test mode")


def test_manual_check_on_a_test_mode_claim_keeps_its_status(billing_studio, ch):
    [claim] = _drafts(billing_studio, "office_paid")
    _submit(billing_studio, claim["id"])
    _ingest(billing_studio, "tx_m", ack_277(claim["claim_number"]), ch)
    out = billing_studio.post(f"/api/claims/{claim['id']}/check").json()
    assert out["status"] == "accepted" and out["stage"] == "accepted"
    assert out["checks"][0]["state"] == "unavailable"
    assert not [s for s in ch.submitted if "claimstatus" in json.dumps(s)]


def test_needs_call_rules_for_submitted_claims():
    now = datetime(2026, 12, 20, tzinfo=UTC)
    sent = datetime(2026, 11, 16, tzinfo=UTC)

    def claim(stage, **kw):
        return Claim(stage=stage, submitted_at=sent, submitted_on="2026-11-16", **kw)

    assert follow_up.needs_call(claim("draft"), now) is None
    assert follow_up.needs_call(claim("rejected", status="rejected"), now) is None
    assert follow_up.needs_call(claim("denied", status="denied"), now) is None
    assert "No acknowledgement" in follow_up.needs_call(claim("submitted"), now)
    assert "no remittance" in follow_up.needs_call(claim("accepted"), now)
    assert follow_up.needs_call(claim("accepted", era_at=now), now) is None
    assert follow_up.due(claim("rejected"), now) is False
