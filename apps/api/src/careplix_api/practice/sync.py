"""Sync the practice mirror from NexHealth and probe what the location supports (FO-0).

Read path (EMR design §2.3): webhooks trigger targeted re-reads; an incremental pass
(``updated_since`` with a small overlap) catches anything missed; reference data refreshes daily.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import PayerConnection, SyncCursor
from careplix_api.phi import PhiBox
from careplix_api.practice import records
from careplix_api.practice.connections import audit, location_id, nexhealth_client
from careplix_api.settings import Settings

REFERENCE_TTL = timedelta(hours=24)
OVERLAP = timedelta(minutes=5)
WINDOW_BACK, WINDOW_AHEAD = timedelta(days=1), timedelta(days=60)

# resource → (path, mapper, incremental by updated_since)
RESOURCES: dict[str, tuple[str, Any, bool]] = {
    "provider": ("/providers", records.map_provider, False),
    "appointment_type": ("/appointment_types", records.map_appointment_type, False),
    "insurance_plan": ("/insurance_plans", records.map_insurance_plan, False),
    "patient": ("/patients", records.map_patient, True),
    "coverage": ("/insurance_coverages", records.map_coverage, True),
    "appointment": ("/appointments", records.map_appointment, True),
}
SINGLE = {  # webhook resource_type → (resource, path template)
    "Patient": ("patient", "/patients/{id}"),
    "Appointment": ("appointment", "/appointments/{id}"),
    "PatientInsuranceCoverage": ("coverage", "/insurance_coverages/{id}"),
}


async def _cursor(session: AsyncSession, conn: PayerConnection, resource: str) -> SyncCursor:
    cursor = await session.scalar(
        select(SyncCursor).where(
            SyncCursor.connection_id == conn.id, SyncCursor.resource == resource
        )
    )
    if cursor is None:
        cursor = SyncCursor(
            workspace_id=conn.workspace_id, connection_id=conn.id, resource=resource
        )
        session.add(cursor)
    return cursor


def _params(resource: str, conn: PayerConnection, since: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"location_id": location_id(conn)}
    if resource == "appointment":
        now = utcnow()
        params.update(start=(now - WINDOW_BACK).isoformat(), end=(now + WINDOW_AHEAD).isoformat())
    if since is not None:
        params["updated_since"] = since.isoformat()
    return params


async def _sync_location(
    session: AsyncSession, conn: PayerConnection, client: NexHealthClient, box: PhiBox
) -> int:
    loc_id = location_id(conn)
    items = [
        records.map_location(inst, loc)
        for inst in await client.institutions()
        for loc in inst.get("locations") or []
        if str(loc.get("id")) == loc_id
    ]
    await records.upsert(session, conn, "location", items, box)
    return len(items)


async def sync_practice(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """One sync pass. Per-resource failures are recorded and never stop the other resources."""
    box, client = PhiBox(settings), nexhealth_client(conn, settings, transport)
    report: dict[str, Any] = {"full": full, "resources": {}, "transitions": []}
    try:
        try:
            report["resources"]["location"] = {
                "rows": await _sync_location(session, conn, client, box)
            }
        except NexHealthError as err:
            report["resources"]["location"] = {"error": err.message}
        for resource, (path, mapper, incremental) in RESOURCES.items():
            cursor = await _cursor(session, conn, resource)
            last_full = as_utc(cursor.last_full_at)
            reference_fresh = last_full is not None and utcnow() - last_full < REFERENCE_TTL
            if not incremental and not full and reference_fresh:
                continue
            since = None
            if incremental and not full and cursor.high_water is not None:
                since = as_utc(cursor.high_water) - OVERLAP
            started, calls_before = time.perf_counter(), client.calls
            try:
                rows, pages = await client.list_all(path, _params(resource, conn, since))
            except NexHealthError as err:
                cursor.last_error, cursor.last_run_at = err.message[:200], utcnow()
                cursor.api_calls += client.calls - calls_before
                report["resources"][resource] = {"error": err.message}
                await audit(
                    session,
                    conn,
                    f"nexhealth.sync.{resource}",
                    "sync",
                    ok=False,
                    status=err.status,
                    http_status=err.http_status,
                    error=err.message,
                )
                continue
            items = [mapper(r) for r in rows if r.get("id") is not None]
            transitions = await records.upsert(session, conn, resource, items, box)
            if resource == "appointment":
                report["transitions"] = transitions
            newest = max((i.source_updated_at for i in items if i.source_updated_at), default=None)
            if newest and (cursor.high_water is None or newest > as_utc(cursor.high_water)):
                cursor.high_water = newest
            ms = round((time.perf_counter() - started) * 1000)
            cursor.rows, cursor.duration_ms, cursor.last_error = len(items), ms, None
            cursor.api_calls += client.calls - calls_before
            cursor.last_run_at = utcnow()
            if since is None:
                cursor.last_full_at = cursor.last_run_at
            report["resources"][resource] = {"rows": len(items), "pages": pages, "ms": ms}
            await audit(
                session,
                conn,
                f"nexhealth.sync.{resource}",
                "sync",
                ok=True,
                status="success",
                duration_ms=ms,
                summary={"rows": len(items), "pages": pages, "incremental": bool(since)},
            )
    finally:
        report["api_calls"] = client.calls
        await client.aclose()
    caps = dict(conn.capability or {})
    caps["last_sync"] = {"at": utcnow().isoformat(), "full": full, "api_calls": client.calls}
    conn.capability = caps
    return report


async def refresh_records(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    resource_type: str,
    ids: list[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Re-read records named by a webhook (the webhook body itself is never trusted or stored)."""
    resource, template = SINGLE[resource_type]
    mapper = RESOURCES[resource][1]
    box, client = PhiBox(settings), nexhealth_client(conn, settings, transport)
    try:
        items = []
        for record_id in ids[:50]:
            data = await client.get(
                template.format(id=record_id), {"location_id": location_id(conn)}
            )
            if isinstance(data, dict) and data.get("id") is not None:
                items.append(mapper(data))
        return await records.upsert(session, conn, resource, items, box)
    finally:
        cursor = await _cursor(session, conn, resource)
        cursor.api_calls += client.calls
        await client.aclose()


