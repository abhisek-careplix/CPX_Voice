"""Medicare benefits planner: parsing CMS HETS 271s, plan suggestions, guardrails, endpoints.

Fixtures (all synthetic, each names its source):

- ``careplix_api/integrations/samples/cms_hets_sample.json``: derived from the CMS HETS 270/271
  Companion Guide v15-1 Appendix B sample 271 (plus the §7.28 AWV, §7.29 Pap and §7.10.1
  examples), mapped to Stedi's 2026-06-01 ``plans`` shape. The Studio's test-mode sample too.
- ``fixtures/stedi/cms_hets_section7.json``: variants hand-built from §7.9, 7.11, 7.12, 7.16,
  7.20, 7.21 and 7.26 example segments (open hospice and home health, therapy past the KX
  threshold, MSP, QMB, Part B-ID, ESRD).
- ``fixtures/stedi/cms_jane_doe.json``: Stedi's CMS mock member (a Medicare Advantage reply).

HTTP goes through ``httpx.MockTransport``; nothing leaves the test.
"""

import copy
import json
import pathlib
from datetime import date, timedelta

import pytest

from careplix_api.db import utcnow
from careplix_api.integrations import medicare_benefits as mb
from careplix_api.models import (
    AuditEvent,
    IntegrationCall,
    MedicareBenefitSnapshot,
    ProviderEnrollment,
)
from careplix_api.practice import medicare, medicare_planner

from .test_practice import _db_rows
from .test_receptionist import JANE, _call
from .test_stedi_eligibility import _medicare, body, practice, production, run_async

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "stedi"
TODAY = date(2026, 11, 11)
MBI = "1EG4TE5MK73"


def sample() -> dict:
    return copy.deepcopy(medicare.sample_body())


def variant(name: str) -> dict:
    return json.loads((FIXTURES / "cms_hets_section7.json").read_text())["bodies"][name]


def parse(*bodies, dos=TODAY, today=TODAY, **kw) -> dict:
    return mb.parse(
        list(bodies),
        date_of_service=dos,
        today=today,
        requested_hcpcs=list(mb.HCPCS_SERVICES),
        x12=[b.get("x12") for b in bodies if b.get("x12")],
        **kw,
    )


def items(b: dict) -> dict[str, dict]:
    return {i["key"]: i for i in b["items"]}


def plan(b: dict, today=TODAY, visit=None) -> list[dict]:
    return medicare_planner.suggestions(b, today=today, visit=visit)["suggestions"]


def titles(b: dict, **kw) -> list[str]:
    return [s["title"] for s in plan(b, **kw)]


# --- parsing (Appendix B) -----------------------------------------------------------------------


def test_entitlement_deductibles_and_coinsurance():
    b = parse(sample())
    a, part_b = b["entitlement"]["part_a"], b["entitlement"]["part_b"]
    assert a["covered"] is True and a["start"] == "2015-04-01" and a["reason"] == "age"
    assert part_b["covered"] is True and part_b["part_b_id_only"] is False
    d = b["deductibles"]
    assert d["part_a"] | {"source": None} == {
        "total": "1736",
        "remaining": "1736",
        "used": "0",
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "spell": False,
        "qmb": False,
        "source": None,
    }
    assert (d["part_b"]["total"], d["part_b"]["remaining"], d["part_b"]["used"]) == (
        "283",
        "0",
        "283",
    )
    assert d["part_b_coinsurance_pct"] == "20"
    assert d["part_b"]["source"].startswith("plans[0].benefits.deductible[")


def test_a_spell_within_60_days_decides_part_a_and_hospital_days():
    b = parse(sample(), dos=date(2025, 2, 1), today=date(2025, 2, 1))
    assert b["deductibles"]["part_a"]["remaining"] == "0" and b["deductibles"]["part_a"]["spell"]
    days = items(b)
    assert days["inpatient_days"]["remaining"] == "56" and days["inpatient_days"]["used"] == "4"
    assert days["snf_days"]["remaining"] == "16"
    assert days["inpatient_coinsurance_days"]["amount"] == "419"
    assert "Ongoing" in days["spell_2025-01-16"]["note"]


