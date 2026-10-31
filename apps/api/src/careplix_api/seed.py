"""Development seed: one workspace, one demo administrator, synthetic cases, starter drafts.

Starter agents are unpublished drafts built from templates; nothing here pretends to be a call,
result or metric (doc 05: no invented records).
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_contracts as contracts
from careplix_api import agents_service
from careplix_api.auth import Principal
from careplix_api.models import Agent, Case, Membership, User, Workspace
from careplix_api.settings import Settings

DEMO_EMAIL = "demo.admin@careplix.local"
FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "synthetic" / "cases"


async def demo_principal(session: AsyncSession) -> Principal | None:
    user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        return None
    membership = await session.scalar(select(Membership).where(Membership.user_id == user.id))
    return Principal(user.id, membership.workspace_id, membership.role, user.display_name)


async def seed(session: AsyncSession, settings: Settings) -> None:
    if await demo_principal(session) is not None:
        return
    workspace = Workspace(name="CarePlix Demo")
    user = User(email=DEMO_EMAIL, display_name="Demo Administrator")
    session.add_all([workspace, user])
    await session.flush()
    session.add(Membership(user_id=user.id, workspace_id=workspace.id, role="admin"))

    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text())
        contracts.validate("synthetic_case", data)
        session.add(Case(workspace_id=workspace.id, case_ref=data["case_id"], data=data))

    principal = Principal(user.id, workspace.id, "admin", user.display_name)
    if not await session.scalar(select(Agent).where(Agent.workspace_id == workspace.id)):
        await agents_service.create_agent(session, principal, settings, "Avery", "auth-status")
        await agents_service.create_agent(
            session, principal, settings, "Avery Inbound", "inbound-enquiry"
        )
    await session.commit()
