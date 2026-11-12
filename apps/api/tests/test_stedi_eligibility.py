"""Stedi eligibility: parsing, errors and retries, payers, Medicare and Medicaid, the voice tool.

Responses are fixtures under ``fixtures/stedi`` (each names its source): Stedi's published mock
members, the documentation's examples and a few synthetic shapes Stedi publishes no mock for.
Everyone in them is synthetic. HTTP goes through ``httpx.MockTransport``; nothing leaves the test.
"""

import asyncio
import json
import pathlib
from contextlib import contextmanager
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from careplix_api.db import utcnow
from careplix_api.integrations import stedi
from careplix_api.models import CallAttempt, EhrRecord, EligibilityCheck, IntegrationCall
from careplix_api.phi import PhiBox
from careplix_api.practice import eligibility, insurance, payers

from .test_practice import AETNA, _client, _db_rows, _worker
from .test_receptionist import JANE, _call

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "stedi"
MOCK_ANSWERS = {
    "AETNA12345": "aetna_active",
    "1AA2CC3DD45": "cms_jane_doe",
    "UHCINACTIVE": "uhc_inactive",
    "HUMANA123": "humana_medicare_advantage",
    **{f"UHCAAA{c}": f"aaa_{c}" for c in ("42", "43", "72", "73", "75", "79")},
    "M-ACTIVE": "aetna_active",
    "M-INACTIVE": "uhc_inactive",
}


def load(name: str) -> tuple[int, dict]:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return data["http_status"], data["body"]


def body(name: str) -> dict:
    return load(name)[1]


def summary(name: str, **kw) -> dict:
    return stedi.summarize_eligibility(body(name), **kw)


class Stedi:
    """Stedi fake: answers by member ID from the fixtures, and searches payers by query."""

    def __init__(self, key: str = "st-test"):
        self.key = key
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.searches: list[dict] = []
        self.answers: dict[str, list] = {}  # member ID -> queue of fixture names / (status, body)
        self.payers: dict[str, list[dict]] = {}  # lower-case query -> payer records

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Key {self.key}"
        if request.url.path.endswith("/payers/search"):
            params = request.url.params
            self.searches.append({k: params.get_list(k) for k in params})
            items = self.payers.get(params.get("query", "").lower(), [AETNA])
            return httpx.Response(200, json={"items": [{"payer": p} for p in items]})
        sent = json.loads(request.content)
        self.bodies.append(sent)
        self.headers.append(dict(request.headers))
        member = sent["subscriber"].get("memberId")
        queue = self.answers.get(member)
        answer = queue.pop(0) if queue else MOCK_ANSWERS.get(member, "aetna_active")
        status, payload = load(answer) if isinstance(answer, str) else answer
        return httpx.Response(status, json=payload)


@contextmanager
def practice(tmp_path, **overrides):
    """The NexHealth + Stedi practice from test_practice, with this file's Stedi fake."""
    key = overrides.pop("key", "st-test")
    setup = overrides.pop("setup", None)
    c, router = _client(tmp_path, eligibility_autopilot=False, **overrides)
    router.stedi = Stedi(key)
    router._st = httpx.MockTransport(router.stedi)
    if setup:
        setup(router)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)  # mirror the practice
        yield c, router


def production(tmp_path, **overrides):
    return practice(
        tmp_path,
        stedi_env="production",
        stedi_api_key="st-test",  # a legacy (test) key that production must never use
        stedi_prod_api_key="st-prod",
        key="st-prod",
        **overrides,
    )


def check(c, appointment="500") -> dict:
    response = c.post(f"/api/practices/appointments/{appointment}/eligibility")
    assert response.status_code == 200, response.text
    return response.json()


def run_async(c, fn):
    async def go():
        async with c.app.state.db.sessions() as session:
            out = await fn(session)
            await session.commit()
            return out

    return c.portal.call(go)


def set_coverage_pii(c, coverage_id: str, values: dict, settings_attr="settings"):
    box = PhiBox(getattr(c.app.state, settings_attr))

    async def go(session):
        row = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.resource == "coverage", EhrRecord.source_id == coverage_id
            )
        )
        row.pii = box.seal({**box.open(row.pii), **values})

    run_async(c, go)


# --- B1: active coverage ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "ACTIVE_COVERAGE",
        "ACTIVE_FULL_RISK_CAPITATION",
        "ACTIVE_SERVICES_CAPITATED",
        "ACTIVE_SERVICES_CAPITATED_TO_PRIMARY_CARE_PROVIDER",
        "ACTIVE_PENDING_INVESTIGATION",
    ],
)
def test_every_active_status_is_active_coverage(status):
    response = {
        "plans": [{"benefits": {"statuses": [{"service": {"value": "30"}, "status": status}]}}]
    }
    assert stedi.summarize_eligibility(response)["coverage"] == "active"


def test_inactive_statuses_and_the_requested_service_win():
    def lines(*pairs):
        return {
            "plans": [
                {
                    "benefits": {
                        "statuses": [{"service": {"value": s}, "status": v} for s, v in pairs]
                    }
                }
            ]
        }

    assert stedi.summarize_eligibility(lines(("30", "INACTIVE_PENDING_INVESTIGATION")))[
        "coverage"
    ] == ("inactive")
    # STC 30 inactive, pharmacy active: the requested service decides.
    both = lines(("30", "INACTIVE"), ("88", "ACTIVE_COVERAGE"))
    assert stedi.summarize_eligibility(both)["coverage"] == "inactive"
    assert stedi.summarize_eligibility(both, service_type="88")["coverage"] == "active"
    # No line for the requested service: any status line still counts.
    assert stedi.summarize_eligibility(lines(("1", "ACTIVE_COVERAGE")))["coverage"] == "active"


