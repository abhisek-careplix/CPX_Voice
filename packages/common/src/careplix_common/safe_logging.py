"""PHI-safe structured logging (doc 07 SEC08).

Logs carry IDs, timings and error categories. Free text is scrubbed of phone numbers, email
addresses, SSN-shaped values and long digit runs before it is emitted. Scrubbing is a backstop,
not permission to log content: callers must still log identifiers, never transcripts or case data.
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


def scrub(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        for key in ALLOWED_FIELDS:
            if (value := getattr(record, key, None)) is not None:
                line[key] = value
        if record.exc_info:
            # Exception type only: messages and tracebacks can embed request content.
            line["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(line, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
