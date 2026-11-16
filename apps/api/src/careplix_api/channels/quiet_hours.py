"""Quiet hours: texts go out only inside the workspace's contact window, in its own time zone.

Outside the window a text is held and sent when the window next opens (TCPA: 8 am to 9 pm at the
recipient's location; the default window, ``CONTACT_HOURS``, is narrower). The workspace's time
zone stands in for the recipient's until person memory knows where each person is.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_window(raw: str) -> tuple[time, time]:
    start, _, end = raw.partition("-")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def zone(name: str | None, fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def _inside(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # a window that crosses midnight


def is_quiet(now: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    return not _inside(now.astimezone(tz).time(), start, end)


def next_open(now: datetime, tz: ZoneInfo, start: time, end: time) -> datetime:
    """The next moment texting is allowed (``now`` itself when the window is open)."""
    if not is_quiet(now, tz, start, end):
        return now
    local = now.astimezone(tz)
    candidate = datetime.combine(local.date(), start, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), start, tzinfo=tz)
    return candidate.astimezone(now.tzinfo)
