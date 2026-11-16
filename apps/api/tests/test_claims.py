"""Claims groundwork: the white-labelled clearinghouse gateway, the payer directory, 276/277 claim
status kept as evidence, and the follow-up schedule that opens one payer call per stuck claim.

The 277 bodies follow the clearinghouse's documented examples (UnitedHealthcare paid, accepted and
awaiting payment, Anthem denied, Cigna no match). HTTP goes through ``httpx.MockTransport``;
nothing leaves the test. Everyone is synthetic.
"""

import ast
import json
import pathlib
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api.claims import follow_up, service
from careplix_api.claims.models import Claim, ClaimStatusCheck
from careplix_api.db import utcnow
from careplix_api.integrations import clearinghouse, stedi
from careplix_api.main import create_app
from careplix_api.models import Case, IntegrationCall
from careplix_api.phi import PhiBox

from .conftest import FakeGateway, make_settings

ROOT = pathlib.Path(__file__).resolve().parents[3]
API_SRC = ROOT / "apps" / "api" / "src" / "careplix_api"
WEB_SRC = ROOT / "apps" / "web" / "src"

UHC_PAID = {
    "claims": [
        {
            "claimStatus": {
                "amountPaid": "108.77",
                "checkIssueDate": "2025-07-17",
                "checkNumber": "A123456789",
                "claimServiceDate": "20250701",
                "effectiveDate": "2025-07-17",
                "paidDate": "2025-07-15",
                "patientAccountNumber": "12345678",
                "statusCategoryCode": "F1",
                "statusCategoryCodeValue": "Finalized/Payment - The claim/line has been paid.",
                "statusCode": "65",
                "statusCodeValue": "Claim/line has been paid.",
                "submittedAmount": "267.54",
                "trackingNumber": "0123456789",
                "tradingPartnerClaimNumber": "0123456789",
            },
            "serviceDetails": [
                {
                    "service": {"procedureId": "99213", "amountPaid": "108.77"},
                    "status": [{"statusCategoryCode": "F1", "statusCode": "65"}],
                }
            ],
        }
    ],
    "payer": {"organizationName": "UnitedHealthcare"},
    "meta": {"applicationMode": "test"},
    "x12": "ISA*00*~ST*277*0001~SE*2*0001~",
}


def _claim(category, code="20", *, paid="0", lines=(), account=None, **extra):
    return {
        "claimStatus": {
            "statusCategoryCode": category,
            "statusCode": code,
            "amountPaid": paid,
            "effectiveDate": "2025-07-10",
            **({"patientAccountNumber": account} if account else {}),
            **extra,
        },
        "serviceDetails": [
            {"service": {"procedureId": p}, "status": [{"statusCategoryCode": c}]} for p, c in lines
        ],
    }


# --- white label ---------------------------------------------------------------------------------


