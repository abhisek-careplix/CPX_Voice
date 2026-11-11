"""Sync the practice mirror from NexHealth and probe what the location supports (FO-0).

Read path (EMR design §2.3, NexHealth "Data pipelines configuration guide"): webhooks trigger
targeted re-reads; an incremental pass (``updated_since`` with a small overlap, sorted by
``updated_at``) catches anything missed; reference data refreshes daily.

- A list is read to its last page or not at all: a cursor never advances past rows it didn't see.
- Patients are read with ``location_strict=true``: on a shared server a plain search returns
  sister offices' patients too.
- Appointments: the forward window (``nexhealth_window_days``) is loaded in monthly chunks on a full
  pass; history (``nexhealth_history_months``) is backfilled a few calendar months per pass.
- Coverages: the active list, then ``active=false`` so ended coverage turns inactive here too. The
  subscriber of a dependant's coverage is read from ``GET /insurance_coverages/{id}``.
- Placeholders: a patient created through the API is deleted by NexHealth once the office's real
  chart links to it. The mirror marks it ``deleted`` and points its visits at the real chart.
- Patient alerts have no location-wide list: they are read per patient, for the next week's visits.
- v3.1 beta-only resources (forms, form responses) are not read: they need beta access.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import as_utc, utcnow
from careplix_api.integrations.nexhealth import NexHealthClient, NexHealthError
from careplix_api.models import EhrRecord, PayerConnection, SyncCursor
from careplix_api.phi import PhiBox
from careplix_api.practice import records
from careplix_api.practice.connections import audit, location_id, nexhealth_client
from careplix_api.settings import Settings

REFERENCE_TTL = timedelta(hours=24)
OVERLAP = timedelta(minutes=5)
WINDOW_BACK = timedelta(days=1)
CHUNK = timedelta(days=30)  # one "month" of the forward window per request on a full pass
HISTORY_CHUNKS_PER_PASS = 3
MAX_SUBSCRIBER_READS = 300  # coverage detail reads per pass (each is a billable call)
ALERT_HORIZON = timedelta(days=7)
MAX_ALERT_PATIENTS = 100
MAX_PLACEHOLDER_READS = 50

# resource → (path, mapper, incremental by updated_since)
RESOURCES: dict[str, tuple[str, Any, bool]] = {
    "provider": ("/providers", records.map_provider, False),
    "operatory": ("/operatories", records.map_operatory, False),
    "working_hour": ("/working_hours", records.map_working_hour, False),
    "appointment_type": ("/appointment_types", records.map_appointment_type, False),
    "insurance_plan": ("/insurance_plans", records.map_insurance_plan, False),
    "patient": ("/patients", records.map_patient, True),
    "coverage": ("/insurance_coverages", records.map_coverage, True),
    "appointment": ("/appointments", records.map_appointment, True),
    "patient_recall": ("/patient_recalls", records.map_patient_recall, True),
}
LINKED_TO_PATIENT = ("appointment", "coverage", "patient_recall", "patient_alert")
# Reference lists read whole: a record missing from the list is gone at the source.
PRUNED = {"operatory", "working_hour"}
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
            workspace_id=conn.workspace_id,
            connection_id=conn.id,
            resource=resource,
            rows=0,
            api_calls=0,
            duration_ms=0,
        )
        session.add(cursor)
    return cursor


def _iso(moment: datetime) -> str:
    return as_utc(moment).isoformat()


def _params(resource: str, conn: PayerConnection, since: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if resource != "coverage":  # coverages are listed per institution; location_id isn't taken
        params["location_id"] = location_id(conn)
    if resource == "patient":
        params["location_strict"] = "true"
    if resource in ("patient", "appointment"):
        params["sort"] = "updated_at"
    if resource in ("operatory", "working_hour"):
        params["include[]"] = "appointment_types"
    if since is not None:
        params["updated_since"] = _iso(since)
    return params


def appointment_windows(
    now: datetime, ahead_days: int, *, chunked: bool
) -> list[tuple[datetime, datetime]]:
    """The forward window, whole (incremental) or in 30-day pieces (full pass)."""
    start, end = now - WINDOW_BACK, now + timedelta(days=max(1, ahead_days))
    if not chunked:
        return [(start, end)]
    out = []
    while start < end:
        out.append((start, min(start + CHUNK, end)))
        start += CHUNK
    return out


def month_start(moment: datetime) -> datetime:
    moment = as_utc(moment)
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


def previous_month(moment: datetime) -> datetime:
    first = month_start(moment)
    return month_start(first - timedelta(days=1))


async def _pull(
    resource: str,
    path: str,
    client: NexHealthClient,
    conn: PayerConnection,
    settings: Settings,
    since: Any,
) -> tuple[list[dict[str, Any]], int]:
    """Every row of one resource for this pass (raises rather than return a partial list)."""
    base = _params(resource, conn, since)
    if resource == "appointment":
        rows: dict[Any, dict[str, Any]] = {}
        pages = 0
        windows = appointment_windows(
            utcnow(), settings.nexhealth_window_days, chunked=since is None
        )
        for start, end in windows:
            got, n = await client.list_all(path, {**base, "start": _iso(start), "end": _iso(end)})
            rows.update({r.get("id"): r for r in got})
            pages += n
        return list(rows.values()), pages
    if resource == "coverage":
        # Active coverages, then the ones that ended: GET lists only active ones by default.
        active, n1 = await client.list_all(path, base)
        ended, n2 = await client.list_all(path, {**base, "active": "false"})
        return active + [{**r, "active": False} for r in ended], n1 + n2
    return await client.list_all(path, base)


async def _sync_location(
    session: AsyncSession, conn: PayerConnection, client: NexHealthClient, box: PhiBox
) -> int:
    """The location from ``GET /locations`` (time zone, insertion setting, last sync)."""
    loc_id = location_id(conn)
    try:
        institutions = (await client.request("GET", "/locations")).get("data") or []
    except NexHealthError as err:
        if err.http_status not in (400, 404):
            raise
        institutions = await client.institutions()
    items = [
        records.map_location(inst, loc)
        for inst in institutions
        for loc in inst.get("locations") or []
        if str(loc.get("id")) == loc_id
    ]
    await records.upsert(session, conn, "location", items, box)
    if items:
        attrs = items[0].attrs
        conn.capability = {
            **(conn.capability or {}),
            "location_tz": attrs.get("tz"),
            "insert_appt_client": attrs.get("insert_appt_client"),
            "location_last_sync_time": attrs.get("last_sync_time"),
        }
    return len(items)


async def _existing(
    session: AsyncSession, conn: PayerConnection, resource: str, ids: list[str]
) -> dict[str, EhrRecord]:
    out: dict[str, EhrRecord] = {}
    for start in range(0, len(ids), 500):
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == resource,
                EhrRecord.source_id.in_(ids[start : start + 500]),
            )
        ):
            out[rec.source_id] = rec
    return out


async def _with_subscribers(
    session: AsyncSession,
    conn: PayerConnection,
    client: NexHealthClient,
    box: PhiBox,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add the subscriber to each dependant's coverage (the list endpoint leaves it out).

    Beyond the per-pass read budget, a coverage keeps the subscriber already in the mirror;
    one never read stays ``pending`` and is read on a later pass.
    """
    ids = [str(r["id"]) for r in rows if r.get("id") is not None]
    known = await _existing(session, conn, "coverage", ids)
    pending = [
        rec.source_id
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id, EhrRecord.resource == "coverage"
            )
        )
        if rec.attrs.get("subscriber_details") == "pending" and rec.source_id not in known
    ]
    budget = MAX_SUBSCRIBER_READS
    out = []
    for row in rows + [{"id": p, "_pending": True} for p in pending]:
        pending_only = row.pop("_pending", False)
        if not pending_only and records.subscriber_is_patient(row):
            out.append(row)
            continue
        if budget > 0:
            budget -= 1
            try:
                detail = await client.get(f"/insurance_coverages/{row['id']}")
            except NexHealthError as err:
                if err.http_status != 404:
                    raise
                detail = None
            if isinstance(detail, dict) and detail.get("id") is not None:
                if row.get("active") is False:
                    detail = {**detail, "active": False}
                out.append(detail)
                continue
        if pending_only:
            continue
        rec = known.get(str(row["id"]))
        if rec is not None and rec.attrs.get("subscriber_details") == "loaded":
            pii = box.open(rec.pii)
            row = {
                **row,
                "subscriber": {
                    "id": rec.attrs.get("subscriber_id"),
                    "name": pii.get("subscriber_name"),
                    "date_of_birth": pii.get("subscriber_date_of_birth"),
                },
            }
        out.append(row)
    return out


