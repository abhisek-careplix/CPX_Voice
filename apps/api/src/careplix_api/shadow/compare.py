"""Comparing two vendors' answers and summarising a shadow run into a parity report.

Pure functions, no database: the service feeds them opened values.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

OUTCOMES = ("match", "mismatch", "ours_only", "theirs_only", "neither")
ADJUDICATIONS = ("ours", "theirs", "both", "neither")  # who was right, decided by a person
_FILLER = re.compile(r"[^0-9a-z]+")
_EMPTY = {"", "na", "none", "null", "unknown", "notprovided"}


def normalize(value: Any) -> str:
    """Case, spacing and punctuation don't count: "PA-48213" equals "pa 48213"."""
    if value is None:
        return ""
    text = _FILLER.sub("", str(value).lower())
    return "" if text in _EMPTY else text


def compare(fields: list[str], ours: dict[str, Any], theirs: dict[str, Any]) -> list[dict]:
    out = []
    for field in fields:
        a, b = normalize(ours.get(field)), normalize(theirs.get(field))
        if a and b:
            outcome = "match" if a == b else "mismatch"
        elif a:
            outcome = "ours_only"
        elif b:
            outcome = "theirs_only"
        else:
            outcome = "neither"
        out.append({"field": field, "outcome": outcome, "adjudication": None})
    return out


def resolved(fields: list[str], values: dict[str, Any]) -> bool:
    """An item is resolved for a side when that side captured every compared field."""
    return bool(fields) and all(normalize(values.get(f)) for f in fields)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _median(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def summarise(fields: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    """The parity report. ``items``: dicts with ``state``, ``comparison``, ``incumbent_metrics``
    and ``our_metrics``. Rates are over finished items only."""
    done = [i for i in items if i["state"] == "done" and i.get("comparison") is not None]
    errors = sum(1 for i in items if i["state"] == "done" and i.get("comparison") is None)
    per_field: dict[str, dict[str, int]] = {f: dict.fromkeys(OUTCOMES, 0) for f in fields}
    adjudicated = {"ours": 0, "theirs": 0, "both": 0, "neither": 0, "open": 0}
    for item in done:
        for row in item["comparison"]:
            if row["field"] in per_field:
                per_field[row["field"]][row["outcome"]] += 1
            if row["outcome"] in ("mismatch", "ours_only", "theirs_only"):
                adjudicated[row.get("adjudication") or "open"] += 1

    both = sum(c["match"] + c["mismatch"] for c in per_field.values())
    matches = sum(c["match"] for c in per_field.values())
    ours_resolved = sum(1 for i in done if i["our_metrics"].get("resolved"))
    theirs_known = [i for i in done if i["incumbent_metrics"].get("resolved") is not None]
    theirs_resolved = sum(1 for i in theirs_known if i["incumbent_metrics"]["resolved"])
    return {
        "items": len(items),
        "finished": len(done),
        "errors": errors,
        "open": sum(1 for i in items if i["state"] in ("queued", "running")),
        "agreement": _rate(matches, both),
        "disagreements": sum(c["mismatch"] for c in per_field.values()),
        "resolution": {
            "ours": _rate(ours_resolved, len(done)),
            "theirs": _rate(theirs_resolved, len(theirs_known)),
        },
        "median_duration_s": {
            "ours": _median(
                [i["our_metrics"]["duration_s"] for i in done if i["our_metrics"].get("duration_s")]
            ),
            "theirs": _median(
                [
                    i["incumbent_metrics"]["duration_s"]
                    for i in done
                    if i["incumbent_metrics"].get("duration_s")
                ]
            ),
        },
        "adjudicated": adjudicated,
        "fields": [
            {
                "field": f,
                **counts,
                "agreement": _rate(counts["match"], counts["match"] + counts["mismatch"]),
            }
            for f, counts in per_field.items()
        ],
    }
