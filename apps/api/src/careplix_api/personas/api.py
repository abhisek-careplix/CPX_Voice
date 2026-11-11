"""Voice personas API: the workspace persona library."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.personas import service as personas
from careplix_api.studio_api import Session

router = APIRouter()


class PersonaIn(BaseModel):
    name: str = Field(max_length=80)
    description: str | None = Field(default=None, max_length=240)
    agent_name: str = Field(max_length=60)
    role: str = Field(default="Assistant", max_length=120)
    voice: str = Field(default="Puck", max_length=30)
    greeting: str = Field(max_length=400)
    warmth: str = "neutral"
    formality: str = "neutral"
    pace: str = "normal"
    empathy: str = "standard"
    languages: list[str] = Field(default_factory=lambda: ["en-US"], max_length=9)
    avoid: list[str] = Field(default_factory=list, max_length=20)
    signature_phrases: list[str] = Field(default_factory=list, max_length=10)
    character: str | None = Field(default=None, max_length=600)


@router.get("/personas")
async def list_personas(
    principal: Principal = Depends(require("view")), session: AsyncSession = Session
):
    return {
        "personas": [personas.view(p) for p in await personas.list_all(session, principal)],
        "voices": list(personas.VOICES),
        "languages": [{"code": c, "label": label} for c, label in personas.LANGUAGES.items()],
    }


@router.post("/personas")
async def create_persona(
    body: PersonaIn,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
):
    persona = await personas.create(session, principal, body.model_dump())
    await session.commit()
    return personas.view(persona)


@router.post("/personas/starters")
async def add_starters(
    principal: Principal = Depends(require("edit_draft")), session: AsyncSession = Session
):
    added = await personas.add_starters(session, principal)
    await session.commit()
    return {"added": added}


@router.put("/personas/{persona_id}")
async def update_persona(
    persona_id: str,
    body: PersonaIn,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
):
    persona = await personas.get(session, principal, persona_id)
    personas.apply(persona, body.model_dump())
    personas.audit(session, principal, "persona.updated", persona)
    await session.commit()
    return personas.view(persona)


@router.delete("/personas/{persona_id}")
async def delete_persona(
    persona_id: str,
    principal: Principal = Depends(require("edit_draft")),
    session: AsyncSession = Session,
):
    persona = await personas.get(session, principal, persona_id)
    personas.audit(session, principal, "persona.deleted", persona)
    await session.delete(persona)
    await session.commit()
    return {"deleted": True}
