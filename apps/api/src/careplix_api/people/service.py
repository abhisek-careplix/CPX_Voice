"""Person memory: profiles, identity, the memory an agent reads, evidence of checks.

Every query is scoped by the caller's workspace (principal or call attempt), never by a value
from the client.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, EhrRecord, PayerConnection
from careplix_api.people import policy
from careplix_api.people.models import (
    Credential,
    Observation,
    Person,
    Relationship,
    VerificationEvent,
    VerificationPolicy,
)
from careplix_api.phi import PhiBox, initials

PII_FIELDS = (
    "first_name",
    "last_name",
    "preferred_name",
    "date_of_birth",
    "phone",
    "email",
    "address",
    "zip",
    "member_id",
    "lat",
    "lon",
)
# Kinds whose identity is patient information: the list shows initials only.
PROTECTED = ("patient", "client", "family")
SYNTHETIC = re.compile(r"synthetic|sample|example|test", re.IGNORECASE)


# --- identity ------------------------------------------------------------------------------------


async def ehr_identity(session: AsyncSession, person: Person, box: PhiBox) -> dict[str, Any]:
    """Name, birth date, phone and ZIP from the practice mirror for a linked patient."""
    if not (person.ehr_connection_id and person.ehr_patient_ref):
        return {}
    record = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.workspace_id == person.workspace_id,
            EhrRecord.connection_id == person.ehr_connection_id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == person.ehr_patient_ref,
        )
    )
    if record is None:
        return {}
    pii = box.open(record.pii)
    address = pii.get("address") or {}
    return {
        "first_name": pii.get("first_name"),
        "last_name": pii.get("last_name"),
        "date_of_birth": pii.get("date_of_birth"),
        "phone": pii.get("phone"),
        "email": pii.get("email"),
        "zip": address.get("zip_code") if isinstance(address, dict) else None,
    }


async def identity(session: AsyncSession, person: Person, box: PhiBox) -> dict[str, Any]:
    """What is on file for this person: the EHR's copy first, then our own profile fields."""
    own = box.open(person.pii)
    linked = await ehr_identity(session, person, box)
    return {**own, **{k: v for k, v in linked.items() if v}}


def full_name(pii: dict[str, Any]) -> str:
    return " ".join(p for p in (pii.get("first_name"), pii.get("last_name")) if p) or "Unnamed"


def list_label(person: Person, pii: dict[str, Any]) -> str:
    return initials(pii) if person.kind in PROTECTED else full_name(pii)


# --- policy --------------------------------------------------------------------------------------


async def load_policy(session: AsyncSession, workspace_id: str) -> dict[str, Any]:
    row = await session.scalar(
        select(VerificationPolicy).where(VerificationPolicy.workspace_id == workspace_id)
    )
    return {**policy.DEFAULT_POLICY, **(row.rules if row else {})}


async def save_policy(
    session: AsyncSession, principal: Principal, rules: dict[str, Any]
) -> dict[str, Any]:
    row = await session.scalar(
        select(VerificationPolicy).where(VerificationPolicy.workspace_id == principal.workspace_id)
    )
    try:
        clean = policy.clean_policy(rules, row.rules if row else None)
    except policy.PolicyError as err:
        raise ApiError(422, "invalid_policy", str(err)) from None
    if row is None:
        row = VerificationPolicy(workspace_id=principal.workspace_id, rules=clean)
        session.add(row)
    else:
        row.rules = clean
    row.updated_by = principal.user_id
    _audit(session, principal, "people.policy.updated", principal.workspace_id, clean)
    return clean


async def evidence(
    session: AsyncSession,
    workspace_id: str,
    attempt_id: str | None,
    check: str,
    outcome: str,
    *,
    person_id: str | None = None,
    subject_id: str | None = None,
    reason: str | None = None,
    identifiers: list[str] | None = None,
    topic: str | None = None,
    policy_version: int = 1,
) -> VerificationEvent:
    row = VerificationEvent(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        check=check,
        outcome=outcome,
        person_id=person_id,
        subject_id=subject_id,
        reason=reason,
        identifiers=identifiers or [],
        topic=topic,
        policy_version=policy_version,
    )
    session.add(row)
    await session.flush()
    return row


# --- reads ---------------------------------------------------------------------------------------


async def get(session: AsyncSession, workspace_id: str, person_id: str) -> Person:
    person = await session.get(Person, person_id)
    if person is None or person.workspace_id != workspace_id:
        raise ApiError(404, "person_not_found", "No accessible person matches that reference.")
    return person


