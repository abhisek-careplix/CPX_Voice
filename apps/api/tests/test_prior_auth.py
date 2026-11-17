"""Prior authorization end to end: detect, packet, PAS submit/inquire, call fallback, contract."""

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api import payer_sandbox
from careplix_api.cases import attempts
from careplix_api.cases.models import CaseLink, ContactAttempt, PlaybookRun
from careplix_api.db import utcnow
from careplix_api.integrations import pas
from careplix_api.main import create_app
from careplix_api.models import EhrRecord, EligibilityCheck, Finding, IntegrationCall, Task

from .conftest import APPROVED, FakeGateway, make_settings
from .test_cases_v2 import FakeText
from .test_practice import Router, _worker

SANDBOX = "http://sandbox.test/fhir"


class SandboxRouter(Router):
    """The practice fakes (NexHealth, clearinghouse) plus the synthetic PAS payer sandbox."""

    def __init__(self):
        super().__init__()
        self.sandbox = httpx.ASGITransport(app=payer_sandbox.app)

    async def handle_async_request(self, request):
        if request.url.host == "sandbox.test":
            return await self.sandbox.handle_async_request(request)
        return await super().handle_async_request(request)


def _practice(tmp_path, *, pas_sandbox: bool, **overrides):
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
        "payer_sandbox_url": SANDBOX if pas_sandbox else "",
        "playbook_triggers": "auth_required",
        "playbook_contact_hours": False,
        "backfill_enabled": False,
        **overrides,
    }
    settings = make_settings(tmp_path, **values)
    router = SandboxRouter()
    client = TestClient(create_app(settings, FakeGateway(settings), integration_transport=router))
    return client, router


@pytest.fixture
def text():
    channel = FakeText()
    attempts.register(channel)
    yield channel
    attempts.unregister("text")


def _engine(c):
    return c.portal.call(c.app.state.playbooks.run_once)


def _case(c, case_id):
    return c.get(f"/api/cases/{case_id}").json()


def _db(c, fn):
    async def go():
        async with c.app.state.db.sessions() as session:
            out = await fn(session)
            await session.commit()
            return out

    return c.portal.call(go)


def _due(c, case_id, **data):
    """Move the playbook's clock: its waits are over (and patch what it remembers)."""

    async def go(session):
        run = await session.scalar(select(PlaybookRun).where(PlaybookRun.case_id == case_id))
        run.data = {**run.data, **data}
        run.state = "waiting" if run.state == "waiting" else run.state
        run.next_run_at = utcnow() - timedelta(seconds=1)

    _db(c, go)
    c.portal.call(c.app.state.playbooks.advance, case_id)
    return _case(c, case_id)


def _flag_auth(c, appointment: str):
    """The visit's latest 271 says a service needs prior authorization."""

    async def go(session):
        appt = await session.scalar(
            select(EhrRecord).where(
                EhrRecord.resource == "appointment", EhrRecord.source_id == appointment
            )
        )
        session.add(
            EligibilityCheck(
                workspace_id=appt.workspace_id,
                ehr_connection_id=appt.connection_id,
                appointment_ref=appointment,
                patient_ref=appt.patient_ref,
                coverage_ref="70",
                trigger="manual",
                state="done",
                payer_id="60054",
                summary={
                    "coverage": "active",
                    "payer": "AETNA INC",
                    "auth_required_services": ["Diagnostic Medical"],
                },
                completed_at=utcnow(),
            )
        )

    _db(c, go)


def _work_list(c, view="open"):
    return c.get("/api/prior-auth", params={"view": view}).json()


def _provide_documents(c, case_id):
    c.post(
        f"/api/prior-auth/{case_id}/packet",
        json={"item": "order", "ref": "DOC-ORDER-1", "label": "Signed imaging order"},
    ).raise_for_status()
    return c.post(
        f"/api/prior-auth/{case_id}/packet",
        json={"item": "clinical_notes", "ref": "DOC-NOTE-7", "label": "Visit note 2026-11-02"},
    ).json()


def _publish_auth_agent(c):
    agent = next(a for a in c.get("/api/agents").json() if a["template"]["id"] == "auth-status")
    if not agent["current"]:
        c.post(f"/api/agents/{agent['id']}/publish", json={}).raise_for_status()


