"""G0 connection inventory: run every readiness check and store an evidence record.

make probe-readiness
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from careplix_api import readiness
from careplix_api.settings import get_settings
from careplix_common.evidence import EvidenceRecord

EVIDENCE_DIR = Path("evidence/g0")


async def main() -> int:
    result = await readiness.run_all(get_settings())
    for check in result.checks:
        print(f"{check.status.value:<11} {check.name:<16} {check.message}")
    record = EvidenceRecord(
        test_id="G0-READINESS",
        gate="G0",
        expected="All connection checks ready or unverified-with-probe-evidence; no blockers.",
        observed=result.as_dict(),
        result="blocked" if result.overall == readiness.Status.BLOCKED else "pass",
    )
    path = record.write(EVIDENCE_DIR)
    print(f"\noverall: {result.overall.value}\nevidence: {path}")
    return 0 if result.overall != readiness.Status.BLOCKED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