def test_inpatient_snf_reserve_and_blood():
    i = items(parse(sample()))
    assert (i["inpatient_days"]["total"], i["inpatient_days"]["remaining"]) == ("60", "60")
    assert (
        i["inpatient_coinsurance_days"]["total"],
        i["inpatient_coinsurance_days"]["remaining"],
    ) == ("30", "30")
    assert i["inpatient_coinsurance_days"]["amount"] == "434"
    assert (i["snf_days"]["total"], i["snf_coinsurance_days"]["remaining"]) == ("20", "80")
    reserve = i["lifetime_reserve_days"]
    assert (reserve["total"], reserve["remaining"], reserve["used"], reserve["amount"]) == (
        "60",
        "58",
        "2",
        "868",
    )
    assert i["stay_48_2025-01-16"]["other_npi"] == "1234567893"
    blood = i["blood_deductible"]
    assert (blood["total"], blood["remaining"], blood["used"], blood["unit"]) == (
        "3",
        "2",
        "1",
        "units",
    )
    assert blood["source"].startswith("plans[0].benefits.exclusions[")


def test_home_health_hospice_therapy_rehab_smoking_esrd():
    i = items(parse(sample()))
    hh = i["home_health_2024-12-22"]
    assert hh["other_npi"] == "1234567890" and hh["blocker"] is False and hh["note"] == "Closed"
    election = i["hospice_election_2018-03-28"]
    assert election["revocation_code"] == "1" and election["blocker"] is False
    assert i["hospice_period_2018-04-05"]["used"] == "7"
    ot = i["therapy_ot"]
    assert (ot["used"], ot["total"], ot["remaining"]) == ("200", "2480", "2280")
    assert ot["threshold"]["kx"] == 2480 and ot["threshold"]["review"] == 3000
    assert i["therapy_pt_slp"]["used"] == "0"
    assert i["pulmonary_rehab_technical"]["remaining"] == "72"
    icr = i["cardiac_rehab_intensive_technical"]
    assert (icr["used"], icr["total"], icr["remaining"]) == ("15", "72", "57")
    smoking = i["smoking_cessation"]
    assert (smoking["total"], smoking["remaining"], smoking["used"]) == ("8", "6", "2")
    assert i["esrd_2024-03-01"]["covered"] is False


def test_preventive_dates_history_and_modifiers_from_the_x12():
    b = parse(sample())
    i = items(b)
    assert i["colorectal_screening_74263_all"]["next_eligible"] == "2026-01-07"
    assert (
        i["awv_G0439_26"]["next_eligible"] == "2026-11-08" and i["awv_G0439_26"]["modifier"] == "26"
    )
    assert {i["pap_Q0091_26"]["next_eligible"], i["pap_Q0091_TC"]["next_eligible"]} == {
        "2027-02-01",
        "2026-12-01",
    }
    ppv = i["pneumococcal_90684_2026-01-05"]
    assert ppv["last_service"] == "2026-01-05" and ppv["other_npi"] == "1234567893"
    assert i["hcv_screening_G0567_2025-11-05"]["other_npi"] == "1234567893"
    assert i["flu_vaccine_90653_2025-09-01"]["category"] == "vaccine"
    assert i["covid_vaccine_91300_2025-08-23"]["program"] == "covid_vaccine"
    assert i["cognitive_99483_2023-01-03"]["last_service"] == "2023-01-03"
    missing = {m["program"] for m in b["no_data"]}
    assert {"alcohol_screening", "ippe", "hbv_screening"} <= missing
    assert "colorectal_screening" in missing  # other colorectal codes had no date
    assert {g["key"] for g in b["not_in_hets"]} == {"dsmt", "mnt"}


def test_x12_modifiers():
    x12 = sample()["x12"]
    mods = mb.x12_modifiers(x12)
    assert mods[("Q0091", "2026-12-01")] == "TC" and mods[("Q0091", "2027-02-01")] == "26"
    assert mb.x12_modifiers("") == {} and mb.x12_modifiers("not x12") == {}


def test_request_groups_and_fallback_split():
    stc, hcpcs = mb.request_groups()
    assert stc["system"] == "STC" and stc["services"][0] == "30" and len(stc["services"]) == 19
    assert hcpcs["system"] == "HCPCS" and "G0438" not in hcpcs["services"]
    assert {"G0402", "77067", "G0567", "90684"} <= set(hcpcs["services"])
    assert all(len(g["services"]) <= 10 for g in mb.request_groups(10))
    assert [len(g["services"]) for g in mb.split(stc, 5)] == [5, 5, 5, 4]
    assert [g["services"] for g in mb.split({"system": "STC", "services": ["30", "10"]}, 5)] == [
        ["30"],
        ["10"],
    ]


