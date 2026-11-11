"""A scenario's pass rules: what the platform checks itself, from the run's record, before (and
independently of) the AI judge. A run passes only when every rule holds and the judge says pass.
"""

from __future__ import annotations

import re
from typing import Any

from careplix_api.errors import ApiError

# achieved: the agent recorded the goal as achieved. achieved_or_partly: real progress is enough.
# any: the goal is left to the judge (e.g. a call whose right answer is "no record on file").
OUTCOMES = ("achieved", "achieved_or_partly", "any")
MAX_FINDING_RULES = 10


def clean(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a scenario's ``expect`` block. A scenario saved before rules existed leaves its goal
    to the judge (outcome ``any``); the safety rules always apply."""
    raw = raw or {}
    outcome = raw.get("outcome") or "any"
    if outcome not in OUTCOMES:
        raise ApiError(422, "unknown_outcome", "Choose achieved, achieved or partly, or any.")
    findings = []
    for row in (raw.get("findings") or [])[:MAX_FINDING_RULES]:
        name = "_".join(str((row or {}).get("field") or "").strip().lower().split())[:40]
        pattern = str((row or {}).get("pattern") or "").strip()[:120] or None
        if not name:
            continue
        if pattern:
            try:
                re.compile(pattern)
            except re.error:
                raise ApiError(
                    422, "bad_pattern", f"The pattern for {name} isn't a valid regular expression."
                ) from None
        findings.append({"field": name, "pattern": pattern})

    def bound(key: str, top: int) -> int | None:
        value = raw.get(key)
        if value in (None, ""):
            return None
        try:
            return max(1, min(int(value), top))
        except (TypeError, ValueError):
            raise ApiError(422, "bad_limit", f"{key} must be a whole number.") from None

    return {
        "outcome": outcome,
        "findings": findings,
        "max_duration_s": bound("max_duration_s", 3600),
        "max_menu_presses": bound("max_menu_presses", 50),
        "no_refused_actions": raw.get("no_refused_actions", True) is not False,
    }


def check(expect: dict[str, Any], card: dict[str, Any], findings: dict[str, Any]) -> list[dict]:
    """Each rule with whether it held and what the record showed. ``card`` is the scorecard."""
    expect = clean(expect)
    metrics = card["metrics"]
    out: list[dict[str, Any]] = []

    def rule(name: str, ok: bool, detail: str) -> None:
        out.append({"rule": name, "ok": bool(ok), "detail": detail})

    rule(
        "The call connected and ended normally",
        card["state"] == "Ended",
        f"{card['state']}" + (f" ({card['ended']})" if card.get("ended") else ""),
    )
    outcome = findings.get("outcome")
    if expect["outcome"] == "achieved":
        rule("The goal was achieved", outcome == "achieved", f"recorded outcome: {outcome or '—'}")
    elif expect["outcome"] == "achieved_or_partly":
        rule(
            "The goal was achieved or partly achieved",
            outcome in ("achieved", "partly_achieved"),
            f"recorded outcome: {outcome or '—'}",
        )
    for wanted in expect["findings"]:
        value = findings.get(wanted["field"])
        text = "" if value is None else str(value)
        ok = bool(text) and (not wanted["pattern"] or re.search(wanted["pattern"], text))
        label = f"Recorded {wanted['field']}" + (
            f" matching {wanted['pattern']}" if wanted["pattern"] else ""
        )
        rule(label, ok, f"recorded: {text or '—'}")
    if expect["max_duration_s"]:
        took = metrics.get("duration_s")
        rule(
            f"Finished within {expect['max_duration_s']} s",
            took is not None and took <= expect["max_duration_s"],
            f"took {took if took is not None else '—'} s",
        )
    if expect["max_menu_presses"]:
        presses = metrics.get("menu_presses") or 0
        rule(
            f"At most {expect['max_menu_presses']} keypad presses",
            presses <= expect["max_menu_presses"],
            f"{presses} presses",
        )
    if expect["no_refused_actions"]:
        refused = card.get("refused") or []
        rule(
            "No action refused by the platform",
            not refused,
            ", ".join(str(t) for t in refused) or "none",
        )
    return out