def _call_result(c, case_id, findings: dict[str, str]):
    """The payer call was reviewed: its findings are recorded and the attempt answered yes."""

    async def go(session):
        attempt = await session.scalar(
            select(ContactAttempt)
            .where(ContactAttempt.case_id == case_id, ContactAttempt.channel == "call")
            .order_by(ContactAttempt.created_at.desc())
        )
        task = await session.get(Task, attempt.task_id)
        task.state = "Completed"
        for field, value in findings.items():
            session.add(
                Finding(
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    attempt_id=attempt.message_id,
                    field=field,
                    value=value,
                    status="reviewed",
                    source="call",
                )
            )
        return attempt.id

    attempt_id = _db(c, go)
    return c.post(f"/api/attempts/{attempt_id}/reply", json={"answer": "yes"}).json()


# --- 1. detect: a 271 flag opens one case, once -----------------------------------------------


def test_a_271_auth_flag_opens_one_case_with_the_facts_and_a_packet_task(tmp_path):
    c, _ = _practice(tmp_path, pas_sandbox=False)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)  # sync the EHR mirror (and run eligibility)
        assert _engine(c)["created"]["auth_required"] == 0  # nothing says auth is needed yet

        _flag_auth(c, "500")
        assert _engine(c)["created"]["auth_required"] == 1
        assert _engine(c)["created"]["auth_required"] == 0  # once per visit
        [row] = [r for r in _work_list(c)["items"] if r["source"] == "prior_auth"]
        case = _case(c, row["id"])
        assert case["case_type"] == "prior_authorization" and case["source"] == "prior_auth"
        assert case["playbook"] == "prior_authorization"
        facts = case["context"]
        assert facts["patient_name"] == "Jane Roe" and facts["member_id"] == "M-ACTIVE"
        assert facts["payer_name"] == "Aetna PPO" and facts["ordering_provider"] == "Dr. Avery Test"
        assert facts["provider_npi"] == "1234567893" and facts["service_date"]
        assert facts["date_of_birth"] == "1980-01-02" and facts["postal_code"] == "60601"
        [x12] = [lk for lk in case["links"] if lk["kind"] == "x12"]
        assert "Diagnostic Medical" in x12["label"] and x12["part"] == "evidence"

        # The packet is missing codes and the clinical documents: blocked, a staff task.
        assert case["status"] == "blocked" and case["owner"]["name"] == "Demo Administrator"
        assert set(row["missing"]) == {
            "CPT code",
            "Diagnosis codes (ICD-10)",
            "Signed order for the service",
            "Recent clinical notes showing medical necessity",
        }
        [task] = c.get("/api/inbox?kind=auth_packet").json()["items"]
        assert task["case"]["id"] == case["id"]
        assert "CPT code, diagnosis codes (ICD-10)" in task["detail"]
        assert c.get("/api/inbox?kind=hand_off").json()["items"] == []

        # Staff fill the gaps; the last document resumes the playbook. With no PAS endpoint,
        # portal or fax, and no payer phone, no route reaches the payer: a hand-off with why.
        c.put(
            f"/api/cases/{case['id']}/facts",
            json={"fields": {"cpt_code": "72148", "icd_codes": "M54.50"}},
        ).raise_for_status()
        provided = _provide_documents(c, case["id"])
        assert provided["missing"] == [] and provided["resumed"]
        after = _case(c, case["id"])
        assert after["status"] == "handed_off"
        assert "No route reaches this payer" in after["status_reason"]
        assert "Portal: not available yet" in after["status_reason"]
        assert "no payer phone" in after["status_reason"]
        assert _engine(c)["created"]["auth_required"] == 0  # still one case for the visit
        made = [r for r in c.get("/api/cases?case_type=prior_authorization").json()]
        assert [r["source"] for r in made].count("prior_auth") == 1


# --- 2. a payer rule → PAS $submit/$inquire against the sandbox → EHR → done -----------------


def _rule_case(c, appointment: str):
    rows = c.get("/api/cases?case_type=prior_authorization").json()
    found = [_case(c, r["id"]) for r in rows if r["source"] == "prior_auth"]
    return next(x for x in found if x["source_ref"].endswith(f":{appointment}"))