# --- plan suggestions ---------------------------------------------------------------------------


def test_due_now_due_on_and_info():
    out = plan(parse(sample()), visit=date(2026, 12, 5))
    by = {s["title"]: s for s in out}
    assert by["Annual wellness visit: due now"]["detail"] == "HCPCS G0439 (modifier 26)"
    assert by["Screening mammography: due now"]["kind"] == "due_now"
    assert by["Screening Pap test: allowed by the visit on 2026-12-05"]["kind"] == "due_now"
    assert "Flu vaccine: none billed to Medicare this season" in by
    assert by["Pneumococcal vaccine: last on 2026-01-05"]["kind"] == "info"
    assert "Diabetes self-management training (DSMT) hours" in by
    # Without a visit, the Pap test is due on its own date.
    assert "Screening Pap test: due on 2026-12-01" in titles(parse(sample()))
    kinds = [s["kind"] for s in out]
    assert kinds == sorted(kinds, key=medicare_planner.ORDER.__getitem__)


def test_flu_given_this_season_is_not_due():
    b = parse(sample())
    b["items"].append(
        mb._item(
            "flu_vaccine_90674_x",
            "vaccine",
            "Flu vaccine",
            "x",
            last_service="2026-10-02",
            program="flu_vaccine",
        )
    )
    assert "Flu vaccine: already given this season (2026-10-02)" in titles(b)


def test_blocked_limited_and_qmb():
    b = parse(variant("coordination"), practice_npi="1999999984")
    by = {s["title"]: s for s in plan(b)}
    assert (
        by["Open hospice election: coordinate with the hospice (NPI 1649726464)"]["kind"]
        == "blocked"
    )
    assert by["Current hospice benefit period: coordinate with the hospice (NPI 1649726464)"]
    assert by["Open home health episode with another agency (NPI 1234567893)"]["kind"] == "blocked"
    assert by["Medicare is secondary: bill XYZ HEALTHPLAN first"]["kind"] == "blocked"
    assert by["QMB: don't bill the patient Medicare cost sharing"]["kind"] == "qmb"
    pt = by["Physical + speech therapy (KX threshold, combined): $2,200 of $2,480 used (89%)"]
    assert pt["kind"] == "limited" and "KX modifier" in pt["detail"]
    ot = by["Occupational therapy (KX threshold): $3,100 used, past $3,000 review level"]
    assert "medical review" in ot["detail"]
    assert by["Smoking cessation: no sessions left in this 12-month period"]["kind"] == "limited"
    # The practice's own home health episode isn't another agency's.
    own = parse(variant("coordination"), practice_npi="1234567893")
    assert items(own)["home_health_2026-10-20"]["blocker"] is False


def test_part_b_id_only_blocks_planning():
    b = parse(variant("part_b_id"))
    assert b["entitlement"]["part_b"]["part_b_id_only"] is True
    assert b["entitlement"]["part_a"]["covered"] is False
    out = plan(b)
    assert out[0]["title"] == "Part B-ID only: immunosuppressive drugs only"
    assert not any(s["kind"] == "due_now" for s in out)


def test_qmb_lines_and_esrd_dates():
    b = parse(variant("qmb_esrd"))
    assert b["deductibles"]["part_a"]["qmb"] and b["deductibles"]["part_a"]["total"] == "0"
    assert b["deductibles"]["part_b_coinsurance_pct"] == "0"
    assert b["entitlement"]["part_b"]["esrd"] is True
    esrd = items(b)["esrd_2021-06-01"]
    assert esrd["covered"] and esrd["transplant"] == "2023-03-15"
    assert esrd["dialysis"] == {"start": "2021-06-01", "end": "2023-02-28"}
    assert "QMB: don't bill the patient Medicare cost sharing" in titles(b)