def test_benefit_lines_without_a_status_mean_active():
    response = {
        "plans": [{"benefits": {"coPayment": [{"service": {"value": "98"}, "amount": "20"}]}}]
    }
    out = stedi.summarize_eligibility(response)
    assert out["coverage"] == "active" and out["office_copay"] == "20"


def test_uhc_inactive_mock():
    out = summary("uhc_inactive")
    assert out["coverage"] == "inactive" and out["error_class"] is None
    assert out["plan_dates"] == {"start": "2024-01-01", "end": "2025-12-31"}


# --- H5: patient responsibility ----------------------------------------------------------------


def test_individual_family_and_out_of_network_amounts_are_separate():
    out = summary("aetna_active")
    assert out["coverage"] == "active" and out["plan"] == "Gold Plan"
    assert out["office_copay"] == "30" and out["office_copay_out_of_network"] == "60"
    assert out["deductible"] == {"total": "500", "remaining": "350"}  # INDIVIDUAL beats FAMILY
    assert out["deductible_family"] == {"total": "1500", "remaining": "1100"}
    assert out["deductible_out_of_network"] == {"total": "1000", "remaining": "1000"}
    assert out["out_of_pocket"] == {"total": "7000", "remaining": "6200"}  # SERVICE_YEAR
    assert out["out_of_pocket_family"] == {"total": "14000", "remaining": None}  # YEARS
    assert out["plan_dates"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert out["payer"] == "AETNA INC" and out["redirect"] is None


def test_coinsurance_fractions_become_percent():
    response = {
        "plans": [
            {
                "benefits": {
                    "statuses": [{"service": {"value": "30"}, "status": "ACTIVE_COVERAGE"}],
                    "coInsurance": [
                        {
                            "service": {"value": "98"},
                            "network": {"indicator": "IN_NETWORK"},
                            "percent": "0.2",
                        }
                    ],
                }
            }
        ]
    }
    assert stedi.summarize_eligibility(response)["office_coinsurance_pct"] == "20"


# --- B2/H4: Medicare ---------------------------------------------------------------------------


def test_cms_jane_doe_parts_qmb_part_d_and_medicare_advantage():
    # Derived from Stedi's published MBI-lookup response for the CMS mock member (see fixture).
    out = summary("cms_jane_doe", payer_id="CMS")
    assert out["coverage"] == "active"
    medicare = out["medicare"]
    assert medicare["advantage"] is True and medicare["bill_option"] == "A"
    assert medicare["qmb"] is True and medicare["part_d"] == "UHC OF CALIFORNIA"
    assert medicare["part_a"]["status"] == "ACTIVE_COVERAGE"
    assert medicare["part_a"]["deductible"] == "0"  # EPISODE, per benefit period
    assert medicare["part_b"]["deductible"] == "0"  # CALENDAR_YEAR
    assert medicare["part_b"]["coinsurance_pct"] == "0"
    assert out["deductible"] == {"total": "0", "remaining": None}  # office visits: Part B
    redirect = out["redirect"]
    assert redirect["kind"] == "medicare_advantage" and redirect["name"] == "UHC OF CALIFORNIA"
    assert redirect["contract"] == "A0505" and redirect["plan_benefit_package"] == "555"
    assert redirect["phone"] == "8006446644"
    assert redirect["insurance_type"] == "HEALTH_MAINTENANCE_ORGANIZATION_HMO_MEDICARE_RISK"
    assert "1AA2CC3DD45" not in json.dumps(out)  # outcomes only, never the MBI


def test_medicare_advantage_from_a_commercial_payer():
    out = summary("humana_medicare_advantage", payer_id="61101")
    assert out["coverage"] == "active"  # ACTIVE_SERVICES_CAPITATED_TO_PRIMARY_CARE_PROVIDER
    assert out["plan"] == "HUMANA GOLD PLUS HMO"
    assert out["medicare"]["advantage"] is True and out["medicare"]["mbi_returned"] is True
    assert out["redirect"] is None  # the payer is the MA plan itself
    assert "1A22BB3CC44" not in json.dumps(out)


def test_medicare_secondary_payer_types_are_reported():
    response = {
        "plans": [
            {
                "benefits": {
                    "statuses": [{"service": {"value": "30"}, "status": "ACTIVE_COVERAGE"}],
                    "otherOrAdditionalPayer": [
                        {
                            "insuranceType": "MEDICARE_SECONDARY_WORKING_AGED_BENEFICIARY_OR_"
                            "SPOUSE_WITH_EMPLOYER_GROUP_HEALTH_PLAN",
                            "relatedEntities": [
                                {"type": "PRIMARY_PAYER", "name": {"organization": "ACME HEALTH"}}
                            ],
                        }
                    ],
                }
            }
        ]
    }
    out = stedi.summarize_eligibility(response, payer_id="CMS")
    assert out["medicare"]["secondary"][0].startswith("MEDICARE_SECONDARY_WORKING_AGED")
    assert out["other_payers"] == ["ACME HEALTH"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1eg4-te5-mk73", "1EG4TE5MK73"), (" 1AA2CC3DD45 ", "1AA2CC3DD45")],
)
def test_mbi_is_normalised(raw, expected):
    assert stedi.normalize_mbi(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("123456789A", "legacy_medicare_number"),  # HICN
        ("123-45-6789-B1", "legacy_medicare_number"),
        ("1SG4TE5MK73", "invalid_mbi"),  # S is never used
        ("1EG4TE5MK7", "invalid_mbi"),  # 10 characters
        ("0EG4TE5MK73", "invalid_mbi"),  # starts with 0
    ],
)
def test_bad_medicare_numbers_are_refused(raw, code):
    with pytest.raises(stedi.InvalidMbi) as err:
        stedi.normalize_mbi(raw)
    assert err.value.code == code


