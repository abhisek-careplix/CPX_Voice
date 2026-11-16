"""Text messages (P2): signed Twilio webhooks, consent and STOP, quiet hours, the sandbox gate,
YES/NO routing to open offers, delivery evidence, and warm transfer on voice. Synthetic only."""

from datetime import UTC, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api import channels
from careplix_api.channels import content, quiet_hours, service, twilio
from careplix_api.channels.models import CallHandoff, ChannelMessage, ContactConsent
from careplix_api.channels.twilio import Sent, SmsError
from careplix_api.main import create_app
from careplix_api.models import Case, Workspace

from .conftest import APPROVED, WORKER, FakeGateway, make_settings
from .test_lifecycle import _agent, _publish

TOKEN = "twilio-test-auth-token"
HOOKS = "https://hooks.careplix.test"
SENDER = "+13125550100"
OTHER = "+12025550199"  # never approved
NIGHT = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 21:00 the evening before in Chicago
MORNING = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # 10:00 in Chicago


class FakeSms:
    def __init__(self):
        self.sent: list[tuple[str, str, str, str | None]] = []
        self.error: SmsError | None = None

    async def send(self, to, body, sender, status_callback):
        if self.error:
            raise self.error
        self.sent.append((to, body, sender, status_callback))
        return Sent(sid=f"SM{len(self.sent):032d}", status="queued")


@pytest.fixture
def texting(tmp_path):
    settings = make_settings(
        tmp_path,
        twilio_account_sid="ACtest",
        twilio_auth_token=TOKEN,
        sms_from_number=SENDER,
        webhook_public_url=HOOKS,
        workspace_timezone="America/Chicago",
        contact_hours="08:00-20:00",
        run_background=False,
        payer_sandbox_url="",
    )
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        c.app.state.sms_provider = FakeSms()
        yield c


def _run(c, fn):
    async def go():
        async with c.app.state.db.sessions() as session:
            return await fn(session, c.app.state.settings)

    return c.portal.call(go)


def _workspace_id(c) -> str:
    return _run(c, lambda s, _: s.scalar(select(Workspace.id)))


def _rows(c, model):
    async def load(session, _):
        return list(await session.scalars(select(model)))

    return _run(c, load)


def _opt_in(c, number=APPROVED):
    response = c.post(
        "/api/channels/consents",
        json={
            "number": number,
            "state": "opted_in",
            "source": "intake_form",
            "note": "Signed intake form (synthetic)",
        },
    )
    assert response.status_code == 200, response.text


def _send(c, to=APPROVED, body="Riverside Clinic: your visit is Tue 9:30 am.", now=MORNING, **kw):
    ws = _workspace_id(c)

    async def go(session, settings):
        return await channels.send(
            session, settings, ws, "sms", to, body, provider=c.app.state.sms_provider, now=now, **kw
        )

    return _run(c, go)


def _post_twilio(c, path, params, token=TOKEN, url=None):
    url = url or f"{HOOKS}{path}"
    sig = twilio.signature(token, url, sorted(params.items()))
    return c.post(
        path,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": sig,
        },
    )


def _inbound(c, body, frm=APPROVED, sid=None, **extra):
    n = len(_rows(c, ChannelMessage))
    params = {
        "AccountSid": "ACtest",
        "MessageSid": sid or f"SMin{n:030d}",
        "From": frm,
        "To": SENDER,
        "Body": body,
        **extra,
    }
    return _post_twilio(c, "/webhooks/twilio/sms", params)


# --- signatures ---------------------------------------------------------------------------------


def test_signature_matches_twilios_published_example():
    # The worked example in Twilio's security guide (twilio.com/docs/usage/security).
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }
    url = "https://example.com/myapp.php?foo=1&bar=2"
    sig = twilio.signature("12345", url, params.items())
    assert sig == "L/OH5YylLD5NRKLltdqwSvS0BnU="
    assert twilio.verify("12345", [url], list(params.items()), sig)
    assert not twilio.verify("12345", [url + "&x=1"], list(params.items()), sig)