def _string_constants(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def test_no_api_string_names_the_vendor():
    """Docstrings and identifiers may; anything that can be shown to a person may not."""
    found = [
        f"{path.relative_to(API_SRC)}:{line}"
        for path in API_SRC.rglob("*.py")
        for line, text in _string_constants(path)
        if "Stedi" in text
    ]
    assert found == []


def test_no_studio_text_names_the_vendor():
    found = [
        f"{path.relative_to(WEB_SRC)}:{n}"
        for path in WEB_SRC.rglob("*.ts*")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "Stedi" in line or "stedi.com" in line
    ]
    assert found == []


def test_white_label_rewrites_vendor_text():
    assert clearinghouse.white_label("Stedi's mock member") == "the clearinghouse's mock member"
    assert clearinghouse.white_label("contact STEDI support") == "contact the clearinghouse support"
    assert clearinghouse.white_label(None) is None
    assert "Stedi" not in stedi.error_message([{"code": "79", "followup_code": None}], None)


async def test_vendor_error_messages_are_white_labelled():
    def handler(request):
        return httpx.Response(400, json={"message": "Stedi could not parse the request"})

    client = stedi.StediClient(
        "https://ch.test", "k", mode=stedi.TEST, transport=httpx.MockTransport(handler)
    )
    gateway = clearinghouse.Clearinghouse(client)
    with pytest.raises(clearinghouse.ClearinghouseError) as err:
        await gateway.claim_status(_query(), today=date(2026, 11, 16))
    await gateway.aclose()
    assert err.value.message == "the clearinghouse could not parse the request"


# --- 277 parsing ---------------------------------------------------------------------------------


def test_paid_277_becomes_paid_with_amounts_and_dates():
    s = clearinghouse.summarize_claim_status(UHC_PAID)
    assert s["status"] == "paid" and s["label"] == "Paid"
    assert s["category_code"] == "F1" and s["status_code"] == "65"
    assert s["amount_paid"] == "108.77" and s["paid_date"] == "2025-07-15"
    assert s["service_date"] == "2025-07-01" and s["payer_claim_number"] == "0123456789"
    assert s["service_lines"][0]["status"] == "paid"
    assert s["payer"] == "UnitedHealthcare"


@pytest.mark.parametrize(
    "category,expected",
    [
        ("A1", "accepted"),
        ("A4", "not_found"),
        ("A7", "rejected"),
        ("P1", "pending"),
        ("P5", "pending"),
        ("R4", "info_requested"),
        ("E1", "error"),
        ("F2", "denied"),
        ("F4", "no_payment"),
        ("D0", "not_found"),
    ],
)
def test_categories_map_to_statuses(category, expected):
    response = {"claims": [_claim(category)]}
    assert clearinghouse.summarize_claim_status(response)["status"] == expected


def test_finalized_claims_are_paid_or_denied_by_their_lines():
    denied = {"claims": [_claim("F0", "1", lines=[("99213", "F2"), ("93000", "F2")])]}
    assert clearinghouse.summarize_claim_status(denied)["status"] == "denied"
    paid = {"claims": [_claim("F0", "1", paid="40.00", lines=[("99213", "F2")])]}
    assert clearinghouse.summarize_claim_status(paid)["status"] == "paid"
    mixed = {"claims": [_claim("F3", "1", lines=[("99213", "F2"), ("93000", "F1")])]}
    assert clearinghouse.summarize_claim_status(mixed)["status"] == "finalized"


def test_no_claims_is_not_found_unless_the_payer_errored():
    assert clearinghouse.summarize_claim_status({"claims": []})["status"] == "not_found"
    errored = {"errorResponse": {"errors": [{"code": "42", "description": "Unable to respond"}]}}
    s = clearinghouse.summarize_claim_status(errored)
    assert s["status"] == "error" and s["errors"] == ["42"]


def test_our_claim_is_picked_from_several():
    response = {"claims": [_claim("F1", account="OTHER"), _claim("P1", account="SYN-CL-00009")]}
    s = clearinghouse.summarize_claim_status(response, patient_account_number="SYN-CL-00009")
    assert s["status"] == "pending" and s["claims_returned"] == 2


# --- requests and test mode ----------------------------------------------------------------------


def _query(**kw):
    values = {
        "payer_id": "62308",
        "provider_npi": "1234567893",
        "subscriber": {
            "first_name": "Alder",
            "last_name": "Synthetic",
            "member_id": "SYN12345678",
            "date_of_birth": "1960-03-14",
            "gender": "F",
        },
        "service_from": date(2026, 11, 12),
        "billed_amount": "142.00",
        "patient_account_number": "SYN-CL-00001",
    }
    return clearinghouse.ClaimQuery(**(values | kw))


def test_276_window_never_reaches_the_future_and_names_no_provider():
    body = clearinghouse.claim_status_request(_query(), today=date(2026, 11, 15))
    assert body["encounter"]["beginningDateOfService"] == "20261105"
    assert body["encounter"]["endDateOfService"] == "20261115"
    assert body["encounter"]["patientAccountNumber"] == "SYN-CL-00001"
    assert body["providers"] == [{"npi": "1234567893", "providerType": "BillingProvider"}]
    assert body["subscriber"]["dateOfBirth"] == "19600314"
    assert body["tradingPartnerServiceId"] == "62308"


async def test_test_mode_sends_the_published_test_claim_never_the_patient():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=UHC_PAID)

    client = stedi.StediClient(
        "https://ch.test", "k", mode=stedi.TEST, transport=httpx.MockTransport(handler)
    )
    answer = await clearinghouse.Clearinghouse(client).claim_status(_query(), today=date.today())
    assert answer.mock and sent == [stedi.CLAIM_STATUS_MOCK]
    assert "Synthetic" not in json.dumps(sent)
    assert answer.summary["status"] == "paid"


