"""Customer sign-in, invitations, product lines and tenant isolation (OPERATING_PLAN P1).

The demo admin (CarePlix staff in dev) creates a customer workspace; its invited admin signs in
with an email code. Nothing the customer creates may reach the demo workspace, through any list
or by id, and nothing written in one workspace's request may land in another.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from careplix_api import identity, tenancy
from careplix_api.db import Database, utcnow
from careplix_api.errors import ApiError
from careplix_api.main import create_app
from careplix_api.models import Case, EmailCode, PayerConnection, Workspace

from .conftest import FakeGateway, make_settings

CLINIC_ADMIN = "bea.admin@clinic-b.test"


def _email_sign_in(c: TestClient, email: str) -> dict:
    code = c.post("/api/auth/email/start", json={"email": email}).json()["dev_code"]
    r = c.post("/api/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _new_customer(c: TestClient, line: str = "home_health") -> str:
    r = c.post(
        "/api/platform/workspaces",
        json={"name": "Clinic B (synthetic)", "product_line": line, "admin_email": CLINIC_ADMIN},
    )
    assert r.status_code == 200, r.text
    return r.json()["workspace_id"]


def _dev_login(c: TestClient) -> None:
    c.cookies.clear()
    c.post("/api/auth/dev-login").raise_for_status()


def test_invited_admin_signs_in_to_their_own_workspace(client):
    assert client.get("/api/me").json()["platform"] is True
    ws_b = _new_customer(client)

    client.cookies.clear()
    me = _email_sign_in(client, CLINIC_ADMIN)
    assert me["workspace_id"] == ws_b and me["role"] == "admin" and me["platform"] is False
    full = client.get("/api/me").json()
    assert full["product_line"] == "home_health"
    assert full["pack"]["label"] == "CarePlix Home Health"
    assert "practice" not in full["pack"]["sections"]
    # Only the workspaces this person belongs to.
    assert [w["workspace_id"] for w in client.get("/api/auth/workspaces").json()] == [ws_b]


def test_nothing_a_customer_creates_reaches_another_workspace(client):
    demo_ws = client.get("/api/me").json()["workspace_id"]
    demo_cases = {c["id"] for c in client.get("/api/cases").json()}
    ws_b = _new_customer(client)

    client.cookies.clear()
    _email_sign_in(client, CLINIC_ADMIN)
    made = client.post("/api/cases/samples", json={"case_type": "eligibility_check", "count": 2})
    assert made.status_code == 200, made.text
    b_ids = {c["id"] for c in made.json()}
    inv = client.post("/api/workspace/invitations", json={"email": "cal@clinic-b.test"}).json()
    secrets_of_b = b_ids | {inv["id"], ws_b, "cal@clinic-b.test", CLINIC_ADMIN}
    # The customer sees its own cases and none of the demo workspace's.
    seen = {c["id"] for c in client.get("/api/cases").json()}
    assert seen == b_ids and not seen & demo_cases

    _dev_login(client)
    assert client.get("/api/me").json()["workspace_id"] == demo_ws
    for case_id in b_ids:
        assert client.get(f"/api/cases/{case_id}").status_code == 404
    assert client.delete(f"/api/workspace/invitations/{inv['id']}").status_code == 404

    # Every list in the Studio API, as the demo admin: none of B's ids or addresses appear.
    leaks = []
    for route in client.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if "{" in route.path or not route.path.startswith("/api/") or "/platform/" in route.path:
            continue
        r = client.get(route.path)
        if r.status_code == 200:
            body = r.text
            leaks += [(route.path, s) for s in secrets_of_b if s in body]
    assert leaks == []


def test_a_write_tagged_with_another_workspace_is_refused(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path}/w.db")
        tenancy.install()
        await db.create_all()
        async with db.sessions() as s:
            a, b = Workspace(name="A"), Workspace(name="B")
            s.add_all([a, b])
            await s.commit()
            ids = a.id, b.id
        tenancy.set_tenant(ids[0])
        try:
            async with db.sessions() as s:
                assert [w.id for w in await s.scalars(select(Workspace))] == [ids[0]]
                s.add(Case(workspace_id=ids[1], case_ref="X", data={}))
                with pytest.raises(ApiError):
                    await s.flush()
        finally:
            tenancy.set_tenant(None)
            await db.dispose()

    asyncio.run(run())


def test_the_product_line_limits_case_types(client):
    _new_customer(client, line="home_health")
    client.cookies.clear()
    _email_sign_in(client, CLINIC_ADMIN)
    offered = {t["id"] for t in client.get("/api/case-types").json()}
    assert "appointment_reminder" not in offered and "eligibility_check" in offered
    r = client.post("/api/cases/samples", json={"case_type": "appointment_reminder", "count": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "case_type_not_in_line"


def test_customer_workspaces_never_get_the_lab_ehr(tmp_path):
    settings = make_settings(tmp_path, nexhealth_api_key="nk", nexhealth_subdomain="lab")
    with TestClient(create_app(settings, FakeGateway(settings))) as c:
        c.post("/api/auth/dev-login").raise_for_status()
        ws_b = _new_customer(c, line="practice")

        async def kinds(ws_id):
            async with c.app.state.db.sessions() as s:
                rows = await s.scalars(
                    select(PayerConnection.kind).where(PayerConnection.workspace_id == ws_id)
                )
                return set(rows)

        demo = c.get("/api/me").json()["workspace_id"]
        assert "nexhealth" in c.portal.call(kinds, demo)
        assert "nexhealth" not in c.portal.call(kinds, ws_b)


def test_people_without_an_invitation_get_no_workspace(client):
    client.cookies.clear()
    code = client.post("/api/auth/email/start", json={"email": "stranger@else.test"}).json()
    r = client.post(
        "/api/auth/email/verify", json={"email": "stranger@else.test", "code": code["dev_code"]}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "no_workspace"
    assert client.get("/api/me").status_code == 401


def test_email_codes_are_single_use_limited_and_expire(client):
    email = "limits@clinic-b.test"
    client.cookies.clear()
    code = client.post("/api/auth/email/start", json={"email": email}).json()["dev_code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = client.post("/api/auth/email/verify", json={"email": email, "code": wrong})
        assert r.status_code == 401
    # Five wrong tries burn the code, even the right one fails now.
    assert client.post("/api/auth/email/verify", json={"email": email, "code": code}).status_code
    # Five codes an hour.
    for _ in range(4):
        assert client.post("/api/auth/email/start", json={"email": email}).status_code == 200
    r = client.post("/api/auth/email/start", json={"email": email})
    assert r.status_code == 429

    async def expire_all():
        async with client.app.state.db.sessions() as s:
            for row in await s.scalars(select(EmailCode)):
                row.expires_at = utcnow() - timedelta(seconds=1)
                row.created_at = utcnow() - timedelta(hours=2)
            await s.commit()

    client.portal.call(expire_all)
    code = client.post("/api/auth/email/start", json={"email": email}).json()["dev_code"]
    client.portal.call(expire_all)
    assert client.post("/api/auth/email/verify", json={"email": email, "code": code}).status_code


def test_admins_manage_members_but_never_remove_the_last_admin(client):
    _new_customer(client)
    client.cookies.clear()
    me = _email_sign_in(client, CLINIC_ADMIN)
    r = client.post(
        "/api/workspace/invitations", json={"email": "cal@clinic-b.test", "role": "reviewer"}
    )
    assert r.status_code == 200
    assert (
        client.post("/api/workspace/invitations", json={"email": CLINIC_ADMIN}).status_code == 409
    )
    assert (
        client.patch(
            f"/api/workspace/members/{me['user_id']}", json={"role": "operator"}
        ).status_code
        == 409
    )
    assert client.delete(f"/api/workspace/members/{me['user_id']}").status_code == 409

    client.cookies.clear()
    cal = _email_sign_in(client, "cal@clinic-b.test")
    assert cal["role"] == "reviewer"
    assert (
        client.post("/api/workspace/invitations", json={"email": "dee@clinic-b.test"}).status_code
        == 403
    )
    members = client.get("/api/workspace/members").json()
    assert {m["email"] for m in members["members"]} == {CLINIC_ADMIN, "cal@clinic-b.test"}
    assert members["invitations"] == []  # only admins see open invitations

    client.cookies.clear()
    _email_sign_in(client, CLINIC_ADMIN)
    assert (
        client.patch(f"/api/workspace/members/{cal['user_id']}", json={"role": "admin"}).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/workspace/members/{me['user_id']}", json={"role": "operator"}
        ).status_code
        == 200
    )


def test_only_carePlix_staff_manage_workspaces_and_shared_telephony(client):
    _new_customer(client)
    client.cookies.clear()
    _email_sign_in(client, CLINIC_ADMIN)
    assert client.get("/api/platform/workspaces").status_code == 403
    assert (
        client.post(
            "/api/platform/workspaces",
            json={"name": "Sneaky", "admin_email": "x@y.test"},
        ).status_code
        == 403
    )
    r = client.put("/api/connections/outbound", json={"approved_destinations": ["+12025550199"]})
    assert r.status_code == 403


def test_switching_workspaces_needs_a_membership(client):
    ws_b = _new_customer(client)
    assert client.post("/api/auth/workspace", json={"workspace_id": ws_b}).status_code == 404
    assert client.post(f"/api/platform/workspaces/{ws_b}/join").status_code == 200
    r = client.post("/api/auth/workspace", json={"workspace_id": ws_b})
    assert r.status_code == 200 and r.json()["workspace_id"] == ws_b
    assert client.get("/api/me").json()["workspace_name"] == "Clinic B (synthetic)"


# --- Google and Microsoft -----------------------------------------------------------------------


def _app_client(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        careplix_env="labs",
        session_secret="s" * 48,
        google_client_id="gid.apps.googleusercontent.com",
        microsoft_client_id="11111111-2222-3333-4444-555555555555",
        **overrides,
    )
    return TestClient(create_app(settings, FakeGateway(settings)), base_url="https://testserver")


def _invite_through_db(c: TestClient, email: str) -> None:
    from careplix_api.models import Invitation

    async def add():
        async with c.app.state.db.sessions() as s:
            ws = await s.scalar(select(Workspace))
            s.add(
                Invitation(
                    workspace_id=ws.id,
                    email=email,
                    role="operator",
                    expires_at=utcnow() + timedelta(days=1),
                )
            )
            await s.commit()

    c.portal.call(add)


def test_google_sign_in_needs_a_verified_email(tmp_path, monkeypatch):
    claims = {"iss": "https://accounts.google.com", "sub": "g-1", "email": "Gia@Clinic.test"}
    monkeypatch.setattr(identity, "_google_claims", lambda cred, cid: dict(claims))
    with _app_client(tmp_path) as c:
        mode = c.get("/api/auth/mode").json()
        assert mode["mode"] == "app" and mode["methods"]["google"] and not mode["methods"]["email"]
        _invite_through_db(c, "gia@clinic.test")
        assert c.post("/api/auth/google", json={"credential": "x" * 40}).status_code == 401
        claims["email_verified"] = True
        r = c.post("/api/auth/google", json={"credential": "x" * 40})
        assert r.status_code == 200 and r.json()["role"] == "operator"
        assert c.get("/api/me").json()["email"] == "gia@clinic.test"


def _ms_token(key, **claims) -> str:
    now = int(time.time())
    base = {"aud": "11111111-2222-3333-4444-555555555555", "exp": now + 600, "iat": now}
    return jwt.encode({**base, **claims}, key, algorithm="RS256")


def test_microsoft_sign_in_needs_a_domain_verified_email(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(identity, "_ms_signing_key", lambda token: key.public_key())
    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    good = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "tid": tid,
        "oid": "o-1",
        "email": "mo@clinic.test",
        "name": "Mo Clinic",
    }
    with _app_client(tmp_path) as c:
        _invite_through_db(c, "mo@clinic.test")
        # nOAuth: an unverified email claim from a work tenant is not trusted.
        r = c.post("/api/auth/microsoft", json={"id_token": _ms_token(key, **good)})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unverified_email"
        bad_iss = {**good, "iss": "https://evil.test/v2.0", "xms_edov": True}
        r = c.post("/api/auth/microsoft", json={"id_token": _ms_token(key, **bad_iss)})
        assert r.status_code == 401
        r = c.post("/api/auth/microsoft", json={"id_token": _ms_token(key, **good, xms_edov=True)})
        assert r.status_code == 200, r.text
        assert c.get("/api/me").json()["display_name"] == "Mo Clinic"


def test_email_codes_are_off_outside_dev_without_a_sender(tmp_path):
    with _app_client(tmp_path) as c:
        r = c.post("/api/auth/email/start", json={"email": "a@b.test"})
        assert r.status_code == 404
        assert json.loads(c.get("/api/auth/mode").text)["methods"]["email"] is False