def test_plan_kinds_keep_medicare_advantage_and_medigap_away_from_cms():
    kind = payers.plan_kind
    assert kind("Medicare Part B") == payers.MEDICARE
    assert kind("Medicare", "CMS") == payers.MEDICARE
    assert kind("Aetna Medicare Advantage PPO") == payers.MEDICARE_ADVANTAGE
    assert kind("Humana Medicare HMO") == payers.MEDICARE_ADVANTAGE
    assert kind("AARP Medicare Supplement Plan G") == payers.COMMERCIAL
    assert kind("Medi-Cal") == payers.MEDICAID
    assert kind("Aetna PPO") == payers.COMMERCIAL


def test_cms_requests_carry_a_normalised_mbi_and_never_a_provider_name():
    request = stedi.patient_request(
        payer_id="CMS",
        provider_npi="1234567893",
        subscriber={
            "first_name": "Ann",
            "last_name": "Test",
            "date_of_birth": "1950-02-03",
            "member_id": "1eg4-te5-mk73",
        },
        pin="pin-1",
        service_date="2026-11-20",
    )
    assert request["subscriber"]["memberId"] == "1EG4TE5MK73"
    assert request["provider"] == {
        "npi": "1234567893",
        "additionalInformation": {"personalIdentificationNumber": "pin-1"},
    }
    assert request["encounter"]["dates"] == {"service": {"start": "2026-11-20"}}
    with pytest.raises(stedi.InvalidMbi):
        stedi.patient_request(
            payer_id="CMS",
            provider_npi="1234567893",
            subscriber={"first_name": "A", "last_name": "B", "member_id": "123456789A"},
        )
    with pytest.raises(ValueError):
        stedi.patient_request(
            payer_id="60054",
            provider_npi=stedi.MOCK_PROVIDER_NPI,
            subscriber={"first_name": "A", "last_name": "B", "member_id": "X1"},
        )


def test_npi_check_digit():
    assert stedi.valid_npi("1234567893") and stedi.valid_npi(stedi.MOCK_PROVIDER_NPI)
    assert not stedi.valid_npi("1234567890") and not stedi.valid_npi("12345")


def test_service_dates_stay_within_what_payers_accept():
    today = date(2026, 11, 11)
    assert stedi.service_date(today, today, "60054") is None  # today: let the payer default
    assert stedi.service_date(date(2026, 11, 30), today, "60054") == "2026-11-30"
    assert stedi.service_date(date(2026, 12, 2), today, "60054") is None
    assert stedi.service_date(date(2026, 12, 31), today, "CMS") == "2026-12-31"  # next month
    assert stedi.service_date(date(2027, 1, 1), today, "CMS") is None
    december = date(2026, 12, 20)
    assert stedi.service_date(date(2027, 1, 15), december, "CMS") == "2027-01-15"


# --- H3: Medicaid ------------------------------------------------------------------------------


def test_medicaid_managed_care_redirect_and_amounts():
    out = summary("medicaid_mco", payer_id="100065")
    assert out["coverage"] == "active"
    assert out["redirect"] == {
        "kind": "managed_care",
        "name": "SYNTHETIC HEALTH PLAN OF CA",
        "payer_id": "SYN01",
        "phone": "8005550199",
        "type": "MANAGED_CARE_ORGANIZATION",
    }
    assert out["medicaid"] == {
        "cost_containment": "25",
        "spend_down": "180",
        "card_reported_stolen": True,
    }
    assert out["medicare"]["advantage"] is False


@pytest.mark.parametrize(
    ("relation", "pii", "medicaid", "expected"),
    [
        ("self", {}, False, ("subscriber", None)),
        # Medicaid children are enrolled under their own member ID: the subscriber.
        ("child", {}, True, ("subscriber", None)),
        # A different policyholder number on file: the patient's own ID.
        (
            "child",
            {"subscriber": {"first_name": "Pat", "last_name": "Roe", "member_id": "H-1"}},
            False,
            ("subscriber", None),
        ),  # noqa: E501
        # Known only by the policyholder's ID: a dependent.
        (
            "child",
            {"subscriber_first_name": "Pat", "subscriber_last_name": "Roe"},
            False,
            ("dependent", "CHILD"),
        ),  # noqa: E501
        (
            "spouse",
            {"subscriber": {"first_name": "Pat", "last_name": "Roe", "member_id": "M-1"}},
            False,
            ("dependent", "SPOUSE"),
        ),  # noqa: E501
        ("child", {}, False, (None, None)),
    ],
)
def test_who_is_sent_as_the_subscriber(relation, pii, medicaid, expected):
    patient = {"first_name": "Kid", "last_name": "Roe", "date_of_birth": "2015-01-01"}
    subscriber, dependent, why = eligibility.who_to_send(relation, patient, "M-1", pii, medicaid)
    if expected[0] is None:
        assert subscriber is None and why
    elif expected[0] == "subscriber":
        assert subscriber["first_name"] == "Kid" and dependent is None
    else:
        assert subscriber["first_name"] == "Pat" and subscriber["member_id"] == "M-1"
        assert dependent["first_name"] == "Kid" and dependent["relation"] == expected[1]


