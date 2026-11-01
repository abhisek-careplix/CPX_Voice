"""The connector contract: one class per EHR path, so engines never branch on the EHR.

Adding an EHR path = a ``Connector`` subclass + its catalog profile(s) + a fake for tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.connect.catalog import (
    INTENT_OPERATION,
    PROFILES,
    Profile,
    Support,
    support,
)
from careplix_api.db import as_utc
from careplix_api.models import PayerConnection, WriteIntent
from careplix_api.settings import Settings

# How long the hub waits for an EHR to confirm an accepted write before reading it back.
CONFIRM_SLA = timedelta(hours=2)


class Connector:
    kind: str = ""

    def __init__(
        self,
        conn: PayerConnection,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.conn, self.settings, self.transport = conn, settings, transport
        self._client: Any = None

    # --- identity and capability ---------------------------------------------------------------

    @property
    def profile(self) -> Profile | None:
        """The catalog profile this connection behaves as."""
        return None

    @property
    def emulating(self) -> Profile | None:
        """Set when a test EHR is running another EHR's profile (OpenEMR emulation)."""
        return None

    def support(self, operation: str) -> Support:
        return support(self.profile, operation)

    def intent_support(self, kind: str) -> Support:
        return self.support(INTENT_OPERATION.get(kind, kind))

    def view(self) -> dict[str, Any]:
        profile, emulating = self.profile, self.emulating
        return {
            "connection_id": self.conn.id,
            "kind": self.kind,
            "name": self.conn.name,
            "profile": profile.id if profile else None,
            "profile_name": profile.name if profile else None,
            "emulating": emulating.id if emulating else None,
            "links": self.links(),
        }

    def links(self) -> dict[str, str] | None:
        return None

    def sync_interval_s(self) -> int:
        return self.settings.practice_sync_interval_s

    # --- client lifecycle ---------------------------------------------------------------------

    def _make_client(self) -> Any:
        raise NotImplementedError

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    @property
    def calls(self) -> int:
        return self._client.calls if self._client is not None else 0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # --- the contract -------------------------------------------------------------------------

    async def sync(self, session: AsyncSession, *, full: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    async def probe(self, session: AsyncSession) -> dict[str, Any]:
        raise NotImplementedError

    async def health(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Sync health (read/write status per sync). Direct connectors report their last probe."""
        return (self.conn.capability or {}).get("sync") or []

    async def write(self, intent: WriteIntent, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one write: ``{"id", "foreign_id"}``; a ``foreign_id`` means it is written."""
        raise NotImplementedError

    async def read_back(self, intent: WriteIntent) -> dict[str, Any] | None:
        raise NotImplementedError

    def confirm_due(self, intent: WriteIntent, now: datetime) -> bool:
        """Whether an accepted write should be read back now (else keep waiting for the EHR)."""
        return now - (as_utc(intent.sent_at) or now) > CONFIRM_SLA

    async def slot_free(self, opening: Any) -> str | None:
        """The operatory free at the opening's start (``""`` if none are mapped), or ``None``."""
        raise NotImplementedError

    async def available_slots(
        self,
        start: datetime,
        end: datetime,
        minutes: int,
        providers: list[str],
        tz: tzinfo,
    ) -> list[tuple[datetime, str, str]]:
        """Bookable times in ``[start, end]``: ``(start_utc, provider_ref, operatory_ref)``, sorted.

        Every time returned must pass ``slot_free`` right after, so offering and re-checking
        agree (the receptionist offers these, then re-checks before booking).
        """
        raise NotImplementedError


def profile(profile_id: str | None) -> Profile | None:
    return PROFILES.get(profile_id or "")
