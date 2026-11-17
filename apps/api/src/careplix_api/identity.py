"""Customer sign-in (OPERATING_PLAN P1): verify an identity, then find the person's workspace.

Sign-in methods, each on only when configured:

- Google: the browser gets an ID token from Google Identity Services; we verify its signature,
  audience (our client id), issuer and ``email_verified``.
- Microsoft: an ID token from Microsoft identity platform (multi-tenant). Work and school accounts
  only count when the email domain is verified by the tenant (``xms_edov``), which closes the
  "nOAuth" email-spoofing hole; personal Microsoft accounts carry Microsoft-verified email.
- Email: a six-digit code, stored only as an HMAC, valid for ten minutes, five tries, rate limited.
- IAP (CarePlix staff behind Identity-Aware Proxy) and dev login keep working.

Access is by invitation: signing in with an invited address accepts the invitation. CarePlix staff
(platform admins) with no membership land in the lab workspace as admins, as before.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import environment, tenancy
from careplix_api.auth import Principal
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import (
    AuditEvent,
    EmailCode,
    Invitation,
    Membership,
    User,
    UserIdentity,
    Workspace,
)
from careplix_api.settings import Settings

log = logging.getLogger("careplix.identity")

ROLES = ("admin", "publisher", "author", "operator", "reviewer", "clinician")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MS_KEYS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
MS_PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
MAX_CODE_TRIES = 5


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    subject: str
    email: str
    name: str


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise ApiError(422, "invalid_email", "Enter a valid email address.")
    return email


def _name_from(email: str, name: str | None) -> str:
    return (name or "").strip()[:120] or email.split("@")[0].replace(".", " ").title()


# --- Google -------------------------------------------------------------------------------------


def _google_claims(credential: str, client_id: str) -> dict[str, Any]:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(credential, GoogleRequest(), audience=client_id)


async def verify_google(credential: str, settings: Settings) -> VerifiedIdentity:
    if not settings.google_client_id:
        raise ApiError(404, "not_found", "Google sign-in is not enabled.")
    try:
        claims = await asyncio.to_thread(_google_claims, credential, settings.google_client_id)
    except Exception as exc:
        raise ApiError(
            401, "unauthenticated", "Your Google sign-in could not be verified."
        ) from exc
    if claims.get("iss") not in GOOGLE_ISSUERS or not claims.get("email_verified"):
        raise ApiError(401, "unauthenticated", "Your Google account's email is not verified.")
    email = normalize_email(str(claims.get("email", "")))
    return VerifiedIdentity(
        "google", str(claims["sub"]), email, _name_from(email, claims.get("name"))
    )


# --- Microsoft ----------------------------------------------------------------------------------

_ms_jwks: jwt.PyJWKClient | None = None


def _ms_signing_key(token: str):
    global _ms_jwks
    if _ms_jwks is None:
        _ms_jwks = jwt.PyJWKClient(MS_KEYS_URL, cache_keys=True)
    return _ms_jwks.get_signing_key_from_jwt(token).key


async def verify_microsoft(token: str, settings: Settings) -> VerifiedIdentity:
    if not settings.microsoft_client_id:
        raise ApiError(404, "not_found", "Microsoft sign-in is not enabled.")
    try:
        key = await asyncio.to_thread(_ms_signing_key, token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.microsoft_client_id,
            options={"require": ["exp", "iss", "aud", "tid", "oid"]},
        )
    except Exception as exc:
        raise ApiError(
            401, "unauthenticated", "Your Microsoft sign-in could not be verified."
        ) from exc
    tid, oid = str(claims["tid"]), str(claims["oid"])
    if claims["iss"] != f"https://login.microsoftonline.com/{tid}/v2.0":
        raise ApiError(401, "unauthenticated", "Your Microsoft sign-in could not be verified.")
    email = str(claims.get("email") or "")
    personal = tid == MS_PERSONAL_TENANT
    verified = personal or claims.get("xms_edov") in (True, 1, "1", "true", "True")
    if not (email and verified):
        raise ApiError(
            401,
            "unverified_email",
            "Your organisation hasn't verified this email address with Microsoft. Use another "
            "sign-in method.",
        )
    email = normalize_email(email)
    return VerifiedIdentity(
        "microsoft", f"{tid}:{oid}", email, _name_from(email, claims.get("name"))
    )


# --- Email codes --------------------------------------------------------------------------------


def _code_hash(settings: Settings, email: str, code: str) -> str:
    return hmac.new(
        settings.signing_key().encode(), f"email-code:{email}:{code}".encode(), hashlib.sha256
    ).hexdigest()


async def send_email(settings: Settings, to: str, subject: str, text: str) -> bool:
    """Send a plain-text email through Resend. No PHI ever goes in these messages."""
    key = settings.resend_api_key.get_secret_value()
    if not (key and settings.email_from):
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "text": text},
        )
    if r.status_code >= 300:
        log.warning("email send failed: status=%s", r.status_code)
        return False
    return True


async def start_email_code(session: AsyncSession, settings: Settings, email: str) -> str | None:
    """Create and send a code. Returns the code only in dev without a sender (shown on screen)."""
    if not settings.email_sign_in:
        raise ApiError(404, "not_found", "Email sign-in is not enabled.")
    email = normalize_email(email)
    since = utcnow() - timedelta(hours=1)
    recent = await session.scalar(
        select(func.count())
        .select_from(EmailCode)
        .where(EmailCode.email == email, EmailCode.created_at >= since)
    )
    if (recent or 0) >= settings.email_codes_per_hour:
        raise ApiError(429, "too_many_codes", "Too many codes requested. Try again in an hour.")
    code = f"{secrets.randbelow(10**6):06d}"
    session.add(
        EmailCode(
            email=email,
            code_hash=_code_hash(settings, email, code),
            expires_at=utcnow() + timedelta(seconds=settings.email_code_ttl_s),
        )
    )
    await session.commit()
    minutes = settings.email_code_ttl_s // 60
    sent = await send_email(
        settings,
        email,
        f"Your CarePlix sign-in code: {code}",
        f"Your CarePlix sign-in code is {code}. It expires in {minutes} minutes.\n\n"
        "If you didn't ask for this, ignore this email.",
    )
    if sent:
        return None
    if settings.is_dev:
        return code
    raise ApiError(503, "email_unavailable", "We couldn't send the code. Try another method.")


async def verify_email_code(
    session: AsyncSession, settings: Settings, email: str, code: str
) -> VerifiedIdentity:
    email = normalize_email(email)
    row = await session.scalar(
        select(EmailCode)
        .where(EmailCode.email == email, EmailCode.used_at.is_(None))
        .order_by(EmailCode.created_at.desc())
        .limit(1)
    )
    wrong = ApiError(401, "invalid_code", "That code is wrong or has expired. Ask for a new one.")
    if row is None or as_utc(row.expires_at) < utcnow() or row.attempts >= MAX_CODE_TRIES:
        raise wrong
    row.attempts += 1
    ok = hmac.compare_digest(row.code_hash, _code_hash(settings, email, (code or "").strip()))
    if ok:
        row.used_at = utcnow()
    await session.commit()
    if not ok:
        raise wrong
    return VerifiedIdentity("email", email, email, _name_from(email, None))


# --- Establishing a session ---------------------------------------------------------------------


def _audit(session: AsyncSession, workspace_id: str, actor: str, action: str, target: str, **kw):
    session.add(
        AuditEvent(workspace_id=workspace_id, actor=actor, action=action, target=target, detail=kw)
    )


async def _user_for(session: AsyncSession, ident: VerifiedIdentity) -> User:
    link = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == ident.provider, UserIdentity.subject == ident.subject
        )
    )
    user = await session.get(User, link.user_id) if link else None
    if user is None:
        user = await session.scalar(select(User).where(User.email == ident.email))
    if user is None:
        user = User(email=ident.email, display_name=ident.name)
        session.add(user)
        await session.flush()
    if link is None:
        link = UserIdentity(user_id=user.id, provider=ident.provider, subject=ident.subject)
        session.add(link)
    link.last_seen_at = utcnow()
    return user


async def accept_invitations(session: AsyncSession, user: User) -> list[str]:
    """Accept every open invitation for the user's email; returns the workspaces joined."""
    now = utcnow()
    joined: list[str] = []
    for inv in await session.scalars(
        select(Invitation).where(
            Invitation.email == user.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ):
        if as_utc(inv.expires_at) < now:
            continue
        existing = await session.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.workspace_id == inv.workspace_id
            )
        )
        if existing is None:
            session.add(Membership(user_id=user.id, workspace_id=inv.workspace_id, role=inv.role))
            joined.append(inv.workspace_id)
        inv.accepted_at = now
        _audit(session, inv.workspace_id, user.id, "invitation.accepted", inv.id, role=inv.role)
    return joined


