"""Data retention (G4 PHI gate): delete what has outlived its purpose, once a day.

- Email sign-in codes: a day after they expire (they hold an address and a code hash).
- Call transcripts: ``TRANSCRIPT_RETENTION_DAYS`` after the words were spoken. 0 keeps them; the
  period is a policy decision (docs/solution/G4_PHI_GATE.md), so nothing is deleted by default.
  Call outcomes, findings and the audit log are kept: they are the record of what was done.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete

from careplix_api import tenancy
from careplix_api.db import Database, utcnow
from careplix_api.models import EmailCode, TranscriptSegment
from careplix_api.settings import Settings

log = logging.getLogger("careplix.retention")

EMAIL_CODE_GRACE = timedelta(days=1)


async def purge(db: Database, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """One pass. Returns how many rows of each kind were deleted."""
    now = now or utcnow()
    counts: dict[str, int] = {}
    with tenancy.unscoped():
        async with db.sessions() as session:
            result = await session.execute(
                delete(EmailCode).where(EmailCode.expires_at < now - EMAIL_CODE_GRACE)
            )
            counts["email_codes"] = result.rowcount or 0
            if settings.transcript_retention_days > 0:
                cutoff = now - timedelta(days=settings.transcript_retention_days)
                result = await session.execute(
                    delete(TranscriptSegment).where(TranscriptSegment.occurred_at < cutoff)
                )
                counts["transcript_segments"] = result.rowcount or 0
            await session.commit()
    return counts


async def run_forever(db: Database, settings: Settings) -> None:
    while True:
        try:
            counts = await purge(db, settings)
            if any(counts.values()):
                log.info("retention pass deleted %s", counts, extra={"check": "retention"})
        except Exception:
            log.exception("retention pass failed")
        await asyncio.sleep(settings.retention_interval_s)