# --- H1: AAA errors and retries ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "outcome"),
    [
        ("aaa_42", stedi.RETRY),
        ("aaa_43", stedi.CORRECT),
        ("aaa_72", stedi.CORRECT),
        ("aaa_73", stedi.CORRECT),
        ("aaa_75", stedi.NOT_FOUND),
        ("aaa_79", stedi.CORRECT),
    ],
)
def test_aaa_mocks_are_classified(name, outcome):
    status, payload = load(name)
    errors = stedi.parse_errors(payload)
    assert stedi.classify(errors, status if status != 200 else None) == outcome
    if status == 200:
        out = stedi.summarize_eligibility(payload)
        assert out["coverage"] == "error" and out["error_class"] == outcome
        assert out["message"] and f"AAA {name[-2:]}" in out["message"]
        assert out["error_details"][0]["location"]


def test_follow_up_actions_decide_whether_42_is_retried():
    def err(code, followup):
        return stedi.parse_errors({"errors": [{"code": code, "followupAction": followup}]})

    assert stedi.classify(err("42", "Resubmission Allowed")) == stedi.RETRY
    assert stedi.classify(err("42", "Please Wait 10 Days and Resubmit")) == stedi.RETRY
    for followup in (
        "Please Correct and Resubmit",
        "Resubmission Not Allowed",
        "Do Not Resubmit; Inquiry Initiated to a Third Party",
    ):
        assert stedi.classify(err("42", followup)) == stedi.CORRECT
    assert stedi.classify(err("80", "Please Correct and Resubmit")) == stedi.RETRY
    both = stedi.parse_errors(
        {
            "errors": [
                {"code": "79", "followupAction": "Please Correct and Resubmit"},
                {"code": "42", "followupAction": "Resubmission Allowed"},
            ]
        }
    )
    assert stedi.classify(both) == stedi.RETRY
    assert stedi.classify([], 429) == stedi.RETRY
    assert stedi.classify([], None, "timeout") == stedi.RETRY


def test_cms_enrollment_messages():
    e41 = stedi.parse_errors({"errors": [{"code": "41", "followupAction": "C"}]})
    assert stedi.error_message(e41, "CMS") == "CMS attestation/enrollment required for this NPI."
    assert "enrol" in stedi.error_message(e41, "60054")
    e97 = stedi.parse_errors({"errors": [{"code": "97"}]})
    assert "suspended" in stedi.error_message(e97, "CMS")


def _client_for(handler, mode=stedi.TEST, **kw) -> stedi.StediClient:
    return stedi.StediClient(
        "https://stedi.test", "k", mode=mode, transport=httpx.MockTransport(handler), **kw
    )


@pytest.mark.asyncio
async def test_400_and_429_bodies_raise_with_their_aaa_errors():
    answers = {"UHCAAA79": load("aaa_79"), "UHCAAA42": load("busy_429")}

    def handler(request):
        member = json.loads(request.content)["subscriber"]["memberId"]
        status, payload = answers[member]
        return httpx.Response(status, json=payload)

    client = _client_for(handler)
    with pytest.raises(stedi.StediError) as err:
        await client.eligibility(stedi.mock_request("aaa_79"))
    assert err.value.http_status == 400 and err.value.errors[0]["code"] == "79"
    assert err.value.outcome == stedi.CORRECT
    with pytest.raises(stedi.StediError) as err:
        await client.eligibility(stedi.mock_request("aaa_42"))
    assert err.value.status == "busy" and err.value.outcome == stedi.RETRY
    assert client._http.timeout.read >= 125
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrency_is_capped_per_process():
    in_flight, peak = 0, 0

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return httpx.Response(200, json=body("aetna_active"))

    clients = [
        stedi.StediClient(
            "https://stedi.test", "k", mode=stedi.TEST, transport=Slow(), max_in_flight=2
        )
        for _ in range(3)
    ]
    await asyncio.gather(
        *(c.eligibility(stedi.mock_request("aetna_active")) for c in clients for _ in range(3))
    )
    assert peak == 2
    for c in clients:
        await c.aclose()


# --- B4: test and production never mix ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_production_client_never_sends_mock_data_and_a_test_client_never_sends_patients():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=body("aetna_active"))

    prod = _client_for(handler, mode=stedi.PRODUCTION)
    for request in (
        stedi.mock_request("aetna_active"),
        {**stedi.mock_request("cms_active"), "provider": {"npi": "1234567893"}},
        {"payerId": "60054", "provider": {"npi": stedi.MOCK_PROVIDER_NPI}, "subscriber": {}},
    ):
        with pytest.raises(stedi.StediError) as err:
            await prod.eligibility(request)
        assert err.value.status == "refused"
    test = _client_for(handler, mode=stedi.TEST)
    patient = stedi.patient_request(
        payer_id="60054",
        provider_npi="1234567893",
        subscriber={"first_name": "Ann", "last_name": "Test", "member_id": "W1"},
    )
    with pytest.raises(stedi.StediError):
        await test.eligibility(patient)
    with pytest.raises(stedi.StediError):  # a mock member with a dependent isn't a mock request
        await test.eligibility({**stedi.mock_request("aetna_active"), "dependent": {"x": 1}})
    assert sent == []
    await test.eligibility(stedi.mock_request("aetna_active"))
    assert len(sent) == 1
    await prod.aclose()
    await test.aclose()


