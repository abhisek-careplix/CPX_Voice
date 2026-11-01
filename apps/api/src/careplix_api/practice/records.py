"""Map NexHealth records into the practice mirror (allow-listed fields only) and upsert them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.models import EhrRecord, PayerConnection
from careplix_api.phi import PhiBox


def parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Mirrored:
    source_id: str
    attrs: dict[str, Any]
    pii: dict[str, Any] = field(default_factory=dict)
    patient_ref: str | None = None
    starts_at: datetime | None = None
    status: str | None = None
    source_updated_at: datetime | None = None


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


def map_location(institution: dict[str, Any], location: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(location["id"]),
        {
            "name": location.get("name"),
            "inactive": bool(location.get("inactive")),
            "institution_id": _id(institution.get("id")),
            "institution_name": institution.get("name"),
            "subdomain": institution.get("subdomain"),
            "emrs": institution.get("emrs") or [],
        },
        status="inactive" if location.get("inactive") else "active",
    )


def map_provider(r: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(r["id"]),
        {
            "name": r.get("name") or r.get("display_name"),
            "npi": r.get("npi"),
            "specialty_code": r.get("specialty_code"),
            "inactive": bool(r.get("inactive")),
        },
        status="inactive" if r.get("inactive") else "active",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def map_appointment_type(r: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(r["id"]),
        {
            "name": r.get("name"),
            "minutes": r.get("minutes"),
            "bookable_online": r.get("bookable_online"),
        },
    )


def map_patient(r: dict[str, Any]) -> Mirrored:
    bio = r.get("bio") or {}
    return Mirrored(
        _id(r["id"]),
        {
            "foreign_id": r.get("foreign_id"),
            "provider_id": _id(r.get("provider_id")),
            "inactive": bool(r.get("inactive")),
            "has_phone": bool(bio.get("phone_number") or bio.get("cell_phone_number")),
        },
        pii={
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "date_of_birth": bio.get("date_of_birth"),
            "gender": bio.get("gender"),
            "phone": bio.get("cell_phone_number") or bio.get("phone_number"),
            "email": r.get("email"),
        },
        patient_ref=_id(r["id"]),
        status="inactive" if r.get("inactive") else "active",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def map_insurance_plan(r: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(r["id"]),
        {
            "name": r.get("name"),
            "payer_id": r.get("payer_id"),
            "group_num": r.get("group_num"),
        },
        status="deleted" if r.get("deleted_at") else "active",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def map_coverage(r: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(r["id"]),
        {
            "plan_id": _id(r.get("plan_id")),
            "priority": r.get("priority"),
            "insurance_type": r.get("insurance_type"),
            "effective_date": r.get("effective_date"),
            "expiration_date": r.get("expiration_date"),
            "subscription_relation": r.get("subscription_relation"),
        },
        pii={"member_id": r.get("subscriber_num")},
        patient_ref=_id(r.get("patient_id")),
        status="active" if r.get("active") is not False else "inactive",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def appointment_status(r: dict[str, Any]) -> str:
    if r.get("deleted"):
        return "deleted"
    if r.get("cancelled"):
        return "cancelled"
    if r.get("patient_missed"):
        return "missed"
    if r.get("checked_out"):
        return "completed"
    return "confirmed" if r.get("confirmed") else "booked"


def map_appointment(r: dict[str, Any]) -> Mirrored:
    # Free-text fields (note, misc) are never mirrored: they can carry clinical detail.
    return Mirrored(
        _id(r["id"]),
        {
            "provider_id": _id(r.get("provider_id")),
            "provider_name": r.get("provider_name"),
            "appointment_type_id": _id(r.get("appointment_type_id")),
            "operatory_id": _id(r.get("operatory_id")),
            "location_id": _id(r.get("location_id")),
            "end_time": r.get("end_time"),
            "timezone": r.get("timezone"),
            "cancelled_at": r.get("cancelled_at"),
            "sooner_if_possible": bool(r.get("sooner_if_possible")),
            "foreign_id": r.get("foreign_id"),
            "last_sync_time": r.get("last_sync_time"),
            "unavailable": bool(r.get("unavailable")),
        },
        patient_ref=_id(r.get("patient_id")),
        starts_at=parse_dt(r.get("start_time")),
        status=appointment_status(r),
        source_updated_at=parse_dt(r.get("updated_at")),
    )


async def upsert(
    session: AsyncSession,
    conn: PayerConnection,
    resource: str,
    items: list[Mirrored],
    box: PhiBox,
) -> list[dict[str, Any]]:
    """Insert or update mirror rows. Returns status transitions (ids and statuses only)."""
    existing: dict[str, EhrRecord] = {}
    ids = [m.source_id for m in items]
    for start in range(0, len(ids), 500):
        for rec in await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == conn.id,
                EhrRecord.resource == resource,
                EhrRecord.source_id.in_(ids[start : start + 500]),
            )
        ):
            existing[rec.source_id] = rec
    transitions: list[dict[str, Any]] = []
    now = utcnow()
    for m in items:
        rec = existing.get(m.source_id)
        if rec is None:
            rec = EhrRecord(
                workspace_id=conn.workspace_id,
                connection_id=conn.id,
                resource=resource,
                source_id=m.source_id,
            )
            session.add(rec)
            existing[m.source_id] = rec
            transitions.append({"id": m.source_id, "from": None, "to": m.status})
        elif rec.status != m.status:
            transitions.append({"id": m.source_id, "from": rec.status, "to": m.status})
        rec.attrs = m.attrs
        rec.pii = box.seal(m.pii) if m.pii else None
        rec.patient_ref, rec.starts_at, rec.status = m.patient_ref, m.starts_at, m.status
        rec.source_updated_at, rec.synced_at = m.source_updated_at, now
    await session.flush()
    return transitions
