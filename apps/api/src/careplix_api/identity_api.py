"""Sign-in, workspace switching, members and invitations, and platform workspace management."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import identity, packs, tenancy
from careplix_api.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    Principal,
    issue_session,
    require,
    require_platform,
)
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Invitation, Membership, User, Workspace
from careplix_api.settings import Settings, get_settings

router = APIRouter()


async def get_session(request: Request):
    async with request.app.state.db.sessions() as session:
        yield session


Session = Depends(get_session)
admin = require("admin")
viewer = require("view")
Role = Literal["admin", "publisher", "author", "operator", "reviewer"]
Line = Literal["practice", "home_health"]


def set_session_cookie(response: Response, settings: Settings, principal: Principal) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(settings, principal),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=not settings.is_dev,
    )


def me_payload(p: Principal) -> dict[str, Any]:
    from careplix_api.auth import PERMISSIONS

    return {
        "user_id": p.user_id,
        "display_name": p.display_name,
        "role": p.role,
        "workspace_id": p.workspace_id,
        "platform": p.platform,
        "permissions": sorted(k for k, roles in PERMISSIONS.items() if p.role in roles),
    }


# --- Sign-in ------------------------------------------------------------------------------------


@router.get("/auth/mode")
async def auth_mode(settings: Settings = Depends(get_settings)):
    iap = bool(settings.iap_audience)
    return {
        # dev: demo button; sso: behind IAP (CarePlix staff); app: customer sign-in methods
        "mode": "dev" if settings.is_dev else ("sso" if iap else "app"),
        "methods": {
            "dev": settings.is_dev,
            "iap": iap,
            "google": bool(settings.google_client_id),
            "microsoft": bool(settings.microsoft_client_id),
            "email": settings.email_sign_in,
        },
        "google_client_id": settings.google_client_id or None,
        "microsoft_client_id": settings.microsoft_client_id or None,
    }


class GoogleIn(BaseModel):
    credential: str = Field(min_length=20, max_length=8000)


class MicrosoftIn(BaseModel):
    id_token: str = Field(min_length=20, max_length=8000)


class EmailStartIn(BaseModel):
    email: str = Field(max_length=200)


class EmailVerifyIn(BaseModel):
    email: str = Field(max_length=200)
    code: str = Field(min_length=6, max_length=6)


async def _signed_in(response, session, settings, ident) -> dict[str, Any]:
    principal = await identity.establish(session, settings, ident)
    set_session_cookie(response, settings, principal)
    return me_payload(principal)


@router.post("/auth/google")
async def google_sign_in(
    body: GoogleIn,
    response: Response,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    ident = await identity.verify_google(body.credential, settings)
    return await _signed_in(response, session, settings, ident)


@router.post("/auth/microsoft")
async def microsoft_sign_in(
    body: MicrosoftIn,
    response: Response,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    ident = await identity.verify_microsoft(body.id_token, settings)
    return await _signed_in(response, session, settings, ident)


@router.post("/auth/email/start")
async def email_start(
    body: EmailStartIn, session: AsyncSession = Session, settings: Settings = Depends(get_settings)
):
    dev_code = await identity.start_email_code(session, settings, body.email)
    return {"sent": True, "dev_code": dev_code}


@router.post("/auth/email/verify")
async def email_verify(
    body: EmailVerifyIn,
    response: Response,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    ident = await identity.verify_email_code(session, settings, body.email, body.code)
    return await _signed_in(response, session, settings, ident)


@router.post("/auth/iap-login")
async def iap_login(
    request: Request,
    response: Response,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Exchange the IAP-verified Google identity for a Studio session (CarePlix staff)."""
    from careplix_api.auth import verify_iap_assertion

    email = verify_iap_assertion(request, settings)
    ident = identity.VerifiedIdentity("iap", email, email, identity._name_from(email, None))
    return await _signed_in(response, session, settings, ident)


# --- Workspaces the signed-in person belongs to -------------------------------------------------


