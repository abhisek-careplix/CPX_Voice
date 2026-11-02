"""Schema upgrade on start: a model's new nullable column reaches an existing database.

Regression: careplix-labs (Postgres) kept its old ``call_attempts`` table when the receptionist
added ``front_desk``, and every call failed with "Unexpected error". Set
CAREPLIX_TEST_POSTGRES_URL (postgresql+asyncpg://…) to run the Postgres case too.
"""

import os

import pytest
from sqlalchemy import inspect, select, text

import careplix_api.models  # noqa: F401  registers every table
from careplix_api.db import Database
from careplix_api.models import CallAttempt

PG = os.environ.get("CAREPLIX_TEST_POSTGRES_URL")
URLS = ["sqlite"] + (["postgres"] if PG else [])


async def _columns(db: Database, table: str) -> set[str]:
    async with db.engine.connect() as conn:
        return await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns(table)})


@pytest.mark.parametrize("backend", URLS)
async def test_start_adds_a_new_nullable_column_to_an_existing_table(tmp_path, backend):
    url = f"sqlite+aiosqlite:///{tmp_path}/old.db" if backend == "sqlite" else PG
    db = Database(url)
    try:
        await db.create_all()
        # Simulate a database created before the column existed.
        async with db.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE call_attempts DROP COLUMN front_desk"))
        assert "front_desk" not in await _columns(db, "call_attempts")

        await db.create_all()  # what the API does on start
        await db.create_all()  # and again: safe to repeat
        assert "front_desk" in await _columns(db, "call_attempts")
        async with db.sessions() as session:
            await session.execute(select(CallAttempt).limit(1))  # the query that failed in labs
    finally:
        if backend == "postgres":
            async with db.engine.begin() as conn:
                await conn.run_sync(careplix_api.models.Base.metadata.drop_all)
        await db.dispose()