def test_unsigned_or_tampered_webhooks_are_refused(texting):
    c = texting
    params = {"AccountSid": "ACtest", "MessageSid": "SMx", "From": APPROVED, "To": SENDER}
    params["Body"] = "YES"
    assert _post_twilio(c, "/webhooks/twilio/sms", params, token="wrong").status_code == 403
    unsigned = c.post("/webhooks/twilio/sms", content=urlencode(params))
    assert unsigned.status_code == 403
    sig = twilio.signature(TOKEN, f"{HOOKS}/webhooks/twilio/sms", sorted(params.items()))
    tampered = c.post(
        "/webhooks/twilio/sms",
        content=urlencode({**params, "Body": "NO"}),
        headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert tampered.status_code == 403
    assert _rows(c, ChannelMessage) == []
    ok = _post_twilio(c, "/webhooks/twilio/sms", params)
    assert ok.status_code == 200 and ok.text.endswith("<Response></Response>")
    # Twilio retries a delivery it thinks failed: stored once.
    assert _post_twilio(c, "/webhooks/twilio/sms", params).status_code == 200
    assert len(_rows(c, ChannelMessage)) == 1


def test_another_accounts_webhook_is_refused(texting):
    params = {"AccountSid": "ACother", "MessageSid": "SMy", "From": APPROVED, "To": SENDER}
    assert (
        _post_twilio(texting, "/webhooks/twilio/sms", {**params, "Body": "hi"}).status_code == 403
    )


# --- gates ---------------------------------------------------------------------------------------


def test_sandbox_gate_texts_only_approved_destinations(texting):
    c = texting
    _opt_in(c, OTHER)
    result = _send(c, to=OTHER)
    assert (result.status, result.reason) == ("blocked", "not_approved")
    assert c.app.state.sms_provider.sent == []
    assert _rows(c, ChannelMessage)[0].peer is not None  # the attempt is on record, sealed


def test_no_text_without_consent_on_file(texting):
    result = _send(texting)
    assert (result.status, result.reason) == ("blocked", "no_consent")
    _opt_in(texting)
    sent = _send(texting)
    assert sent.status == "queued" and sent.ok
    to, body, sender, callback = texting.app.state.sms_provider.sent[0]
    assert (to, sender) == (APPROVED, SENDER)
    assert body.endswith("Reply STOP to opt out.")  # first text to this person
    assert callback == f"{HOOKS}/webhooks/twilio/sms/status?m={sent.message_id}"
    _send(texting)
    assert "STOP" not in texting.app.state.sms_provider.sent[1][1]  # not on every text


@pytest.mark.parametrize(
    "body,reason",
    [
        ("Your DOB on file is 01/02/1980.", "date_of_birth"),
        ("Member ID 99123 needs updating.", "identifier"),
        ("Your lab results are ready.", "clinical"),
        ("x" * 600, "too_long"),
    ],
)
def test_bodies_carry_minimum_necessary_phi(texting, body, reason):
    _opt_in(texting)
    result = _send(texting, body=body)
    assert (result.status, result.reason) == ("blocked", reason)
    assert _rows(texting, ChannelMessage)[0].body is None  # a refused body isn't kept


def test_templates_pass_the_phi_check():
    values = {"brand": "Riverside", "first_name": "Sam", "when": "Tue 9:30 am", "area": "60614"}
    values |= {"staff": "the front desk", "promise": "today"}
    for key in content.TEMPLATES:
        assert content.check(channels.render(key, **values)) is None, key


def test_emergency_stop_blocks_texts(texting):
    c = texting
    _opt_in(c)

    async def stop(session, _):
        (await session.scalar(select(Workspace))).emergency_stop = True
        await session.commit()

    _run(c, stop)
    assert _send(c).reason == "emergency_stop"


def test_idempotency_key_never_texts_twice(texting):
    _opt_in(texting)
    first = _send(texting, idempotency_key="offer:1:person:1")
    again = _send(texting, idempotency_key="offer:1:person:1")
    assert first.message_id == again.message_id
    assert len(texting.app.state.sms_provider.sent) == 1


# --- quiet hours ---------------------------------------------------------------------------------


def test_quiet_hours_hold_until_the_window_opens_in_the_workspace_zone(texting):
    c = texting
    _opt_in(c)
    held = _send(c, now=NIGHT)
    assert (held.status, held.reason) == ("held", "quiet_hours")
    assert held.send_after == datetime(2026, 11, 16, 14, 0, tzinfo=UTC)  # 08:00 Chicago
    provider = c.app.state.sms_provider

    def deliver(at):
        return _run(c, lambda s, st: service.deliver_due(s, st, provider, now=at))

    deliver(datetime(2026, 11, 16, 13, 59, tzinfo=UTC))
    assert provider.sent == []
    deliver(datetime(2026, 11, 16, 14, 0, tzinfo=UTC))
    assert len(provider.sent) == 1
    assert _rows(c, ChannelMessage)[0].status == "queued"


def test_workspace_time_zone_and_window_are_its_own(texting):
    c = texting
    response = c.put(
        "/api/channels/settings",
        json={"timezone": "America/Los_Angeles", "contact_start": "09:00", "contact_end": "17:00"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["timezone"] == "America/Los_Angeles"
    _opt_in(c)
    # 10:00 in Chicago is 08:00 in Los Angeles: before this workspace's 09:00 start.
    held = _send(c, now=MORNING)
    assert held.status == "held"
    assert held.send_after == datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
    bad = c.put("/api/channels/settings", json={"timezone": "Mars/Olympus"})
    assert bad.status_code == 422


def test_a_held_text_is_rechecked_before_it_goes(texting):
    c = texting
    _opt_in(c)
    _send(c, now=NIGHT)
    _inbound(c, "STOP")
    _run(c, lambda s, st: service.deliver_due(s, st, c.app.state.sms_provider, now=MORNING))
    row = next(m for m in _rows(c, ChannelMessage) if m.direction == "outbound")
    assert (row.status, row.status_reason) == ("blocked", "opted_out")
    assert c.app.state.sms_provider.sent == []


def test_quiet_hours_windows():
    tz = ZoneInfo("America/Chicago")
    start, end = time(8), time(20)
    assert quiet_hours.is_quiet(NIGHT, tz, start, end)
    assert not quiet_hours.is_quiet(MORNING, tz, start, end)
    assert quiet_hours.next_open(MORNING, tz, start, end) == MORNING
    # A window across midnight (night shifts): 20:00 to 06:00.
    assert not quiet_hours.is_quiet(NIGHT, tz, time(20), time(6))
    assert quiet_hours.is_quiet(MORNING, tz, time(20), time(6))


# --- STOP, START, HELP ---------------------------------------------------------------------------


def test_stop_opts_out_at_once_and_start_opts_back_in(texting):
    c = texting
    _opt_in(c)
    assert _inbound(c, " stop ").status_code == 200
    [row] = _rows(c, ContactConsent)
    assert (row.state, row.source) == ("opted_out", "keyword_stop")
    assert _send(c).reason == "opted_out"
    # Twilio flags its own opt-in keywords (YES re-subscribes an opted-out number).
    _inbound(c, "Yes", OptOutType="START")
    assert _rows(c, ContactConsent)[0].state == "opted_in"
    assert _send(c).status == "queued"


def test_carrier_unsubscribed_error_records_the_opt_out(texting):
    c = texting
    _opt_in(c)
    c.app.state.sms_provider.error = SmsError(twilio.UNSUBSCRIBED, 400)
    result = _send(c)
    assert (result.status, result.reason) == ("blocked", "opted_out")
    assert _rows(c, ContactConsent)[0].source == "carrier_opt_out"


def test_keyword_replies_come_from_twilio_by_default_or_from_the_app(tmp_path, texting):
    assert _inbound(texting, "HELP").text.endswith("<Response></Response>")
    settings = make_settings(
        tmp_path / "app",
        twilio_account_sid="ACtest",
        twilio_auth_token=TOKEN,
        sms_from_number=SENDER,
        webhook_public_url=HOOKS,
        sms_keyword_replies="app",
        run_background=False,
        payer_sandbox_url="",
    )
    (tmp_path / "app").mkdir()
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        c.put("/api/channels/settings", json={"brand_name": "Riverside", "help_phone": OTHER})
        help_ = _inbound(c, "help")
        assert "<Message>Riverside: Appointment and scheduling texts." in help_.text
        assert OTHER in help_.text
        assert "You're unsubscribed" in _inbound(c, "STOP").text


# --- replies to offers ---------------------------------------------------------------------------


def test_yes_and_no_replies_reach_the_offer_they_answer(texting):
    c = texting
    _opt_in(c)
    seen: list[channels.InboundReply] = []

    @channels.on_reply
    async def take(session, reply):
        if not (reply.reply_ref or "").startswith("offer:"):
            return False
        seen.append(reply)
        return True

    try:
        offer = _send(
            c,
            body=channels.render(
                "opening_offer", brand="Riverside", first_name="Sam", when="Tue 9:30 am"
            ),
            reply_ref="offer:abc",
            expects_reply=True,
            case_id="case-1",
            now=_last_morning(),
        )
        assert offer.status == "queued"
        _inbound(c, "Maybe later, what times?")  # free text: for a person, offer stays open
        _inbound(c, "Yes please")
        _run(c, lambda s, st: service.route_replies(s, st))
        assert [r.intent for r in seen] == ["other", "yes"]
        yes = seen[1]
        assert (yes.reply_ref, yes.case_id, yes.offer_message_id) == (
            "offer:abc",
            "case-1",
            offer.message_id,
        )
        assert yes.workspace_id == _workspace_id(c) and yes.text == "Yes please"
        # Answered: a second reply has no open offer to land on.
        _inbound(c, "No")
        _run(c, lambda s, st: service.route_replies(s, st))
        assert len(seen) == 2
        rows = {m.id: m for m in _rows(c, ChannelMessage)}
        assert rows[offer.message_id].answered_by == yes.message_id
        routed = sorted(m.routed_to for m in rows.values() if m.direction == "inbound")
        assert routed.count("no_open_offer") == 1
    finally:
        service._HANDLERS[:] = [h for h in service._HANDLERS if h[1] is not take]


def _last_morning() -> datetime:
    """The latest 10:00 in Chicago before now: inside the window, and offers still open."""
    now = datetime.now(UTC)
    at = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return at if at <= now else at - timedelta(days=1)


def test_intents():
    assert content.intent("YES") == "yes"
    assert content.intent("y") == "yes"
    assert content.intent("No thanks") == "no"
    assert content.intent("yes no") == "other"
    assert content.intent("Stop") == "stop"
    assert content.intent("please stop texting me") == "other"  # not a keyword: a person reads it
    assert content.intent("HELP") == "help"
    assert content.intent("I can come on Tuesday if that works") == "other"


# --- delivery status and evidence ---------------------------------------------------------------


def test_status_callbacks_record_delivery_evidence_in_order(texting):
    c = texting
    _opt_in(c)
    sent = _send(c, case_id="case-7")
    sid = "SM" + "1".zfill(32)  # FakeSms's first sid
    path = f"/webhooks/twilio/sms/status?m={sent.message_id}"

    def status(value, **extra):
        params = {"AccountSid": "ACtest", "MessageSid": sid, "MessageStatus": value, **extra}
        assert _post_twilio(c, path, params).status_code == 200

    status("delivered")
    status("sent")  # late, out of order: ignored
    ws = _workspace_id(c)
    evidence = _run(c, lambda s, _: service.delivery_evidence(s, ws, "case-7"))
    assert [(e["status"], e["provider_id"]) for e in evidence] == [("delivered", sid)]
    assert evidence[0]["delivered_at"] and evidence[0]["to"] == f"•••• {APPROVED[-4:]}"
    listed = c.get("/api/channels/messages?case_id=case-7").json()
    assert listed[0]["status"] == "delivered"
    # Another message's id with this sid is not ours to change.
    params = {"AccountSid": "ACtest", "MessageSid": "SMother", "MessageStatus": "failed"}
    assert _post_twilio(c, path, params).status_code == 200
    assert _rows(c, ChannelMessage)[0].status == "delivered"


def test_studio_send_and_settings(texting):
    c = texting
    view = c.get("/api/channels/settings").json()
    assert view["configured"] and view["sender"] == SENDER
    assert view["inbound_webhook"] == f"{HOOKS}/webhooks/twilio/sms"
    _opt_in(c)
    out = c.post("/api/channels/messages", json={"to": APPROVED, "body": "Test from the Studio"})
    assert out.status_code == 200, out.text
    assert out.json()["body"] == "Test from the Studio"
    assert c.get(f"/api/channels/consents?number={APPROVED}").json()["state"] == "opted_in"


# --- warm transfer -------------------------------------------------------------------------------


class Call:
    def __init__(self, c):
        agent = _agent(c, "front-desk-receptionist")
        if not agent["current"]:
            _publish(c, agent["id"])
        session = c.post(
            "/api/browser-sessions", json={"agent_id": agent["id"], "use": "published"}
        )
        assert session.status_code == 200, session.text
        self.attempt_id = session.json()["attempt_id"]
        claim = c.post(
            f"/internal/attempts/{self.attempt_id}/claim",
            json={
                "room": f"att_{self.attempt_id.replace('-', '')}",
                "job_id": "AJ",
                "runtime_build": "t",
            },
            headers=WORKER,
        )
        assert claim.status_code == 200, claim.text
        self.claim = claim.json()
        self.c = c
        self.auth = {"Authorization": f"Bearer {self.claim['attempt_token']}"}

    def transfer(self, call_id="t1"):
        response = self.c.post(
            f"/internal/attempts/{self.attempt_id}/tools/transfer_to_staff",
            json={
                "call_id": call_id,
                "arguments": {
                    "reason": "caller_asked_for_person",
                    "summary": "Synthetic caller wants to move Tuesday's visit to a morning.",
                },
            },
            headers=self.auth,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def report(self, handoff_id, outcome):
        return self.c.post(
            f"/internal/attempts/{self.attempt_id}/handoffs/{handoff_id}",
            json={"outcome": outcome},
            headers=self.auth,
        )


def test_receptionist_can_transfer(texting):
    call = Call(texting)
    assert "transfer_to_staff" in {t["name"] for t in call.claim["tools"]}


def test_transfer_without_a_staff_number_becomes_a_call_back_case(texting):
    c = texting
    out = Call(c).transfer()
    assert out["state"] == "succeeded"
    result = out["result"]
    assert result["state"] == "callback_promised" and "dial" not in result
    assert "call them back" in result["message"]
    case = next(x for x in _rows(c, Case) if x.id == result["case_id"])
    assert (case.case_type, case.status) == ("call_back", "open")
    assert case.data["handoff"]["summary"].startswith("Synthetic caller wants")
    [handoff] = c.get(f"/api/channels/handoffs?case_id={case.id}").json()
    assert (handoff["state"], handoff["detail"]) == ("callback_promised", "no_staff_number")


def test_unapproved_staff_number_is_never_dialled(texting):
    c = texting
    c.put("/api/channels/settings", json={"staff_number": OTHER}).raise_for_status()
    result = Call(c).transfer()["result"]
    assert result["state"] == "callback_promised" and "dial" not in result
    assert _rows(c, CallHandoff)[0].detail == "staff_number_not_approved"


def test_warm_transfer_hands_the_worker_the_dial_and_records_the_outcome(texting):
    c = texting
    c.put(
        "/api/channels/settings",
        json={"staff_number": APPROVED, "staff_label": "the front desk"},
    ).raise_for_status()
    call = Call(c)
    result = call.transfer()["result"]
    assert result["state"] == "transferring"
    dial = result["dial"]
    assert (dial["number"], dial["trunk_id"]) == (APPROVED, "ST_test")
    assert "Synthetic caller wants to move" in dial["whisper"]
    assert "the front desk" in dial["whisper"]
    # One transfer at a time on a call.
    assert call.transfer("t2")["result"]["error"] == "transfer_in_progress"
    connected = call.report(result["handoff_id"], "connected")
    assert connected.json() == {"state": "connected", "case_id": None}
    assert call.report(result["handoff_id"], "failed").json()["state"] == "connected"  # settled
    [handoff] = c.get(f"/api/channels/handoffs?attempt_id={call.attempt_id}").json()
    assert handoff["state"] == "connected"
    assert handoff["summary"].startswith("Synthetic caller")
    assert not [x for x in _rows(c, Case) if x.case_type == "call_back"]


def test_a_transfer_nobody_answers_becomes_a_call_back_promise(texting):
    c = texting
    c.put("/api/channels/settings", json={"staff_number": APPROVED}).raise_for_status()
    call = Call(c)
    result = call.transfer()["result"]
    out = call.report(result["handoff_id"], "no_answer").json()
    assert out["state"] == "callback_promised" and out["case_id"]
    assert "call them back" in out["message"]
    assert _rows(c, CallHandoff)[0].detail == "staff_no_answer"
    assert call.report("no-such-handoff", "connected").status_code == 404