@router.get("/auth/workspaces")
async def my_workspaces(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    with tenancy.unscoped():  # the switcher lists every workspace this person belongs to
        rows = (
            await session.execute(
                select(Membership.role, Workspace.id, Workspace.name, Workspace.product_line)
                .join(Workspace, Workspace.id == Membership.workspace_id)
                .where(Membership.user_id == principal.user_id)
                .order_by(Workspace.name)
            )
        ).all()
    return [
        {
            "workspace_id": r.id,
            "name": r.name,
            "product_line": packs.line_of(r.product_line),
            "role": r.role,
            "current": r.id == principal.workspace_id,
        }
        for r in rows
    ]


class SwitchIn(BaseModel):
    workspace_id: str = Field(max_length=36)


@router.post("/auth/workspace")
async def switch_workspace(
    body: SwitchIn,
    response: Response,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    with tenancy.unscoped():
        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == principal.user_id,
                Membership.workspace_id == body.workspace_id,
            )
        )
        if membership is None:
            raise ApiError(404, "not_found", "You don't belong to that workspace.")
        membership.last_used_at = utcnow()
        await session.commit()
    switched = Principal(
        principal.user_id,
        membership.workspace_id,
        membership.role,
        principal.display_name,
        platform=principal.platform,
    )
    set_session_cookie(response, settings, switched)
    return me_payload(switched)


# --- Members and invitations (workspace admins) ------------------------------------------------


def _audit(session, principal: Principal, action: str, target: str, **detail) -> None:
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=target,
            detail=detail,
        )
    )


