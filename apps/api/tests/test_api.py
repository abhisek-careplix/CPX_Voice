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


def _sso_client(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        careplix_env="labs",
        session_secret="s" * 48,
        iap_audience="/projects/1/locations/us-central1/services/careplix-studio",
        **overrides,
    )
    return TestClient(create_app(settings, FakeGateway(settings)), base_url="https://testserver")


def test_iap_sign_in_provisions_user_and_session(tmp_path, monkeypatch):
    from google.oauth2 import id_token

    seen = {}

    def fake_verify(token, request, audience=None, certs_url=None, **_):
        seen.update(token=token, audience=audience, certs_url=certs_url)
        return {"email": "Asha.Rao@careplix.com"}

    monkeypatch.setattr(id_token, "verify_token", fake_verify)
    with _sso_client(tmp_path) as c:
        assert c.get("/api/auth/mode").json() == {"mode": "sso"}
        assert c.post("/api/auth/dev-login").status_code == 404
        assert c.post("/api/auth/iap-login").status_code == 401  # no signed header
        r = c.post("/api/auth/iap-login", headers={"x-goog-iap-jwt-assertion": "signed"})
        assert r.status_code == 200, r.text
        assert seen["audience"].endswith("/services/careplix-studio")
        assert seen["certs_url"] == "https://www.gstatic.com/iap/verify/public_key"
        me = c.get("/api/me").json()
        assert me["display_name"] == "Asha Rao" and me["role"] == "admin"


def test_iap_rejects_other_domains(tmp_path, monkeypatch):
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_token", lambda *a, **k: {"email": "someone@gmail.com"})
    with _sso_client(tmp_path) as c:
        r = c.post("/api/auth/iap-login", headers={"x-goog-iap-jwt-assertion": "signed"})
        assert r.status_code == 403


def test_studio_is_served_with_spa_fallback(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>studio</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    settings = make_settings(tmp_path, studio_dist=str(dist))
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        assert "studio" in c.get("/").text
        assert "studio" in c.get("/agents/123").text  # client-side route
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/nope").json()["error"]["code"] == "http_error"