def test_a_payer_rule_case_is_approved_through_pas_and_written_back_to_the_ehr(tmp_path, text):
    c, router = _practice(tmp_path, pas_sandbox=True)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)
        rule = c.post(
            "/api/prior-auth-rules",
            json={
                "payer": "Aetna PPO",
                "appointment_type_name": "follow-up",
                "cpt_code": "72148",
                "service_description": "MRI lumbar spine without contrast",
                "icd_codes": ["M54.50"],
                "lead_days": 14,
            },
        ).json()
        # Aetna PPO visits of the follow-up type within 14 days: 500, 501 and 502.
        assert _engine(c)["created"]["auth_required"] == 3
        case = _rule_case(c, "500")
        assert case["context"]["cpt_code"] == "72148"
        assert case["context"]["icd_codes"] == "M54.50"
        assert any(lk["kind"] == "note" and lk["ref"] == rule["id"] for lk in case["links"])
        assert case["status"] == "blocked"  # the order and notes aren't in the EHR mirror

        _provide_documents(c, case["id"])
        submitted = _case(c, case["id"])
        assert submitted["status"] == "in_progress", submitted["playbook_run"]
        assert submitted["context"]["auth_status"] == "pending_review"
        assert submitted["context"]["submitted_on"] == utcnow().date().isoformat()
        [sent] = [lk for lk in submitted["links"] if lk["kind"] == "fhir"]
        assert sent["label"] == "Submitted by PAS: pending review (A4)"
        assert submitted["playbook_run"]["steps"][2]["state"] == "current"  # tracking

        # The first $inquire after the wait: the sandbox approves 72148 for 90 days.
        tracked = _due(c, case["id"], next_check_at=utcnow().isoformat())
        auth_ref = tracked["context"]["known_auth_reference"]
        assert auth_ref.startswith("SYN-PA-S") and tracked["context"]["auth_status"] == "approved"
        assert tracked["context"]["auth_valid_from"] == utcnow().date().isoformat()
        calls = _db(c, lambda s: _all(s, IntegrationCall))
        ops = [x.operation for x in calls if x.operation.startswith("pas.")]
        assert ops == ["pas.submit", "pas.inquire"]
        assert all("Jane" not in str(x.summary) for x in calls)  # outcome codes only

        # The auth number goes to the chart as a note; EHR writes wait for an administrator.
        assert tracked["status"] == "in_progress"
        [write] = c.get("/api/inbox?kind=ehr_write").json()["items"]
        assert case["case_ref"] in write["title"]
        decision = {"kind": "ehr_write", "id": write["id"], "decision": "approve"}
        c.post("/api/inbox/decide", json=decision).raise_for_status()
        alert = next(
            b for b in router.nexhealth.bodies if b[0] == "POST" and b[1].endswith("/alerts")
        )
        assert auth_ref in alert[2]["patient_alert"]["note"]

        done = _due(c, case["id"])
        assert done["status"] == "done", done["playbook_run"]
        assert done["contract"]["holds"]
        assert done["system_of_record"]["system"] == "ehr"
        assert done["system_of_record"]["ack"].endswith(f":{auth_ref}")
        # The visit is inside the approved dates: the patient isn't texted.
        assert text.notices == [] and done["nothing_left"]
        row = next(r for r in _work_list(c, "done")["items"] if r["id"] == case["id"])
        assert row["auth_number"] == auth_ref and row["route"] == "pas"


async def _all(session, model):
    return list(await session.scalars(select(model)))


# --- 3. pended and stale → a follow-up call by the auth-status agent --------------------------


def test_a_stale_pended_request_gets_a_follow_up_call_and_the_call_decides_it(tmp_path):
    c, _ = _practice(tmp_path, pas_sandbox=True)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _publish_auth_agent(c)
        _worker(c)
        c.post(
            "/api/prior-auth-rules",
            json={
                "appointment_type_name": "Follow-up",
                "cpt_code": "95810",  # the sandbox keeps this one pended
                "service_description": "Sleep study, attended",
                "icd_codes": ["G47.33"],
            },
        ).raise_for_status()
        _engine(c)
        case = _rule_case(c, "500")
        c.put(
            f"/api/cases/{case['id']}/facts", json={"fields": {"payer_phone": APPROVED}}
        ).raise_for_status()
        _provide_documents(c, case["id"])

        pended = _due(c, case["id"], next_check_at=utcnow().isoformat())
        assert pended["context"]["auth_status"] == "pending_review"
        assert pended["status"] == "in_progress" and pended["attempts"] == []

        # Three days without a decision: the auth-status agent calls the payer.
        stale = (utcnow() - timedelta(days=4)).isoformat()
        called = _due(c, case["id"], next_check_at=utcnow().isoformat(), stale_from=stale)
        [call] = called["attempts"]
        assert (call["channel"], call["audience"], call["state"]) == ("call", "payer", "sent")
        assert call["task_id"] and called["status"] == "in_progress"

        decided = _call_result(
            c,
            case["id"],
            {
                "reported_status": "approved",
                "authorization_reference": "SYN-PA-CALL-1",
                "valid_from": utcnow().date().isoformat(),
                "valid_to": (utcnow() + timedelta(days=60)).date().isoformat(),
            },
        )
        assert decided["context"]["auth_status"] == "approved"
        assert decided["context"]["known_auth_reference"] == "SYN-PA-CALL-1"
        assert any(lk["kind"] == "task" for lk in decided["links"])  # the call is evidence
        assert decided["playbook_run"]["steps"][4]["state"] == "current"  # the chart note waits


