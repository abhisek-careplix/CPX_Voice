"""Form pages by secure link. No sign-in: the token is the key, and it opens one form only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.forms import service
from careplix_api.models import Case
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session

router = APIRouter()


class SubmitIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict, max_length=60)
    fields: dict[str, Any] = Field(default_factory=dict, max_length=60)


@router.get("/forms/{token}")
async def open_form(
    token: str, session: AsyncSession = Session, settings: Settings = Depends(get_settings)
):
    link = await service.by_token(session, settings, token)
    case = await session.get(Case, link.case_id)
    if link.opened_at is None:
        link.opened_at = utcnow()
        await session.commit()
    return service.definition(case, link)


@router.post("/forms/{token}")
async def submit_form(
    token: str,
    body: SubmitIn,
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    link = await service.by_token(session, settings, token)
    values = {"answers": body.answers} if link.kind == "questionnaire" else body.fields
    out = await service.submit(session, settings, link, values)
    await session.commit()
    return out
