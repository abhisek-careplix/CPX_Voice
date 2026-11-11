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


def location_synced(location: dict[str, Any]) -> bool:
    """A ``foreign_id_type`` of ``nex``, or one starting ``--`` or ``none``, means no sync."""
    kind = str(location.get("foreign_id_type") or "").lower()
    return bool(kind) and kind != "nex" and not kind.startswith(("--", "none"))


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
            # The practice's own time zone (GET /locations): spoken times use it.
            "tz": location.get("tz"),
            # False: bookings stay in NexHealth and never reach the EHR.
            "insert_appt_client": location.get("insert_appt_client"),
            "last_sync_time": location.get("last_sync_time"),
            "map_by_operatory": location.get("map_by_operatory"),
            "set_availability_by_operatory": location.get("set_availability_by_operatory"),
            "foreign_id_type": location.get("foreign_id_type"),
            "synced": location_synced(location) if "foreign_id_type" in location else None,
        },
        status="inactive" if location.get("inactive") else "active",
        source_updated_at=parse_dt(location.get("updated_at")),
    )


def map_operatory(r: dict[str, Any]) -> Mirrored:
    return Mirrored(
        _id(r["id"]),
        {
            "name": r.get("display_name") or r.get("name"),
            "foreign_id": r.get("foreign_id"),
            "location_id": _id(r.get("location_id")),
            "active": r.get("active") is not False,
            "bookable_online": r.get("bookable_online"),
            "appointment_type_ids": _type_ids(r),
        },
        status="inactive" if r.get("active") is False else "active",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def _type_ids(r: dict[str, Any]) -> list[str | None]:
    return [_id(t.get("id")) for t in r.get("appointment_types") or [] if isinstance(t, dict)]


def map_working_hour(r: dict[str, Any]) -> Mirrored:
    label = r.get("label") if isinstance(r.get("label"), dict) else {}
    return Mirrored(
        _id(r["id"]),
        {
            "provider_id": _id(r.get("provider_id")),
            "operatory_id": _id(r.get("operatory_id")),
            "location_id": _id(r.get("location_id")),
            "begin_time": r.get("begin_time"),  # practice-local clock time, e.g. "08:00"
            "end_time": r.get("end_time"),
            "days": r.get("days") or [],
            "specific_date": r.get("specific_date"),
            "custom_recurrence": r.get("custom_recurrence"),
            "tz_offset": r.get("tz_offset"),
            "source": r.get("source"),  # manual | synced
            "label": label.get("name"),  # no label: the provider's base hours
            "appointment_type_ids": _type_ids(r),
        },
        status="inactive" if r.get("active") is False else "active",
    )


def map_patient_recall(r: dict[str, Any]) -> Mirrored:
    kind = r.get("recall_type") if isinstance(r.get("recall_type"), dict) else {}
    return Mirrored(
        _id(r["id"]),
        {
            "recall_id": _id(r.get("recall_id")),
            "recall_type": kind.get("name"),
            "interval_num": kind.get("interval_num"),
            "interval_unit": kind.get("interval_unit"),
            "date_due": r.get("date_due"),
            "appointment_id": _id(r.get("appointment_id")),
            "foreign_id": r.get("foreign_id"),
        },
        patient_ref=_id(r.get("patient_id")),
        status="scheduled" if r.get("appointment_id") else "due",
        source_updated_at=parse_dt(r.get("updated_at")),
    )


def map_patient_alert(r: dict[str, Any]) -> Mirrored:
    # The note is free text the office wrote: it can carry clinical detail, so it is sealed.
    return Mirrored(
        _id(r["id"]),
        {"disabled_at": r.get("disabled_at")},
        pii={"note": r.get("note")},
        patient_ref=_id(r.get("patient_id")),
        status="disabled" if r.get("disabled_at") else "active",
        source_updated_at=parse_dt(r.get("updated_at")),
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


PHONE_FIELDS = (
    "cell_phone_number",
    "phone_number",
    "home_phone_number",
    "work_phone_number",
    "custom_contact_number",
)
ADDRESS_FIELDS = ("address_line_1", "address_line_2", "street_address", "city", "state", "zip_code")


def is_placeholder(r: dict[str, Any]) -> bool:
    """A patient that exists only in NexHealth (created through the API, not yet a chart).

    When the office creates the real chart, NexHealth links the two and deletes this record.
    """
    bio = r.get("bio") or {}
    foreign_id = str(r.get("foreign_id") or "")
    return (
        not foreign_id
        or foreign_id.endswith("_np")
        or str(r.get("foreign_id_type") or "").lower() == "nex"
        or bool(bio.get("new_patient"))
    )


def map_patient(r: dict[str, Any]) -> Mirrored:
    bio = r.get("bio") or {}
    phones = {k: bio.get(k) for k in PHONE_FIELDS if bio.get(k)}
    address = {k: bio.get(k) for k in ADDRESS_FIELDS if bio.get(k)}
    return Mirrored(
        _id(r["id"]),
        {
            "foreign_id": r.get("foreign_id"),
            "foreign_id_type": r.get("foreign_id_type"),
            "provider_id": _id(r.get("provider_id")),
            "guarantor_id": _id(r.get("guarantor_id")),
            "inactive": bool(r.get("inactive")),
            "has_phone": bool(phones),
            "new_patient": bool(bio.get("new_patient")),
            "non_patient": bool(bio.get("non_patient")),
            "placeholder": is_placeholder(r),
            "last_sync_time": r.get("last_sync_time"),
        },
        pii={
            "first_name": r.get("first_name"),
            "middle_name": r.get("middle_name"),
            "last_name": r.get("last_name"),
            "date_of_birth": bio.get("date_of_birth"),
            "gender": bio.get("gender"),
            "phone": bio.get("cell_phone_number") or bio.get("phone_number"),
            "phones": phones,
            "email": r.get("email"),
            "address": address,
            "preferred_language": r.get("preferred_language"),
            "chart_id": r.get("chart_id"),
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


def subscriber_is_patient(r: dict[str, Any]) -> bool:
    return str(r.get("subscription_relation") or "self").lower() == "self"


def map_coverage(r: dict[str, Any]) -> Mirrored:
    """A coverage. Its subscriber comes only from ``GET /insurance_coverages/{id}``.

    Fields for the eligibility handoff, all on the ``coverage`` mirror record:

    - ``attrs.subscriber_id``: the subscriber's NexHealth id (a patient id).
    - ``attrs.subscriber_is_patient``: true when ``subscription_relation`` is ``self``.
    - ``attrs.subscriber_details``: ``loaded`` (name and birth date below), ``self`` (use the
      patient's own record) or ``pending`` (not read yet; the next sync pass reads it).
    - ``pii.subscriber_name`` and ``pii.subscriber_date_of_birth`` (``YYYY-MM-DD``), sealed.
    """
    subscriber = r.get("subscriber") if isinstance(r.get("subscriber"), dict) else None
    is_patient = subscriber_is_patient(r)
    details = "loaded" if subscriber is not None else ("self" if is_patient else "pending")
    pii: dict[str, Any] = {"member_id": r.get("subscriber_num")}
    if subscriber is not None:
        pii["subscriber_name"] = subscriber.get("name")
        pii["subscriber_date_of_birth"] = subscriber.get("date_of_birth")
    if subscriber is not None:
        subscriber_id = _id(subscriber.get("id"))
    else:
        subscriber_id = _id(r.get("patient_id")) if is_patient else None
    return Mirrored(
        _id(r["id"]),
        {
            "plan_id": _id(r.get("plan_id")),
            "priority": r.get("priority"),
            "insurance_type": r.get("insurance_type"),
            "effective_date": r.get("effective_date"),
            "expiration_date": r.get("expiration_date"),
            "subscription_relation": r.get("subscription_relation"),
            "subscriber_id": subscriber_id,
            "subscriber_is_patient": is_patient,
            "subscriber_details": details,
        },
        pii=pii,
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
            "confirmed": bool(r.get("confirmed")),
            "checkin_at": r.get("checkin_at"),
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
