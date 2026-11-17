"""Consent to text: the one place that decides whether a workspace may text a number.

Kept behind :func:`can_text` and :func:`record` so person memory (workstream D) can take over the
storage later without touching callers. Rules:

- An opt-out always wins: a STOP, a carrier "unsubscribed" error, or staff recording one.
- Otherwise a text needs an opt-in on file for that workspace (an intake form, a staff entry with
  where the consent is kept, a verbal yes on a recorded call, or a START keyword).
- STOP and START reach every workspace that texts from the same number: carriers apply them per
  sender, so a person who stops one practice on a shared number has stopped the number.
"""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.channels.models import ContactConsent
from careplix_api.settings import Settings

STATES = ("opted_in", "opted_out")
SOURCES = (
    "staff_entry",
    "intake_form",
    "verbal_on_call",
    "keyword_start",
    "keyword_stop",
    "carrier_opt_out",
)


def peer_hash(settings: Settings, number: str) -> str:
    """Keyed hash of an E.164 number: lets us find a person's texts without storing the number."""
    key = settings.data_encryption_key.get_secret_value() or settings.signing_key()
    return hmac.new(f"peer:{key}".encode(), number.encode(), hashlib.sha256).hexdigest()


async def _row(
    session: AsyncSession, workspace_id: str, channel: str, digest: str
) -> ContactConsent | None:
    return await session.scalar(
        select(ContactConsent).where(
            ContactConsent.workspace_id == workspace_id,
            ContactConsent.channel == channel,
            ContactConsent.peer_hash == digest,
        )
    )


async def state(
    session: AsyncSession, settings: Settings, workspace_id: str, number: str, channel: str = "sms"
) -> str:
    row = await _row(session, workspace_id, channel, peer_hash(settings, number))
    return row.state if row else "unknown"


async def can_text(
    session: AsyncSession, settings: Settings, workspace_id: str, number: str, channel: str = "sms"
) -> str | None:
    """``None`` when texting is allowed, else why not: ``opted_out`` or ``no_consent``."""
    current = await state(session, settings, workspace_id, number, channel)
    if current == "opted_out":
        return "opted_out"
    if current != "opted_in":
        return "no_consent"
    return None


async def record(
    session: AsyncSession,
    settings: Settings,
    workspace_id: str,
    number: str,
    new_state: str,
    source: str,
    *,
    actor: str = "system",
    note: str | None = None,
    channel: str = "sms",
) -> ContactConsent:
    if new_state not in STATES or source not in SOURCES:
        raise ValueError("unknown consent state or source")
    digest = peer_hash(settings, number)
    row = await _row(session, workspace_id, channel, digest)
    if row is None:
        row = ContactConsent(
            workspace_id=workspace_id, channel=channel, peer_hash=digest, peer_last4=number[-4:]
        )
        session.add(row)
    row.state, row.source, row.recorded_by, row.note = new_state, source, actor, note
    await session.flush()
    return row
