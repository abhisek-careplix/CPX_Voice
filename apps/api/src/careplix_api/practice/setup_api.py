"""Studio API for onboarding a clinic: add a practice, follow its setup, go-live, automation."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.connect.connectors import connector_for
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, PayerConnection, Workspace
from careplix_api.practice import automation, golive, onboarding
from careplix_api.practice.connections import EHR_KINDS, ensure_practice_connections
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter(prefix="/practices")


def _transport(request: Request):
    return getattr(request.app.state, "integration_transport", None)


async def _practice(session: AsyncSession, principal: Principal, conn_id: str) -> PayerConnection:
    conn = await session.get(PayerConnection, conn_id)
    if conn is None or conn.workspace_id != principal.workspace_id or conn.kind not in EHR_KINDS:
        raise ApiError(404, "not_found", "No practice with that id in this workspace.")
    return conn


async def _view(session: AsyncSession, conn: PayerConnection, settings: Settings) -> dict[str, Any]:
    ref = conn.auth_ref or {}
    connector = connector_for(conn, settings)
    workspace = await session.get(Workspace, conn.workspace_id)
    own = (ref.get("automation") or {}).get("sync_interval_s")
    return {
        "connection": {
            **connector.view(),
            "env": ref.get("env"),
            "subdomain": ref.get("subdomain"),
            "location_id": ref.get("location_id"),
            "expected_profile": ref.get("expected_profile"),
            "source": ref.get("source") or "env",
            "enabled": conn.enabled,
            "verified_at": conn.verified_at,
            "last_error": conn.last_error,
            "location": (conn.capability or {}).get("location"),
            "sync": (conn.capability or {}).get("sync") or [],
        },
        "onboarding": onboarding.status(conn),
        "golive": await golive.checklist(session, conn),
        "sync_interval_s": {
            "own": own,
            "effective": automation.sync_interval(
                conn, workspace, settings, connector.sync_interval_s()
            ),
        },
    }


@router.get("/setup/options")
async def options(
    principal: Principal = Depends(require("view")),
    settings: Settings = Depends(get_settings),
):
    """What the Add practice form offers in this environment."""
    envs = settings.nexhealth_envs

    def reason(env: str) -> str | None:
        if env == "production" and not settings.production_allowed:
            return "This is a sandbox deployment: production practices live in production."
        if env == "sandbox" and settings.production_allowed:
            return "This is a production deployment: sandbox practices live in labs."
        if env not in envs:
            return f"No NexHealth {env} key in this environment."
        if env == "production" and not principal.platform:
            return "CarePlix staff add production practices."
        return None

    return {
        "profiles": onboarding.nexhealth_profiles(),
        "envs": [
            {"env": e, "available": reason(e) is None, "reason": reason(e)}
            for e in ("sandbox", "production")
        ],
        "webhooks_ready": bool(settings.webhook_public_url),
    }


class PracticeIn(BaseModel):
    profile: str = Field(max_length=60)
    subdomain: str = Field(min_length=1, max_length=63)
    location_id: str = Field(min_length=1, max_length=12)
    env: Literal["sandbox", "production"] = "sandbox"
    name: str | None = Field(default=None, max_length=80)


@router.post("/setup")
async def add_practice(
    body: PracticeIn,
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Add a NexHealth practice, verify it, register its webhook and queue its first sync."""
    conn = await onboarding.create(
        session,
        settings,
        workspace_id=principal.workspace_id,
        actor=principal.user_id,
        platform=principal.platform,
        profile=body.profile,
        subdomain=body.subdomain,
        location_id=body.location_id,
        env=body.env,
        name=body.name,
    )
    await session.commit()  # the practice exists even if a later step fails
    await onboarding.run(session, conn, settings, _transport(request))
    await session.commit()
    return await _view(session, conn, settings)