def test_medicare_advantage_is_never_planned_from_cms():
    b = parse(body("cms_jane_doe"))
    ma = b["routing"]["medicare_advantage"]
    assert ma["name"] == "UHC OF CALIFORNIA" and ma["bill_option"] == "A"
    assert (ma["contract"], ma["plan_benefit_package"]) == ("A0505", "555")
    out = plan(b)
    assert [s["title"] for s in out] == [
        "Medicare Advantage: check benefits with UHC OF CALIFORNIA"
    ]


def test_errors_surface_attestation():
    b = parse(
        {
            "payerId": "CMS",
            "errors": [{"code": "41", "followupAction": "Please Correct and Resubmit"}],
        }
    )
    assert b["message"] == "CMS attestation/enrollment required for this NPI."
    assert titles(b)[0] == "CMS attestation required"
    b = parse({"payerId": "CMS", "errors": [{"code": "97"}]})
    assert titles(b)[0] == "CMS attestation suspended" and "suspended" in b["message"]


def test_the_caller_hears_only_safe_facts():
    b = parse(sample())
    both = medicare_planner.caller_facts(
        b, today=TODAY, allowed=set(medicare_planner_disclosures())
    )
    assert both["coverage"] == {"part_a": "covered", "part_b": "covered"}
    services = {p["service"]: p["when"] for p in both["preventive"]}
    assert (
        services["Annual wellness visit"] == "now"
        and services["Screening Pap test"] == "2026-12-01"
    )
    text = json.dumps(both)
    for secret in ("1234567893", "1987654323", "2026-01-05", "Pneumococcal", "283", "HCPCS"):
        assert secret not in text
    only = medicare_planner.caller_facts(b, today=TODAY, allowed={"medicare_coverage"})
    assert set(only) == {"coverage"}
    ma = medicare_planner.caller_facts(
        parse(body("cms_jane_doe")),
        today=TODAY,
        allowed={"medicare_coverage", "medicare_preventive_due"},
    )
    assert ma["medicare_advantage"] == "UHC OF CALIFORNIA" and "preventive" not in ma


def medicare_planner_disclosures():
    from careplix_api.practice import medicare_tool

    return medicare_tool.DISCLOSURES


# --- endpoints and guardrails -------------------------------------------------------------------

URL = "/api/practices/patients/1/medicare-benefits"


def refresh(c, **payload):
    return c.post(URL, json=payload)


def audits(c) -> list[AuditEvent]:
    return [a for a in _db_rows(c, AuditEvent) if a.action == "medicare.benefits_check"]


def test_a_purpose_is_required(tmp_path):
    with practice(tmp_path) as (c, router):
        none = refresh(c)
        assert none.status_code == 422 and none.json()["error"]["code"] == "purpose_required"
        other = refresh(c, appointment_id="502")  # patient 2's visit
        assert other.status_code == 404
        assert router.stedi.bodies == []
        assert c.get(URL).json()["snapshot"] is None


def test_test_mode_sends_only_stedis_cms_mock_and_reuses_the_day(tmp_path):
    with practice(tmp_path) as (c, router):
        first = refresh(c, appointment_id="500")
        assert first.status_code == 200, first.text
        out = first.json()
        [sent] = router.stedi.bodies
        assert sent["payerId"] == "CMS" and sent["subscriber"]["memberId"] == "1AA2CC3DD45"
        assert sent["encounter"] == {"services": [{"value": "30", "system": "STC"}]}
        assert "Roe" not in json.dumps(sent)  # never the patient in test mode
        snap = out["snapshot"]
        assert snap["test_data"] is True and snap["mode"] == "test" and snap["reused"] is False
        assert snap["purpose"] == "appointment" and snap["appointment_id"] == "500"
        assert out["plan"]["suggestions"][0]["title"].startswith("Medicare Advantage")
        again = refresh(c, reason="upcoming_visit").json()
        assert again["snapshot"]["reused"] is True and len(router.stedi.bodies) == 1
        view = c.get(URL).json()
        assert view["snapshot"]["id"] == snap["id"] and view["mode"] == "test"
        assert view["patient"] == "J. R." and view["benefits"]["routing"]["medicare_advantage"]
        assert [a.detail["outcome"] for a in audits(c)] == ["done", "reused"]
        [call] = [
            x for x in _db_rows(c, IntegrationCall) if x.operation == "stedi.medicare_benefits"
        ]
        assert call.summary["mock"] is True and "1AA2CC3DD45" not in json.dumps(call.summary)


