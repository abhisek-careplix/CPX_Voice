"""The prior auth packet: what a payer needs to decide, and whether the case has it.

Two kinds of item:

- **Facts** on the case: the case type's required facts plus what a new request needs (diagnosis
  codes, ordering provider, date of service). Staff fill gaps on the case's facts form.
- **Documents** from the EHR mirror: the signed order, recent clinical notes and supporting
  documents (imaging, results). The NexHealth mirror carries none of these today, so until a
  connector syncs them a person attaches each one (``POST /api/prior-auth/{case}/packet``), naming
  the EHR document it is. The checklist then counts it as present, from staff.

Missing required items block the playbook with the list as its next step; that is the staff task
in the review inbox.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.cases import types
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Case, EhrRecord

# Facts a new request needs beyond the case type's required ones. ``submitted_on`` is filled by
# the playbook when it submits, so it is never a gap here.
REQUEST_FACTS = ("icd_codes", "ordering_provider", "service_date")
FILLED_BY_PLAYBOOK = ("submitted_on",)
# (key, label, EHR mirror resources that satisfy it, required)
DOCUMENTS: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("order", "Signed order for the service", ("order", "service_request"), True),
    (
        "clinical_notes",
        "Recent clinical notes showing medical necessity",
        ("note", "clinical_note", "encounter"),
        True,
    ),
    (
        "supporting_documents",
        "Supporting documents (imaging, test results)",
        ("document", "document_reference"),
        False,
    ),
)
DOCUMENT_KEYS = tuple(d[0] for d in DOCUMENTS)


async def _ehr_documents(session: AsyncSession, case: Case) -> dict[str, list[EhrRecord]]:
    ehr = (case.data or {}).get("ehr") or {}
    if not ehr.get("connection_id") or not ehr.get("patient_id"):
        return {}
    wanted = {r for _, _, resources, _ in DOCUMENTS for r in resources}
    rows = list(
        await session.scalars(
            select(EhrRecord).where(
                EhrRecord.connection_id == ehr["connection_id"],
                EhrRecord.patient_ref == str(ehr["patient_id"]),
                EhrRecord.resource.in_(wanted),
            )
        )
    )
    return {key: [r for r in rows if r.resource in resources] for key, _, resources, _ in DOCUMENTS}


async def checklist(session: AsyncSession, case: Case, provided: dict[str, Any]) -> list[dict]:
    """Every packet item with whether it's present and where it came from."""
    items: list[dict[str, Any]] = []
    for field in types.get("prior_authorization")["fields"]:
        key = field["key"]
        if key in FILLED_BY_PLAYBOOK or not (field["required"] or key in REQUEST_FACTS):
            continue
        value = types.value(case.data or {}, field["path"])
        items.append(
            {
                "key": key,
                "label": field["label"],
                "kind": "fact",
                "required": True,
                "present": value not in (None, "", []),
                "source": "case" if value not in (None, "", []) else None,
            }
        )
    found = await _ehr_documents(session, case)
    for key, label, _, required in DOCUMENTS:
        from_ehr = found.get(key) or []
        staff = provided.get(key)
        items.append(
            {
                "key": key,
                "label": label,
                "kind": "document",
                "required": required,
                "present": bool(from_ehr or staff),
                "source": "ehr" if from_ehr else ("staff" if staff else None),
                "ref": from_ehr[0].source_id if from_ehr else (staff or {}).get("ref"),
                "document_label": (staff or {}).get("label") if not from_ehr else label,
            }
        )
    return items


def in_sentence(label: str) -> str:
    """A label mid-sentence: "Diagnosis codes (ICD-10)" → "diagnosis codes (ICD-10)"; "CPT code"
    stays as it is."""
    return label if label[:2].isupper() else label[:1].lower() + label[1:]


def missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["required"] and not i["present"]]


def documents(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The documents a submission lists (labels and EHR references only)."""
    return [
        {"item": i["key"], "label": i.get("document_label") or i["label"], "ref": i.get("ref")}
        for i in items
        if i["kind"] == "document" and i["present"]
    ]


def provide(
    data: dict[str, Any], item: str, ref: str, label: str | None, actor: str
) -> dict[str, Any]:
    """A person attaches a packet document (by its EHR document id). Returns the new run data."""
    if item not in DOCUMENT_KEYS:
        raise ApiError(422, "bad_item", f"Packet documents are {', '.join(DOCUMENT_KEYS)}.")
    ref = (ref or "").strip()
    if not ref:
        raise ApiError(422, "ref_required", "Name the EHR document (its id or file name).")
    provided = dict(data.get("packet_provided") or {})
    provided[item] = {
        "ref": ref[:80],
        "label": (label or "").strip()[:120] or None,
        "by": actor,
        "at": utcnow().isoformat(),
    }
    return {**data, "packet_provided": provided}
