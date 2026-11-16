"""Add a practice from the Studio: pick the EHR, enter NexHealth's subdomain and location.

Until NexHealth approves its v3.1 onboarding link, a practice is identified by the subdomain and
location id NexHealth shows in its Developer Portal. The flow:

1. Create the connection, disabled (the worker ignores it): EHR profile, env, subdomain, location.
2. Verify: the env's key reaches the subdomain and the location exists (the capability probe).
3. Register the signed per-connection webhook (needs WEBHOOK_PUBLIC_URL).
4. Enable it: the practice worker's next pass runs the first full sync.

Each step's outcome is kept on the connection (``auth_ref.onboarding``) so the Studio can show
progress and retry a step. Production practices are added by CarePlix staff only, and one
location belongs to one workspace: the subdomain alone doesn't prove a clinic is yours, and
CarePlix's production key reaches every practice that authorised it.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api import environment, tenancy
from careplix_api.connect.catalog import PROFILES
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations.nexhealth import NexHealthError
from careplix_api.models import AuditEvent, PayerConnection, SyncCursor
from careplix_api.practice import webhooks
from careplix_api.practice.connections import EHR, verify
from careplix_api.settings import Settings

SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LOCATION = re.compile(r"^[0-9]{1,12}$")
STEPS = ("created", "verified", "webhooks", "first_sync")


def nexhealth_profiles() -> list[dict[str, Any]]:
    return [p.view() for p in PROFILES.values() if p.path == "nexhealth"]


def _onboarding(conn: PayerConnection) -> dict[str, Any]:
    return dict((conn.auth_ref or {}).get("onboarding") or {})


def _step(conn: PayerConnection, key: str, ok: bool | None, detail: str) -> None:
    state = _onboarding(conn)
    steps = dict(state.get("steps") or {})
    steps[key] = {"ok": ok, "detail": detail[:240], "at": utcnow().isoformat()}
    state["steps"] = steps
    conn.auth_ref = {**(conn.auth_ref or {}), "onboarding": state}


async def _claimed_elsewhere(
    session: AsyncSession, workspace_id: str, subdomain: str, location: str
) -> PayerConnection | None:
    """Any connection (in any workspace) for this NexHealth location."""
    with tenancy.unscoped():
        rows = list(
            await session.scalars(select(PayerConnection).where(PayerConnection.kind == EHR))
        )
    for conn in rows:
        ref = conn.auth_ref or {}
        if ref.get("subdomain") == subdomain and str(ref.get("location_id")) == location:
            return conn
    return None


async def create(
    session: AsyncSession,
    settings: Settings,
    *,
    workspace_id: str,
    actor: str,
    platform: bool,
    profile: str,
    subdomain: str,
    location_id: str,
    env: str,
    name: str | None,
) -> PayerConnection:
    subdomain, location_id = subdomain.strip().lower(), str(location_id).strip()
    chosen = PROFILES.get(profile)
    if chosen is None or chosen.path != "nexhealth":
        raise ApiError(422, "unknown_profile", "Choose one of the EHRs NexHealth connects.")
    if not SUBDOMAIN.match(subdomain):
        raise ApiError(
            422,
            "invalid_subdomain",
            "The subdomain is the short name NexHealth shows (a-z, 0-9, -).",
        )
    if not LOCATION.match(location_id):
        raise ApiError(422, "invalid_location", "The location id is a number from NexHealth.")
    if env not in ("sandbox", "production"):
        raise ApiError(422, "invalid_env", "Choose sandbox or production.")
    if env == "production":
        environment.refuse_production_here("A production NexHealth connection", settings)
    else:
        environment.refuse_in_production("A NexHealth sandbox connection", settings)
    if env not in settings.nexhealth_envs:
        raise ApiError(
            409,
            "env_unavailable",
            f"This environment has no NexHealth {env} key, so {env} practices can't be added here.",
        )
    if env == "production" and not platform:
        raise ApiError(
            403,
            "forbidden",
            "CarePlix staff add production practices until NexHealth's onboarding link is live: "
            "the subdomain alone doesn't prove the practice is yours.",
        )
    existing = await _claimed_elsewhere(session, workspace_id, subdomain, location_id)
    if existing is not None:
        here = existing.workspace_id == workspace_id
        raise ApiError(
            409,
            "already_connected",
            "This location is already connected here."
            if here
            else "This location is connected to another workspace.",
        )
    label = (name or "").strip()[:80] or f"{chosen.name} · {subdomain}"
    conn = PayerConnection(
        workspace_id=workspace_id,
        name=label,
        kind=EHR,
        base_url=settings.nexhealth_base_url,
        ig_version="nexhealth-v3",
        auth_type="api_key",
        auth_ref={
            "source": "studio",
            "key_env": settings.nexhealth_key_env(env),
            "env": env,
            "subdomain": subdomain,
            "location_id": location_id,
            "expected_profile": chosen.id,
            "onboarding": {"started_at": utcnow().isoformat(), "started_by": actor, "steps": {}},
        },
        payer_names=["*"],
        synthetic=env == "sandbox",
        enabled=False,  # the worker leaves it alone until it is verified
    )
    session.add(conn)
    await session.flush()
    _step(conn, "created", True, f"{chosen.name} practice {subdomain}, location {location_id}.")
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor=actor,
            action="practice.added",
            target=conn.id,
            detail={"profile": chosen.id, "env": env},
        )
    )
    return conn


async def run(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    """Verify, then register webhooks, then hand the first full sync to the worker.

    Safe to run again: a verified practice is re-verified, webhooks are replaced (that also
    reactivates them) and an enabled practice stays enabled.
    """
    try:
        result = await verify(session, conn, settings, transport)
    except NexHealthError as err:  # e.g. no key for this env any more
        result = {"ok": False, "error": err.message}
    if not result.get("ok"):
        detail = result.get("error") or conn.last_error or "NexHealth didn't find that location."
        _step(conn, "verified", False, f"{detail} Check the subdomain and location id.")
        return status(conn)
    location = ((conn.capability or {}).get("location") or {}).get("name") or "the location"
    _step(conn, "verified", True, f"NexHealth reaches {location}.")
    if not settings.webhook_public_url:
        _step(
            conn,
            "webhooks",
            False,
            "WEBHOOK_PUBLIC_URL isn't set in this environment, so NexHealth can't send changes "
            "and writes can't be confirmed.",
        )
    else:
        try:
            registered = await webhooks.register(session, conn, settings, transport)
            _step(conn, "webhooks", True, f"Subscribed to {len(registered['events'])} events.")
        except (NexHealthError, ApiError) as err:
            _step(conn, "webhooks", False, f"Registration failed: {err.message}")
    if not conn.enabled:
        conn.enabled = True  # the worker's next pass runs the first full sync
    _step(conn, "first_sync", None, "Queued: the first full sync runs in the background.")
    return status(conn)


async def refresh_first_sync(session: AsyncSession, conn: PayerConnection) -> None:
    """Mark the first-sync step done once the appointment list has been read in full."""
    steps = _onboarding(conn).get("steps") or {}
    if (steps.get("first_sync") or {}).get("ok") is not None or "verified" not in steps:
        return
    cursors = list(
        await session.scalars(select(SyncCursor).where(SyncCursor.connection_id == conn.id))
    )
    appts = next((c for c in cursors if c.resource == "appointment"), None)
    if appts is None or appts.last_run_at is None:
        return
    if appts.last_full_at and not appts.last_error:
        rows = sum(c.rows or 0 for c in cursors)
        _step(conn, "first_sync", True, f"Mirrored {rows} records.")
    elif appts.last_error:
        _step(conn, "first_sync", False, f"Appointments: {appts.last_error}")


def status(conn: PayerConnection) -> dict[str, Any]:
    state = _onboarding(conn)
    steps = state.get("steps") or {}
    return {
        "connection_id": conn.id,
        "enabled": conn.enabled,
        "started_at": state.get("started_at"),
        "steps": [{"key": k, **steps[k]} for k in STEPS if k in steps],
        "done": all((steps.get(k) or {}).get("ok") for k in STEPS),
    }
