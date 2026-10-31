"""Evidence records in the doc 08 format.

Every gate test stores: fixture version, runtime build, agent version, model ID, route, timestamp,
actual call/room IDs, expected and observed outcome, references, pass/fail, reviewer and defects.
Records are local files under ``evidence/`` (git-ignored) until the access-controlled evidence
store exists. A screenshot of a configured page is not evidence of a completed call.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


def runtime_build() -> str:
    if build := os.environ.get("CAREPLIX_RUNTIME_BUILD"):
        return build
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return f"git:{sha}"
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


@dataclass
class EvidenceRecord:
    test_id: str
    gate: str
    expected: str
    observed: dict[str, Any]
    result: Literal["pass", "fail", "blocked"]
    environment: str = field(default_factory=lambda: os.environ.get("CAREPLIX_ENV", "dev"))
    runtime_build: str = field(default_factory=runtime_build)
    recorded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    fixture_version: str | None = None
    agent_version: str | None = None
    model_id: str | None = None
    route: str | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    reviewer: str | None = None
    defects: list[str] = field(default_factory=list)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.recorded_at.replace(":", "").replace("-", "")[:15]
        path = directory / f"{self.test_id}-{stamp}.json"
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        return path
