"""Person memory: profiles, the verification and proxy policy on a call, memory read and
write-back, and the ranking contact-many uses. Everyone is synthetic (the sandbox agency)."""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from careplix_api.db import utcnow
from careplix_api.people import policy
from careplix_api.people.models import Observation, Person, VerificationEvent

from .test_practice import _client, _db_rows, _worker
from .test_receptionist import JANE, _call

CHICAGO = ZoneInfo("America/Chicago")
LUCIA = {"first_name": "Lucia", "last_name": "Sample-Delgado", "date_of_birth": "1970-02-14"}
ROSA = {"first_name": "Rosa", "last_name": "Sample-Delgado", "date_of_birth": "1944-12-03"}
HAROLD = {"first_name": "Harold", "last_name": "Synthetic-Brooks", "date_of_birth": "1938-04-11"}
MARTIN = {"first_name": "Martin", "last_name": "Synthetic-Brooks", "date_of_birth": "1965-10-01"}
NORA = {"first_name": "Nora", "last_name": "Test-Hughes", "date_of_birth": "1943-03-30"}
WALTER = {"first_name": "Walter", "last_name": "Test-Hughes", "date_of_birth": "1941-08-28"}


@pytest.fixture
def agency(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login").raise_for_status()
        _worker(c)  # mirror the practice (for the EHR-linked patient tests)
        added = c.post("/api/people/samples").json()
        assert added["added"] > 10
        assert c.post("/api/people/samples").json()["added"] == 0  # once only
        yield c


def _people(c, **params):
    return {p["name"]: p for p in c.get("/api/people", params=params).json()}


def _id(c, name):
    rows = c.get("/api/people").json()
    return next(p["id"] for p in rows if p["name"] == name)


def _shift(weeks_ahead=0, weekday=0, start=9, hours=4):
    """A local Chicago shift on the next given weekday (Monday by default)."""
    today = utcnow().astimezone(CHICAGO).date()
    day = today + timedelta(days=(weekday - today.weekday()) % 7 or 7, weeks=weeks_ahead)
    begin = datetime(day.year, day.month, day.day, start, tzinfo=CHICAGO)
    return begin.isoformat(), (begin + timedelta(hours=hours)).isoformat()


# --- policy, pure ------------------------------------------------------------------------------


def _rel(**kw):
    base = dict(proxy=True, scope=["schedule"], revoked_at=None, starts_on=None, expires_on=None)
    return SimpleNamespace(**{**base, **kw})


def test_proxy_needs_an_active_in_scope_unexpired_unrevoked_authorisation():
    today = date(2026, 11, 16)
    assert policy.proxy_decision([], "schedule", today).reason == "no_relationship"
    assert policy.proxy_decision([_rel(proxy=False)], "schedule", today).authorized is False
    ok = policy.proxy_decision([_rel(expires_on=today)], "schedule", today)
    assert ok.authorized and ok.scope == ("schedule",)
    expired = _rel(expires_on=today - timedelta(days=1))
    assert policy.proxy_decision([expired], "schedule", today).reason == "expired"
    future = _rel(starts_on=today + timedelta(days=1))
    assert policy.proxy_decision([future], "schedule", today).reason == "not_started"
    revoked = _rel(revoked_at=datetime(2026, 11, 1))
    assert policy.proxy_decision([revoked], "schedule", today).reason == "revoked"
    assert policy.proxy_decision([_rel()], "billing", today).reason == "out_of_scope"
    assert policy.proxy_decision([_rel(scope=["all"])], "records", today).authorized
    # One valid authorisation is enough even beside a lapsed one.
    assert policy.proxy_decision([expired, _rel()], "schedule", today).authorized


def test_policy_needs_two_real_identifiers_and_a_distinct_step_up():
    with pytest.raises(policy.PolicyError):
        policy.clean_policy({"identifiers": ["name"]})
    with pytest.raises(policy.PolicyError):
        policy.clean_policy({"identifiers": ["name", "caller_phone"]})
    with pytest.raises(policy.PolicyError):
        policy.clean_policy({"identifiers": ["name", "zip"], "step_up_identifiers": ["zip"]})
    rules = policy.clean_policy({"identifiers": ["name", "date_of_birth", "zip"]}, {"version": 3})
    assert rules["version"] == 4 and rules["max_attempts"] == 2


def test_do_not_contact_wins_over_everything():
    person = SimpleNamespace(
        do_not_contact=True,
        status="active",
        consent={"sms": {"state": "granted"}},
        contact_windows=[],
        timezone="America/Chicago",
    )
    assert policy.can_contact(person, "sms", utcnow()).reason == "do_not_contact"
    person.do_not_contact = False
    assert policy.can_contact(person, "sms", utcnow()).allowed
    person.consent = {"sms": {"state": "unknown"}}
    assert policy.can_contact(person, "sms", utcnow()).reason == "no_consent"
    assert policy.can_contact(person, "voice", utcnow()).allowed
    person.consent = {"voice": {"state": "revoked"}}
    assert policy.can_contact(person, "voice", utcnow()).reason == "consent_revoked"


# --- ranking ------------------------------------------------------------------------------------


def test_ranking_for_a_client_shift_skips_expired_credentials_and_do_not_contact(agency):
    c = agency
    starts, ends = _shift()
    out = c.post(
        "/api/people/rank",
        json={
            "starts_at": starts,
            "ends_at": ends,
            "skills": ["personal_care"],
            "client_id": _id(c, "H. S."),
        },
    ).json()
    eligible = [e["name"] for e in out["eligible"]]
    excluded = {e["name"]: e for e in out["excluded"]}
    # The client's preferred, most reliable caregiver, nearby, comes first.
    assert eligible[0] == "Ana Synthetic-Reyes"
    assert "client's preferred caregiver" in out["eligible"][0]["reasons"]
    assert out["eligible"][0]["channel"] == "sms"
    assert {"Ben Sample-Okafor", "Finn Sample-Murphy"} <= set(eligible)
    assert excluded["Dev Test-Patel"]["reason"] == "expired_credential"
    assert excluded["Dev Test-Patel"]["detail"] == "cpr"
    assert excluded["Gia Example-Rossi"]["reason"] == "do_not_contact"
    assert excluded["Eve Synthetic-Kowalski"]["reason"] == "unavailable"
    assert excluded["Chloe Example-Nguyen"]["reason"] == "missing_skill"
    assert all(e["score"] > 0 for e in out["eligible"])


def test_a_credential_expiring_before_the_shift_ends_excludes_the_caregiver(agency):
    c = agency
    need = {"skills": ["skilled_nursing"], "credentials": ["rn_license"]}
    soon = dict(zip(("starts_at", "ends_at"), _shift(weekday=2), strict=True))
    later = dict(zip(("starts_at", "ends_at"), _shift(weeks_ahead=5, weekday=2), strict=True))
    first = c.post("/api/people/rank", json={**need, **soon, "lat": 41.95, "lon": -87.66}).json()
    assert [e["name"] for e in first["eligible"]] == ["Chloe Example-Nguyen"]
    second = c.post("/api/people/rank", json={**need, **later, "lat": 41.95, "lon": -87.66}).json()
    assert second["eligible"] == []
    chloe = next(e for e in second["excluded"] if e["name"] == "Chloe Example-Nguyen")
    assert chloe["reason"] == "expired_credential" and chloe["detail"] == "rn_license"


def test_a_declined_caregiver_is_never_offered_to_that_client(agency):
    c = agency
    starts, ends = _shift()
    out = c.post(
        "/api/people/rank",
        json={
            "starts_at": starts,
            "ends_at": ends,
            "skills": ["hoyer_lift"],
            "client_id": _id(c, "W. T."),
        },
    ).json()
    excluded = {e["name"]: e["reason"] for e in out["excluded"]}
    assert excluded["Ben Sample-Okafor"] == "declined_by_client"
    # The only other caregiver with that skill lives just beyond how far they'll travel.
    assert excluded["Finn Sample-Murphy"] == "too_far" and out["eligible"] == []


def test_ranking_rejects_a_need_that_ends_before_it_starts(agency):
    starts, ends = _shift()
    bad = agency.post("/api/people/rank", json={"starts_at": ends, "ends_at": starts})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_need"


# --- profiles ----------------------------------------------------------------------------------


def test_profiles_are_synthetic_and_the_list_hides_patient_identity(agency):
    c = agency
    rows = _people(c)
    assert "Ana Synthetic-Reyes" in rows  # workforce: full name
    assert "R. S." in rows and rows["R. S."]["kind"] == "client"  # clients: initials only
    real = c.post(
        "/api/people", json={"kind": "caregiver", "profile": {"first_name": "Jo", "last_name": "Q"}}
    )
    assert real.status_code == 422 and real.json()["error"]["code"] == "synthetic_only"
    made = c.post(
        "/api/people",
        json={
            "kind": "caregiver",
            "profile": {
                "first_name": "Kim",
                "last_name": "Test-Lee",
                "date_of_birth": "1990-01-01",
            },
            "skills": ["companionship"],
            "availability": [{"days": [0, 1], "start": "09:00", "end": "17:00"}],
        },
    ).json()
    assert made["name"] == "Kim Test-Lee" and made["availability"][0]["days"] == [0, 1]
    bad_window = c.put(
        f"/api/people/{made['id']}",
        json={"availability": [{"days": [0], "start": "17:00", "end": "09:00"}]},
    )
    assert bad_window.status_code == 422
    cred = c.post(
        f"/api/people/{made['id']}/credentials",
        json={"kind": "cpr", "expires_on": "2026-01-01"},
    ).json()
    assert cred["credentials"][0]["state"] == "expired"
    assert c.post(f"/api/people/{made['id']}/credentials", json={"kind": "x"}).status_code == 422
    dnc = c.put(f"/api/people/{made['id']}", json={"do_not_contact": True}).json()
    assert dnc["do_not_contact"] and dnc["do_not_contact_reason"] == "Set by staff"
    assert c.get("/api/people/not-a-person").status_code == 404


def test_a_representative_needs_scope_and_evidence_and_can_be_revoked(agency):
    c = agency
    rosa, ana = _id(c, "R. S."), _id(c, "Ana Synthetic-Reyes")
    kim = c.post(
        "/api/people",
        json={"kind": "family", "profile": {"first_name": "Kim", "last_name": "Sample-Delgado"}},
    ).json()
    url = f"/api/people/{rosa}/relationships"
    no_scope = c.post(url, json={"related_id": kim["id"], "relation": "child", "proxy": True})
    assert no_scope.json()["error"]["code"] == "scope_required"
    no_evidence = c.post(
        url,
        json={"related_id": kim["id"], "relation": "child", "proxy": True, "scope": ["general"]},
    )
    assert no_evidence.json()["error"]["code"] == "evidence_required"
    saved = c.post(
        url,
        json={
            "related_id": kim["id"],
            "relation": "child",
            "proxy": True,
            "scope": ["general"],
            "evidence": "Form on file (synthetic)",
            "expires_on": "2099-01-01",
        },
    ).json()
    rel = next(r for r in saved["relationships"] if r["other_id"] == kim["id"])
    assert rel["state"] == "active" and rel["scope"] == ["general"]
    revoked = c.post(f"/api/people/{rosa}/relationships/{rel['id']}/revoke").json()
    assert next(r for r in revoked["relationships"] if r["id"] == rel["id"])["state"] == "revoked"
    # Caregiver fit links a client to a caregiver only.
    bad_fit = c.post(url, json={"related_id": kim["id"], "relation": "preferred_caregiver"})
    assert bad_fit.json()["error"]["code"] == "fit_needs_caregiver"
    fit = c.post(url, json={"related_id": ana, "relation": "preferred_caregiver"})
    assert fit.status_code == 200


def test_the_verification_policy_is_validated_and_versioned(agency):
    c = agency
    assert c.get("/api/people/policy").json()["identifiers"] == ["name", "date_of_birth"]
    weak = {
        "identifiers": ["name", "caller_phone"],
        "step_up_identifiers": ["zip"],
        "step_up_topics": ["billing"],
        "max_attempts": 2,
        "proxy_subject_identifiers": ["name", "date_of_birth"],
    }
    refused = c.put("/api/people/policy", json=weak)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "invalid_policy"
    saved = c.put(
        "/api/people/policy",
        json={
            **weak,
            "identifiers": ["name", "date_of_birth", "zip"],
            "step_up_identifiers": ["phone_last4"],
        },
    ).json()
    assert saved["version"] == 2 and saved["identifiers"] == ["name", "date_of_birth", "zip"]


# --- on a call ----------------------------------------------------------------------------------


def _checks(c, check=None):
    rows = _db_rows(c, VerificationEvent)
    return [r for r in rows if check is None or r.check == check]


def test_nothing_is_shared_before_the_caller_is_verified(agency):
    c = agency
    call = _call(c)
    refused = call.tool("get_person_memory", topic="general")
    assert refused["state"] == "rejected" and refused["result"]["error"] == "not_verified"
    assert call.tool("check_proxy", topic="schedule", **ROSA)["result"]["error"] == "not_verified"
    stranger = {"first_name": "Nobody", "last_name": "Synthetic", "date_of_birth": "1950-01-01"}
    assert call.ok("verify_person", **stranger) == {"verified": False}
    assert call.ok("verify_person", **{**LUCIA, "date_of_birth": "1970-02-15"}) == {
        "verified": False
    }
    assert call.tool("verify_person", **LUCIA)["result"]["error"] == "verification_limit"
    failed = _checks(c, "identity")
    assert [r.outcome for r in failed] == ["failed", "failed"]
    assert failed[0].identifiers == ["name", "date_of_birth"]


def test_a_representative_reads_only_in_scope_memory_with_step_up_for_billing(agency):
    c = agency
    call = _call(c)
    assert call.ok("verify_person", **LUCIA) == {"verified": True, "level": "basic"}
    proxy = call.ok("check_proxy", topic="schedule", **ROSA)
    assert proxy["authorized"] and proxy["scope"] == ["general", "schedule", "billing"]

    memory = call.ok("get_person_memory", topic="schedule")
    facts = memory["facts"]
    assert facts["about"] == "the person they represent" and facts["first_name"] == "Rosa"
    assert facts["language"] == "es" and facts["preferred_caregivers"] == ["Ana"]
    assert any("Spanish" in n["text"] for n in facts["notes"])
    text = json.dumps(memory)
    for secret in ("1944", "555", "60625", "Delgado", "41.96"):
        assert secret not in text  # no birth date, phone, ZIP, surname or home location

    # Billing is a step-up topic: ask for one more identifier first.
    billing = call.ok("get_person_memory", topic="billing")
    assert billing["state"] == "step_up_required" and "zip code" in billing["message"].lower()
    assert call.ok("verify_person", zip="60625") == {"verified": True, "level": "step_up"}
    assert call.ok("get_person_memory", topic="billing")["state"] == "ok"
    # Records were never authorised for Lucia.
    assert call.ok("get_person_memory", topic="records")["state"] == "not_permitted"
    disclosures = {(r.topic, r.outcome, r.reason) for r in _checks(c, "disclosure")}
    assert ("records", "refused", "out_of_scope") in disclosures
    assert ("billing", "refused", "step_up_required") in disclosures
    assert ("schedule", "passed", None) in disclosures


@pytest.mark.parametrize(
    "caller,subject,reason",
    [(MARTIN, HAROLD, "expired"), (NORA, WALTER, "revoked")],
    ids=["expired", "revoked"],
)
def test_an_expired_or_revoked_representative_gets_nothing(agency, caller, subject, reason):
    c = agency
    call = _call(c)
    assert call.ok("verify_person", **caller)["verified"]
    out = call.ok("check_proxy", topic="schedule", **subject)
    assert out["authorized"] is False and "can't share anything" in out["message"]
    assert reason not in json.dumps(out)  # the caller isn't told why
    # Still only the caller's own memory: the subject was never bound.
    mine = call.ok("get_person_memory", topic="general")
    assert mine["facts"]["about"] == "the caller"
    assert mine["facts"]["first_name"] == caller["first_name"]
    [check] = _checks(c, "proxy")
    assert check.outcome == "refused" and check.reason == reason and check.subject_id


def test_a_note_from_the_call_keeps_its_source_and_stop_contact_is_respected(agency):
    c = agency
    call = _call(c)
    assert call.ok("verify_person", **HAROLD)["verified"]
    kept = call.ok("remember_person_note", topic="contact", text="Prefers calls after 10am.")
    assert kept == {"state": "recorded", "stop_contact": False}
    stop = call.ok(
        "remember_person_note",
        topic="contact",
        text="Asked not to be called again.",
        stop_contact=True,
    )
    assert stop["stop_contact"] is True
    notes = [o for o in _db_rows(c, Observation) if o.source == "call"]
    assert {o.source_attempt_id for o in notes} == {call.claim["attempt_id"]}
    harold = c.get(f"/api/people/{_id(c, 'H. S.')}").json()
    assert harold["do_not_contact"] is True
    assert call.claim["attempt_id"][:8] in harold["do_not_contact_reason"]
    assert any(o["source_attempt_id"] == call.claim["attempt_id"] for o in harold["observations"])
    # The next outreach list for a slot leaves him out.
    starts, ends = _shift()
    ranked = c.post(
        "/api/people/rank",
        json={"starts_at": starts, "ends_at": ends, "kinds": ["client", "patient"]},
    ).json()
    assert {"H. S.": "do_not_contact"}.items() <= {
        e["name"]: e["reason"] for e in ranked["excluded"]
    }.items()


def test_patients_link_to_the_practice_mirror_without_a_copy(agency):
    c = agency
    linked = c.post("/api/people/link-ehr", json={}).json()["linked"]
    assert linked >= 1
    assert c.post("/api/people/link-ehr", json={}).json()["linked"] == 0
    jane = next(p for p in _db_rows(c, Person) if p.ehr_patient_ref == "1")
    assert jane.pii is None  # name and birth date stay in the mirror
    detail = c.get(f"/api/people/{jane.id}").json()
    assert detail["ehr"]["found"] and detail["name"] == "Jane Roe"

    # verify_person reads identity from the mirror and opens the appointment tools.
    call = _call(c)
    assert call.ok("verify_person", **JANE)["verified"]
    assert call.ok("list_appointments")["appointments"]


def test_find_patient_is_enough_to_read_a_linked_patients_memory(agency):
    c = agency
    c.post("/api/people/link-ehr", json={}).raise_for_status()
    other = _call(c)
    assert other.ok("find_patient", **JANE) == {"verified": True}
    assert other.ok("get_person_memory", topic="general")["facts"]["first_name"] == "Jane"
    assert any(r.reason == "find_patient" for r in _checks(c, "identity"))


def test_another_workspaces_people_are_invisible(agency):
    c = agency
    from careplix_api.models import Workspace
    from careplix_api.phi import PhiBox

    async def other_workspace():
        async with c.app.state.db.sessions() as session:
            ws = Workspace(name="Other agency (synthetic)")
            session.add(ws)
            await session.flush()
            box = PhiBox(c.app.state.settings)
            person = Person(
                workspace_id=ws.id,
                kind="client",
                pii=box.seal({**ROSA, "first_name": "Olga", "lat": 41.97, "lon": -87.66}),
            )
            session.add(person)
            await session.commit()
            return person.id

    foreign = c.portal.call(other_workspace)
    assert c.get(f"/api/people/{foreign}").status_code == 404
    assert foreign not in {p["id"] for p in c.get("/api/people").json()}
    starts, ends = _shift()
    ranked = c.post(
        "/api/people/rank", json={"starts_at": starts, "ends_at": ends, "client_id": foreign}
    )
    assert ranked.status_code == 404
    rosa = _id(c, "R. S.")
    linked = c.post(
        f"/api/people/{rosa}/relationships", json={"related_id": foreign, "relation": "child"}
    )
    assert linked.status_code == 404
    call = _call(c)
    olga = {**ROSA, "first_name": "Olga"}
    assert call.ok("verify_person", **olga) == {"verified": False}