def _identity(pii: dict[str, Any]) -> tuple[str, str, str] | None:
    first = "".join(ch for ch in str(pii.get("first_name") or "").lower() if ch.isalpha())
    last = "".join(ch for ch in str(pii.get("last_name") or "").lower() if ch.isalpha())
    dob = str(pii.get("date_of_birth") or "")[:10]
    return (first, last, dob) if first and last and dob else None


async def merge_placeholders(
    session: AsyncSession,
    conn: PayerConnection,
    client: NexHealthClient,
    box: PhiBox,
    charts: list[records.Mirrored] | None,
) -> int:
    """Follow NexHealth's placeholder → real chart merge.

    ``charts`` are real (non-placeholder) patients just read; a placeholder with the same name and
    birth date is re-read, and when NexHealth has deleted it the mirror marks it ``deleted`` and
    moves its visits and coverage to the real chart. ``None`` re-checks every placeholder (full
    pass). Returns the number of placeholders merged.
    """
    placeholders = [
        rec
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id, EhrRecord.resource == "patient"
            )
        )
        if rec.attrs.get("placeholder") and rec.status != "deleted"
    ]
    if not placeholders:
        return 0
    real: dict[tuple[str, str, str], str] = {}
    for m in charts or []:
        key = _identity(m.pii)
        if key and not m.attrs.get("placeholder"):
            real[key] = m.source_id
    candidates = []
    for rec in placeholders:
        key = _identity(box.open(rec.pii))
        if charts is None or (key and key in real):
            candidates.append((rec, key))
    merged = 0
    for rec, key in candidates[:MAX_PLACEHOLDER_READS]:
        try:
            data = await client.get(f"/patients/{rec.source_id}")
        except NexHealthError as err:
            if err.http_status != 404:
                raise
            data = None
        if isinstance(data, dict) and data.get("id") is not None:
            await records.upsert(session, conn, "patient", [records.map_patient(data)], box)
            continue
        target = real.get(key) if key else None
        if target is None and key:
            for other in await session.scalars(
                select(EhrRecord).where(
                    EhrRecord.connection_id == conn.id,
                    EhrRecord.resource == "patient",
                    EhrRecord.source_id != rec.source_id,
                )
            ):
                if (
                    not other.attrs.get("placeholder")
                    and other.status != "deleted"
                    and _identity(box.open(other.pii)) == key
                ):
                    target = other.source_id
                    break
        rec.status = "deleted"
        rec.attrs = {**rec.attrs, "merged_into": target}
        merged += 1
    await session.flush()
    if merged:
        await repoint_merged(session, conn)
    return merged