async def test_each_mode_refuses_the_other_modes_claims():
    def handler(request):  # pragma: no cover - never reached
        raise AssertionError("nothing may be sent")

    transport = httpx.MockTransport(handler)
    prod = stedi.StediClient("https://ch.test", "k", mode=stedi.PRODUCTION, transport=transport)
    with pytest.raises(stedi.StediError) as err:
        await prod.claim_status(stedi.claim_status_mock_request())
    assert err.value.status == "refused"
    test = stedi.StediClient("https://ch.test", "k", mode=stedi.TEST, transport=transport)
    real = clearinghouse.claim_status_request(_query(), today=date(2026, 11, 15))
    with pytest.raises(stedi.StediError):
        await test.claim_status(real)
    await prod.aclose()
    await test.aclose()


def test_traditional_medicare_has_no_electronic_claim_status():
    assert clearinghouse.claim_status_route("CMS", None)
    assert clearinghouse.claim_status_route("06102", "NOT_SUPPORTED")
    assert clearinghouse.claim_status_route("60054", "SUPPORTED") is None


# --- the Studio flow -----------------------------------------------------------------------------


class Fake:
    """The clearinghouse: claim status answers from a queue, payer search from a list."""

    def __init__(self):
        self.answers: list[tuple[int, dict]] = []
        self.sent: list[dict] = []
        self.payers: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ch.test"
        if request.url.path.endswith("/payers/search"):
            return httpx.Response(200, json={"items": [{"payer": p} for p in self.payers]})
        self.sent.append(json.loads(request.content))
        status, body = self.answers.pop(0) if self.answers else (200, UHC_PAID)
        return httpx.Response(status, json=body)


@pytest.fixture
def fake():
    return Fake()


def _app(tmp_path, transport=None, **overrides):
    settings = make_settings(tmp_path, run_background=False, payer_sandbox_url="", **overrides)
    return TestClient(create_app(settings, FakeGateway(settings), integration_transport=transport))


@pytest.fixture
def studio(tmp_path):
    """No clearinghouse connected: sample claims answer with their synthetic 277."""
    with _app(tmp_path) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


@pytest.fixture
def connected(tmp_path, fake):
    with _app(
        tmp_path,
        httpx.MockTransport(fake),
        stedi_api_key="st-test",
        stedi_base_url="https://ch.test",
    ) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        yield c


def _rows(c, model, *where):
    async def load():
        async with c.app.state.db.sessions() as session:
            return list(await session.scalars(select(model).where(*where)))

    return c.portal.call(load)


def _samples(c, *scenarios):
    response = c.post("/api/claims/samples", json={"scenarios": list(scenarios) or None})
    assert response.status_code == 200, response.text
    return {row["sample_scenario"]: row for row in response.json()}


def _run(c, now=None):
    return c.portal.call(
        lambda: follow_up.run_due(c.app.state.db, c.app.state.settings, None, now=now)
    )


def _cases(c):
    return _rows(c, Case, Case.source == follow_up.SOURCE)


def test_stale_status_opens_one_claim_status_case_not_two(studio):
    claims = _samples(studio)
    first = _run(studio)
    by_ref = {case.source_ref: case for case in _cases(studio)}
    expect = {claims[k]["id"] for k in ("stale", "info", "not_found", "medicare")}
    assert {ref.removeprefix("claim:") for ref in by_ref} == expect
    assert first["cases"] == 4 and first["errors"] == 0
    # The schedule runs again (and a person checks the stale claim by hand): no second case.
    later = utcnow() + timedelta(days=8)
    assert _run(studio, later)["cases"] == 0
    stale = claims["stale"]["id"]
    assert studio.post(f"/api/claims/{stale}/check").status_code == 200
    assert len(_cases(studio)) == 4
    case = by_ref[f"claim:{stale}"]
    assert case.case_type == "claim_status" and case.status == "open"
    detail = studio.get(f"/api/cases/{case.id}").json()
    facts = {f["key"]: f["value"] for f in detail["fields"]}
    assert facts["claim_number"].startswith("SYNPAYER") and facts["payer_name"] == "Cigna"
    assert facts["payer_phone"].startswith("+1202555") and detail["missing"] == []
    view = studio.get(f"/api/claims/{stale}").json()
    assert view["follow_up_case_id"] == case.id and view["follow_up_reason"].startswith("Stale")


