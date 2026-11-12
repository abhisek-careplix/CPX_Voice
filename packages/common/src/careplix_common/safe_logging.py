"""PHI-safe structured logging (doc 07 SEC08).

Logs carry IDs, timings and error categories. Free text is scrubbed of phone numbers, email
addresses, SSN-shaped values and long digit runs before it is emitted. Scrubbing is a backstop,
not permission to log content: callers must still log identifiers, never transcripts or case data.

Lines use Cloud Logging's field names (``severity``, ``message``, ``time``) so a Cloud Run log
filter such as ``severity>=WARNING`` finds application errors.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (re.compile(r"\+?\d[\d ().-]{7,}\d"), "[number]"),
]

# Extra fields allowed onto the JSON line. Anything else passed via `extra=` is dropped.
ALLOWED_FIELDS = frozenset(
    {
        "request_id",
        "workspace_id",
        "task_id",
        "attempt_id",
        "room",
        "job_id",
        "event_type",
        "check",
        "status",
        "duration_ms",
        "error_code",
        "model_id",
        "agent_version_id",
        "runtime_build",
    }
)


_SEVERITIES = (
    (logging.CRITICAL, "CRITICAL"),
    (logging.ERROR, "ERROR"),
    (logging.WARNING, "WARNING"),
    (logging.INFO, "INFO"),
)
_HTTPX_STATUS = re.compile(r'"HTTP/[\d.]+ (\d{3})')


def severity(levelno: int) -> str:
    """Cloud Logging severity for a stdlib level; LiveKit's TRACE and DEV round down."""
    for floor, name in _SEVERITIES:
        if levelno >= floor:
            return name
    return "DEBUG"


def scrub(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": severity(record.levelno),
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        for key in ALLOWED_FIELDS:
            if (value := getattr(record, key, None)) is not None:
                line[key] = value
        if record.exc_info:
            # Exception type only: messages and tracebacks can embed request content.
            line["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(line, default=str)


class HttpxFailuresOnly(logging.Filter):
    """httpx logs every request at INFO, which buried the app's own lines (one per NexHealth
    call). Keep only responses of 400 and above, raised to WARNING so they are easy to find."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        match = _HTTPX_STATUS.search(record.getMessage())
        if not match or int(match.group(1)) < 400:
            return False
        record.levelno, record.levelname = logging.WARNING, "WARNING"
        record.severity = "WARNING"
        return True


def quiet_httpx() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, HttpxFailuresOnly) for f in logger.filters):
        logger.addFilter(HttpxFailuresOnly())


def tag_severity() -> None:
    """Give every record a ``severity`` attribute. LiveKit's JSON formatter (used by the worker)
    writes ``level`` but copies record attributes onto the line, so this is how its lines get a
    severity Cloud Logging understands."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "careplix_severity", False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.severity = severity(record.levelno)
        return record

    factory.careplix_severity = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    quiet_httpx()
