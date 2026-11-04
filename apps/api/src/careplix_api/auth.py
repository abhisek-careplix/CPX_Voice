"""Identity, roles and worker credentials.

- Users: workspace and role come from the server-side membership, never from the client (SEC02).
  Development uses a dev-login session cookie; OIDC replaces it before any shared environment.
- Workers: a shared service credential is exchanged once per attempt for an attempt-scoped token
  (ADR09, SEC03). Every internal call is checked against the attempt in the token.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, Request
from sqlalchemy import select

from careplix_api.errors import ApiError
from careplix_api.settings import Settings, get_settings

SESSION_COOKIE = "cpx_session"
SESSION_TTL = timedelta(hours=12)
WORKER_AUDIENCE = "careplix-worker"
DEV_WORKER_TOKEN = "dev-only-worker-token"

# Doc 04 permission matrix as code. "admin" is the demo administrator.
PERMISSIONS: dict[str, frozenset[str]] = {
    "edit_draft": frozenset({"author", "publisher", "admin"}),
    "browser_test": frozenset({"author", "publisher", "admin"}),
    "publish": frozenset({"publisher", "admin"}),
    "start_task": frozenset({"operator", "publisher", "admin"}),
    "stop_calls": frozenset({"operator", "publisher", "admin"}),
    # Supervised browser runs: start one, approve its steps, take over (BROWSER_AGENT_PLAN §6a).
    "browser_supervise": frozenset({"operator", "publisher", "admin"}),
    "review": frozenset({"reviewer", "admin"}),
    "view": frozenset({"author", "operator", "publisher", "admin", "reviewer"}),
    "admin": frozenset({"admin"}),
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str
    role: str
    display_name: str

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS[permission]


def issue_session(settings: Settings, principal: Principal) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": principal.user_id,
            "ws": principal.workspace_id,
            "role": principal.role,
            "name": principal.display_name,
            "iat": now,
            "exp": now + SESSION_TTL,
            "aud": "careplix-studio",
        },
        settings.signing_key(),
        algorithm="HS256",
    )


async def current_principal(
    request: Request, settings: Settings = Depends(get_settings)
) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            claims = jwt.decode(
                token, settings.signing_key(), algorithms=["HS256"], audience="careplix-studio"
            )
        except jwt.PyJWTError:
            claims = None
        if claims:
            # Role and workspace come from the current membership, not the token, so revoked or
            # changed access takes effect immediately (SEC02).
            from careplix_api.models import Membership

            async with request.app.state.db.sessions() as session:
                membership = await session.scalar(
                    select(Membership).where(
                        Membership.user_id == claims["sub"],
                        Membership.workspace_id == claims["ws"],
                    )
                )
            if membership is not None:
                return Principal(claims["sub"], claims["ws"], membership.role, claims["name"])
    raise ApiError(401, "unauthenticated", "Sign in to continue.")


def require(permission: str):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(permission):
            raise ApiError(403, "forbidden", "Your role does not allow this action.")
        return principal

    return dependency


# --- Worker credentials --------------------------------------------------------------------------


def _worker_service_token(settings: Settings) -> str:
    token = settings.worker_service_token.get_secret_value()
    if token:
        return token
    if settings.is_dev:
        return DEV_WORKER_TOKEN
    raise ApiError(503, "worker_access_not_configured", "Worker access is not configured.")


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


def require_worker_service(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if not hmac.compare_digest(_bearer(request).encode(), _worker_service_token(settings).encode()):
        raise ApiError(401, "unauthenticated", "Invalid worker credential.")


def issue_attempt_token(settings: Settings, attempt_id: str, workspace_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": attempt_id,
            "ws": workspace_id,
            "aud": WORKER_AUDIENCE,
            "iat": now,
            "exp": now
            + timedelta(seconds=settings.max_call_duration_s + settings.max_hold_s + 600),
        },
        settings.signing_key(),
        algorithm="HS256",
    )


@dataclass(frozen=True)
class AttemptGrant:
    attempt_id: str
    workspace_id: str


def require_attempt_token(
    attempt_id: str, request: Request, settings: Settings = Depends(get_settings)
) -> AttemptGrant:
    try:
        claims = jwt.decode(
            _bearer(request), settings.signing_key(), algorithms=["HS256"], audience=WORKER_AUDIENCE
        )
    except jwt.PyJWTError as exc:
        raise ApiError(401, "unauthenticated", "Invalid or expired attempt token.") from exc
    if claims["sub"] != attempt_id:
        raise ApiError(403, "forbidden", "Token is not valid for this attempt.")
    return AttemptGrant(claims["sub"], claims["ws"])


# --- Identity-Aware Proxy (cloud) ---------------------------------------------------------------

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


def verify_iap_assertion(request: Request, settings: Settings) -> str:
    """Return the signed-in Google account email from IAP's signed header, or raise 401.

    Only the signed ``x-goog-iap-jwt-assertion`` counts; plain identity headers are spoofable if
    traffic ever bypasses IAP. The audience pins the token to this exact service.
    """
    import logging

    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
    if not (settings.iap_audience and assertion):
        raise ApiError(401, "unauthenticated", "Sign in through CarePlix single sign-on.")
    try:
        claims = id_token.verify_token(
            assertion, GoogleRequest(), audience=settings.iap_audience, certs_url=IAP_CERTS_URL
        )
    except Exception as exc:
        try:
            seen_aud = jwt.decode(assertion, options={"verify_signature": False}).get("aud")
        except jwt.PyJWTError:
            seen_aud = "unreadable"
        # The audience is a resource path, not personal data; logging it makes misconfig obvious.
        logging.getLogger("careplix.auth").warning("IAP assertion rejected; token aud=%s", seen_aud)
        raise ApiError(401, "unauthenticated", "Your sign-in could not be verified.") from exc
    email = str(claims.get("email", "")).lower()
    if not email.endswith("@" + settings.iap_allowed_domain.lower()):
        raise ApiError(403, "forbidden", "This account is not allowed in this workspace.")
    return email
