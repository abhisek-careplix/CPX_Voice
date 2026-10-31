"""Call event log: the single source the live UI reads (ADR12).

Producers supply ``event_id`` for de-duplication; the server assigns a per-attempt ``seq`` that
clients use as a resume cursor after refresh (FR08, T11). Payloads hold identifiers and states,
not raw content; transcript text lives in ``transcript_segments`` and is joined on authorized reads.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.models import CallAttempt, CallEvent


async def append(
    session: AsyncSession,
    attempt: CallAttempt,
    type_: str,
    source: str,
    payload: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CallEvent | None:
    event_id = event_id or str(uuid.uuid4())
    if await session.get(CallEvent, event_id) is not None:
        return None  # duplicate delivery
    event = CallEvent(
        id=event_id,
        workspace_id=attempt.workspace_id,
        attempt_id=attempt.id,
        seq=attempt.next_event_seq,
        type=type_,
        source=source,
        occurred_at=occurred_at or utcnow(),
        payload=payload or {},
    )
    attempt.next_event_seq += 1
    session.add(event)
    return event


async def set_state(
    session: AsyncSession, attempt: CallAttempt, state: str, source: str, **payload: Any
) -> None:
    if attempt.state == state:
        return
    previous, attempt.state = attempt.state, state
    await append(
        session,
        attempt,
        "attempt.state_changed",
        source,
        {"from": previous, "to": state, **payload},
    )
