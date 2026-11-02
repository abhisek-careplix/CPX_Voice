"""The connectors CarePlix ships: OpenEMR (direct) and NexHealth (hub to 15 EHRs).

They wrap the practice layer's existing clients and sync code; behaviour is unchanged. OpenEMR can
also emulate any catalog profile (``auth_ref.emulate``), so one test clinic demos every EHR.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.connect.base import Connector, profile
from careplix_api.connect.catalog import (
    PROFILES,
    Profile,
    Support,
    nexhealth_profile_for,
)
from careplix_api.connect.catalog import (
    support as catalog_support,
)
from careplix_api.db import as_utc
from careplix_api.models import PayerConnection, WriteIntent
from careplix_api.settings import Settings

# Emulated EHRs confirm writes the way the real one would: later, not on the API call.
EMULATED_CONFIRM_DELAY = timedelta(seconds=60)
OFFICE_HOURS = (time(8, 0), time(18, 0))  # cloud EHRs sync only while the office is signed in


def office_open(timezone: str | None, now: datetime) -> bool:
    """Weekday office hours in the practice's time zone (when a cloud EHR's sync runs)."""
    local = now.astimezone(ZoneInfo(timezone or "America/New_York"))
    return local.weekday() < 5 and OFFICE_HOURS[0] <= local.time() < OFFICE_HOURS[1]


class OpenEmrConnector(Connector):
    kind = "openemr"

    def _make_client(self):
        from careplix_api.practice.connections import openemr_client

        return openemr_client(self.conn, self.settings, self.transport)

    @property
    def profile(self) -> Profile:
        return self.emulating or PROFILES["openemr"]

    @property
    def emulating(self) -> Profile | None:
        return profile((self.conn.auth_ref or {}).get("emulate"))

    def support(self, operation: str) -> Support:
        own = PROFILES["openemr"].support(operation)
        if self.emulating is None or own == "no":
            return own
        return catalog_support(self.emulating, operation)

    def links(self) -> dict[str, str] | None:
        from careplix_api.practice.openemr import deep_links

        base = (self.conn.auth_ref or {}).get("ui_url") or self.conn.base_url
        return deep_links(base)

    def sync_interval_s(self) -> int:
        return self.settings.openemr_sync_interval_s

    async def sync(self, session: AsyncSession, *, full: bool = False) -> dict[str, Any]:
        from careplix_api.practice import openemr

        return await openemr.sync_practice(
            session, self.conn, self.settings, self.transport, full=full
        )

    async def probe(self, session: AsyncSession) -> dict[str, Any]:
        from careplix_api.practice import openemr

        return await openemr.probe(session, self.conn, self.settings, self.transport)

    async def write(self, intent: WriteIntent, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self.client.write(intent.kind, intent, payload)
        if self.emulating is not None and self.emulating.confirm != "immediate":
            # The emulated EHR accepts now and confirms later (read back by reconcile).
            return {**data, "foreign_id": None}
        return data

    async def read_back(self, intent: WriteIntent) -> dict[str, Any] | None:
        return await self.client.read_back(intent.kind, intent)

    def confirm_due(self, intent: WriteIntent, now: datetime) -> bool:
        emulating = self.emulating
        if emulating is None:
            return super().confirm_due(intent, now)
        if now - (as_utc(intent.sent_at) or now) < EMULATED_CONFIRM_DELAY:
            return False
        if emulating.confirm == "async_office_hours":
            return office_open((self.conn.auth_ref or {}).get("timezone"), now)
        return True

    async def slot_free(self, opening: Any) -> str | None:
        return await self.client.slot_free(opening)

    async def available_slots(self, start, end, minutes, providers, tz: tzinfo):
        # OpenEMR's API has no availability: clinic hours minus booked visits, the same rule
        # slot_free re-checks.
        return await self.client.free_slots(start, end, minutes, providers)


class NexHealthConnector(Connector):
    kind = "nexhealth"

    def _make_client(self):
        from careplix_api.practice.connections import nexhealth_client

        return nexhealth_client(self.conn, self.settings, self.transport)

    @property
    def profile(self) -> Profile | None:
        cap = self.conn.capability or {}
        names = [s.get("emr") for s in cap.get("sync") or []] + list(cap.get("emrs") or [])
        return next((p for p in map(nexhealth_profile_for, names) if p), None)

    async def sync(self, session: AsyncSession, *, full: bool = False) -> dict[str, Any]:
        from careplix_api.practice import sync

        return await sync.sync_nexhealth(
            session, self.conn, self.settings, self.transport, full=full
        )

    async def probe(self, session: AsyncSession) -> dict[str, Any]:
        from careplix_api.practice import sync

        return await sync.probe_nexhealth(session, self.conn, self.settings, self.transport)

    async def health(self, session: AsyncSession) -> list[dict[str, Any]]:
        from careplix_api.practice import sync

        return await sync.refresh_nexhealth_status(self.conn, self.client)

    async def write(self, intent: WriteIntent, payload: dict[str, Any]) -> dict[str, Any]:
        from careplix_api.practice.intents import KINDS

        return await KINDS[intent.kind].send(self.client, intent, payload)

    async def read_back(self, intent: WriteIntent) -> dict[str, Any] | None:
        from careplix_api.practice.intents import KINDS

        return await KINDS[intent.kind].read_back(self.client, intent)

    async def slot_free(self, opening: Any) -> str | None:
        from careplix_api.practice.backfill import nexhealth_free_operatory

        return await nexhealth_free_operatory(self.client, self.conn, opening)

    async def available_slots(self, start, end, minutes, providers, tz: tzinfo):
        from careplix_api.practice.connections import location_id

        first, last = start.astimezone(tz).date(), end.astimezone(tz).date()
        params: dict[str, Any] = {
            "start_date": first.isoformat(),  # NexHealth reads dates in the location's time
            "days": max(1, min((last - first).days + 1, 30)),
            "lids[]": location_id(self.conn),
            "pids[]": providers,
            "slot_length": minutes,
            "slot_interval": 15,
        }
        body = await self.client.request("GET", "/available_slots", params)
        out: list[tuple[datetime, str, str]] = []
        for entry in body.get("data") or []:
            for slot in entry.get("slots") or []:
                at = _parse_time(slot.get("time"))
                if at is None or not (start <= at <= end):
                    continue
                pid = str(slot.get("provider_id") or entry.get("pid") or "")
                if providers and pid not in providers:
                    continue
                out.append((at, pid, str(slot.get("operatory_id") or "")))
        return sorted(out)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


CONNECTORS: dict[str, type[Connector]] = {
    OpenEmrConnector.kind: OpenEmrConnector,
    NexHealthConnector.kind: NexHealthConnector,
}


def connector_for(
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Connector:
    try:
        return CONNECTORS[conn.kind](conn, settings, transport)
    except KeyError:
        raise ValueError(f"No connector for connection kind {conn.kind!r}") from None
