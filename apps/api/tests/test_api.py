import pytest
from fastapi.testclient import TestClient

from careplix_api import readiness
from careplix_api.main import create_app
from careplix_api.settings import Settings, get_settings

SENTINEL_SECRET = "lk-secret-sentinel-do-not-leak-0123456789"


def _client(**overrides) -> TestClient:
    settings = Settings(_env_file=None, **overrides)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_healthz():
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_readiness_fails_closed_without_admin_token():
    response = _client().get("/readiness")
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "admin_access_not_configured"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_readiness_rejects_wrong_token():
    response = _client(admin_api_token="right").get(
        "/readiness", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_readiness_reports_each_blocker_separately():
    response = _client(admin_api_token="t").get("/readiness", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    body = response.json()
    assert body["overall"] == "blocked"
    names = [c["name"] for c in body["checks"]]
    assert names == [
        "database",
        "livekit_api",
        "outbound_route",
        "inbound_route",
        "voice_model",
        "agent_worker",
    ]
    assert all(c["message"] for c in body["checks"])


def test_unknown_route_uses_error_envelope():
    response = _client().get("/nope")
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
