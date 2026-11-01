"""FO-2 cancellation backfill against the NexHealth and Stedi fakes (synthetic people only)."""

from datetime import timedelta

from careplix_api.models import AuditEvent

from .test_practice import _client, _db_rows, _worker


def _board(c):
    return c.get("/api/practices/backfill").json()


def _ready(tmp_path, **overrides):
    """Sync, check eligibility and open the cancelled slot (appointment 504, in 3 days)."""
    c, router = _client(tmp_path, **overrides)
    c.__enter__()
    c.post("/api/auth/dev-login")
    _worker(c)
    _worker(c)
    return c, router


def test_cancelled_slot_opens_with_ranked_candidates(tmp_path):
    c, _ = _ready(tmp_path)
    try:
        board = _board(c)
        [opening] = board["active"]
        assert opening["state"] == "open" and opening["source_appointment_id"] == "504"
        assert opening["minutes"] == 30 and opening["type"] == "Follow-up"
        ranked = [(o["patient_id"], o["tier"]) for o in opening["offers"]]
        # ASAP first, then the move-up. Patient 3 has no phone; patient 2 cancelled this slot.
        assert ranked == [("4", "asap"), ("1", "move_up")]
        assert opening["next_offer_id"] == opening["offers"][0]["id"]
        assert "Asked to come in sooner" in opening["offers"][0]["reasons"]
        assert "Coverage verified" in opening["offers"][1]["reasons"]
        assert [e["event"] for e in opening["timeline"]] == [
            "detected",
            "slot_confirmed_free",
            "ranked",
        ]
    finally:
        c.__exit__(None, None, None)


def test_a_slot_nexhealth_no_longer_shows_free_is_not_offered(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        router.nexhealth.taken.add(router.nexhealth.appointments[504]["start_time"])
        _worker(c)
        board = _board(c)
        assert board["active"] == []
        assert board["recent"][0]["state"] == "taken"


def test_accepting_a_move_up_books_first_then_releases_the_later_visit(tmp_path):
    c, router = _ready(tmp_path)
    try:
        opening = _board(c)["active"][0]
        asap, move_up = opening["offers"]
        c.post(
            f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "declined"}
        ).raise_for_status()
        result = c.post(
            f"/api/practices/backfill/offers/{move_up['id']}", json={"outcome": "accepted"}
        ).json()
        assert result["state"] == "filled" and result["filled_appointment_id"] == "900"
        [booked] = router.nexhealth.booked
        assert booked["patient_id"] == 1 and booked["start_time"].startswith(opening["start"][:16])
        assert booked["operatory_id"] == 30 and booked["note"] == "Booked by CarePlix backfill"
        writes = [
            (m, p)
            for m, p, _ in router.nexhealth.requests
            if m in ("POST", "PATCH") and "appointments" in p
        ]
        assert writes == [
            ("POST", "/appointments"),
            ("PATCH", "/appointments/501"),
        ]  # book, then release
        assert router.nexhealth.appointments[501]["cancelled"] is True
        assert [e["event"] for e in result["timeline"]][-3:] == [
            "offer_accepted",
            "booked",
            "released_later_appointment",
        ]
        intents = c.get("/api/practices/intents").json()
        assert {(i["kind"], i["state"], i["source"]) for i in intents} == {
            ("appointment_book", "accepted", "backfill"),
            ("appointment_cancel", "accepted", "backfill"),
        }
    finally:
        c.__exit__(None, None, None)


def test_a_slot_can_only_be_booked_once(tmp_path):
    c, router = _ready(tmp_path)
    try:
        asap, move_up = _board(c)["active"][0]["offers"]
        assert (
            c.post(
                f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "accepted"}
            ).json()["state"]
            == "filled"
        )
        second = c.post(
            f"/api/practices/backfill/offers/{move_up['id']}", json={"outcome": "accepted"}
        )
        assert second.status_code == 409  # superseded once the slot was filled
        assert len(router.nexhealth.booked) == 1
        # The ASAP patient moved up, so their later visit was released.
        assert router.nexhealth.appointments[506]["cancelled"] is True
    finally:
        c.__exit__(None, None, None)


def test_a_refused_booking_reopens_the_slot_for_the_next_patient(tmp_path):
    c, router = _ready(tmp_path)
    try:
        asap, move_up = _board(c)["active"][0]["offers"]
        router.nexhealth.book_error = "Provider not available"
        refused = c.post(
            f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "accepted"}
        ).json()
        assert refused["state"] == "open" and "Provider not available" in refused["state_reason"]
        assert router.nexhealth.appointments[506]["cancelled"] is False  # nothing released
        router.nexhealth.book_error = None
        filled = c.post(
            f"/api/practices/backfill/offers/{move_up['id']}", json={"outcome": "accepted"}
        ).json()
        assert filled["state"] == "filled"
    finally:
        c.__exit__(None, None, None)


def test_the_slot_is_rechecked_with_nexhealth_before_booking(tmp_path):
    c, router = _ready(tmp_path)
    try:
        asap, _ = _board(c)["active"][0]["offers"]
        router.nexhealth.taken.add(
            router.nexhealth.appointments[504]["start_time"]
        )  # staff rebooked it
        result = c.post(
            f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "accepted"}
        ).json()
        assert result["state"] == "taken" and router.nexhealth.booked == []
    finally:
        c.__exit__(None, None, None)


def test_reinstated_appointment_closes_the_opening(tmp_path):
    c, router = _ready(tmp_path)
    try:
        router.nexhealth.appointments[504]["cancelled"] = False
        c.post("/api/practices/sync", params={"full": True})
        c.post("/api/practices/backfill/scan").raise_for_status()
        board = _board(c)
        assert board["active"] == [] and board["recent"][0]["state"] == "closed"
    finally:
        c.__exit__(None, None, None)


def test_revealing_a_phone_number_is_audited(tmp_path):
    c, _ = _ready(tmp_path)
    try:
        contact = c.get("/api/practices/patients/4/contact").json()
        assert contact == {"name": "Max Cole", "phone": "5550104"}
        actions = [e.action for e in _db_rows(c, AuditEvent)]
        assert "phi.contact_viewed" in actions
        board = _board(c)
        assert "5550104" not in str(board) and "Max" not in str(board)  # the board shows initials
    finally:
        c.__exit__(None, None, None)


def test_metrics_report_fill_rate_and_minutes_recovered(tmp_path):
    c, _ = _ready(tmp_path)
    try:
        asap, _ = _board(c)["active"][0]["offers"]
        c.post(f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "accepted"})
        metrics = _board(c)["metrics"]
        assert metrics["states"]["filled"] == 1 and metrics["fill_rate"] == 1.0
        assert metrics["visit_minutes_recovered"] == 30
    finally:
        c.__exit__(None, None, None)


def test_a_booked_patient_drops_off_other_slots(tmp_path):
    c, router = _client(tmp_path)
    with c:
        c.post("/api/auth/dev-login")
        # A second cancelled slot of the same type, two days after the first.
        router.nexhealth.appointments[507] = router.nexhealth._appt(
            507, 3, timedelta(days=5, hours=2), cancelled=True
        )
        _worker(c)
        _worker(c)
        first, second = _board(c)["active"]
        asap = first["offers"][0]
        assert asap["patient_id"] == "4" and second["offers"][0]["patient_id"] == "4"
        c.post(f"/api/practices/backfill/offers/{asap['id']}", json={"outcome": "accepted"})
        second = next(o for o in _board(c)["active"] if o["id"] == second["id"])
        assert {o["patient_id"]: o["state"] for o in second["offers"]}["4"] == "superseded"