async def list_people(
    session: AsyncSession, principal: Principal, box: PhiBox, kind: str | None, q: str
) -> list[dict[str, Any]]:
    query = select(Person).where(Person.workspace_id == principal.workspace_id)
    if kind:
        query = query.where(Person.kind == kind)
    people = list(await session.scalars(query))
    creds = await _credentials(session, principal.workspace_id, [p.id for p in people])
    today = utcnow().date()
    needle = policy.norm(q)
    out = []
    for p in people:
        pii = await identity(session, p, box)
        if needle and needle not in policy.norm(full_name(pii)):
            continue
        states = [policy.credential_state(c.expires_on, today) for c in creds.get(p.id, [])]
        out.append(
            {
                "id": p.id,
                "kind": p.kind,
                "kind_label": policy.KIND_LABEL[p.kind],
                "name": list_label(p, pii),
                "status": p.status,
                "synthetic": p.synthetic,
                "preferred_channel": p.preferred_channel,
                "do_not_contact": p.do_not_contact,
                "language": p.language,
                "linked_to_ehr": bool(p.ehr_patient_ref),
                "skills": p.skills or [],
                "credentials_expired": states.count("expired"),
                "credentials_expiring": states.count("expiring"),
                "organisation": p.organisation,
            }
        )
    out.sort(key=lambda r: (r["kind"], r["name"].lower()))
    return out


async def _credentials(
    session: AsyncSession, workspace_id: str, ids: list[str]
) -> dict[str, list[Credential]]:
    out: dict[str, list[Credential]] = defaultdict(list)
    if not ids:
        return out
    for c in await session.scalars(
        select(Credential).where(
            Credential.workspace_id == workspace_id, Credential.person_id.in_(ids)
        )
    ):
        out[c.person_id].append(c)
    return out


async def labels(
    session: AsyncSession, workspace_id: str, box: PhiBox, protect: bool = False
) -> dict[str, str]:
    """Names by person id; ``protect`` gives patients, clients and families initials only."""
    people = await session.scalars(select(Person).where(Person.workspace_id == workspace_id))
    out = {}
    for p in people:
        pii = await identity(session, p, box)
        out[p.id] = list_label(p, pii) if protect else full_name(pii)
    return out


async def detail(
    session: AsyncSession, workspace_id: str, person: Person, box: PhiBox
) -> dict[str, Any]:
    today = utcnow().date()
    own = box.open(person.pii)
    linked = await ehr_identity(session, person, box)
    creds = (await _credentials(session, workspace_id, [person.id])).get(person.id, [])
    rels = list(
        await session.scalars(
            select(Relationship).where(
                Relationship.workspace_id == workspace_id,
                (Relationship.subject_id == person.id) | (Relationship.related_id == person.id),
            )
        )
    )
    names = await labels(session, workspace_id, box)
    notes = list(
        await session.scalars(
            select(Observation)
            .where(Observation.workspace_id == workspace_id, Observation.person_id == person.id)
            .order_by(Observation.created_at.desc())
            .limit(50)
        )
    )
    checks = list(
        await session.scalars(
            select(VerificationEvent)
            .where(
                VerificationEvent.workspace_id == workspace_id,
                (VerificationEvent.person_id == person.id)
                | (VerificationEvent.subject_id == person.id),
            )
            .order_by(VerificationEvent.created_at.desc())
            .limit(25)
        )
    )
    return {
        "id": person.id,
        "kind": person.kind,
        "kind_label": policy.KIND_LABEL[person.kind],
        "status": person.status,
        "synthetic": person.synthetic,
        "name": full_name({**own, **{k: v for k, v in linked.items() if v}}),
        "profile": {k: own.get(k) for k in PII_FIELDS},
        "ehr": (
            {
                "connection_id": person.ehr_connection_id,
                "patient_ref": person.ehr_patient_ref,
                "found": bool(linked),
                "name": full_name(linked) if linked else None,
                "date_of_birth": linked.get("date_of_birth"),
            }
            if person.ehr_patient_ref
            else None
        ),
        "organisation": person.organisation,
        "role_title": person.role_title,
        "language": person.language,
        "preferred_channel": person.preferred_channel,
        "do_not_contact": person.do_not_contact,
        "do_not_contact_reason": person.do_not_contact_reason,
        "timezone": person.timezone,
        "contact_windows": person.contact_windows or [],
        "consent": person.consent or {},
        "skills": person.skills or [],
        "availability": person.availability or [],
        "max_travel_km": person.max_travel_km,
        "reliability": person.reliability or {},
        "credentials": [
            {
                "id": c.id,
                "kind": c.kind,
                "label": policy.CREDENTIALS.get(c.kind, c.kind),
                "identifier": c.identifier,
                "issued_on": c.issued_on,
                "expires_on": c.expires_on,
                "state": policy.credential_state(c.expires_on, today),
            }
            for c in sorted(creds, key=lambda c: c.kind)
        ],
        "relationships": [
            _relationship_view(r, person.id, names, today)
            for r in sorted(rels, key=lambda r: as_utc(r.created_at))
        ],
        "observations": [
            {
                "id": o.id,
                "topic": o.topic,
                "text": box.open(o.text).get("text"),
                "visibility": o.visibility,
                "source": o.source,
                "source_attempt_id": o.source_attempt_id,
                "confirmed": o.confirmed,
                "created_at": o.created_at,
            }
            for o in notes
        ],
        "checks": [
            {
                "id": v.id,
                "check": v.check,
                "outcome": v.outcome,
                "reason": v.reason,
                "identifiers": v.identifiers,
                "topic": v.topic,
                "attempt_id": v.attempt_id,
                "as": "subject" if v.subject_id == person.id else "caller",
                "created_at": v.created_at,
            }
            for v in checks
        ],
    }


