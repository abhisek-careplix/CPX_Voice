"""Call events are de-duplicated by event id, also when two requests deliver the same one at once.

Labs, 17 Nov 2026: the worker posted two event batches 8 ms apart carrying the same state event.
Both passed the duplicate check before either committed, and the second insert failed on
``call_events_pkey`` (HTTP 500). The append now locks the call's row before checking, so the
second request waits and then sees the first's row. The concurrent case needs Postgres row locks:
set CAREPLIX_TEST_POSTGRES_URL (postgresql+asyncpg://…) to run it.
"""

import asyncio
import os

import pytest
from sqlalchemy import func, select

import careplix_api.models  # noqa: F401  registers every table
from careplix_api import events
from careplix_api.db import Database
from careplix_api.models import Agent, AgentVersion, CallAttempt, CallEvent, Workspace

PG = os.environ.get("CAREPLIX_TEST_POSTGRES_URL")


async def _attempt(db: Database) -> str:
    async with db.sessions() as s:
        ws = Workspace(name="Events (synthetic)")
        s.add(ws)
        await s.flush()
        agent = Agent(workspace_id=ws.id, name="A", template_id="t")
        s.add(agent)
        await s.flush()
        version = AgentVersion(
            workspace_id=ws.id,
            agent_id=agent.id,
            number=1,
            state="published",
            config={},
            updated_by="test",
        )
        s.add(version)
        await s.flush()
        attempt = CallAttempt(
            workspace_id=ws.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            direction="outbound",
            room_name="events-test-room",
        )
        s.add(attempt)
        await s.commit()
        return attempt.id


async def _deliver(db: Database, attempt_id: str, event_id: str, hold: float = 0.0):
    async with db.sessions() as s:
        attempt = await s.get(CallAttempt, attempt_id)
        made = await events.append(s, attempt, "test.event", "worker", event_id=event_id)
        await asyncio.sleep(hold)  # keep the transaction open, like a slow batch
        await s.commit()
        return made


async def _count(db: Database, event_id: str) -> int:
    async with db.sessions() as s:
        return await s.scalar(
            select(func.count()).select_from(CallEvent).where(CallEvent.id == event_id)
        )


async def test_a_repeated_event_is_skipped(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/events.db")
    try:
        await db.create_all()
        attempt_id = await _attempt(db)
        assert await _deliver(db, attempt_id, "evt-1") is not None
        assert await _deliver(db, attempt_id, "evt-1") is None
        assert await _count(db, "evt-1") == 1
    finally:
        await db.dispose()


@pytest.mark.skipif(not PG, reason="needs Postgres row locks (CAREPLIX_TEST_POSTGRES_URL)")
async def test_two_requests_delivering_the_same_event_at_once():
    db = Database(PG)
    try:
        await db.create_all()
        attempt_id = await _attempt(db)
        first = asyncio.create_task(_deliver(db, attempt_id, "evt-race", hold=0.5))
        await asyncio.sleep(0.1)  # the first has inserted but not committed
        second = await _deliver(db, attempt_id, "evt-race")
        assert await first is not None
        assert second is None  # it waited for the lock, then saw the committed row
        assert await _count(db, "evt-race") == 1
    finally:
        async with db.engine.begin() as conn:
            await conn.run_sync(careplix_api.models.Base.metadata.drop_all)
        await db.dispose()