def test_a_closed_case_is_not_replaced(studio):
    claims = _samples(studio, "info")
    _run(studio)
    [case] = _cases(studio)
    studio.post(f"/api/cases/{case.id}/status", json={"status": "closed"}).raise_for_status()
    _run(studio, utcnow() + timedelta(days=8))
    assert [c.id for c in _cases(studio)] == [case.id]
    assert studio.get(f"/api/claims/{claims['info']['id']}").json()["follow_up_case_id"] == case.id


def test_schedule_follows_claim_age(studio):
    claims = _samples(studio, "young", "pending", "paid", "medicare")
    report = _run(studio)
    assert report["checked"] == 3  # young isn't due; Medicare is recorded, not asked
    view = {k: studio.get(f"/api/claims/{v['id']}").json() for k, v in claims.items()}
    assert view["young"]["status"] is None and view["young"]["checks"] == []
    assert view["paid"]["status"] == "paid" and view["paid"]["next_check_at"] is None
    assert view["pending"]["status"] == "pending" and view["pending"]["next_check_at"]
    assert view["pending"]["needs_call"] is None
    medicare = view["medicare"]
    assert medicare["status"] == "unavailable" and "Medicare" in medicare["no_route"]
    assert medicare["checks"][0]["source"] == "none"
    assert medicare["follow_up_case_id"]
    # A week later the pending claim is asked again; the paid one isn't.
    _run(studio, utcnow() + timedelta(days=8))
    pending = studio.get(f"/api/claims/{claims['pending']['id']}").json()
    paid = studio.get(f"/api/claims/{claims['paid']['id']}").json()
    assert len(pending["checks"]) == 2 and len(paid["checks"]) == 1


def test_needs_call_rules():
    now = utcnow()
    today = now.date()

    def claim(status, age, since=None, no_route=None):
        return Claim(
            status=status,
            submitted_on=(today - timedelta(days=age)).isoformat(),
            status_since=(today - timedelta(days=since)).isoformat() if since else None,
            no_route=no_route,
        )

    assert follow_up.needs_call(claim("paid", 90), now) is None
    assert follow_up.needs_call(claim("pending", 45, since=10), now) is None
    assert follow_up.needs_call(claim("pending", 45, since=25), now).startswith("Stale")
    assert follow_up.needs_call(claim("pending", 20, since=20), now) is None  # too young
    assert follow_up.needs_call(claim("rejected", 15), now)
    assert follow_up.needs_call(claim("unavailable", 29, no_route="No route."), now) is None
    assert "31 days" in follow_up.needs_call(claim("unavailable", 31, no_route="No route."), now)


def test_sample_answers_are_kept_as_sealed_evidence(studio):
    claims = _samples(studio, "denied")
    claim_id = claims["denied"]["id"]
    studio.post(f"/api/claims/{claim_id}/check").raise_for_status()
    [row] = _rows(studio, ClaimStatusCheck)
    assert row.source == "sample" and row.status == "denied" and row.summary["category_code"]
    assert "F2" not in (row.raw or "")  # sealed
    opened = PhiBox(studio.app.state.settings).open(row.raw)
    assert opened["response"]["claims"][0]["claimStatus"]["statusCategoryCode"] == "F2"
    evidence = studio.get(f"/api/claims/{claim_id}/checks/{row.id}/response").json()
    assert evidence["response"]["claims"][0]["claimStatus"]["statusCode"] == "585"


def test_test_mode_check_through_the_clearinghouse(connected, fake):
    claims = _samples(connected, "stale")
    claim_id = claims["stale"]["id"]
    detail = connected.post(f"/api/claims/{claim_id}/check?via=clearinghouse").json()
    assert fake.sent == [stedi.CLAIM_STATUS_MOCK]  # the patient never leaves
    check = detail["checks"][0]
    assert check["source"] == "clearinghouse" and check["mode"] == "test" and check["mock"]
    assert detail["status"] == "paid" and detail["status_mock"]
    assert detail["payer_claim_number"] is None  # a test answer's numbers aren't this claim's
    [call] = _rows(connected, IntegrationCall, IntegrationCall.operation.like("clearinghouse.%"))
    assert call.operation == "clearinghouse.claim_status" and call.ok
    evidence = connected.get(f"/api/claims/{claim_id}/checks/{check['id']}/response").json()
    assert evidence["request"] == stedi.CLAIM_STATUS_MOCK and evidence["response"]["x12"]