def _relationship_view(
    r: Relationship, me: str, names: dict[str, str], today: date
) -> dict[str, Any]:
    other = r.related_id if r.subject_id == me else r.subject_id
    if r.revoked_at:
        state = "revoked"
    elif r.starts_on and today < r.starts_on:
        state = "not_started"
    elif r.expires_on and today > r.expires_on:
        state = "expired"
    else:
        state = "active"
    return {
        "id": r.id,
        "direction": "for" if r.related_id == me else "has",
        "other_id": other,
        "other_name": names.get(other),
        "relation": r.relation,
        "proxy": r.proxy,
        "scope": r.scope or [],
        "starts_on": r.starts_on,
        "expires_on": r.expires_on,
        "evidence": r.evidence,
        "state": state,
    }


# --- writes --------------------------------------------------------------------------------------


def _audit(
    session: AsyncSession, principal: Principal, action: str, target: str, detail: dict
) -> None:
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=target,
            detail={k: v for k, v in detail.items() if k not in PII_FIELDS},
        )
    )


def _check_profile(values: dict[str, Any]) -> None:
    if values.get("kind") is not None and values["kind"] not in policy.KINDS:
        raise ApiError(422, "unknown_kind", "Choose one of the listed kinds of person.")
    if values.get("preferred_channel") not in (None, *policy.CHANNELS):
        raise ApiError(422, "unknown_channel", "Choose voice, text or email.")
    if any(s not in policy.SKILLS for s in values.get("skills") or []):
        raise ApiError(422, "unknown_skill", "Choose skills from the listed ones.")
    for key in ("contact_windows", "availability"):
        if values.get(key) is not None:
            try:
                values[key] = policy.clean_windows(values[key])
            except policy.PolicyError as err:
                raise ApiError(422, "invalid_window", str(err)) from None
    consent = values.get("consent")
    if consent is not None:
        for channel, entry in consent.items():
            if channel not in policy.CHANNELS or (entry or {}).get("state") not in (
                "granted",
                "revoked",
                "unknown",
            ):
                raise ApiError(422, "invalid_consent", "Consent is granted, revoked or unknown.")


async def _link(
    session: AsyncSession, workspace_id: str, connection_id: str, patient_ref: str
) -> None:
    conn = await session.get(PayerConnection, connection_id)
    record = await session.scalar(
        select(EhrRecord).where(
            EhrRecord.workspace_id == workspace_id,
            EhrRecord.connection_id == connection_id,
            EhrRecord.resource == "patient",
            EhrRecord.source_id == patient_ref,
        )
    )
    if conn is None or conn.workspace_id != workspace_id or record is None:
        raise ApiError(404, "ehr_patient_not_found", "That patient isn't in the practice mirror.")
    taken = await session.scalar(
        select(Person).where(
            Person.workspace_id == workspace_id,
            Person.ehr_connection_id == connection_id,
            Person.ehr_patient_ref == patient_ref,
        )
    )
    if taken is not None:
        raise ApiError(409, "already_linked", "That patient already has a profile.")


