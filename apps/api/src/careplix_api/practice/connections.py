"""Practice connections: EHRs (NexHealth, OpenEMR) and Stedi (clearinghouse) per workspace.

Both reuse ``PayerConnection`` so they appear in the Integrations audit, and their keys are
referenced by setting name (``auth_ref.key_env``) like every other connection: never stored.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.integrations import stedi
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import IntegrationCall, PayerConnection, Workspace
from careplix_api.settings import Settings

EHR, OPENEMR, CLEARINGHOUSE = "nexhealth", "openemr", "stedi"
EHR_KINDS = (OPENEMR, EHR)  # preference order when a workspace has more than one practice


def secret(settings: Settings, ref: dict[str, Any] | None) -> str:
    name = (ref or {}).get("key_env") or ""
    value = getattr(settings, name.lower(), None)
    if value is not None and hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return os.environ.get(name, "")


def nexhealth_env(conn: PayerConnection) -> str:
    """``production`` only when the connection says so; anything else is the sandbox."""
    return "production" if (conn.auth_ref or {}).get("env") == "production" else "sandbox"


def nexhealth_client(
    conn: PayerConnection, settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> NexHealthClient:
    """A client with the key for the connection's env and nothing else: a production practice
    reads NEXHEALTH_PROD_API_KEY, a sandbox one NEXHEALTH_API_KEY (never the production key).

    Every client for the same key shares one token and one throttle (the limits are per key), so
    many clinics on CarePlix's production key stay under its 2,000/min together.
    """
    env = nexhealth_env(conn)
    key = settings.nexhealth_key(env)
    if not key:
        name = settings.nexhealth_key_env(env)
        raise NexHealthError("auth", f"This environment has no NexHealth {env} key ({name}).")
    return NexHealthClient(
        conn.base_url, key, (conn.auth_ref or {})["subdomain"], transport=transport
    )


def openemr_client(
    conn: PayerConnection, settings: Settings, transport: httpx.AsyncBaseTransport | None
):
    from careplix_api.practice.openemr import OpenEmrClient

    ref = conn.auth_ref or {}
    return OpenEmrClient(
        conn.base_url,
        client_id=ref["client_id"],
        client_secret=secret(settings, ref),
        username=ref["username"],
        password=secret(settings, {"key_env": ref.get("password_env", "")}),
        timezone=ref.get("timezone") or "UTC",
        verify_tls=ref.get("verify_tls", True),
        transport=transport,
    )


def stedi_mode(conn: PayerConnection) -> str:
    return stedi.TEST if is_test(conn) else stedi.PRODUCTION


def stedi_client(
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
    *,
    timeout_s: float = stedi.TIMEOUT_S,
) -> stedi.StediClient:
    """A client locked to the connection's mode, with that mode's key only: a production
    connection reads STEDI_PROD_API_KEY and nothing else, a test one never reads it."""
    mode = stedi_mode(conn)
    if mode == stedi.PRODUCTION:
        key = settings.stedi_prod_api_key.get_secret_value()
    else:
        key = (
            settings.stedi_test_api_key.get_secret_value()
            or settings.stedi_api_key.get_secret_value()
        )
    return stedi.StediClient(
        conn.base_url,
        key,
        mode=mode,
        transport=transport,
        timeout_s=timeout_s,
        max_in_flight=settings.stedi_max_in_flight,
    )


def location_id(conn: PayerConnection) -> str:
    return str((conn.auth_ref or {}).get("location_id") or "")


def is_test(conn: PayerConnection) -> bool:
    return (conn.auth_ref or {}).get("env") in {"sandbox", "test"}


async def audit(
    session: AsyncSession,
    conn: PayerConnection,
    operation: str,
    phase: str,
    *,
    ok: bool,
    status: str,
    duration_ms: int = 0,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
    http_status: int | None = None,
    task_id: str | None = None,
) -> IntegrationCall:
    call = IntegrationCall(
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        operation=operation,
        phase=phase,
        task_id=task_id,
        ok=ok,
        status=status,
        http_status=http_status,
        duration_ms=duration_ms,
        summary=summary or {},
        error=(error or "")[:200] or None,
    )
    session.add(call)
    await session.flush()
    return call


def is_env_managed(conn: PayerConnection) -> bool:
    """Connections the deployment's env creates (the lab practice, the clearinghouse), as
    opposed to practices a workspace adds in the Studio (``auth_ref.source == "studio"``)."""
    return (conn.auth_ref or {}).get("source") != "studio"


def clearinghouse_key_env(settings: Settings, mode: str) -> str:
    if mode == "production":
        return "STEDI_PROD_API_KEY"
    test_key = settings.stedi_test_api_key.get_secret_value()
    return "STEDI_TEST_API_KEY" if test_key else "STEDI_API_KEY"


def clearinghouse_spec(
    settings: Settings, mode: str
) -> tuple[str, str, str, dict[str, Any], bool] | None:
    """The clearinghouse connection for one mode, or None when that mode's key isn't set."""
    key_env = clearinghouse_key_env(settings, mode)
    if not secret(settings, {"key_env": key_env}):
        return None
    return (
        CLEARINGHOUSE,
        "Clearinghouse" + (" (test mode: mock payers)" if mode == "test" else ""),
        settings.stedi_base_url,
        {"key_env": key_env, "env": mode},
        mode == "test",
    )


async def ensure_practice_connections(session: AsyncSession, settings: Settings) -> None:
    """Register the clearinghouse for every workspace (in the workspace's chosen mode), and the
    env EHR for lab workspaces. Practices added in the Studio are left alone."""
    from careplix_api.practice import automation

    env_ehrs: list[tuple[str, str, str, dict[str, Any], bool]] = []
    if settings.nexhealth_configured:
        env_ehrs.append(
            (
                EHR,
                f"NexHealth · {settings.nexhealth_subdomain}",
                settings.nexhealth_base_url,
                {
                    "key_env": settings.nexhealth_key_env(settings.nexhealth_env),
                    "env": settings.nexhealth_env,
                    "subdomain": settings.nexhealth_subdomain,
                    "institution_id": settings.nexhealth_institution_id,
                    "location_id": settings.nexhealth_location_id,
                },
                settings.nexhealth_env == "sandbox",
            )
        )
    if settings.openemr_configured:
        env_ehrs.append(
            (
                OPENEMR,
                "OpenEMR · local clinic",
                settings.openemr_base_url,
                {
                    "key_env": "OPENEMR_CLIENT_SECRET",
                    "password_env": "OPENEMR_PASSWORD",
                    "client_id": settings.openemr_client_id,
                    "username": settings.openemr_username,
                    "timezone": settings.openemr_timezone,
                    "verify_tls": settings.openemr_verify_tls,
                    "location_id": settings.openemr_facility_id,
                    "ui_url": settings.openemr_ui_url or settings.openemr_base_url,
                    "env": "local",
                },
                True,
            )
        )
    for workspace in list(await session.scalars(select(Workspace))):
        wanted = []
        if workspace.env_integrations is not False:
            # The deployment's own EHR is the lab practice. Customer workspaces never see it.
            wanted += env_ehrs
        mode = automation.effective(workspace, settings).clearinghouse_env
        spec = clearinghouse_spec(settings, mode)
        if spec is not None:
            # The clearinghouse account is CarePlix's and serves every workspace.
            wanted.append(spec)
        for kind, name, url, ref, synthetic in wanted:
            conn = next(
                (
                    c
                    for c in await session.scalars(
                        select(PayerConnection).where(
                            PayerConnection.workspace_id == workspace.id,
                            PayerConnection.kind == kind,
                        )
                    )
                    if is_env_managed(c)
                ),
                None,
            )
            if conn is None:
                session.add(
                    PayerConnection(
                        workspace_id=workspace.id,
                        name=name,
                        kind=kind,
                        base_url=url,
                        ig_version={EHR: "nexhealth-v3", OPENEMR: "openemr-7"}.get(
                            kind, "stedi-2026-06-01"
                        ),
                        auth_type="oauth_password" if kind == OPENEMR else "api_key",
                        auth_ref=ref,
                        payer_names=["*"],
                        synthetic=synthetic,
                    )
                )
            else:
                if kind == CLEARINGHOUSE and (conn.auth_ref or {}).get("env") != ref["env"]:
                    # A new mode is a new account: its old verification says nothing about it.
                    conn.verified_at, conn.capability, conn.last_error = None, None, None
                # Keep the webhook registration; follow configuration changes for the rest.
                conn.name, conn.base_url, conn.synthetic = name, url, synthetic
                conn.auth_ref = {**(conn.auth_ref or {}), **ref}
    await session.commit()


async def verify_stedi(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Test keys: Stedi's mock eligibility request. Production keys: payer directory only."""
    client = stedi_client(conn, settings, transport)
    try:
        if is_test(conn):
            body, ms = await client.eligibility(stedi.mock_request("aetna_active"))
            summary = stedi.summarize_eligibility(body)
            ok = summary["coverage"] == "active"
            detail = {"mock": True, "coverage": summary["coverage"], "plan": summary["plan"]}
        else:
            started = time.perf_counter()
            payers = await client.search_payers("Aetna")
            ms = round((time.perf_counter() - started) * 1000)
            ok, detail = bool(payers), {"payer_directory": len(payers)}
    except stedi.StediError as err:
        conn.last_error, conn.verified_at = err.message[:120], None
        await audit(
            session,
            conn,
            "clearinghouse.verify",
            "verify",
            ok=False,
            status=err.status,
            http_status=err.http_status,
            error=err.message,
        )
        return {"ok": False, "error": err.message}
    finally:
        await client.aclose()
    await audit(
        session,
        conn,
        "clearinghouse.verify",
        "verify",
        ok=ok,
        status="success" if ok else "error",
        duration_ms=ms,
        summary=detail,
    )
    conn.capability = {
        "env": (conn.auth_ref or {}).get("env"),
        "operations": ["eligibility 270/271", "payer search"],
        "verified": detail,
    }
    conn.last_error, conn.verified_at = (None, utcnow()) if ok else ("check failed", None)
    return {"ok": ok, "capability": conn.capability, "duration_ms": ms}


async def verify(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if conn.kind == CLEARINGHOUSE:
        return await verify_stedi(session, conn, settings, transport)
    from careplix_api.practice.sync import probe

    try:
        capability = await probe(session, conn, settings, transport)
    except NexHealthError as err:
        conn.last_error, conn.verified_at = err.message[:120], None
        return {"ok": False, "error": err.message}
    ok = capability.get("location") is not None
    conn.last_error, conn.verified_at = (None, utcnow()) if ok else ("location not found", None)
    return {"ok": ok, "capability": capability}