def test_unanswered_test_mode_is_unavailable_then_a_payer_call(connected, fake):
    claims = _samples(connected, "stale")
    claim_id = claims["stale"]["id"]
    fake.answers = [(403, {"message": "Stedi test keys can't check claim status"})]
    detail = connected.post(f"/api/claims/{claim_id}/check?via=clearinghouse").json()
    assert detail["status"] == "unavailable" and detail["checks"][0]["state"] == "unavailable"
    assert "Stedi" not in json.dumps(detail)
    assert "clearinghouse" in detail["no_route"]
    assert detail["follow_up_case_id"]  # 46 days old with no electronic status: call the payer


def test_connectivity_errors_retry_before_giving_up(connected, fake):
    claims = _samples(connected, "stale")
    claim_id = claims["stale"]["id"]
    busy = (429, {"message": "Too many requests"})
    fake.answers = [busy, busy, busy]
    for attempt in (1, 2):
        detail = connected.post(f"/api/claims/{claim_id}/check?via=clearinghouse").json()
        assert detail["checks"][0]["state"] == "failed" and detail["failures"] == attempt
        assert detail["status"] is None and not detail["follow_up_case_id"]
    detail = connected.post(f"/api/claims/{claim_id}/check?via=clearinghouse").json()
    assert detail["status"] == "unavailable" and detail["follow_up_case_id"]


# --- payer directory and the admin page ----------------------------------------------------------


def test_clearinghouse_overview_is_white_labelled(connected):
    data = connected.get("/api/clearinghouse").json()
    assert data["name"] == "Clearinghouse" and data["connected"] and data["mode"] == "test"
    payers = {p["payer_id"]: p for p in data["payers"]}
    assert payers["06102"]["claim_status_route"] and payers["60054"]["claim_status_route"] is None
    integrations = connected.get("/api/integrations").json()
    text = json.dumps(integrations) + json.dumps(data)
    assert "Stedi" not in text and "stedi.com" not in text
    names = [c["name"] for c in integrations["connections"] if c["kind"] == "stedi"]
    assert names == ["Clearinghouse (test mode: mock payers)"]


def test_payer_directory_refreshes_from_the_network(connected, fake):
    fake.payers = [
        {
            "displayName": "Cigna",
            "primaryPayerId": "62308",
            "stediId": "XYZAB",
            "aliases": ["CIGNA"],
            "programs": ["COMMERCIAL"],
            "transactionSupport": {"eligibilityCheck": "SUPPORTED", "claimStatus": "SUPPORTED"},
        },
        {
            "displayName": "Synthetic Regional Plan",
            "primaryPayerId": "SYN01",
            "transactionSupport": {"claimStatus": "NOT_SUPPORTED"},
        },
    ]
    refreshed = connected.post("/api/clearinghouse/payers/refresh", json={"query": "cigna"})
    assert refreshed.status_code == 200, refreshed.text
    payers = {p["payer_id"]: p for p in connected.get("/api/clearinghouse/payers").json()}
    assert payers["62308"]["support"]["claim_status"] == "SUPPORTED"
    assert payers["62308"]["support"]["claims"] == "SUPPORTED"  # built-in value kept
    assert payers["SYN01"]["source"] == "network" and payers["SYN01"]["claim_status_route"]
    assert [p["payer_id"] for p in connected.get("/api/clearinghouse/payers?q=cigna").json()] == [
        "62308"
    ]
    row = payers["SYN01"]["id"]
    phone = connected.patch(f"/api/clearinghouse/payers/{row}", json={"phone": "+12025550199"})
    assert phone.json()["phone"] == "+12025550199"


def test_claims_are_scoped_to_the_workspace(studio):
    claims = _samples(studio, "paid")
    claim_id = claims["paid"]["id"]
    rows = _rows(studio, Claim)
    assert [r.workspace_id for r in rows] and service.age_days(rows[0], utcnow().date()) == 21
    assert studio.get("/api/claims/nope").status_code == 404
    assert studio.get(f"/api/claims/{claim_id}").json()["claim_number"].startswith("SYN-CL-")