async def create(
    session: AsyncSession, principal: Principal, box: PhiBox, values: dict[str, Any]
) -> Person:
    _check_profile(values)
    pii = {k: values.get(k) for k in PII_FIELDS}
    linked = values.get("ehr_connection_id") and values.get("ehr_patient_ref")
    if linked:
        if values["kind"] != "patient":
            raise ApiError(422, "link_patients_only", "Only patients link to the practice.")
        await _link(
            session, principal.workspace_id, values["ehr_connection_id"], values["ehr_patient_ref"]
        )
    elif not (pii.get("first_name") and pii.get("last_name")):
        raise ApiError(422, "name_required", "Give a first and last name.")
    elif not SYNTHETIC.search(f"{pii['first_name']} {pii['last_name']}"):
        # Until the PHI gate, hand-entered people must say they are made up (as cases do).
        raise ApiError(
            422,
            "synthetic_only",
            "People hold synthetic data only for now. Use a made-up name that includes "
            '"Synthetic", "Sample", "Example" or "Test".',
        )
    person = Person(
        workspace_id=principal.workspace_id,
        kind=values["kind"],
        pii=box.seal(pii),
        ehr_connection_id=values.get("ehr_connection_id") if linked else None,
        ehr_patient_ref=values.get("ehr_patient_ref") if linked else None,
        created_by=principal.user_id,
        **{k: values[k] for k in EDITABLE if values.get(k) is not None},
    )
    session.add(person)
    await session.flush()
    _audit(session, principal, "people.created", person.id, {"kind": person.kind})
    return person


EDITABLE = (
    "status",
    "organisation",
    "role_title",
    "language",
    "preferred_channel",
    "do_not_contact",
    "do_not_contact_reason",
    "timezone",
    "contact_windows",
    "consent",
    "skills",
    "availability",
    "max_travel_km",
    "reliability",
)


async def update(
    session: AsyncSession,
    principal: Principal,
    box: PhiBox,
    person: Person,
    values: dict[str, Any],
) -> Person:
    _check_profile(values)
    if values.get("status") not in (None, "active", "inactive"):
        raise ApiError(422, "unknown_status", "Status is active or inactive.")
    changed = []
    for key in EDITABLE:
        if key in values and values[key] is not None and getattr(person, key) != values[key]:
            setattr(person, key, values[key])
            changed.append(key)
    profile = values.get("profile")
    if profile is not None:
        current = box.open(person.pii)
        merged = {**current, **{k: v for k, v in profile.items() if k in PII_FIELDS}}
        name = f"{merged.get('first_name') or ''} {merged.get('last_name') or ''}"
        if not person.ehr_patient_ref and not SYNTHETIC.search(name):
            raise ApiError(
                422,
                "synthetic_only",
                'Keep a made-up name that includes "Synthetic", "Sample", "Example" or "Test".',
            )
        if merged != current:
            person.pii = box.seal(merged)
            changed.append("profile")
    if person.do_not_contact and not person.do_not_contact_reason:
        person.do_not_contact_reason = "Set by staff"
    if not person.do_not_contact:
        person.do_not_contact_reason = None
    _audit(session, principal, "people.updated", person.id, {"fields": changed})
    return person


async def add_credential(
    session: AsyncSession, principal: Principal, person: Person, values: dict[str, Any]
) -> Credential:
    if person.kind != "caregiver":
        raise ApiError(422, "caregivers_only", "Credentials are kept for caregivers.")
    if values["kind"] not in policy.CREDENTIALS:
        raise ApiError(422, "unknown_credential", "Choose one of the listed credentials.")
    issued, expires = values.get("issued_on"), values.get("expires_on")
    if issued and expires and expires < issued:
        raise ApiError(422, "invalid_dates", "A credential can't expire before it's issued.")
    row = Credential(
        workspace_id=principal.workspace_id,
        person_id=person.id,
        kind=values["kind"],
        identifier=values.get("identifier"),
        issued_on=values.get("issued_on"),
        expires_on=values.get("expires_on"),
        verified_by=principal.user_id,
    )
    session.add(row)
    await session.flush()
    _audit(session, principal, "people.credential.added", person.id, {"kind": row.kind})
    return row


async def remove_credential(
    session: AsyncSession, principal: Principal, person: Person, credential_id: str
) -> None:
    row = await session.get(Credential, credential_id)
    if row is None or row.person_id != person.id or row.workspace_id != principal.workspace_id:
        raise ApiError(404, "credential_not_found", "No such credential on this person.")
    await session.delete(row)
    _audit(session, principal, "people.credential.removed", person.id, {"kind": row.kind})