async def repoint_merged(session: AsyncSession, conn: PayerConnection) -> int:
    """Move visits, coverage and recalls off merged placeholders onto the real chart.

    Run after every pass too: a re-read can still name the placeholder until NexHealth updates
    the appointment itself.
    """
    merged = {
        rec.source_id: rec.attrs["merged_into"]
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "patient",
                EhrRecord.status == "deleted",
            )
        )
        if rec.attrs.get("merged_into")
    }
    if not merged:
        return 0
    moved = 0
    for linked in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource.in_(LINKED_TO_PATIENT),
            EhrRecord.patient_ref.in_(list(merged)),
        )
    ):
        linked.patient_ref = merged[linked.patient_ref]
        moved += 1
    await session.flush()
    return moved


async def _sync_alerts(
    session: AsyncSession,
    conn: PayerConnection,
    client: NexHealthClient,
    box: PhiBox,
) -> dict[str, Any]:
    """Alerts for patients seen in the next week (``GET /patients/{id}/alerts``, sealed notes)."""
    now = utcnow()
    patients: list[str] = []
    for appt in await session.scalars(
        select(EhrRecord)
        .where(
            EhrRecord.connection_id == conn.id,
            EhrRecord.resource == "appointment",
            EhrRecord.starts_at >= now,
            EhrRecord.starts_at <= now + ALERT_HORIZON,
            EhrRecord.status.in_(("booked", "confirmed")),
        )
        .order_by(EhrRecord.starts_at)
    ):
        if appt.patient_ref and appt.patient_ref not in patients:
            patients.append(appt.patient_ref)
    rows = 0
    for patient in patients[:MAX_ALERT_PATIENTS]:
        try:
            data = await client.get(f"/patients/{patient}/alerts") or []
        except NexHealthError as err:
            if err.http_status in (400, 404):
                continue  # placeholder patients have no chart to hold alerts
            raise
        items = [
            records.map_patient_alert({"patient_id": patient, **a})
            for a in data
            if isinstance(a, dict) and a.get("id") is not None
        ]
        await records.upsert(session, conn, "patient_alert", items, box)
        seen = {i.source_id for i in items}
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == "patient_alert",
                EhrRecord.patient_ref == patient,
            )
        ):
            if rec.source_id not in seen:
                rec.status = "disabled"
        rows += len(items)
    return {"rows": rows, "patients": min(len(patients), MAX_ALERT_PATIENTS)}