# Reads the probe tries, with the minimum parameters each needs (verified on the sandbox).
PROBE_READS: dict[str, tuple[str, dict[str, Any]]] = {
    "providers": ("/providers", {}),
    "appointment_types": ("/appointment_types", {}),
    "patients": ("/patients", {}),
    "appointments": ("/appointments", {"window": True}),
    "insurance_coverages": ("/insurance_coverages", {}),
    "insurance_plans": ("/insurance_plans", {}),
    "working_hours": ("/working_hours", {}),
    "charges": ("/charges", {"since": True}),
    "claims": ("/claims", {"since": True}),
    "payments": ("/payments", {"since": True}),
    "guarantor_balances": ("/guarantor_balances", {"since": True}),
    "document_types": ("/document_types", {}),
    "patient_recalls": ("/patient_recalls", {}),
}
# Writes NexHealth documents. Whether each reaches the EHR is proven per location by write intents.
DOCUMENTED_WRITES = {
    "appointments": "POST/PATCH /appointments (insertion webhook)",
    "patients": "POST /patients",
    "patient_alerts": "POST /patients/{id}/alerts",
    "patient_documents": "POST /patients/{id}/documents (insertion webhook)",
    "payments": "POST /payments (insertion webhook)",
    "adjustments": "POST /adjustments (insertion webhook)",
}


async def probe(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Live capability map for this location: what reads answer, sync health, documented writes."""
    client = nexhealth_client(conn, settings, transport)
    loc_id = location_id(conn)
    started = time.perf_counter()
    try:
        institutions = await client.institutions()
        location, emrs = None, []
        for inst in institutions:
            for loc in inst.get("locations") or []:
                if str(loc.get("id")) == loc_id:
                    location, emrs = {"id": loc_id, "name": loc.get("name")}, inst.get("emrs") or []
        reads: dict[str, Any] = {}
        now = utcnow()
        for name, (path, hints) in PROBE_READS.items():
            params: dict[str, Any] = {"location_id": loc_id, "per_page": 1}
            if hints.get("window"):
                params.update(start=now.isoformat(), end=(now + timedelta(days=30)).isoformat())
            if hints.get("since"):
                params["updated_since"] = (now - timedelta(days=3650)).isoformat()
            try:
                data = await client.get(path, params)
                reads[name] = {"ok": True, "has_data": bool(data)}
            except NexHealthError as err:
                reads[name] = {
                    "ok": False,
                    "http_status": err.http_status,
                    "error": err.message[:120],
                }
        try:
            syncs = await client.get("/sync_status", {"location_ids[]": loc_id}) or []
        except NexHealthError as err:
            syncs, reads["sync_status"] = [], {"ok": False, "error": err.message[:120]}
    finally:
        calls = client.calls
        await client.aclose()
    sync = [_sync_view(s) for s in syncs]
    capability = {
        **(conn.capability or {}),
        "probed_at": utcnow().isoformat(),
        "location": location,
        "emrs": emrs,
        "reads": reads,
        "sync": sync,
        # The sandbox demo location has no sync: writes are accepted but never reach an EHR.
        "writes_confirmable": bool(sync),
        "writes": DOCUMENTED_WRITES,
        "probe_api_calls": calls,
    }
    conn.capability = capability
    await audit(
        session,
        conn,
        "nexhealth.probe",
        "verify",
        ok=location is not None,
        status="success" if location else "error",
        duration_ms=round((time.perf_counter() - started) * 1000),
        summary={
            "reads_ok": sum(1 for r in reads.values() if r.get("ok")),
            "reads": len(reads),
            "syncs": len(sync),
        },
    )
    return capability


def _sync_view(s: dict[str, Any]) -> dict[str, Any]:
    emr = s.get("emr") or {}
    return {
        "emr": emr.get("display_name"),
        "type": emr.get("type"),
        "read_status": s.get("read_status"),
        "read_status_at": s.get("read_status_at"),
        "write_status": s.get("write_status"),
        "write_status_at": s.get("write_status_at"),
    }


def write_status(conn: PayerConnection) -> str:
    """green | red | none (no sync, e.g. the sandbox demo location)."""
    syncs = (conn.capability or {}).get("sync") or []
    if not syncs:
        return "none"
    return "green" if all(s.get("write_status") == "green" for s in syncs) else "red"


async def refresh_sync_status(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Cheap re-read of sync health (one call), used when NexHealth reports a status change."""
    client = nexhealth_client(conn, settings, transport)
    try:
        syncs = await client.get("/sync_status", {"location_ids[]": location_id(conn)}) or []
    finally:
        await client.aclose()
    sync = [_sync_view(s) for s in syncs]
    conn.capability = {**(conn.capability or {}), "sync": sync, "writes_confirmable": bool(sync)}
    return sync