@router.get("/setup/{conn_id}")
async def practice_setup(
    conn_id: str,
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    conn = await _practice(session, principal, conn_id)
    await onboarding.refresh_first_sync(session, conn)
    await session.commit()
    return await _view(session, conn, settings)


@router.post("/setup/{conn_id}/retry")
async def retry_setup(
    conn_id: str,
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Run verification and webhook registration again (e.g. after fixing the location id)."""
    conn = await _practice(session, principal, conn_id)
    if conn.kind != "nexhealth":
        raise ApiError(409, "not_nexhealth", "Setup steps apply to NexHealth practices.")
    await onboarding.run(session, conn, settings, _transport(request))
    await session.commit()
    return await _view(session, conn, settings)


class GoLiveTestIn(BaseModel):
    patient_id: str = Field(min_length=1, max_length=20)


@router.post("/setup/{conn_id}/golive-test")
async def golive_test(
    conn_id: str,
    body: GoLiveTestIn,
    request: Request,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Book, confirm and cancel a visit for the practice's test patient (the worker follows it)."""
    from careplix_api.practice import intents

    conn = await _practice(session, principal, conn_id)
    test = await golive.start_test(
        session,
        conn,
        settings,
        _transport(request),
        patient_id=body.patient_id.strip(),
        actor=principal.user_id,
    )
    await session.commit()
    await intents.execute_approved(
        request.app.state.db, settings, _transport(request), [test["intents"]["book"]]
    )
    await session.refresh(conn)
    return await _view(session, conn, settings)


class SyncIntervalIn(BaseModel):
    sync_interval_s: float | None = None


@router.put("/setup/{conn_id}/sync-interval")
async def set_sync_interval(
    conn_id: str,
    body: SyncIntervalIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """This practice's own sync interval; null follows the workspace's."""
    conn = await _practice(session, principal, conn_id)
    clean = automation.validate({"sync_interval_s": body.sync_interval_s}, settings)
    ref = dict(conn.auth_ref or {})
    own = dict(ref.get("automation") or {})
    if clean["sync_interval_s"] is None:
        own.pop("sync_interval_s", None)
    else:
        own["sync_interval_s"] = clean["sync_interval_s"]
    ref["automation"] = own
    conn.auth_ref = ref
    await session.commit()
    return await _view(session, conn, settings)


async def _automation_view(
    session: AsyncSession, workspace_id: str, settings: Settings
) -> dict[str, Any]:
    workspace = await session.get(Workspace, workspace_id)
    return {
        "effective": automation.effective(workspace, settings).view(),
        "defaults": automation.defaults(settings).view(),
        "chosen": workspace.practice_automation or {},
        "clearinghouse_production_available": settings.production_allowed
        and bool(settings.stedi_prod_api_key.get_secret_value()),
    }


@router.get("/automation")
async def get_automation(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await _automation_view(session, principal.workspace_id, settings)


class AutomationIn(BaseModel):
    receptionist_cancel_mode: Literal["approval", "auto"] | None = None
    backfill_enabled: bool | None = None
    eligibility_autopilot: bool | None = None
    sync_interval_s: float | None = None
    clearinghouse_env: Literal["test", "production"] | None = None


@router.put("/automation")
async def put_automation(
    body: AutomationIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Workspace automation choices. A field sent as null goes back to the env default."""
    changes = automation.validate({k: getattr(body, k) for k in body.model_fields_set}, settings)
    workspace = await session.get(Workspace, principal.workspace_id)
    before = automation.effective(workspace, settings).clearinghouse_env
    automation.apply(workspace, changes)
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="practice.automation_settings",
            target=principal.workspace_id,
            detail={"fields": sorted(changes)},
        )
    )
    await session.commit()
    if automation.effective(workspace, settings).clearinghouse_env != before:
        # Switch the workspace's clearinghouse connection to the other mode's key.
        await ensure_practice_connections(session, settings)
    return await _automation_view(session, principal.workspace_id, settings)


@router.get("/setup")
async def list_setups(
    principal: Principal = Depends(require("view")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Every practice in the workspace with its setup and go-live state."""
    rows = list(
        await session.scalars(
            select(PayerConnection)
            .where(
                PayerConnection.workspace_id == principal.workspace_id,
                PayerConnection.kind.in_(EHR_KINDS),
            )
            .order_by(PayerConnection.created_at)
        )
    )
    return [await _view(session, c, settings) for c in rows]