async def _sync_history(
    session: AsyncSession,
    conn: PayerConnection,
    client: NexHealthClient,
    box: PhiBox,
    settings: Settings,
) -> dict[str, Any] | None:
    """Load past appointments one calendar month at a time, newest first, a few per pass.

    The ``appointment_history`` cursor's ``high_water`` is the oldest boundary loaded so far
    (not an ``updated_at``); ``last_full_at`` is set once the configured history is in.
    """
    months = settings.nexhealth_history_months
    if months <= 0:
        return None
    cursor = await _cursor(session, conn, "appointment_history")
    if cursor.last_full_at is not None:
        return None
    now = utcnow()
    oldest = month_start(now)
    for _ in range(months):
        oldest = previous_month(oldest)
    boundary = as_utc(cursor.high_water) if cursor.high_water else now - WINDOW_BACK
    loaded, pages = 0, 0
    calls_before = client.calls
    try:
        for _ in range(HISTORY_CHUNKS_PER_PASS):
            if boundary <= oldest:
                break
            start = month_start(boundary)
            if start == boundary:
                start = previous_month(boundary)
            rows, n = await client.list_all(
                "/appointments",
                {"location_id": location_id(conn), "start": _iso(start), "end": _iso(boundary)},
            )
            items = [records.map_appointment(r) for r in rows if r.get("id") is not None]
            await records.upsert(session, conn, "appointment", items, box)
            loaded, pages, boundary = loaded + len(items), pages + n, start
            cursor.high_water = boundary  # advances only after the month was read in full
    except NexHealthError as err:
        cursor.last_error, cursor.last_run_at = err.message[:200], utcnow()
        cursor.api_calls += client.calls - calls_before
        return {"error": err.message}
    cursor.rows += loaded
    cursor.api_calls += client.calls - calls_before
    cursor.last_run_at, cursor.last_error = utcnow(), None
    if boundary <= oldest:
        cursor.last_full_at = cursor.last_run_at
    return {"rows": loaded, "pages": pages, "through": boundary.date().isoformat()}