@router.get("/workspace/members")
async def members(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.workspace_id == principal.workspace_id)
            .order_by(User.display_name)
        )
    ).all()
    out: dict[str, Any] = {
        "members": [
            {
                "user_id": u.id,
                "display_name": u.display_name,
                "email": u.email,
                "role": m.role,
                "last_used_at": as_utc(m.last_used_at),
                "you": u.id == principal.user_id,
            }
            for m, u in rows
        ],
        "invitations": [],
    }
    if principal.can("admin"):
        now = utcnow()
        for inv in await session.scalars(
            select(Invitation)
            .where(
                Invitation.workspace_id == principal.workspace_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .order_by(Invitation.created_at.desc())
        ):
            out["invitations"].append(
                {
                    "id": inv.id,
                    "email": inv.email,
                    "role": inv.role,
                    "expires_at": as_utc(inv.expires_at),
                    "expired": as_utc(inv.expires_at) < now,
                }
            )
    return out


class InviteIn(BaseModel):
    email: str = Field(max_length=200)
    role: Role = "operator"


async def create_invitation(
    session: AsyncSession,
    settings: Settings,
    workspace: Workspace,
    email: str,
    role: str,
    invited_by: str,
) -> tuple[Invitation, bool]:
    email = identity.normalize_email(email)
    open_inv = await session.scalar(
        select(Invitation).where(
            Invitation.workspace_id == workspace.id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if open_inv is not None:
        open_inv.revoked_at = utcnow()  # a new invitation replaces the old one
    inv = Invitation(
        workspace_id=workspace.id,
        email=email,
        role=role,
        invited_by=invited_by,
        expires_at=utcnow() + timedelta(days=settings.invitation_days),
    )
    session.add(inv)
    await session.flush()
    where = f" at {settings.studio_url}" if settings.studio_url else ""
    emailed = await identity.send_email(
        settings,
        email,
        f"You're invited to {workspace.name} on CarePlix",
        f"You've been invited to join {workspace.name} on CarePlix as {role}.\n\n"
        f"Sign in{where} with this email address ({email}) within "
        f"{settings.invitation_days} days to accept.",
    )
    return inv, emailed


@router.post("/workspace/invitations")
async def invite(
    body: InviteIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    email = identity.normalize_email(body.email)
    already = await session.scalar(
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == principal.workspace_id, User.email == email)
    )
    if already is not None:
        raise ApiError(409, "already_member", f"{email} is already a member.")
    workspace = await session.get(Workspace, principal.workspace_id)
    inv, emailed = await create_invitation(
        session, settings, workspace, email, body.role, principal.user_id
    )
    _audit(session, principal, "invitation.created", inv.id, role=body.role, emailed=emailed)
    await session.commit()
    return {"id": inv.id, "email": inv.email, "role": inv.role, "emailed": emailed}


@router.delete("/workspace/invitations/{invitation_id}")
async def revoke_invitation(
    invitation_id: str, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    inv = await session.get(Invitation, invitation_id)
    if inv is None or inv.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Invitation not found.")
    inv.revoked_at = utcnow()
    _audit(session, principal, "invitation.revoked", inv.id)
    await session.commit()
    return {"ok": True}


async def _admins(session: AsyncSession, workspace_id: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.workspace_id == workspace_id, Membership.role == "admin")
    )


async def _membership(session: AsyncSession, principal: Principal, user_id: str) -> Membership:
    m = await session.scalar(
        select(Membership).where(
            Membership.workspace_id == principal.workspace_id, Membership.user_id == user_id
        )
    )
    if m is None:
        raise ApiError(404, "not_found", "Member not found.")
    return m


class RoleIn(BaseModel):
    role: Role


@router.patch("/workspace/members/{user_id}")
async def change_role(
    user_id: str,
    body: RoleIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
):
    m = await _membership(session, principal, user_id)
    if m.role == "admin" and body.role != "admin" and await _admins(session, m.workspace_id) <= 1:
        raise ApiError(409, "last_admin", "A workspace needs at least one admin.")
    before, m.role = m.role, body.role
    _audit(session, principal, "member.role_changed", user_id, before=before, after=body.role)
    await session.commit()
    return {"ok": True}


@router.delete("/workspace/members/{user_id}")
async def remove_member(
    user_id: str, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    m = await _membership(session, principal, user_id)
    if m.role == "admin" and await _admins(session, m.workspace_id) <= 1:
        raise ApiError(409, "last_admin", "A workspace needs at least one admin.")
    await session.delete(m)
    _audit(session, principal, "member.removed", user_id)
    await session.commit()
    return {"ok": True}


# --- Platform: customer workspaces (CarePlix staff) ---------------------------------------------


@router.get("/platform/workspaces")
async def list_workspaces(
    principal: Principal = Depends(require_platform), session: AsyncSession = Session
):
    with tenancy.unscoped():
        counts = dict(
            (
                await session.execute(
                    select(Membership.workspace_id, func.count()).group_by(Membership.workspace_id)
                )
            ).all()
        )
        rows = list(await session.scalars(select(Workspace).order_by(Workspace.created_at)))
        mine = set(
            await session.scalars(
                select(Membership.workspace_id).where(Membership.user_id == principal.user_id)
            )
        )
    return [
        {
            "workspace_id": w.id,
            "name": w.name,
            "product_line": packs.line_of(w.product_line),
            "members": counts.get(w.id, 0),
            "lab": w.env_integrations is not False,
            "created_at": as_utc(w.created_at),
            "member_of": w.id in mine,
        }
        for w in rows
    ]


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    product_line: Line = "practice"
    admin_email: str = Field(max_length=200)


@router.post("/platform/workspaces")
async def create_workspace(
    body: WorkspaceIn,
    principal: Principal = Depends(require_platform),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """A customer workspace starts empty, with no lab EHR, and an invitation for its admin."""
    from careplix_api.practice.connections import ensure_practice_connections
    from careplix_api.seed import ensure_reference_connections

    with tenancy.unscoped():
        ws = Workspace(
            name=body.name.strip(), product_line=body.product_line, env_integrations=False
        )
        session.add(ws)
        await session.flush()
        inv, emailed = await create_invitation(
            session, settings, ws, body.admin_email, "admin", principal.user_id
        )
        session.add(
            AuditEvent(
                workspace_id=ws.id,
                actor=principal.user_id,
                action="workspace.created",
                target=ws.id,
                detail={"product_line": body.product_line, "invited": inv.id, "emailed": emailed},
            )
        )
        await session.commit()
        await ensure_reference_connections(session)
        await ensure_practice_connections(session, settings)
    return {"workspace_id": ws.id, "invitation_id": inv.id, "emailed": emailed}


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    product_line: Line | None = None


@router.patch("/platform/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: WorkspacePatch,
    principal: Principal = Depends(require_platform),
    session: AsyncSession = Session,
):
    with tenancy.unscoped():
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            raise ApiError(404, "not_found", "Workspace not found.")
        if body.name:
            ws.name = body.name.strip()
        if body.product_line:
            ws.product_line = body.product_line
        session.add(
            AuditEvent(
                workspace_id=ws.id,
                actor=principal.user_id,
                action="workspace.updated",
                target=ws.id,
                detail=body.model_dump(exclude_none=True),
            )
        )
        await session.commit()
    return {"ok": True}


class JoinIn(BaseModel):
    role: Role = "admin"


@router.post("/platform/workspaces/{workspace_id}/join")
async def join_workspace(
    workspace_id: str,
    principal: Principal = Depends(require_platform),
    session: AsyncSession = Session,
):
    """CarePlix staff join a customer workspace (for onboarding and support), audited there."""
    with tenancy.unscoped():
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            raise ApiError(404, "not_found", "Workspace not found.")
        existing = await session.scalar(
            select(Membership).where(
                Membership.user_id == principal.user_id, Membership.workspace_id == ws.id
            )
        )
        if existing is None:
            session.add(Membership(user_id=principal.user_id, workspace_id=ws.id, role="admin"))
            session.add(
                AuditEvent(
                    workspace_id=ws.id,
                    actor=principal.user_id,
                    action="platform.joined",
                    target=principal.user_id,
                    detail={},
                )
            )
            await session.commit()
    return {"ok": True}
