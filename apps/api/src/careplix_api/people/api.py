"""People API: profiles, credentials, representatives, notes, the verification policy, and the
ranking preview that contact-many (workstream B) uses."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.errors import ApiError
from careplix_api.people import policy, ranking, samples, service
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()
editor = require("start_task")


class Window(BaseModel):
    days: list[int] = Field(max_length=7)
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)


class ConsentEntry(BaseModel):
    state: Literal["granted", "revoked", "unknown"]
    at: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=60)


class Profile(BaseModel):
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    preferred_name: str | None = Field(default=None, max_length=60)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=200)
    zip: str | None = Field(default=None, max_length=10)
    member_id: str | None = Field(default=None, max_length=40)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class PersonFields(BaseModel):
    status: Literal["active", "inactive"] | None = None
    organisation: str | None = Field(default=None, max_length=120)
    role_title: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=12)
    preferred_channel: Literal["voice", "sms", "email"] | None = None
    do_not_contact: bool | None = None
    do_not_contact_reason: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=40)
    contact_windows: list[Window] | None = Field(default=None, max_length=14)
    consent: dict[str, ConsentEntry] | None = None
    skills: list[str] | None = Field(default=None, max_length=20)
    availability: list[Window] | None = Field(default=None, max_length=14)
    max_travel_km: float | None = Field(default=None, ge=0, le=500)
    profile: Profile | None = None


class PersonIn(PersonFields):
    kind: Literal["patient", "client", "family", "caregiver", "staff", "payer_contact"]
    ehr_connection_id: str | None = Field(default=None, max_length=36)
    ehr_patient_ref: str | None = Field(default=None, max_length=40)


class CredentialIn(BaseModel):
    kind: str = Field(max_length=30)
    identifier: str | None = Field(default=None, max_length=60)
    issued_on: date | None = None
    expires_on: date | None = None


class RelationshipIn(BaseModel):
    related_id: str = Field(max_length=36)
    relation: str = Field(max_length=24)
    proxy: bool = False
    scope: list[str] = Field(default_factory=list, max_length=7)
    starts_on: date | None = None
    expires_on: date | None = None
    evidence: str | None = Field(default=None, max_length=200)


class ObservationIn(BaseModel):
    topic: str = Field(max_length=16)
    text: str = Field(min_length=3, max_length=500)
    visibility: Literal["agent", "staff"] = "agent"
    source_attempt_id: str | None = Field(default=None, max_length=36)


class PolicyIn(BaseModel):
    identifiers: list[str] = Field(max_length=6)
    step_up_identifiers: list[str] = Field(max_length=6)
    step_up_topics: list[str] = Field(max_length=6)
    max_attempts: int = Field(ge=1, le=3)
    proxy_subject_identifiers: list[str] = Field(max_length=6)


class LinkIn(BaseModel):
    connection_id: str | None = Field(default=None, max_length=36)


class NeedIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    kinds: list[Literal["caregiver", "patient", "client"]] = Field(
        default_factory=lambda: ["caregiver"], max_length=3
    )
    skills: list[str] = Field(default_factory=list, max_length=10)
    credentials: list[str] = Field(default_factory=list, max_length=10)
    client_id: str | None = Field(default=None, max_length=36)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    max_km: float | None = Field(default=None, ge=0, le=500)
    language: str | None = Field(default=None, max_length=12)
    contact_at: datetime | None = None
    exclude: list[str] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=50, ge=1, le=200)


def _values(body: BaseModel) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    for key in ("contact_windows", "availability"):
        if values.get(key) is not None:
            values[key] = [dict(w) for w in values[key]]
    if values.get("consent") is not None:
        values["consent"] = {k: dict(v) for k, v in values["consent"].items()}
    if isinstance(values.get("profile"), dict) and values["profile"].get("date_of_birth"):
        values["profile"]["date_of_birth"] = values["profile"]["date_of_birth"].isoformat()
    return values


async def _detail(session, principal, person, settings) -> dict[str, Any]:
    return await service.detail(session, principal.workspace_id, person, PhiBox(settings))


@router.get("/people/options")
async def options(principal: Principal = Depends(viewer)):
    """The vocabularies the Studio offers (kinds, topics, credentials, skills, identifiers)."""
    return {
        "kinds": [{"id": k, "label": policy.KIND_LABEL[k]} for k in policy.KINDS],
        "topics": [{"id": t, "label": policy.TOPIC_LABEL[t]} for t in policy.TOPICS],
        "relations": list(policy.RELATIONS),
        "credentials": [{"id": k, "label": v} for k, v in policy.CREDENTIALS.items()],
        "skills": list(policy.SKILLS),
        "identifiers": [{"id": i, "label": policy.IDENTIFIER_LABEL[i]} for i in policy.IDENTIFIERS],
        "observation_topics": list(policy.OBSERVATION_TOPICS),
    }


@router.get("/people")
async def list_people(
    kind: str | None = None,
    q: str = "",
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    return await service.list_people(session, principal, PhiBox(settings), kind or None, q)


@router.post("/people")
async def create_person(
    body: PersonIn,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    values = _values(body)
    values.update(values.pop("profile", None) or {})
    person = await service.create(session, principal, PhiBox(settings), values)
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.post("/people/samples")
async def add_samples(
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    added = await samples.seed_agency(session, principal.workspace_id, PhiBox(settings))
    await session.commit()
    return {"added": added, "agency": samples.AGENCY}


@router.post("/people/link-ehr")
async def link_ehr(
    body: LinkIn,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
):
    made = await service.link_ehr_patients(session, principal, body.connection_id)
    await session.commit()
    return {"linked": made}


@router.get("/people/policy")
async def get_policy(principal: Principal = Depends(viewer), session: AsyncSession = Session):
    return await service.load_policy(session, principal.workspace_id)


@router.put("/people/policy")
async def put_policy(
    body: PolicyIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Session,
):
    rules = await service.save_policy(session, principal, body.model_dump())
    await session.commit()
    return rules


@router.post("/people/rank")
async def rank(
    body: NeedIn,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    """Preview who contact-many would reach for a shift or slot, and who is left out and why."""
    location = (body.lat, body.lon) if body.lat is not None and body.lon is not None else None
    need = ranking.Need(
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        kinds=tuple(body.kinds),
        skills=tuple(body.skills),
        credentials=tuple(body.credentials),
        client_id=body.client_id,
        location=location,
        max_km=body.max_km,
        language=body.language,
        contact_at=body.contact_at,
        exclude=tuple(body.exclude),
        limit=body.limit,
    )
    try:
        result = await ranking.rank(session, principal.workspace_id, settings, need)
    except ValueError as err:
        raise ApiError(422, "invalid_need", str(err)) from None
    except LookupError:
        raise ApiError(404, "person_not_found", "No accessible client matches that.") from None
    names = await service.labels(session, principal.workspace_id, PhiBox(settings), protect=True)
    return ranking.view(result, names)


@router.get("/people/{person_id}")
async def get_person(
    person_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    return await _detail(session, principal, person, settings)


@router.put("/people/{person_id}")
async def update_person(
    person_id: str,
    body: PersonFields,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.update(session, principal, PhiBox(settings), person, _values(body))
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.post("/people/{person_id}/credentials")
async def add_credential(
    person_id: str,
    body: CredentialIn,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.add_credential(session, principal, person, body.model_dump())
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.delete("/people/{person_id}/credentials/{credential_id}")
async def remove_credential(
    person_id: str,
    credential_id: str,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.remove_credential(session, principal, person, credential_id)
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.post("/people/{person_id}/relationships")
async def add_relationship(
    person_id: str,
    body: RelationshipIn,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.add_relationship(session, principal, person, body.model_dump())
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.post("/people/{person_id}/relationships/{relationship_id}/revoke")
async def revoke_relationship(
    person_id: str,
    relationship_id: str,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.revoke_relationship(session, principal, person, relationship_id)
    await session.commit()
    return await _detail(session, principal, person, settings)


@router.post("/people/{person_id}/observations")
async def add_observation(
    person_id: str,
    body: ObservationIn,
    principal: Principal = Depends(editor),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    person = await service.get(session, principal.workspace_id, person_id)
    await service.add_observation(
        session,
        principal.workspace_id,
        PhiBox(settings),
        person,
        body.topic,
        body.text,
        source="staff",
        visibility=body.visibility,
        attempt_id=body.source_attempt_id,
        created_by=principal.user_id,
        confirmed=True,
    )
    await session.commit()
    return await _detail(session, principal, person, settings)
