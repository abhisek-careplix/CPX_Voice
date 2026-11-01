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


def nexhealth_client(
    conn: PayerConnection, settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> NexHealthClient:
    return NexHealthClient(
        conn.base_url,
        secret(settings, conn.auth_ref),
        (conn.auth_ref or {})["subdomain"],
        transport=transport,
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


def ehr_client(
    conn: PayerConnection, settings: Settings, transport: httpx.AsyncBaseTransport | None
):
    """The client for this practice's EHR connection (NexHealth or a direct OpenEMR)."""
    if conn.kind == OPENEMR:
        return openemr_client(conn, settings, transport)
    return nexhealth_client(conn, settings, transport)


def stedi_client(
    conn: PayerConnection, settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> stedi.StediClient:
    return stedi.StediClient(conn.base_url, secret(settings, conn.auth_ref), transport=transport)


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


async def ensure_practice_connections(session: AsyncSession, settings: Settings) -> None:
    """Register the configured NexHealth location and Stedi account for every workspace."""
    wanted: list[tuple[str, str, str, dict[str, Any], bool]] = []
    if settings.nexhealth_configured:
        wanted.append(
            (
                EHR,
                f"NexHealth · {settings.nexhealth_subdomain}",
                settings.nexhealth_base_url,
                {
                    "key_env": "NEXHEALTH_API_KEY",
                    "env": settings.nexhealth_env,
                    "subdomain": settings.nexhealth_subdomain,
                    "institution_id": settings.nexhealth_institution_id,
                    "location_id": settings.nexhealth_location_id,
                },
                settings.nexhealth_env == "sandbox",
            )
        )
    if settings.openemr_configured:
        wanted.append(
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
    if settings.stedi_configured:
        wanted.append(
            (
                CLEARINGHOUSE,
                "Stedi" + (" (test mode: mock payers)" if settings.stedi_env == "test" else ""),
                settings.stedi_base_url,
                {"key_env": "STEDI_API_KEY", "env": settings.stedi_env},
                settings.stedi_env == "test",
            )
        )
    for workspace in list(await session.scalars(select(Workspace))):
        for kind, name, url, ref, synthetic in wanted:
            conn = await session.scalar(
                select(PayerConnection).where(
                    PayerConnection.workspace_id == workspace.id, PayerConnection.kind == kind
                )
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
            body, ms = await client.eligibility(
                stedi.eligibility_request(
                    payer_id=stedi.MOCK_PAYER_ID,
                    provider_npi=stedi.MOCK_PROVIDER_NPI,
                    provider_name=settings.practice_name,
                    subscriber=stedi.MOCK_SUBSCRIBER,
                )
            )
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
            "stedi.verify",
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
        "stedi.verify",
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