def test_the_synthetic_sample_loads_in_test_mode_only(tmp_path):
    with practice(tmp_path) as (c, router):
        out = refresh(c, appointment_id="500", sample=True).json()
        assert router.stedi.bodies == []
        assert out["snapshot"]["source"] == "sample" and out["snapshot"]["test_data"] is True
        assert any(i["key"] == "lifetime_reserve_days" for i in out["benefits"]["items"])
        [row] = _db_rows(c, MedicareBenefitSnapshot)
        assert row.benefits and "lifetime_reserve_days" not in row.benefits  # sealed
    (tmp_path / "prod").mkdir()
    with production(tmp_path / "prod", setup=_medicare("1eg4-te5-mk73")) as (c, router):
        refused = refresh(c, appointment_id="500", sample=True)
        assert refused.json()["error"]["code"] == "sample_test_only"


def test_production_sends_two_requests_with_the_mbi_and_the_requesters_ip(tmp_path):
    with production(tmp_path, setup=_medicare("1eg4-te5-mk73")) as (c, router):
        router.stedi.answers[MBI] = [(200, sample()), (200, sample())]
        out = c.post(
            URL, json={"appointment_id": "500"}, headers={"X-Forwarded-For": "203.0.113.9"}
        )
        assert out.status_code == 200, out.text
        stc, hcpcs = router.stedi.bodies
        assert stc["payerId"] == "CMS" and stc["subscriber"]["memberId"] == MBI
        assert stc["provider"] == {"npi": "1234567893"} and "dates" not in stc["encounter"]
        assert [s["value"] for s in stc["encounter"]["services"]] == list(mb.STC_SERVICES)
        assert {s["system"] for s in hcpcs["encounter"]["services"]} == {"HCPCS"}
        assert len(hcpcs["encounter"]["services"]) == len(mb.HCPCS_SERVICES)
        assert all(h["x-forwarded-for"] == "203.0.113.9" for h in router.stedi.headers)
        snap = out.json()["snapshot"]
        assert (
            snap["mode"] == "production" and snap["test_data"] is False and snap["state"] == "done"
        )
        assert [g["outcome"] for g in snap["groups"]] == ["ok", "ok"]
        [row] = _db_rows(c, MedicareBenefitSnapshot)
        assert MBI not in (row.raw or "") and MBI not in json.dumps(row.summary)
        stored = json.dumps(
            [a.detail for a in audits(c)] + [x.summary for x in _db_rows(c, IntegrationCall)]
        )
        assert MBI not in stored and "Roe" not in stored
        # One detailed check per patient per day: the next request reuses it.
        assert refresh(c, appointment_id="501").json()["snapshot"]["reused"] is True
        assert len(router.stedi.bodies) == 2


def test_a_rejected_list_falls_back_to_smaller_groups(tmp_path):
    too_many = (400, {"errors": [{"code": "04", "followupAction": "Please Correct and Resubmit"}]})
    with production(tmp_path, setup=_medicare(MBI), medicare_benefits_fallback_size=5) as (
        c,
        router,
    ):
        router.stedi.answers[MBI] = [too_many] + [(200, sample())] * 5
        out = refresh(c, appointment_id="500").json()
        sizes = [len(b["encounter"]["services"]) for b in router.stedi.bodies]
        assert sizes == [19, 5, 5, 5, 4, len(mb.HCPCS_SERVICES)]
        assert [g["outcome"] for g in out["snapshot"]["groups"]][:2] == ["correct", "ok"]
        assert out["snapshot"]["state"] == "done"


def test_attestation_required_stops_and_is_shown(tmp_path):
    aaa41 = (
        200,
        {
            "payerId": "CMS",
            "errors": [{"code": "41", "followupAction": "Please Correct and Resubmit"}],
        },
    )
    with production(tmp_path, setup=_medicare(MBI)) as (c, router):
        router.stedi.answers[MBI] = [aaa41]
        out = refresh(c, appointment_id="500").json()
        assert len(router.stedi.bodies) == 1  # the HCPCS list isn't sent after AAA 41
        assert out["snapshot"]["state"] == "failed"
        assert out["snapshot"]["message"] == "CMS attestation/enrollment required for this NPI."
        assert out["plan"]["suggestions"][0]["title"] == "CMS attestation required"
        [enrollment] = _db_rows(c, ProviderEnrollment)
        assert (enrollment.payer_id, enrollment.status) == ("CMS", "action_required")