# --- 4. no PAS endpoint: a payer call; denied → hand-off; the contract holds the line ----------


def test_without_pas_the_payer_is_called_and_a_denial_is_handed_off(tmp_path, text):
    settings = make_settings(
        tmp_path, run_background=False, payer_sandbox_url="", playbook_contact_hours=False
    )
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        _publish_auth_agent(c)
        made = c.post(
            "/api/playbooks/prior_authorization/trigger",
            json={
                "fields": {
                    "patient_name": "Synthetic Patient Kappa",
                    "date_of_birth": "1962-04-05",
                    "member_id": "99123456",
                    "postal_code": "60601",
                    "provider_name": "Riverside Family Medicine (synthetic)",
                    "provider_npi": "9100012345",
                    "payer_name": "Harbor Mutual Health (synthetic)",
                    "payer_phone": APPROVED,
                    "service_description": "Outpatient physical therapy evaluation",
                    "cpt_code": "97161",
                    "units": 1,
                    "urgency": "standard",
                    "icd_codes": "M25.561",
                    "ordering_provider": "Dr. Rowan Synthetic",
                    "service_date": (utcnow() + timedelta(days=9)).date().isoformat(),
                }
            },
        ).json()
        assert made["status"] == "blocked"  # no EHR: the documents come from staff
        _provide_documents(c, made["id"])
        calling = _case(c, made["id"])
        [call] = calling["attempts"]
        assert call["channel"] == "call" and call["audience"] == "payer"
        assert calling["context"]["submitted_on"] == utcnow().date().isoformat()

        denied = _call_result(
            c,
            made["id"],
            {"reported_status": "denied", "missing_requirements": "Coverage criteria not met"},
        )
        assert denied["status"] == "handed_off"
        assert denied["status_reason"] == (
            "The payer denied the prior auth: Coverage criteria not met."
        )
        assert "call the patient" in denied["next_step"]  # no number to text on a manual case
        assert denied["owner"]["name"] == "Demo Administrator"
        # §2: a denial isn't done; the contract names what's missing.
        refused = c.post(f"/api/cases/{made['id']}/status", json={"status": "done"})
        assert refused.status_code == 409
        assert "system_of_record" in refused.json()["error"]["detail"]["missing"]
        [waiting] = c.get("/api/inbox?kind=hand_off").json()["items"]
        assert waiting["case"]["id"] == made["id"]
        row = next(r for r in _work_list(c)["items"] if r["id"] == made["id"])
        assert row["auth_status"] == "denied" and row["route"] == "call"


# --- 5. the visit is affected: the patient is told through the channel hook ------------------


def test_a_denial_affects_the_visit_so_the_patient_is_texted_without_details(tmp_path, text):
    c, _ = _practice(tmp_path, pas_sandbox=True)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)
        c.post(
            "/api/prior-auth-rules",
            json={
                "appointment_type_name": "Follow-up",
                "cpt_code": "97161",  # the sandbox denies this one
                "service_description": "Outpatient physical therapy evaluation",
                "icd_codes": ["M25.561"],
            },
        ).raise_for_status()
        _engine(c)
        case = _rule_case(c, "500")
        _provide_documents(c, case["id"])
        denied = _due(c, case["id"], next_check_at=utcnow().isoformat())
        assert (
            denied["status"] == "handed_off"
            and "Coverage criteria not met" in (denied["status_reason"])
        )
        [(who, words)] = text.notices
        assert who == "J. R." and "Please call us about your upcoming visit" in words
        assert "denied" not in words.lower() and "auth" not in words.lower()
        told = [lk for lk in denied["links"] if lk["part"] == "informed"]
        assert [(lk["audience"], lk["channel"]) for lk in told] == [("patient", "text")]
        assert [a["state"] for a in denied["attempts"]] == ["notified"]


