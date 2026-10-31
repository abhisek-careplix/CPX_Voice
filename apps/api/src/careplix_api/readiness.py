"""Connection readiness (doc 05 workspace readiness, doc 08 G0 exit evidence).

Each dependency is reported separately so that a healthy application never implies telephone or
model health. Statuses:

- ready: verified against the live provider, or proven by recent evidence (a worker answered a
  probe dispatch, the model answered a live test, a test call qualified the route).
- blocked: missing or failing; ``message`` is an actionable blocker for the Workspace screen.
- unverified: configured, but not yet proven to work.

Details carry identifiers and counts only, never secrets.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from careplix_api.connections import VERIFICATION_TTL, Effective, effective
from careplix_api.db import as_utc, utcnow
from careplix_api.livekit_gateway import LiveKitGateway
from careplix_api.models import Route, WorkerProbe
from careplix_api.settings import Settings


class Status(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


@dataclass
class Check:
    name: str
    status: Status
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Readiness:
    checked_at: str
    overall: Status
    checks: list[Check]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def check_database(settings: Settings) -> Check:
    if not settings.database_url:
        return Check("database", Status.BLOCKED, "Database connection missing. Set DATABASE_URL.")
    engine = create_async_engine(settings.database_url, pool_pre_ping=False)
    try:
        async with asyncio.timeout(settings.check_timeout_s), engine.connect() as conn:
            sqlite = engine.dialect.name == "sqlite"
            query = "select sqlite_version()" if sqlite else "SHOW server_version"
            version = (await conn.execute(text(query))).scalar_one()
        engine_name = "SQLite (local development)" if sqlite else "PostgreSQL"
        return Check(
            "database",
            Status.READY,
            f"Database reachable · {engine_name}.",
            {"server_version": version},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a categorised blocker
        return Check(
            "database", Status.BLOCKED, "Database unreachable.", {"error": type(exc).__name__}
        )
    finally:
        await engine.dispose()


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


async def check_livekit(
    settings: Settings,
    eff: Effective | None = None,
    routes: list[Route] | None = None,
    gateway: LiveKitGateway | None = None,
) -> list[Check]:
    """Credentials, the selected outbound trunk (OD01) and inbound routes (OD02)."""
    eff = eff or effective(None, settings)
    gateway = gateway or LiveKitGateway(settings)
    if not settings.livekit_configured:
        msg = (
            "Not connected. Add LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET, then restart."
        )
        return [
            Check("livekit_api", Status.BLOCKED, msg),
            Check("outbound_route", Status.BLOCKED, "Needs the voice platform connected first."),
            Check("inbound_route", Status.BLOCKED, "Needs the voice platform connected first."),
        ]

    try:
        async with asyncio.timeout(settings.check_timeout_s):
            inventory = await gateway.inventory()
    except Exception as exc:  # noqa: BLE001
        return [
            Check(
                "livekit_api",
                Status.BLOCKED,
                "Voice service rejected the connection. Check LiveKit URL and credentials.",
                {"error": type(exc).__name__},
            ),
            Check(
                "outbound_route", Status.BLOCKED, "Phone routes unknown: voice service unreachable."
            ),
            Check(
                "inbound_route",
                Status.BLOCKED,
                "Inbound routes unknown: voice service unreachable.",
            ),
        ]

    checks = [
        Check(
            "livekit_api",
            Status.READY,
            "Connected to LiveKit Cloud.",
            {"project": _host(settings.livekit_url), "active_rooms": inventory["active_rooms"]},
        )
    ]

    trunk_ids = [t["id"] for t in inventory["outbound_trunks"]]
    if not eff.outbound_trunk_id:
        checks.append(
            Check(
                "outbound_route",
                Status.BLOCKED,
                "No carrier trunk selected. Add or choose one under Outbound calling."
                if trunk_ids
                else "No carrier trunk. LiveKit numbers are inbound-only; add a SIP carrier trunk.",
                {"outbound_trunks_found": len(trunk_ids)},
            )
        )
    elif eff.outbound_trunk_id not in trunk_ids:
        checks.append(
            Check(
                "outbound_route",
                Status.BLOCKED,
                "The selected carrier trunk no longer exists in LiveKit.",
                {"outbound_trunks_found": len(trunk_ids)},
            )
        )
    elif eff.outbound_verified:
        checks.append(
            Check(
                "outbound_route",
                Status.READY,
                "Carrier trunk qualified by a real test call.",
                {"trunk_id": eff.outbound_trunk_id},
            )
        )
    else:
        checks.append(
            Check(
                "outbound_route",
                Status.UNVERIFIED,
                "Carrier trunk selected. Place a qualification call to an approved test number.",
                {"trunk_id": eff.outbound_trunk_id},
            )
        )

    rules = inventory["dispatch_rules"]
    rule_ids = {r["id"] for r in rules}
    linked = [
        r for r in routes or [] if (r.provider_refs or {}).get("dispatch_rule_id") in rule_ids
    ]
    if not linked:
        checks.append(
            Check(
                "inbound_route",
                Status.BLOCKED,
                "No inbound number connected. Connect a LiveKit number under Inbound numbers.",
                {"dispatch_rules_found": len(rules)},
            )
        )
    elif any(r.verified_at for r in linked):
        checks.append(
            Check(
                "inbound_route",
                Status.READY,
                "Inbound number answered a real call and was qualified.",
                {"numbers": [r.number_e164 for r in linked if r.verified_at]},
            )
        )
    else:
        checks.append(
            Check(
                "inbound_route",
                Status.UNVERIFIED,
                "Number connected. Call it once, then mark the route qualified.",
                {"numbers": [r.number_e164 for r in linked]},
            )
        )
    return checks


def check_model(settings: Settings, eff: Effective | None = None) -> Check:
    eff = eff or effective(None, settings)
    missing = [
        name
        for name, value in [
            ("Google Cloud project", eff.gcp_project),
            ("Vertex AI region", eff.vertex_location),
            ("approved Gemini Live model", eff.approved_model),
        ]
        if not value
    ]
    if missing:
        return Check("voice_model", Status.BLOCKED, f"Choose the {', '.join(missing)}.")
    detail = {"model_id": eff.approved_model, "location": eff.vertex_location}
    check = eff.model_check or {}
    if eff.model_verified:
        return Check(
            "voice_model",
            Status.READY,
            f"Gemini Live answered a live test in {check.get('first_audio_ms')} ms.",
            {**detail, "connect_ms": check.get("connect_ms")},
        )
    if check.get("model_id") == eff.approved_model and check.get("error"):
        return Check(
            "voice_model",
            Status.BLOCKED,
            "The last live test failed. Check Vertex AI access, region and model ID.",
            {**detail, "error": check.get("error")},
        )
    return Check("voice_model", Status.UNVERIFIED, "Model chosen. Run Test model.", detail)


def check_agent_worker(settings: Settings, probe: WorkerProbe | None) -> Check:
    detail = {"agent_name": settings.livekit_agent_name}
    if probe and probe.acked_at and utcnow() - as_utc(probe.acked_at) < VERIFICATION_TTL:
        return Check(
            "agent_worker",
            Status.READY,
            "A worker answered a test dispatch.",
            {**detail, "runtime_build": probe.runtime_build, "answered_at": probe.acked_at},
        )
    return Check(
        "agent_worker",
        Status.UNVERIFIED,
        "Start the worker (make worker-dev), then run Test agent.",
        detail,
    )


async def run_all(
    settings: Settings,
    session: AsyncSession | None = None,
    workspace_id: str | None = None,
    gateway: LiveKitGateway | None = None,
) -> Readiness:
    eff, routes, probe = effective(None, settings), [], None
    if session is not None and workspace_id:
        from careplix_api.models import Workspace

        eff = effective(await session.get(Workspace, workspace_id), settings)
        routes = list(
            await session.scalars(select(Route).where(Route.workspace_id == workspace_id))
        )
        probe = await session.scalar(
            select(WorkerProbe)
            .where(WorkerProbe.workspace_id == workspace_id, WorkerProbe.acked_at.is_not(None))
            .order_by(WorkerProbe.acked_at.desc())
            .limit(1)
        )
    db, lk = await asyncio.gather(
        check_database(settings), check_livekit(settings, eff, routes, gateway)
    )
    checks = [db, *lk, check_model(settings, eff), check_agent_worker(settings, probe)]
    statuses = {c.status for c in checks}
    overall = (
        Status.BLOCKED
        if Status.BLOCKED in statuses
        else Status.UNVERIFIED
        if Status.UNVERIFIED in statuses
        else Status.READY
    )
    return Readiness(datetime.now(UTC).isoformat(), overall, checks)