def test_medicare_advantage_is_never_sent_to_cms(tmp_path):
    def humana(router):
        router.nexhealth.plans[50] = {"id": 50, "name": "Humana Medicare Advantage", "payer_id": ""}

    for env in (practice, production):
        (tmp_path / env.__name__).mkdir()
        with env(tmp_path / env.__name__, setup=humana) as (c, router):
            refused = refresh(c, appointment_id="500")
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "medicare_advantage"
            assert "Humana Medicare Advantage" in refused.json()["error"]["message"]
            assert router.stedi.bodies == []
            assert audits(c)[-1].detail["outcome"] == "medicare_advantage"


def test_production_refuses_patients_without_original_medicare(tmp_path):
    with production(tmp_path) as (c, router):  # an Aetna patient
        refused = refresh(c, appointment_id="500")
        assert refused.json()["error"]["code"] == "not_medicare" and router.stedi.bodies == []


def test_retries_are_capped_per_day(tmp_path):
    with production(tmp_path, setup=_medicare(MBI), medicare_benefits_daily_attempts=2) as (
        c,
        router,
    ):
        router.stedi.answers[MBI] = ["aaa_42", "aaa_42"]
        first = refresh(c, appointment_id="500").json()
        assert first["snapshot"]["state"] == "failed" and len(router.stedi.bodies) == 1
        assert first["snapshot"]["message"] == "CMS isn't answering right now. Try again later."
        assert refresh(c, appointment_id="500").json()["snapshot"]["state"] == "failed"
        third = refresh(c, appointment_id="500")
        assert third.status_code == 429 and third.json()["error"]["code"] == "daily_limit"
        assert len(router.stedi.bodies) == 2


def test_no_sweeps(tmp_path):
    with practice(tmp_path, medicare_benefits_hourly_per_user=1) as (c, router):
        assert refresh(c, appointment_id="500").status_code == 200
        swept = c.post(
            "/api/practices/patients/2/medicare-benefits", json={"appointment_id": "502"}
        )
        assert swept.status_code == 429 and swept.json()["error"]["code"] == "sweep_guard"
        assert len(router.stedi.bodies) == 1


def test_an_old_or_missing_appointment_is_not_a_purpose(tmp_path):
    with practice(tmp_path) as (c, router):
        assert refresh(c, appointment_id="999").status_code == 404

        async def past(session):
            from sqlalchemy import select

            from careplix_api.models import EhrRecord

            appt = await session.scalar(
                select(EhrRecord).where(
                    EhrRecord.resource == "appointment", EhrRecord.source_id == "501"
                )
            )
            appt.starts_at = utcnow() - timedelta(days=3)

        run_async(c, past)
        stale = refresh(c, appointment_id="501")
        assert stale.json()["error"]["code"] == "appointment_not_upcoming"


# --- the voice tool -----------------------------------------------------------------------------


def test_voice_tool_reads_the_latest_snapshot_only(tmp_path):
    with practice(tmp_path) as (c, router):
        call = _call(c)
        assert call.tool("medicare_benefits")["result"]["error"] == "not_verified"
        call.ok("find_patient", **JANE)
        none = call.ok("medicare_benefits")
        assert none["state"] == "not_checked" and "front desk" in none["message"]
        refresh(c, appointment_id="500", sample=True)
        out = call.ok("medicare_benefits", topic="wellness_visit")
        assert out["state"] == "checked" and out["test_data"] is True
        assert out["coverage"] == {"part_a": "covered", "part_b": "covered"}
        assert any(p["service"] == "Annual wellness visit" for p in out["preventive"])
        text = json.dumps(out)
        for secret in ("1234567893", "1987654323", "Pneumococcal", "G0439"):
            assert secret not in text
        assert router.stedi.bodies == []  # the tool never asks CMS


@pytest.mark.parametrize("year,amount", [(2026, 2480), (2025, 2410)])
def test_therapy_threshold_is_dated(year, amount):
    t = mb.therapy_threshold(year)
    assert t["kx"] == amount and t["review"] == 3000 and t["estimated"] is False
    assert mb.therapy_threshold(2030)["estimated"] is True
