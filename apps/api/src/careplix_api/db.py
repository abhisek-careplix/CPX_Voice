"""Database engine and session handling.

Development and careplix-labs use ``create_all`` plus additive nullable columns (SQLite locally,
Postgres in labs). Alembic migrations and Postgres row-level security (ADR13) replace this before
production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every stored timestamp is UTC."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


def _add_missing_columns(conn) -> None:
    """Add new nullable columns to tables that already exist (SQLite and Postgres).

    ``create_all`` creates missing tables but never changes existing ones, so a model that gains
    a column would break every query on a database created earlier (careplix-labs runs Postgres).
    Until Alembic migrations arrive, this adds each new *nullable* column; it never alters, drops
    or adds NOT NULL columns.
    """
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    postgres = conn.dialect.name == "postgresql"
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # create_all just made it, complete
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in present and column.nullable:
                kind = column.type.compile(dialect=conn.dialect)
                guard = "IF NOT EXISTS " if postgres else ""  # two instances starting at once
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN {guard}"{column.name}" {kind}'
                )


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(conn, _record):  # pragma: no cover - driver hook
                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