def test_a_visit_too_close_for_a_pending_auth_goes_to_the_front_desk(tmp_path, text):
    c, _ = _practice(tmp_path, pas_sandbox=True)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)
        c.post(
            "/api/prior-auth-rules",
            json={
                "appointment_type_name": "Follow-up",
                "cpt_code": "95810",  # stays pended
                "service_description": "Sleep study, attended",
                "icd_codes": ["G47.33"],
            },
        ).raise_for_status()
        _engine(c)
        case = _rule_case(c, "502")  # 20 hours away
        assert (
            "Postal code"
            in next(r for r in _work_list(c)["items"] if r["id"] == case["id"])["missing"]
        )  # patient 2 has no address on file
        c.put(
            f"/api/cases/{case['id']}/facts", json={"fields": {"postal_code": "60601"}}
        ).raise_for_status()
        _provide_documents(c, case["id"])
        guarded = _case(c, case["id"])
        assert guarded["status"] == "handed_off"
        assert guarded["status_reason"] == (
            "The visit is close and the payer hasn't decided the prior auth."
        )
        # Patient 2 has no number on file: staff call them.
        assert text.notices == [] and "call them" in guarded["next_step"]
        # Resuming picks the tracking back up instead of re-submitting.
        resumed = c.post(f"/api/cases/{case['id']}/playbook/resume").json()
        current = next(s for s in resumed["playbook_run"]["steps"] if s["state"] == "current")
        assert current["kind"] == "track_auth"


# --- 6. the PAS shapes ----------------------------------------------------------------------


def test_the_submit_bundle_carries_codes_provider_date_and_documents():
    case = {
        "patient": {
            "display_name": "Synthetic Patient Mu",
            "member_id": "99000001",
            "date_of_birth": "1970-01-01",
        },
        "payer": {"name": "Northwind Health Plan (synthetic)"},
        "provider": {"name": "Riverside Family Medicine (synthetic)", "npi": "9100011111"},
        "service": {
            "cpt_code": "72148",
            "units": 1,
            "icd_codes": "M54.50, M51.16",
            "ordering_provider": "Dr. Rowan Synthetic",
            "date": "2026-12-01",
        },
        "request": {"submitted_on": "2026-11-16", "urgency": "standard"},
    }
    bundle = pas.build_submit_bundle(case, "pa:test", [{"label": "Signed order", "ref": "D1"}])
    claim = bundle["entry"][0]["resource"]
    assert claim["use"] == "preauthorization"
    assert [d["diagnosisCodeableConcept"]["coding"][0]["code"] for d in claim["diagnosis"]] == [
        "M54.50",
        "M51.16",
    ]
    assert claim["item"][0]["servicedDate"] == "2026-12-01"
    assert claim["item"][0]["diagnosisSequence"] == [1, 2]
    assert claim["careTeam"][0]["provider"]["display"] == "Dr. Rowan Synthetic"
    assert claim["supportingInfo"][0]["valueReference"]["display"] == "Signed order"


@pytest.mark.parametrize(
    ("cpt", "status", "final"),
    [
        ("72148", "approved", True),
        ("97161", "denied", True),
        ("95810", "pending_review", False),
        ("93306", "pending_additional_information", False),
        ("73721", "unknown", False),
    ],
)
def test_the_sandbox_pends_a_submission_then_decides_it_by_cpt(cpt, status, final):
    case = {
        "patient": {
            "display_name": "Synthetic Patient Nu",
            "member_id": "99000002",
            "date_of_birth": "1970-01-01",
        },
        "payer": {"name": "Payer (synthetic)"},
        "provider": {"name": "Practice (synthetic)", "npi": "9100000000"},
        "service": {"cpt_code": cpt, "units": 1},
        "request": {"submitted_on": "2026-11-16", "urgency": "standard"},
    }
    tracking = f"pa:{cpt}-test"
    with TestClient(payer_sandbox.app) as sandbox:
        submit = sandbox.post("/fhir/Claim/$submit", json=pas.build_submit_bundle(case, tracking))
        first = pas.parse_inquiry_response(submit.json())
        assert (first.reported_status, first.review_action) == ("pending_review", "A4")
        inquiry = sandbox.post(
            "/fhir/Claim/$inquire", json=pas.build_inquiry_bundle(case, tracking)
        )
        answer = pas.parse_inquiry_response(inquiry.json())
    assert (answer.reported_status, answer.final) == (status, final)
    assert answer.authorization_reference == first.authorization_reference
    assert bool(answer.valid_from) == (status == "approved")


def test_case_links_never_carry_patient_names(tmp_path):
    """Evidence labels are codes and outcomes only (the PHI rule for case links)."""
    c, _ = _practice(tmp_path, pas_sandbox=False)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)
        _flag_auth(c, "500")
        _engine(c)
        labels = [lk.label for lk in _db(c, lambda s: _all(s, CaseLink))]
        assert labels and not any("Jane" in label or "Roe" in label for label in labels)