async def sync_practice(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """One sync pass through the practice's connector."""
    from careplix_api.connect.connectors import connector_for

    connector = connector_for(conn, settings, transport)
    try:
        return await connector.sync(session, full=full)
    finally:
        await connector.aclose()


async def sync_nexhealth(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """One NexHealth sync pass. Per-resource failures are recorded and never stop the others."""
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
                rows, pages = await _pull(resource, path, client, conn, settings, since)
                if resource == "coverage":
                    rows = await _with_subscribers(session, conn, client, box, rows)
            except NexHealthError as err:
                # Nothing from this resource is applied and its cursor stays where it was.
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
            if resource in PRUNED:
                seen = {i.source_id for i in items}
                for rec in await session.scalars(
                    select(EhrRecord).where(
                        EhrRecord.connection_id == conn.id, EhrRecord.resource == resource
                    )
                ):
                    if rec.source_id not in seen:
                        rec.status = "removed"
            if resource == "patient":
                try:
                    merged = await merge_placeholders(
                        session, conn, client, box, None if since is None else items
                    )
                except NexHealthError:
                    merged = 0  # retried on the next pass
                if merged:
                    report["placeholders_merged"] = merged
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
        alerts = await _cursor(session, conn, "patient_alert")
        alerts_at = as_utc(alerts.last_full_at)
        if full or alerts_at is None or utcnow() - alerts_at >= REFERENCE_TTL:
            calls_before = client.calls
            try:
                report["resources"]["patient_alert"] = await _sync_alerts(
                    session, conn, client, box
                )
                alerts.last_full_at = alerts.last_run_at = utcnow()
                alerts.last_error = None
            except NexHealthError as err:
                alerts.last_error, alerts.last_run_at = err.message[:200], utcnow()
                report["resources"]["patient_alert"] = {"error": err.message}
            alerts.api_calls += client.calls - calls_before
        history = await _sync_history(session, conn, client, box, settings)
        if history is not None:
            report["resources"]["appointment_history"] = history
        await repoint_merged(session, conn)
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
    """Re-read records named by a webhook (the webhook body itself is never trusted or stored).

    A record the source no longer has (404) is marked ``deleted`` in the mirror.
    """
    resource, template = SINGLE[resource_type]
    mapper = RESOURCES[resource][1]
    box, client = PhiBox(settings), nexhealth_client(conn, settings, transport)
    # GET /patients/{id} and /insurance_coverages/{id} take no location_id.
    params = {"location_id": location_id(conn)} if resource == "appointment" else None
    try:
        items, gone = [], []
        for record_id in ids[:50]:
            try:
                data = await client.get(template.format(id=record_id), params)
            except NexHealthError as err:
                if err.http_status != 404:
                    raise
                gone.append(str(record_id))
                continue
            if isinstance(data, dict) and data.get("id") is not None:
                items.append(mapper(data))
        transitions = await records.upsert(session, conn, resource, items, box)
        for rec in (await _existing(session, conn, resource, gone)).values():
            if rec.status != "deleted":
                transitions.append({"id": rec.source_id, "from": rec.status, "to": "deleted"})
                rec.status = "deleted"
        if resource == "patient" and (items or gone):
            await merge_placeholders(session, conn, client, box, items)
        return transitions
    finally:
        cursor = await _cursor(session, conn, resource)
        cursor.api_calls += client.calls
        await client.aclose()


# Reads the probe tries, with the minimum parameters each needs (verified on the sandbox).
PROBE_READS: dict[str, tuple[str, dict[str, Any]]] = {
    "locations": ("/locations", {"no_location": True}),
    "providers": ("/providers", {}),
    "operatories": ("/operatories", {}),
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
    "appointments": "POST /appointments (appointment_insertion webhook)",
    "appointment_updates": "PATCH /appointments/{id}: cancel, confirm, check-in, time "
    "(appointment_updated webhook)",
    "patients": "POST /patients (reaches the EHR with the first booked appointment)",
    "patient_alerts": "POST /patients/{id}/alerts",
    "patient_documents": "POST /patients/{id}/documents (insertion webhook)",
    "payments": "POST /payments (insertion webhook)",
    "adjustments": "POST /adjustments (insertion webhook)",
}
# Not read or written: v3.1 beta resources need beta access for the organization.
BETA_ONLY = ["forms", "form_responses", "patient insertion (v3.1)", "insurance writes (v3.1)"]


async def probe(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Live capability map through the practice's connector."""
    from careplix_api.connect.connectors import connector_for

    connector = connector_for(conn, settings, transport)
    try:
        return await connector.probe(session)
    finally:
        await connector.aclose()


async def probe_nexhealth(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """NexHealth capability map for this location: reads, sync health, documented writes."""
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
            params: dict[str, Any] = {} if hints.get("no_location") else {"location_id": loc_id}
            params["per_page"] = 1
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
        "beta_only": BETA_ONLY,
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


def schedule_status(conn: PayerConnection) -> str:
    """green | red | none. Red when any sync's reads or writes are down: what we'd offer may be
    stale, or a booking would wait for the office. NexHealth: check before offering times."""
    syncs = (conn.capability or {}).get("sync") or []
    if not syncs:
        return "none"
    ok = all(s.get("read_status") == "green" and s.get("write_status") == "green" for s in syncs)
    return "green" if ok else "red"


async def refresh_sync_status(
    session: AsyncSession,
    conn: PayerConnection,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Cheap re-read of sync health (one call), used when the EHR reports a status change."""
    from careplix_api.connect.connectors import connector_for

    connector = connector_for(conn, settings, transport)
    try:
        return await connector.health(session)
    finally:
        await connector.aclose()


async def refresh_nexhealth_status(conn: PayerConnection, client: Any) -> list[dict[str, Any]]:
    syncs = await client.get("/sync_status", {"location_ids[]": location_id(conn)}) or []
    sync = [_sync_view(s) for s in syncs]
    conn.capability = {**(conn.capability or {}), "sync": sync, "writes_confirmable": bool(sync)}
    return sync
