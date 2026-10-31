"""Workspace connection configuration: what the Studio sets up on top of environment secrets.

Secrets (LiveKit API secret, carrier SIP passwords, Google keys) stay in the environment, the secret
manager or LiveKit itself. The workspace stores only choices and verification evidence: which trunk
to dial through, approved test destinations, the approved Gemini model, and test results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.models import Workspace
from careplix_api.settings import Settings

VERIFICATION_TTL = timedelta(hours=24)

# Gemini Live models known to the LiveKit Google plugin. Vertex availability differs by region and
# account; the Test model action is what proves a choice works.
KNOWN_LIVE_MODELS = [
    "gemini-live-2.5-flash-native-audio",
    "gemini-3.1-flash-live-preview",
    "gemini-3.8-live",
    "gemini-2.5-flash-native-audio-preview-12-2025",
]


@dataclass(frozen=True)
class Effective:
    outbound_trunk_id: str
    approved_destinations: frozenset[str]
    approved_model: str
    gcp_project: str
    vertex_location: str
    outbound_verified: bool
    model_check: dict[str, Any] | None

    @property
    def model_verified(self) -> bool:
        check = self.model_check or {}
        return bool(
            check.get("ok")
            and check.get("model_id") == self.approved_model
            and _fresh(check.get("at"))
        )


def _fresh(iso: str | None) -> bool:
    if not iso:
        return False
    from datetime import datetime

    return utcnow() - datetime.fromisoformat(iso) < VERIFICATION_TTL


def effective(workspace: Workspace | None, settings: Settings) -> Effective:
    ws = workspace
    trunk = (ws.outbound_trunk_id if ws else None) or settings.livekit_outbound_trunk_id
    destinations = set(settings.approved_destinations) | set(
        (ws.approved_destinations if ws else None) or []
    )
    verified = bool(
        ws
        and ws.outbound_verified_at
        and ws.outbound_trunk_id == trunk
        and utcnow() - as_utc(ws.outbound_verified_at) < timedelta(days=30)
    )
    return Effective(
        outbound_trunk_id=trunk or "",
        approved_destinations=frozenset(destinations),
        approved_model=(ws.approved_model if ws else None) or settings.gemini_live_model,
        gcp_project=(ws.gcp_project if ws else None) or settings.gcp_project,
        vertex_location=(ws.vertex_location if ws else None) or settings.vertex_location,
        outbound_verified=verified,
        model_check=ws.model_check if ws else None,
    )


async def load(session: AsyncSession, workspace_id: str, settings: Settings) -> Effective:
    return effective(await session.get(Workspace, workspace_id), settings)