async def establish(
    session: AsyncSession,
    settings: Settings,
    ident: VerifiedIdentity,
    workspace_id: str | None = None,
) -> Principal:
    """Turn a verified identity into a principal for one of the person's workspaces."""
    with tenancy.unscoped():
        user = await _user_for(session, ident)
        joined = await accept_invitations(session, user)
        platform = settings.is_platform_admin(user.email)
        memberships = list(
            await session.scalars(select(Membership).where(Membership.user_id == user.id))
        )
        if not memberships and platform and environment.auto_provision_staff(settings):
            lab = await session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
            if lab is not None:
                m = Membership(user_id=user.id, workspace_id=lab.id, role=settings.iap_default_role)
                session.add(m)
                memberships = [m]
                _audit(session, lab.id, user.id, "user.provisioned", user.id, role=m.role)
        if not memberships:
            await session.commit()
            raise ApiError(
                403,
                "no_workspace",
                f"{user.email} doesn't have access to a CarePlix workspace yet. Ask your "
                "administrator to invite this address.",
            )
        chosen = next((m for m in memberships if m.workspace_id == workspace_id), None)
        chosen = chosen or next((m for m in memberships if m.workspace_id in joined), None)
        chosen = chosen or max(
            memberships,
            key=lambda m: as_utc(m.last_used_at) or as_utc(utcnow() - timedelta(days=3650)),
        )
        chosen.last_used_at = utcnow()
        _audit(
            session, chosen.workspace_id, user.id, "user.signed_in", user.id, method=ident.provider
        )
        await session.commit()
        return Principal(
            user.id,
            chosen.workspace_id,
            chosen.role,
            user.display_name,
            platform=platform,
            clinician=bool(chosen.clinician),
        )
