"""Care opportunities: rules over the record, a clinician approval queue, then outreach."""

from datetime import date, timedelta

from careplix_api.models import AuditEvent
from careplix_api.opportunities import rules
from careplix_api.opportunities.models import ClinicalFact

from .test_practice import _db_rows

TODAY = date(2026, 11, 11)  # a Wednesday


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def programs(facts):
    return {o["program"] for o in rules.evaluate(facts, TODAY)}


def test_two_chronic_conditions_qualify_for_ccm_and_monitoring():
    facts = rules.Facts(
        "p1",
        age=72,
        sex="F",
        coverage="medicare",
        conditions=[{"code": "E11.9"}, {"code": "I10"}],
        services=[{"code": "G0439", "date": ago(400)}, {"code": "83036", "date": ago(30)}],
    )
    found = programs(facts)
    assert {"ccm", "rpm", "awv", "acp", "colorectal", "mammogram"} <= found
    assert "pcm" not in found and "a1c" not in found  # A1c done a month ago
    ccm = next(o for o in rules.evaluate(facts, TODAY) if o["program"] == "ccm")
    assert ccm["consent_required"] and ccm["staff_time"] and ccm["priority"] == 1
    assert "Hypertension" in ccm["why"][0] and "Type 2 diabetes" in ccm["why"][0]


def test_enrolled_patients_and_recent_services_are_not_proposed_again():
    facts = rules.Facts(
        "p2",
        age=70,
        coverage="medicare",
        conditions=[{"code": "I50.22"}, {"code": "N18.3"}],
        services=[
            {"code": "99490", "date": ago(20)},
            {"code": "99454", "date": ago(10)},
            {"code": "G0439", "date": ago(100)},
        ],
    )
    found = programs(facts)
    assert not {"ccm", "pcm", "rpm", "awv"} & found


def test_one_high_risk_condition_is_principal_care_not_ccm():
    facts = rules.Facts("p3", age=70, coverage="medicare", conditions=[{"code": "J44.9"}])
    found = programs(facts)
    assert "pcm" in found and "ccm" not in found


def test_a_recent_discharge_needs_contact_in_two_business_days():
    friday = TODAY - timedelta(days=5)  # discharged Friday 6 Nov
    facts = rules.Facts(
        "p4", age=80, coverage="medicare", discharges=[{"date": friday.isoformat()}]
    )
    tcm = next(o for o in rules.evaluate(facts, TODAY) if o["program"] == "tcm")
    assert tcm["priority"] == 1 and tcm["due_by"] == "2026-11-10"  # Monday, Tuesday
    old = rules.Facts("p5", age=80, coverage="medicare", discharges=[{"date": ago(30)}])
    assert "tcm" not in programs(old)


def test_behavioral_health_follows_a_positive_screen():
    facts = rules.Facts(
        "p6",
        age=66,
        coverage="medicare",
        screenings=[{"kind": "phq9", "score": "14", "date": ago(10)}],
    )
    bhi = next(o for o in rules.evaluate(facts, TODAY) if o["program"] == "bhi")
    assert "PHQ9 score 14" in bhi["why"][0]
    low = rules.Facts("p7", coverage="medicare", screenings=[{"kind": "phq9", "score": "4"}])
    assert "bhi" not in programs(low)


def test_commercial_patients_get_screenings_but_not_medicare_programs():
    facts = rules.Facts(
        "p8",
        age=58,
        sex="M",
        coverage="commercial",
        conditions=[{"code": "E11.65"}, {"code": "F17.210"}],
    )
    found = programs(facts)
    assert {"colorectal", "a1c", "tobacco"} <= found
    assert not {"awv", "ccm", "rpm", "dsmt_mnt"} & found


# --- the queue ------------------------------------------------------------------------------------


def test_the_sample_fills_a_queue_a_clinician_works(client):
    loaded = client.post("/api/care-opportunities/sample")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["imported"]["patients"] == 5
    assert loaded.json()["scan"]["created"] > 5

    queue = client.get("/api/care-opportunities").json()
    assert queue["patients"] == 5
    by = {(o["patient_ref"], o["program"]): o for o in queue["items"]}
    assert ("SYN-P001", "ccm") in by and ("SYN-P002", "tcm") in by and ("SYN-P003", "bhi") in by
    assert by[("SYN-P001", "ccm")]["patient"] == "M. S."  # initials only
    assert ("SYN-P005", "awv") not in by  # done 90 days ago

    # Scanning again changes nothing.
    again = client.post("/api/care-opportunities/scan").json()
    assert again["created"] == 0

    ccm = by[("SYN-P001", "ccm")]
    early = client.post(
        f"/api/care-opportunities/{ccm['id']}/outcome", json={"outcome": "enrolled"}
    )
    assert early.json()["error"]["code"] == "not_approved"  # approval comes first

    approved = client.post(
        f"/api/care-opportunities/{ccm['id']}/decide", json={"decision": "approve"}
    )
    assert approved.status_code == 200, approved.text
    enrolled = client.post(
        f"/api/care-opportunities/{ccm['id']}/outcome",
        json={"outcome": "enrolled", "note": "Consent recorded"},
    )
    assert enrolled.status_code == 200, enrolled.text
    assert next(o for o in enrolled.json()["items"] if o["id"] == ccm["id"])["status"] == "enrolled"

    mam = by[("SYN-P003", "mammogram")]
    no_reason = client.post(
        f"/api/care-opportunities/{mam['id']}/decide", json={"decision": "decline"}
    )
    assert no_reason.json()["error"]["code"] == "reason_required"
    declined = client.post(
        f"/api/care-opportunities/{mam['id']}/decide",
        json={"decision": "decline", "note": "Done at an outside imaging centre"},
    )
    assert declined.status_code == 200
    assert client.post("/api/care-opportunities/scan").json()["created"] == 0  # not reopened

    actions = {a.action for a in _db_rows(client, AuditEvent)}
    assert {
        "care.facts_imported",
        "care.scanned",
        "care.approved",
        "care.declined",
        "care.outcome",
    } <= actions
    stored = _db_rows(client, ClinicalFact)
    # Sealed at rest: a Fernet token (base64, never a quote), not JSON text. Checking for a
    # plaintext code like "E11" flaked, since random ciphertext can contain those characters.
    assert stored and all(f.payload and '"' not in f.payload for f in stored)


def test_imports_check_their_columns(client):
    bad = client.post("/api/care-opportunities/facts", json={"csv": "name,dob\nA,B\n"})
    assert bad.json()["error"]["code"] == "bad_columns"
    kind = client.post(
        "/api/care-opportunities/facts", json={"csv": "patient_ref,kind\nX,allergy\n"}
    )
    assert kind.json()["error"]["code"] == "bad_kind"
