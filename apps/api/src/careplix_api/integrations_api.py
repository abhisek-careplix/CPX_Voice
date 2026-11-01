"""Studio API for payer integrations (docs/solution/HEALTHCARE_INTEGRATION_LAYER.md)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.errors import ApiError
from careplix_api.integrations import pas
from careplix_api.integrations import service as integration_service
from careplix_api.models import AuditEvent, IntegrationCall, PayerConnection, Task
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter(prefix="/integrations")
admin = require("admin")


def transport(request: Request):
    return getattr(request.app.state, "integration_transport", None)


def _connection_view(c: PayerConnection) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "base_url": c.base_url,
        "ig_version": c.ig_version,
        "auth_type": c.auth_type,
        "auth_ref": {k: v for k, v in (c.auth_ref or {}).items() if k != "key_env"}
        | ({"key_env": "set"} if (c.auth_ref or {}).get("key_env") else {}),
        "payer_names": c.payer_names,
        "synthetic": c.synthetic,
        "enabled": c.enabled,
        "verified_at": c.verified_at,
        "capability": c.capability,
        "last_error": c.last_error,
    }


def _call_view(c: IntegrationCall) -> dict[str, Any]:
    return {
        "id": c.id,
        "connection_id": c.connection_id,
        "operation": c.operation,
        "phase": c.phase,
        "task_id": c.task_id,
        "ok": c.ok,
        "status": c.status,
        "http_status": c.http_status,
        "duration_ms": c.duration_ms,
        "summary": c.summary,
        "error": c.error,
        "at": c.created_at,
    }


@router.get("")
async def overview(
    principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    connections = await session.scalars(
        select(PayerConnection)
        .where(PayerConnection.workspace_id == principal.workspace_id)
        .order_by(PayerConnection.created_at)
    )
    calls = await session.scalars(
        select(IntegrationCall)
        .where(IntegrationCall.workspace_id == principal.workspace_id)
        .order_by(IntegrationCall.created_at.desc())
        .limit(30)
    )
    return {
        "connections": [_connection_view(c) for c in connections],
        "recent_calls": [_call_view(c) for c in calls],
        "supported": [
            {
                "kind": "pas",
                "ig_version": pas.IG_VERSION,
                "operations": ["Claim/$inquire"],
                "status": "available",
            },
            {
                "kind": "pas",
                "ig_version": pas.IG_VERSION,
                "operations": ["Claim/$submit"],
                "status": "planned (H3, human approval)",
            },
            {
                "kind": "x12",
                "ig_version": "005010",
                "operations": ["270/271", "276/277", "278"],
                "status": "planned (H2, clearinghouse)",
            },
            {
                "kind": "crd_dtr",
                "ig_version": "stu2",
                "operations": ["CDS Hooks", "Questionnaire"],
                "status": "planned (H3)",
            },
        ],
    }


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: HttpUrl
    auth_type: Literal["none", "smart_backend"] = "none"
    client_id: str = Field(default="", max_length=200)
    token_url: str = Field(default="", max_length=300)
    key_env: str = Field(
        default="",
        max_length=80,
        pattern=r"^[A-Z0-9_]*$",
        description="Name of the env var holding the private key",
    )
    kid: str = Field(default="", max_length=120)
    payer_names: list[str] = Field(default_factory=list, max_length=50)


@router.post("/payers")
async def add_connection(
    body: ConnectionIn, principal: Principal = Depends(admin), session: AsyncSession = Session
):
    if body.auth_type == "smart_backend" and not (
        body.client_id and body.token_url and body.key_env
    ):
        raise ApiError(
            422,
            "auth_incomplete",
            "SMART Backend Services needs a client ID, token URL and key variable name.",
        )
    conn = PayerConnection(
        workspace_id=principal.workspace_id,
        name=body.name,
        base_url=str(body.base_url).rstrip("/"),
        auth_type=body.auth_type,
        auth_ref={
            "client_id": body.client_id,
            "token_url": body.token_url,
            "key_env": body.key_env,
            "kid": body.kid,
        }
        if body.auth_type == "smart_backend"
        else None,
        payer_names=[p.strip() for p in body.payer_names if p.strip()] or ["*"],
    )
    session.add(conn)
    await session.flush()
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="integration.connection_added",
            target=conn.id,
            detail={"base_url": conn.base_url},
        )
    )
    await session.commit()
    return _connection_view(conn)


async def _connection(
    session: AsyncSession, principal: Principal, connection_id: str
) -> PayerConnection:
    conn = await session.get(PayerConnection, connection_id)
    if conn is None or conn.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Connection not found.")
    return conn


@router.post("/payers/{connection_id}/verify")
async def verify(
    connection_id: str,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
    t=Depends(transport),
    settings: Settings = Depends(get_settings),
):
    conn = await _connection(session, principal, connection_id)
    result = await integration_service.verify_connection(session, conn, t, settings)
    await session.commit()
    return {**result, "connection": _connection_view(conn)}


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/payers/{connection_id}/enabled")
async def set_enabled(
    connection_id: str,
    body: EnabledIn,
    principal: Principal = Depends(admin),
    session: AsyncSession = Session,
):
    conn = await _connection(session, principal, connection_id)
    conn.enabled = body.enabled
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action="integration.connection_enabled",
            target=conn.id,
            detail={"enabled": body.enabled},
        )
    )
    await session.commit()
    return _connection_view(conn)


@router.post("/tasks/{task_id}/check")
async def check_task(
    task_id: str,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    t=Depends(transport),
):
    """Run the electronic payer check for a task now (manual phase)."""
    task = await session.get(Task, task_id)
    if task is None or task.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Task not found.")
    result = await integration_service.run_phase(session, task, "manual", t)
    await session.commit()
    return result