def test_settings_keep_test_and_production_keys_apart(tmp_path):
    from .conftest import make_settings

    legacy = make_settings(tmp_path, stedi_api_key="old", stedi_env="test")
    assert legacy.stedi_key_env == "STEDI_API_KEY" and legacy.stedi_configured
    test = make_settings(tmp_path, stedi_api_key="old", stedi_test_api_key="new")
    assert test.stedi_key_env == "STEDI_TEST_API_KEY"
    # Production never falls back to a test key.
    prod = make_settings(tmp_path, stedi_api_key="old", stedi_env="production")
    assert prod.stedi_key_env == "STEDI_PROD_API_KEY" and not prod.stedi_configured


def test_production_checks_send_the_prod_key_the_billing_npi_and_no_provider_name(tmp_path):
    with production(tmp_path, practice_npi="") as (c, router):
        skipped = check(c)
        assert skipped["state"] == "skipped" and "billing NPI" in skipped["reason"]
        bad = c.put("/api/practices/eligibility/settings", json={"billing_npi": "1234567890"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_npi"
        ok = c.put("/api/practices/eligibility/settings", json={"billing_npi": "1234567893"})
        assert ok.json()["effective_npi"] == "1234567893"
        done = check(c)
        assert done["state"] == "done" and done["summary"]["coverage"] == "active"
        assert done["mock"] is False
        [sent] = router.stedi.bodies
        assert sent["provider"] == {"npi": "1234567893"}  # Stedi fills in the NPPES name
        assert sent["subscriber"]["memberId"] == "M-ACTIVE"
        assert sent["subscriber"]["name"]["person"] == {"firstName": "Jane", "lastName": "Roe"}
        visit = sent["encounter"].get("dates", {}).get("service", {}).get("start")
        assert visit is None or visit > (utcnow() - timedelta(days=2)).date().isoformat()
        payers_view = c.get("/api/practices/payers").json()
        [aetna] = payers_view["payers"]
        assert aetna["state"] == "resolved" and aetna["stedi_payer_id"] == "60054"
        assert aetna["enrollment"] == {"status": "active", "detail": None}
        assert router.stedi.searches[0]["eligibilityCheck"] == ["SUPPORTED"]


# --- M2: test-mode scenarios -------------------------------------------------------------------


def test_test_mode_sends_the_chosen_mock_and_never_the_patient(tmp_path):
    with practice(tmp_path) as (c, router):
        settings = c.get("/api/practices/eligibility/settings").json()
        assert settings["mode"] == "test" and settings["test_scenario"] == "auto"
        assert {s["key"] for s in settings["scenarios"]} >= {"auto", "cms_active", "aaa_75"}
        assert check(c)["summary"]["coverage"] == "active"  # auto: Aetna plan → Aetna mock
        assert router.stedi.bodies[-1]["subscriber"]["memberId"] == "AETNA12345"
        c.put("/api/practices/eligibility/settings", json={"test_scenario": "uhc_inactive"})
        result = check(c, "501")  # same coverage, but the last answer was active: reused
        assert result["reused"] is True
        result = check(c, "502")  # another patient
        assert result["summary"]["coverage"] == "inactive" and result["mock"] is True
        assert router.stedi.bodies[-1]["subscriber"]["memberId"] == "UHCINACTIVE"
        bad = c.put("/api/practices/eligibility/settings", json={"test_scenario": "made_up"})
        assert bad.status_code == 422
        sent = json.dumps(router.stedi.bodies)
        for secret in ("M-ACTIVE", "M-INACTIVE", "Roe", "1980-01-02"):
            assert secret not in sent
        # A mock answer is never flagged on a real chart.
        assert c.get("/api/practices/intents").json() == []


def test_auto_scenario_follows_a_medicare_plan(tmp_path):
    def medicare(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Medicare Part B", "payer_id": ""}

    with practice(tmp_path, setup=medicare) as (c, router):
        result = check(c)
        assert router.stedi.bodies[-1]["payerId"] == "CMS"
        assert result["summary"]["redirect"]["kind"] == "medicare_advantage"
        assert "coverage_redirect" in {
            f
            for r in c.get("/api/practices/huddle", params={"days": 14}).json()["appointments"]
            if r["id"] == "500"
            for f in r["flags"]
        }


# --- H1: retries, caching ------------------------------------------------------------------------


def test_connectivity_errors_requeue_with_backoff_and_errors_are_never_reused(tmp_path):
    with production(tmp_path) as (c, router):
        router.stedi.answers["M-ACTIVE"] = ["aaa_42", "aaa_75", "aetna_active"]
        first = check(c)
        assert first["state"] == "pending" and first["attempts"] == 1
        assert first["summary"]["error_class"] == "retry"
        # Not due yet: the next pass leaves it alone.
        assert (
            c.portal.call(eligibility.run_pending, c.app.state.db, c.app.state.settings, router)
            == 0
        )

        async def due(session):
            row = await session.get(EligibilityCheck, first["id"])
            row.next_attempt_at = utcnow() - timedelta(seconds=1)

        run_async(c, due)
        c.portal.call(eligibility.run_pending, c.app.state.db, c.app.state.settings, router)
        [row] = [r for r in _db_rows(c, EligibilityCheck) if r.id == first["id"]]
        assert row.state == "failed" and row.summary["error_class"] == "not_found"
        assert "couldn't find" in row.reason
        # A failed answer is never reused: the next check asks the payer again.
        again = check(c, "501")
        assert again["state"] == "done" and again["reused"] is False
        assert len(router.stedi.bodies) == 3
        audit = json.dumps([x.summary for x in _db_rows(c, IntegrationCall)])
        assert "M-ACTIVE" not in audit


def test_retries_stop_after_eight_hours(tmp_path):
    with production(tmp_path) as (c, router):
        router.stedi.answers["M-ACTIVE"] = ["aaa_42", "aaa_42"]
        first = check(c)

        async def old(session):
            row = await session.get(EligibilityCheck, first["id"])
            row.created_at = utcnow() - timedelta(hours=9)
            row.next_attempt_at = utcnow() - timedelta(seconds=1)

        run_async(c, old)
        c.portal.call(eligibility.run_pending, c.app.state.db, c.app.state.settings, router)
        [row] = [r for r in _db_rows(c, EligibilityCheck) if r.id == first["id"]]
        assert row.state == "failed" and "Gave up" in row.reason


def test_http_429_is_retried_once_at_once(tmp_path):
    with production(tmp_path) as (c, router):
        router.stedi.answers["M-ACTIVE"] = ["busy_429", "aetna_active"]
        assert check(c)["state"] == "done"
        assert len(router.stedi.bodies) == 2


# --- H2: payer resolution ------------------------------------------------------------------------


def test_ambiguous_payers_wait_for_a_person(tmp_path):
    two = [
        {
            **AETNA,
            "displayName": "Aetna Better Health of Ohio",
            "names": [],
            "primaryPayerId": "ABH01",
            "aliases": [],
        },  # noqa: E501
        {
            **AETNA,
            "displayName": "Aetna Better Health of Texas",
            "names": [],
            "primaryPayerId": "ABH02",
            "aliases": [],
        },  # noqa: E501
    ]

    def plan(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Aetna Better Health", "payer_id": "AB"}

    with production(tmp_path, setup=plan) as (c, router):
        router.stedi.payers = {"ab": [], "aetna better health": two}
        skipped = check(c)
        assert skipped["state"] == "skipped" and "Practice → Payers" in skipped["reason"]
        assert router.stedi.bodies == []  # never guessed
        [mapping] = c.get("/api/practices/payers").json()["payers"]
        assert mapping["state"] == "ambiguous"
        assert [x["payer_id"] for x in mapping["candidates"]] == ["ABH01", "ABH02"]
        confirmed = c.post(
            f"/api/practices/payers/{mapping['id']}/confirm", json={"payer_id": "ABH02"}
        ).json()
        assert confirmed["state"] == "confirmed" and confirmed["stedi_payer_id"] == "ABH02"
        assert check(c)["state"] == "done"
        assert router.stedi.bodies[-1]["payerId"] == "ABH02"


@pytest.mark.asyncio
async def test_payer_search_filters_by_program():
    seen = []

    def handler(request):
        seen.append({k: request.url.params.get_list(k) for k in request.url.params})
        return httpx.Response(200, json={"items": []})

    client = _client_for(handler, mode=stedi.PRODUCTION)
    await client.search_payers("Medi-Cal", programs=["MEDICAID"], states=["CA"])
    await client.aclose()
    assert seen[0] == {
        "query": ["Medi-Cal"],
        "eligibilityCheck": ["SUPPORTED"],
        "programs": ["MEDICAID"],
        "operatingStates": ["CA"],
    }


def test_medicare_advantage_is_never_confirmed_to_cms():
    mapping = payers.PayerMapping(plan_kind=payers.MEDICARE_ADVANTAGE, candidates=[])
    with pytest.raises(ValueError):
        payers.confirm(mapping, "CMS", "user")


# --- B2: Medicare through CMS --------------------------------------------------------------------


def _medicare(member: str):
    def setup(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Medicare", "payer_id": "MEDICARE"}
        router.nexhealth.coverages[70]["subscriber_num"] = member

    return setup


def test_medicare_goes_to_cms_with_the_mbi_and_records_attestation(tmp_path):
    with production(tmp_path, setup=_medicare("1eg4-te5-mk73")) as (c, router):
        router.stedi.answers["1EG4TE5MK73"] = [
            (
                200,
                {
                    "payerId": "CMS",
                    "errors": [
                        {
                            "code": "41",
                            "followupAction": "Please Correct and Resubmit",
                            "location": "PROVIDER",
                        }
                    ],
                },
            ),  # noqa: E501
            "cms_jane_doe",
        ]
        failed = check(c)
        assert failed["state"] == "failed"
        assert failed["reason"] == "CMS attestation/enrollment required for this NPI."
        sent = router.stedi.bodies[-1]
        assert sent["payerId"] == "CMS" and sent["subscriber"]["memberId"] == "1EG4TE5MK73"
        assert router.stedi.searches == []  # fee-for-service Medicare is always CMS
        [mapping] = c.get("/api/practices/payers").json()["payers"]
        assert mapping["stedi_payer_id"] == "CMS" and mapping["enrollment_required"] is True
        assert mapping["enrollment"]["status"] == "action_required"
        done = check(c, "501")
        assert done["state"] == "done" and done["summary"]["medicare"]["advantage"] is True
        # The MA plan is looked up so the next check goes there, never back to CMS.
        assert done["summary"]["redirect"]["payer_id"] == "60054"
        assert c.get("/api/practices/payers").json()["payers"][0]["enrollment"]["status"] == (
            "active"
        )
        [alert] = c.get("/api/practices/intents").json()
        assert alert["reason"] == "Eligibility: patient covered by another plan"


def test_old_medicare_numbers_are_not_sent(tmp_path):
    with production(tmp_path, setup=_medicare("123456789A")) as (c, router):
        skipped = check(c)
        assert skipped["state"] == "skipped" and "old Medicare number" in skipped["reason"]
        assert router.stedi.bodies == []


def test_medicare_advantage_plans_are_not_sent_to_cms(tmp_path):
    def setup(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Humana Medicare Advantage", "payer_id": ""}

    humana = {
        **AETNA,
        "displayName": "Humana",
        "names": ["Humana Medicare Advantage"],
        "primaryPayerId": "61101",
        "aliases": [],
        "programs": ["MEDICARE_ADVANTAGE"],
    }  # noqa: E501
    with production(tmp_path, setup=setup) as (c, router):
        router.stedi.payers = {"humana medicare advantage": [humana]}
        assert check(c)["state"] == "done"
        assert router.stedi.bodies[-1]["payerId"] == "61101"
        assert router.stedi.searches[0]["programs"] == ["MEDICARE_ADVANTAGE"]


# --- H3: Medicaid children and the provider PIN --------------------------------------------------


def test_a_medicaid_child_is_the_subscriber_and_the_pin_is_sent_sealed(tmp_path):
    medi_cal = {
        **AETNA,
        "displayName": "Medi-Cal",
        "names": ["Medi-Cal"],
        "primaryPayerId": "100065",
        "aliases": [],
        "programs": ["MEDICAID"],
        "operatingStates": ["CA"],
    }  # noqa: E501

    def setup(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Medi-Cal", "payer_id": "100065"}
        router.nexhealth.coverages[70]["subscription_relation"] = "child"

    with production(tmp_path, setup=setup) as (c, router):
        router.stedi.payers = {"100065": [medi_cal]}
        assert check(c)["state"] == "done"
        [mapping] = c.get("/api/practices/payers").json()["payers"]
        sent = router.stedi.bodies[-1]
        assert sent["payerId"] == "100065" and "dependent" not in sent
        assert sent["subscriber"]["memberId"] == "M-ACTIVE"
        assert "additionalInformation" not in sent["provider"]
        pinned = c.put(f"/api/practices/payers/{mapping['id']}/pin", json={"pin": "portal-pw"})
        assert pinned.json()["has_pin"] is True and "portal-pw" not in pinned.text
        check(c, "502")  # another patient on the same plan (501 would reuse 500's answer)
        sent = router.stedi.bodies[-1]
        assert sent["subscriber"]["memberId"] == "M-INACTIVE"
        assert sent["provider"]["additionalInformation"] == {
            "personalIdentificationNumber": "portal-pw"
        }
        stored = run_async(c, lambda s: s.scalar(select(payers.PayerMapping.pin)))
        assert stored and "portal-pw" not in stored


def test_a_dependent_known_by_the_policyholders_id(tmp_path):
    def setup(router):
        router.nexhealth.coverages[70]["subscription_relation"] = "child"
        router.nexhealth.subscribers[70] = None  # no policyholder on file in the EHR

    with production(tmp_path, setup=setup) as (c, router):
        assert check(c)["state"] == "skipped"  # no policyholder details in the mirror yet
        set_coverage_pii(c, "70", {"subscriber_first_name": "Pat", "subscriber_last_name": "Roe"})
        assert check(c, "501")["state"] == "done"
        sent = router.stedi.bodies[-1]
        assert sent["subscriber"]["name"]["person"]["firstName"] == "Pat"
        assert sent["subscriber"]["memberId"] == "M-ACTIVE"
        assert sent["dependent"]["name"]["person"]["firstName"] == "Jane"
        assert sent["dependent"]["relationToSubscriber"] == "CHILD"


def test_the_sync_supplies_the_policyholder_for_a_dependent(tmp_path):
    # GET /insurance_coverages/{id} names the subscriber; the sync seals it on the coverage
    # (subscriber_first_name / subscriber_last_name / subscriber_date_of_birth).
    def setup(router):
        router.nexhealth.coverages[70]["subscription_relation"] = "child"
        router.nexhealth.subscribers[70] = {
            "id": 77,
            "name": "Pat Roe",
            "date_of_birth": "1950-02-03",
        }

    with production(tmp_path, setup=setup) as (c, router):
        assert check(c)["state"] == "done"
        sent = router.stedi.bodies[-1]
        assert sent["subscriber"]["name"]["person"]["firstName"] == "Pat"
        assert sent["subscriber"]["name"]["person"]["lastName"] == "Roe"
        assert sent["subscriber"]["memberId"] == "M-ACTIVE"
        assert sent["dependent"]["name"]["person"]["firstName"] == "Jane"


# --- M6: the voice tool ---------------------------------------------------------------------------


def test_voice_insurance_check_in_test_mode(tmp_path):
    with practice(tmp_path) as (c, router):
        call = _call(c)
        refused = call.tool("verify_insurance", payer_name="Aetna", member_id="AETNA12345")
        assert refused["result"]["error"] == "not_verified"
        assert call.ok("find_patient", **JANE) == {"verified": True}
        out = call.ok("verify_insurance", payer_name="Aetna", member_id="AETNA12345")
        assert out["state"] == "checked" and out["coverage"] == "active"
        assert out["office_copay"] == "$30" and out["deductible_remaining"] == "$350"
        assert out["plan"] == "Gold Plan" and out["test_data"] is True
        assert "guarantee" in out["message"]
        # A Medicare number the tester says picks the CMS mock, whose reply names an MA plan.
        out = call.ok("verify_insurance", payer_name="Medicare", member_id="1AA2-CC3-DD45")
        assert out["other_plan"] == {"kind": "medicare_advantage", "name": "UHC OF CALIFORNIA"}
        limit = call.tool("verify_insurance", payer_name="Aetna", member_id="AETNA12345")
        assert limit["result"]["error"] == "insurance_limit"
        assert [b["subscriber"]["memberId"] for b in router.stedi.bodies] == [
            "AETNA12345",
            "1AA2CC3DD45",
        ]


def test_voice_insurance_check_in_production(tmp_path):
    with production(tmp_path) as (c, router):
        call = _call(c)
        call.ok("find_patient", **JANE)
        # A browser test never reaches a real payer.
        refused = call.tool("verify_insurance", payer_name="Aetna", member_id="W123")
        assert refused["result"]["error"] == "test_call_no_payer"

        async def live(session):
            attempt = await session.get(CallAttempt, call.claim["attempt_id"])
            attempt.sandbox = False

        run_async(c, live)
        router.stedi.answers["W123"] = ["aaa_75"]
        out = call.ok("verify_insurance", payer_name="Aetna", member_id="W123")
        assert out["state"] == "not_found"
        sent = router.stedi.bodies[-1]
        assert sent["subscriber"]["memberId"] == "W123"
        assert sent["subscriber"]["name"]["person"] == {"firstName": "Jane", "lastName": "Roe"}
        assert sent["subscriber"]["dateOfBirth"] == "1980-01-02"  # from the chart
        events = json.dumps([x.summary for x in _db_rows(c, IntegrationCall)])
        assert "W123" not in events


def test_the_caller_hears_only_allowed_disclosures():
    full = summary("medicaid_mco")
    only_status = insurance.caller_summary(full, {"coverage_status"}, mock=False)
    assert set(only_status) == {"state", "coverage", "message"}
    everything = insurance.caller_summary(full, set(insurance.DISCLOSURES), mock=False)
    assert everything["other_plan"]["name"] == "SYNTHETIC HEALTH PLAN OF CA"
    assert "front desk" in everything["message"]
    inactive = insurance.caller_summary(summary("uhc_inactive"), {"coverage_status"}, mock=True)
    assert inactive["coverage"] == "inactive" and inactive["test_data"] is True


# --- billing NPI made simple -----------------------------------------------------------------

NPPES_ORG = {
    "enumeration_type": "NPI-2",
    "basic": {"organization_name": "SYNTHETIC FAMILY DENTAL", "status": "A"},
    "addresses": [{"address_purpose": "LOCATION", "city": "CHICAGO", "state": "IL"}],
    "taxonomies": [{"primary": True, "desc": "Dentist, General Practice"}],
}


def test_npi_lookup_names_the_owner_and_explains_bad_numbers(tmp_path):
    with practice(tmp_path) as (c, router):
        router.nppes = {"1234567893": NPPES_ORG}
        found = c.get("/api/practices/eligibility/npi/1234567893").json()
        assert found["status"] == "confirmed"
        assert found["name"] == "SYNTHETIC FAMILY DENTAL" and found["kind"] == "organization"
        assert found["state"] == "IL" and found["taxonomy"].startswith("Dentist")
        typo = c.get("/api/practices/eligibility/npi/1234567890")
        assert typo.status_code == 422 and "check digit" in typo.json()["error"]["message"]
        short = c.get("/api/practices/eligibility/npi/12345")
        assert short.status_code == 422 and "10 digits" in short.json()["error"]["message"]
        missing = c.get("/api/practices/eligibility/npi/1245319599").json()
        assert missing["status"] == "invalid" and missing["reason"] == "not found in NPPES"
        bad = c.put("/api/practices/eligibility/settings", json={"billing_npi": "1234567890"})
        assert "check digit" in bad.json()["error"]["message"]


def test_settings_offer_provider_npis_from_the_practice_system(tmp_path):
    with practice(tmp_path) as (c, _):
        s = c.get("/api/practices/eligibility/settings").json()
        assert s["providers"] == [{"npi": "1234567893", "name": "Dr. Avery Test"}]
        assert s["effective_npi"] == "1234567893" and s["npi_source"] == "deployment"
        s = c.put("/api/practices/eligibility/settings", json={"billing_npi": "1245319599"}).json()
        assert s["effective_npi"] == "1245319599" and s["npi_source"] == "workspace"
        s = c.put("/api/practices/eligibility/settings", json={"billing_npi": None}).json()
        assert s["npi_source"] == "deployment"


def test_huddle_labels_each_visit_with_its_program(tmp_path):
    with practice(tmp_path) as (c, _):
        rows = c.get("/api/practices/huddle?days=7").json()["appointments"]
        assert {r["coverage"]["program"] for r in rows if r["coverage"]} <= set(payers.PROGRAMS)


@pytest.mark.parametrize(
    ("name", "payer_id", "program"),
    [
        ("Medicare Part B", None, "medicare"),
        ("Railroad Medicare", None, "railroad_medicare"),
        ("Humana Medicare Advantage PPO", None, "medicare_advantage"),
        ("AARP Medicare Supplement Plan G", None, "medigap"),
        ("SilverScript PDP (Part D)", None, "part_d"),
        ("Medi-Cal", None, "medicaid"),
        ("Aetna PPO", "60054", "commercial"),
        (None, None, None),
    ],
)
def test_program_labels(name, payer_id, program):
    assert payers.program(name, payer_id) == program
