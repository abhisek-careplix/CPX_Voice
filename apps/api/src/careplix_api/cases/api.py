"""Cases API: the work list, case types, creating cases and the plan for working one."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal, require
from careplix_api.cases import service, types
from careplix_api.settings import Settings, get_settings
from careplix_api.studio_api import Session, viewer

router = APIRouter()


class CaseIn(BaseModel):
    case_type: str
    fields: dict[str, Any] = Field(default_factory=dict, max_length=40)


class SamplesIn(BaseModel):
    case_type: str
    count: int = Field(default=3, ge=1, le=10)


class StatusIn(BaseModel):
    status: Literal["open", "closed"]


@router.get("/case-types")
async def list_case_types(principal: Principal = Depends(viewer)):
    return types.view()


@router.get("/cases")
async def list_cases(
    q: str = "",
    case_type: str | None = None,
    status: str | None = None,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
):
    return await service.list_cases(session, principal, q, case_type or None, status or None)


@router.post("/cases")
async def create_case(
    body: CaseIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.create(session, principal, body.case_type, body.fields)
    await session.commit()
    return await service.detail(session, principal, settings, case)


@router.post("/cases/samples")
async def add_samples(
    body: SamplesIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
):
    made = await service.add_samples(session, principal, body.case_type, body.count)
    await session.commit()
    return [service.row(c) for c in made]


@router.get("/cases/{case_id}")
async def get_case(
    case_id: str,
    principal: Principal = Depends(viewer),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    return await service.detail(session, principal, settings, case)


@router.post("/cases/{case_id}/status")
async def set_status(
    case_id: str,
    body: StatusIn,
    principal: Principal = Depends(require("start_task")),
    session: AsyncSession = Session,
    settings: Settings = Depends(get_settings),
):
    case = await service.get(session, principal, case_id)
    await service.set_status(session, principal, case, body.status)
    await session.commit()
    return await service.detail(session, principal, settings, case)
