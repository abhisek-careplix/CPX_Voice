"""Connection readiness (doc 05 workspace readiness, doc 08 G0 exit evidence).

Each dependency is reported separately so that a healthy application never implies telephone or
model health. Statuses:

- ready: verified against the live provider just now.
- blocked: misconfigured or failing; ``message`` is an actionable blocker for the Workspace screen.
- unverified: configured, but this check cannot prove it works (evidence comes from a probe or,
  from G1, from worker claims).

Details carry identifiers and counts only, never secrets.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from livekit import api as lkapi
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
        return Check("database", Status.READY, "Database reachable.", {"server_version": version})
    except Exception as exc:  # noqa: BLE001 - surfaced as a categorised blocker
        return Check(
            "database", Status.BLOCKED, "Database unreachable.", {"error": type(exc).__name__}
        )
    finally:
        await engine.dispose()


async def check_livekit(settings: Settings) -> list[Check]:
    """Credentials, outbound trunk (OD01) and an inbound route dispatching our agent (OD02)."""
    if not settings.livekit_configured:
        msg = "Voice connection missing. Set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET."
        return [
            Check("livekit_api", Status.BLOCKED, msg),
            Check("outbound_route", Status.BLOCKED, "Phone connection missing (voice not set up)."),
            Check("inbound_route", Status.BLOCKED, "Inbound number missing (voice not set up)."),
        ]

    try:
        async with (
            asyncio.timeout(settings.check_timeout_s),
            lkapi.LiveKitAPI(
                settings.livekit_url,
                settings.livekit_api_key,
                settings.livekit_api_secret.get_secret_value(),
            ) as lk,
        ):
            rooms = await lk.room.list_rooms(lkapi.ListRoomsRequest())
            outbound = await lk.sip.list_outbound_trunk(lkapi.ListSIPOutboundTrunkRequest())
            inbound = await lk.sip.list_inbound_trunk(lkapi.ListSIPInboundTrunkRequest())
            rules = await lk.sip.list_dispatch_rule(lkapi.ListSIPDispatchRuleRequest())
    except Exception as exc:  # noqa: BLE001
        return [
            Check(
                "livekit_api",
                Status.BLOCKED,
                "Voice service rejected the connection. Check LiveKit URL and credentials.",
                {"error": type(exc).__name__},
            )
        ]

    checks = [
        Check(
            "livekit_api",
            Status.READY,
            "Voice service reachable.",
            {"active_rooms": len(rooms.rooms)},
        )
    ]

    trunk_ids = [t.sip_trunk_id for t in outbound.items]
    if not settings.livekit_outbound_trunk_id:
        checks.append(
            Check(
                "outbound_route",
                Status.BLOCKED,
                "Phone connection missing. Select a carrier and set LIVEKIT_OUTBOUND_TRUNK_ID.",
                {"outbound_trunks_found": len(trunk_ids)},
            )
        )
    elif settings.livekit_outbound_trunk_id not in trunk_ids:
        checks.append(
            Check(
                "outbound_route",
                Status.BLOCKED,
                "Configured outbound trunk was not found in the LiveKit project.",
                {"outbound_trunks_found": len(trunk_ids)},
            )
        )
    else:
        checks.append(
            Check(
                "outbound_route",
                Status.UNVERIFIED,
                "Outbound trunk exists. Route qualification (real call to an approved test number) "
                "is still required.",
                {"trunk_id": settings.livekit_outbound_trunk_id},
            )
        )

    agent_rules = [
        r
        for r in rules.items
        if any(a.agent_name == settings.livekit_agent_name for a in r.room_config.agents)
    ]
    if not agent_rules:
        checks.append(
            Check(
                "inbound_route",
                Status.BLOCKED,
                f"No inbound number routes to agent '{settings.livekit_agent_name}'.",
                {
                    "inbound_trunks_found": len(inbound.items),
                    "dispatch_rules_found": len(rules.items),
                },
            )
        )
    else:
        checks.append(
            Check(
                "inbound_route",
                Status.UNVERIFIED,
                "Inbound dispatch rule exists. A real test call is still required.",
                {"dispatch_rule_ids": [r.sip_dispatch_rule_id for r in agent_rules]},
            )
        )
    return checks


def check_model(settings: Settings) -> Check:
    missing = [
        name
        for name, value in [
            ("GCP_PROJECT", settings.gcp_project),
            ("VERTEX_LOCATION", settings.vertex_location),
            ("GEMINI_LIVE_MODEL", settings.gemini_live_model),
        ]
        if not value
    ]
    if missing:
        return Check(
            "voice_model",
            Status.BLOCKED,
            f"Voice model not configured. Set {', '.join(missing)}.",
        )
    return Check(
        "voice_model",
        Status.UNVERIFIED,
        "Model configured. Run the Gemini Live probe from the worker environment to verify access.",
        {"model_id": settings.gemini_live_model, "location": settings.vertex_location},
    )


def check_agent_worker(settings: Settings) -> Check:
    return Check(
        "agent_worker",
        Status.UNVERIFIED,
        "Agent availability is confirmed per call by worker claims (G1). Use the browser-session "
        "probe to confirm a worker answers dispatch.",
        {"agent_name": settings.livekit_agent_name},
    )


async def run_all(settings: Settings) -> Readiness:
    db, lk = await asyncio.gather(check_database(settings), check_livekit(settings))
    checks = [db, *lk, check_model(settings), check_agent_worker(settings)]
    statuses = {c.status for c in checks}
    overall = (
        Status.BLOCKED
        if Status.BLOCKED in statuses
        else Status.UNVERIFIED
        if Status.UNVERIFIED in statuses
        else Status.READY
    )
    return Readiness(datetime.now(UTC).isoformat(), overall, checks)