async def add_relationship(
    session: AsyncSession, principal: Principal, subject: Person, values: dict[str, Any]
) -> Relationship:
    related = await get(session, principal.workspace_id, values["related_id"])
    if related.id == subject.id:
        raise ApiError(422, "same_person", "A person can't be related to themselves.")
    relation = values["relation"]
    if relation not in policy.RELATIONS:
        raise ApiError(422, "unknown_relation", "Choose one of the listed relationships.")
    fit = relation in policy.FIT_RELATIONS
    if fit and (subject.kind not in ("client", "patient") or related.kind != "caregiver"):
        raise ApiError(422, "fit_needs_caregiver", "Caregiver fit links a client to a caregiver.")
    proxy = bool(values.get("proxy")) and not fit
    scope = list(dict.fromkeys(values.get("scope") or []))
    if proxy:
        if not scope:
            raise ApiError(422, "scope_required", "Say what the representative may act on.")
        if any(s not in (*policy.TOPICS, "all") for s in scope):
            raise ApiError(422, "unknown_scope", "Choose scope from the listed topics.")
        if not values.get("evidence"):
            raise ApiError(
                422, "evidence_required", "Say what authorises them (for example a signed form)."
            )
    starts, expires = values.get("starts_on"), values.get("expires_on")
    if starts and expires and expires < starts:
        raise ApiError(422, "invalid_dates", "An authorisation can't end before it starts.")
    existing = await session.scalar(
        select(Relationship).where(
            Relationship.subject_id == subject.id,
            Relationship.related_id == related.id,
            Relationship.relation == relation,
        )
    )
    row = existing or Relationship(
        workspace_id=principal.workspace_id,
        subject_id=subject.id,
        related_id=related.id,
        relation=relation,
        created_by=principal.user_id,
    )
    row.proxy, row.scope = proxy, scope if proxy else []
    row.starts_on, row.expires_on = starts, expires
    row.evidence = values.get("evidence")
    row.revoked_at = row.revoked_by = None
    if existing is None:
        session.add(row)
    await session.flush()
    _audit(
        session,
        principal,
        "people.relationship.saved",
        subject.id,
        {"relation": relation, "proxy": proxy, "scope": row.scope, "related": related.id},
    )
    return row


async def revoke_relationship(
    session: AsyncSession, principal: Principal, person: Person, relationship_id: str
) -> None:
    row = await session.get(Relationship, relationship_id)
    if (
        row is None
        or row.workspace_id != principal.workspace_id
        or person.id not in (row.subject_id, row.related_id)
    ):
        raise ApiError(404, "relationship_not_found", "No such relationship on this person.")
    row.revoked_at, row.revoked_by = utcnow(), principal.user_id
    _audit(session, principal, "people.relationship.revoked", person.id, {"id": row.id})


async def add_observation(
    session: AsyncSession,
    workspace_id: str,
    box: PhiBox,
    person: Person,
    topic: str,
    text: str,
    *,
    source: str,
    visibility: str = "agent",
    attempt_id: str | None = None,
    created_by: str | None = None,
    confirmed: bool = False,
) -> Observation:
    if topic not in policy.OBSERVATION_TOPICS:
        raise ApiError(422, "unknown_topic", "Choose one of the listed topics.")
    row = Observation(
        workspace_id=workspace_id,
        person_id=person.id,
        topic=topic,
        text=box.seal({"text": text.strip()[:500]}),
        visibility=visibility if visibility in ("agent", "staff") else "agent",
        source=source,
        source_attempt_id=attempt_id,
        created_by=created_by,
        confirmed=confirmed,
    )
    session.add(row)
    await session.flush()
    return row


async def link_ehr_patients(
    session: AsyncSession, principal: Principal, connection_id: str | None = None
) -> int:
    """Give every patient in the practice mirror a profile, linked and holding no copy."""
    conns = list(
        await session.scalars(
            select(PayerConnection.id).where(PayerConnection.workspace_id == principal.workspace_id)
        )
    )
    if connection_id:
        if connection_id not in conns:
            raise ApiError(404, "connection_not_found", "No such practice connection.")
        conns = [connection_id]
    known = {
        (p.ehr_connection_id, p.ehr_patient_ref)
        for p in await session.scalars(
            select(Person).where(
                Person.workspace_id == principal.workspace_id, Person.ehr_patient_ref.is_not(None)
            )
        )
    }
    made = 0
    for record in await session.scalars(
        select(EhrRecord).where(
            EhrRecord.workspace_id == principal.workspace_id,
            EhrRecord.resource == "patient",
            EhrRecord.connection_id.in_(conns),
        )
    ):
        if (
            record.status in ("inactive", "deleted")
            or (
                record.connection_id,
                record.source_id,
            )
            in known
        ):
            continue
        session.add(
            Person(
                workspace_id=principal.workspace_id,
                kind="patient",
                ehr_connection_id=record.connection_id,
                ehr_patient_ref=record.source_id,
                created_by=principal.user_id,
            )
        )
        made += 1
    if made:
        _audit(session, principal, "people.linked_ehr", principal.workspace_id, {"count": made})
    return made
