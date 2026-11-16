"""Environment guardrails (G4 PHI gate): production refuses synthetic shortcuts, labs refuses
production keys, and every production write intent leaves an audit event."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api import environment, identity, retention
from careplix_api.claims.models import Claim
from careplix_api.db import Database, utcnow
from careplix_api.errors import ApiError
from careplix_api.main import create_app
from careplix_api.models import (
    Agent,
    AgentVersion,
    AuditEvent,
    CallAttempt,
    EmailCode,
    Membership,
    PayerConnection,
    TranscriptSegment,
    Workspace,
    WriteIntent,
)

from .conftest import FakeGateway, make_settings

FERNET_KEY = Fernet.generate_key().decode()  # a fresh key per run, never a literal


def production(tmp_path, **overrides):
    """Settings a production deployment would run with (deploy-prod.sh)."""
    values = {
        "careplix_env": "production",
        "seed_demo_data": False,
        "payer_sandbox_url": "",
        "nexhealth_env": "production",
        "stedi_env": "production",
        "stedi_prod_api_key": "prod-key",
        "session_secret": "s" * 48,
        "data_encryption_key": FERNET_KEY,
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


@pytest.fixture(autouse=True)
def _restore_environment():
    yield
    environment._env = None  # later tests run as whatever their own settings say


# --- startup rules ------------------------------------------------------------------------------


def test_production_settings_pass(tmp_path):
    assert environment.problems(production(tmp_path)) == []


@pytest.mark.parametrize(
    ("override", "says"),
    [
        ({"seed_demo_data": True}, "SEED_DEMO_DATA"),
        ({"payer_sandbox_url": "http://localhost:8091/fhir"}, "PAYER_SANDBOX_URL"),
        ({"openemr_base_url": "https://10.0.0.5"}, "OPENEMR_BASE_URL"),
        ({"nexhealth_env": "sandbox"}, "NEXHEALTH_ENV"),
        ({"stedi_env": "test"}, "STEDI_ENV"),
        ({"stedi_test_api_key": "test-key"}, "test keys"),
        ({"stedi_api_key": "old-test-key"}, "test keys"),
        ({"sandbox_number": "+13127558589"}, "SANDBOX_NUMBER"),
        ({"session_secret": ""}, "SESSION_SECRET"),
        ({"data_encryption_key": ""}, "DATA_ENCRYPTION_KEY"),
        ({"log_level": "DEBUG"}, "LOG_LEVEL"),
        ({"platform_admins": "someone@gmail.com"}, "PLATFORM_ADMINS"),
    ],
)
def test_production_refuses_each_synthetic_shortcut(tmp_path, override, says):
    found = environment.problems(production(tmp_path, **override))
    assert len(found) == 1 and says in found[0]


def test_the_payer_sandbox_default_is_refused_in_production(tmp_path):
    # PAYER_SANDBOX_URL defaults to the local sandbox; production must clear it explicitly.
    settings = production(tmp_path)
    settings.payer_sandbox_url = "http://localhost:8091/fhir"  # the default
    with pytest.raises(environment.EnvironmentRefused, match="PAYER_SANDBOX_URL"):
        environment.enforce(settings)


@pytest.mark.parametrize(
    "override",
    [{"nexhealth_env": "production"}, {"stedi_env": "production"}, {"stedi_prod_api_key": "pk"}],
)
def test_labs_refuses_production_keys(tmp_path, override):
    settings = make_settings(tmp_path, careplix_env="labs", **override)
    assert len(environment.problems(settings)) == 1
    with pytest.raises(environment.EnvironmentRefused):
        create_app(settings, FakeGateway(settings))


def test_labs_with_test_keys_starts(tmp_path):
    settings = make_settings(tmp_path, careplix_env="labs", stedi_test_api_key="tk")
    assert environment.problems(settings) == []


def test_dev_has_no_extra_rules(tmp_path):
    # Tests and laptops exercise production-mode clients against fakes.
    settings = make_settings(tmp_path, nexhealth_env="production", stedi_env="production")
    assert environment.problems(settings) == []


@pytest.mark.parametrize("name", ["prod", "Production", "live"])
def test_an_unknown_environment_name_is_refused(tmp_path, name):
    # A typo must not quietly skip the production rules.
    with pytest.raises(environment.EnvironmentRefused, match="not one of"):
        environment.enforce(make_settings(tmp_path, careplix_env=name))


def test_a_production_app_starts_and_serves(tmp_path):
    settings = production(tmp_path)
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/api/auth/dev-login").status_code == 404
    assert environment.current() == "production"


def test_refuse_in_production_helper(tmp_path):
    environment.refuse_in_production("Anything", make_settings(tmp_path, careplix_env="labs"))
    with pytest.raises(ApiError) as err:
        environment.refuse_in_production("The payer sandbox", production(tmp_path))
    assert err.value.status == 409 and err.value.code == "not_in_production"


# --- runtime: the ORM hook ----------------------------------------------------------------------


async def _db(tmp_path, env: str) -> tuple[Database, str, str]:
    environment._env = env
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/env.db")
    await db.create_all()
    async with db.sessions() as s:
        ws = Workspace(name="Clinic")
        s.add(ws)
        await s.flush()
        conn = PayerConnection(workspace_id=ws.id, name="NexHealth", base_url="https://nh.test")
        s.add(conn)
        await s.commit()
        return db, ws.id, conn.id


def _intent(ws: str, conn: str) -> WriteIntent:
    return WriteIntent(
        workspace_id=ws,
        connection_id=conn,
        kind="patient_alert",
        level="L2",
        target_resource="patient",
        target_source_id="p-1",
        payload_hash="h",
        reason="eligibility result",
        source="eligibility",
        requested_by="user-1",
    )


@pytest.mark.asyncio
async def test_every_production_write_intent_state_is_audited(tmp_path):
    db, ws, conn = await _db(tmp_path, "production")
    async with db.sessions() as s:
        intent = _intent(ws, conn)
        s.add(intent)
        await s.commit()
        intent.state, intent.approved_by = "approved", "user-2"
        await s.commit()
        intent.reason = "edited reason"  # no state change: no new event
        await s.commit()
        intent.state = "accepted"
        await s.commit()
        events = list(
            await s.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "production.write_intent")
                .order_by(AuditEvent.created_at)
            )
        )
    assert [(e.detail["from"], e.detail["state"]) for e in events] == [
        (None, "proposed"),
        ("proposed", "approved"),
        ("approved", "accepted"),
    ]
    assert all(e.target == intent.id and e.workspace_id == ws for e in events)
    assert [e.actor for e in events] == ["user-1", "user-2", "user-2"]
    assert events[0].detail["kind"] == "patient_alert" and "payload" not in events[0].detail
    await db.dispose()


@pytest.mark.asyncio
async def test_labs_write_intents_are_not_double_audited(tmp_path):
    db, ws, conn = await _db(tmp_path, "labs")
    async with db.sessions() as s:
        s.add(_intent(ws, conn))
        await s.commit()
        assert await s.scalar(select(AuditEvent)) is None
    await db.dispose()


def _claim(ws: str, **kw) -> Claim:
    values = {
        "workspace_id": ws,
        "source": "ehr",
        "source_ref": "c-1",
        "claim_number": "PCN1",
        "payer_name": "Payer",
        "provider_name": "Clinic",
        "provider_npi": "1234567893",
        "date_of_service": "2026-11-01",
        "billed_amount": "100.00",
        "submitted_on": "2026-11-02",
    }
    values.update(kw)
    return Claim(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [{"source": "sample", "synthetic": True}, {}, {"synthetic": True}])
async def test_production_refuses_synthetic_claims(tmp_path, kw):
    # A claim not marked synthetic=False is stored as synthetic by the model's default.
    db, ws, _ = await _db(tmp_path, "production")
    async with db.sessions() as s:
        s.add(_claim(ws, **kw))
        with pytest.raises(ApiError, match="Synthetic claims"):
            await s.commit()
    await db.dispose()


@pytest.mark.asyncio
async def test_production_keeps_real_claims(tmp_path):
    db, ws, _ = await _db(tmp_path, "production")
    async with db.sessions() as s:
        s.add(_claim(ws, synthetic=False))
        await s.commit()
    await db.dispose()


@pytest.mark.asyncio
async def test_production_refuses_emulated_and_synthetic_connections(tmp_path):
    db, ws, conn = await _db(tmp_path, "production")
    async with db.sessions() as s:
        row = await s.get(PayerConnection, conn)
        row.auth_ref = {"emulate": "athenahealth"}
        with pytest.raises(ApiError, match="emulated"):
            await s.commit()
    async with db.sessions() as s:
        s.add(PayerConnection(workspace_id=ws, name="Sandbox", base_url="x", synthetic=True))
        with pytest.raises(ApiError, match="Synthetic"):
            await s.commit()
    await db.dispose()


@pytest.mark.asyncio
async def test_labs_still_emulates(tmp_path):
    db, _, conn = await _db(tmp_path, "labs")
    async with db.sessions() as s:
        (await s.get(PayerConnection, conn)).auth_ref = {"emulate": "athenahealth"}
        await s.commit()
    await db.dispose()


# --- sign-in ------------------------------------------------------------------------------------


def test_staff_cannot_sign_in_with_an_email_code_in_production(tmp_path):
    settings = production(tmp_path)
    with pytest.raises(ApiError) as err:
        environment.check_sign_in(settings, "email", "ops@careplix.com")
    assert err.value.status == 403 and err.value.code == "mfa_required"
    # Google (2-Step Verification enforced by Workspace) and IAP are fine; so are customers.
    environment.check_sign_in(settings, "google", "ops@careplix.com")
    environment.check_sign_in(settings, "iap", "ops@careplix.com")
    environment.check_sign_in(settings, "email", "front.desk@clinic.test")
    # Labs keeps email codes for staff.
    labs = make_settings(tmp_path, careplix_env="labs")
    environment.check_sign_in(labs, "email", "ops@careplix.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(("env", "provisioned"), [("labs", True), ("production", False)])
async def test_staff_join_a_customer_workspace_only_on_purpose_in_production(
    tmp_path, env, provisioned
):
    settings = production(tmp_path) if env == "production" else make_settings(tmp_path)
    settings.careplix_env = env
    db, ws, _ = await _db(tmp_path, env)
    staff = identity.VerifiedIdentity("google", "g-1", "ops@careplix.com", "Ops")
    async with db.sessions() as s:
        if provisioned:
            principal = await identity.establish(s, settings, staff)
            assert principal.workspace_id == ws
        else:
            with pytest.raises(ApiError) as err:
                await identity.establish(s, settings, staff)
            assert err.value.code == "no_workspace"
            assert await s.scalar(select(Membership)) is None
    await db.dispose()


# --- retention ----------------------------------------------------------------------------------


async def _attempt(s, ws: str) -> str:
    agent = Agent(workspace_id=ws, name="A", template_id="t")
    s.add(agent)
    await s.flush()
    version = AgentVersion(
        workspace_id=ws, agent_id=agent.id, number=1, state="draft", config={}, updated_by="u"
    )
    s.add(version)
    await s.flush()
    attempt = CallAttempt(
        workspace_id=ws,
        agent_id=agent.id,
        agent_version_id=version.id,
        direction="outbound",
        room_name="room-1",
    )
    s.add(attempt)
    await s.flush()
    return attempt.id


@pytest.mark.asyncio
@pytest.mark.parametrize(("days", "left"), [(0, 2), (30, 1)])
async def test_retention_deletes_old_transcripts_only_when_a_period_is_set(tmp_path, days, left):
    db, ws, _ = await _db(tmp_path, "dev")
    now = utcnow()
    async with db.sessions() as s:
        attempt = await _attempt(s, ws)
        for seg, age in (("old", 45), ("new", 5)):
            s.add(
                TranscriptSegment(
                    id=seg,
                    attempt_id=attempt,
                    speaker="counterpart",
                    text="words",
                    playout="played",
                    occurred_at=now - timedelta(days=age),
                )
            )
        s.add(EmailCode(email="a@b.test", code_hash="h", expires_at=now - timedelta(days=2)))
        s.add(EmailCode(email="c@d.test", code_hash="h", expires_at=now + timedelta(minutes=5)))
        await s.commit()
    settings = make_settings(tmp_path, transcript_retention_days=days)
    counts = await retention.purge(db, settings, now)
    async with db.sessions() as s:
        segments = list(await s.scalars(select(TranscriptSegment.id)))
        codes = list(await s.scalars(select(EmailCode.email)))
    assert len(segments) == left and "new" in segments
    assert codes == ["c@d.test"] and counts["email_codes"] == 1
    await db.dispose()
