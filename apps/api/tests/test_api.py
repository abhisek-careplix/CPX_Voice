import pytest
from fastapi.testclient import TestClient

from careplix_api import readiness
from careplix_api.main import create_app
from careplix_api.settings import Settings

from .conftest import FakeGateway, make_settings

SENTINEL_SECRET = "lk-secret-sentinel-do-not-leak-0123456789"


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_studio_requires_sign_in(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, FakeGateway(settings))) as anonymous:
        response = anonymous.get("/api/me")
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "unauthenticated"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_dev_login_is_disabled_outside_dev(tmp_path):
    settings = make_settings(tmp_path, careplix_env="staging", session_secret="x" * 40)
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        assert c.post("/api/auth/dev-login").status_code == 404


def test_me_and_readiness_report_each_dependency(client):
    me = client.get("/api/me").json()
    assert me["role"] == "admin" and "publish" in me["permissions"]
    names = [c["name"] for c in client.get("/api/readiness").json()["checks"]]
    assert names == [
        "database",
        "livekit_api",
        "outbound_route",
        "inbound_route",
        "voice_model",
        "agent_worker",
    ]


def test_unknown_route_uses_error_envelope(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"


@pytest.mark.asyncio
async def test_failed_livekit_check_never_echoes_secret():
    settings = Settings(
        _env_file=None,
        livekit_url="http://127.0.0.1:9",  # closed port: connection refused
        livekit_api_key="key",
        livekit_api_secret=SENTINEL_SECRET,
        check_timeout_s=2,
    )
    checks = await readiness.check_livekit(settings)
    assert checks[0].status == readiness.Status.BLOCKED
    assert SENTINEL_SECRET not in repr(checks)


def test_model_check_is_unverified_not_ready_when_configured():
    check = readiness.check_model(
        Settings(
            _env_file=None, gcp_project="p", vertex_location="us-central1", gemini_live_model="m"
        )
    )
    # Configuration alone never proves model access (doc 05: no misleading green badges).
    assert check.status == readiness.Status.UNVERIFIED


def test_session_for_missing_membership_is_rejected(tmp_path, settings, client):
    """A validly signed cookie whose membership no longer exists must not authenticate."""
    other = make_settings(tmp_path / "other")
    (tmp_path / "other").mkdir()
    with TestClient(create_app(other, FakeGateway(other))) as c2:
        c2.cookies.update(client.cookies)
        response = c2.get("/api/me")
    assert response.status_code == 401
