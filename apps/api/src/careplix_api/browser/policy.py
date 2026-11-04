"""Risk tiers for browser actions (BROWSER_AGENT_PLAN §6b). The server decides, not the model.

- read: look, scroll, open a page, click a link or tab. Asks unless allowed for the run or site.
- enter_data: type into a field, tick a box, choose an option. Asks with the values shown.
- commit: submit, save, send, upload, delete, confirm. Always asks, every time.
- person_only: pay, sign, attest, or anything touching a password. The agent never does these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

READ, ENTER, COMMIT, PERSON = "read", "enter_data", "commit", "person_only"
TIERS = (READ, ENTER, COMMIT, PERSON)
ALLOWABLE = (READ, ENTER)  # what "approve similar" / "always allow" may cover

_PERSON_WORDS = re.compile(
    r"\b(pay|payment|purchase|checkout|place order|buy|sign|e-?sign|signature|attest|"
    r"certify|i agree|accept terms|password|change password|delete account)\b",
    re.I,
)
_COMMIT_WORDS = re.compile(
    r"\b(submit|save|send|confirm|delete|remove|upload|apply|post|file|transmit|finish|"
    r"complete|approve|authori[sz]e|request|create|update|cancel|void|resubmit|appeal|"
    r"enroll|register|continue to submit|yes)\b",
    re.I,
)
_SEARCH = re.compile(r"\b(search|find|look ?up|filter|go)\b", re.I)


@dataclass(frozen=True)
class Verdict:
    risk: str
    reason: str | None = None  # why this needs approval beyond the tier itself


def _label(el: dict[str, Any] | None) -> str:
    if not el:
        return ""
    return " ".join(str(el.get(k) or "") for k in ("text", "name", "type", "role")).strip()


def classify(action: dict[str, Any], element: dict[str, Any] | None) -> Verdict:
    kind = action.get("kind")
    label = _label(element)
    el = element or {}

    if el.get("is_password"):
        return Verdict(PERSON, "Passwords are typed by people only.")

    if kind in ("scroll", "wait", "hover", "back", "forward"):
        return Verdict(READ)
    if kind == "navigate":
        return Verdict(READ)

    if kind in ("click", "double_click", "triple_click", "right_click"):
        if _PERSON_WORDS.search(label):
            return Verdict(PERSON, "Paying, signing and attesting are done by a person.")
        is_submit = el.get("type") == "submit" or (el.get("tag") == "button" and el.get("in_form"))
        if _COMMIT_WORDS.search(label) and not _SEARCH.search(label):
            return Verdict(COMMIT)
        if is_submit and not _SEARCH.search(label):
            return Verdict(COMMIT, "This button submits a form.")
        if el.get("tag") in ("input", "select", "textarea") or el.get("type") in (
            "checkbox",
            "radio",
        ):
            return Verdict(ENTER)
        return Verdict(READ)

    if kind == "type":
        if action.get("press_enter") and el.get("in_form") and not _SEARCH.search(label):
            return Verdict(COMMIT, "Pressing Enter here submits the form.")
        return Verdict(ENTER)

    if kind == "key":
        keys = str(action.get("text", "")).lower()
        if keys in ("return", "enter", "kp_enter") and el.get("in_form"):
            if _SEARCH.search(label) or el.get("type") == "search":
                return Verdict(READ)
            return Verdict(COMMIT, "Pressing Enter here submits the form.")
        if keys in ("tab", "shift+tab", "escape", "esc", "up", "down", "pageup", "pagedown"):
            return Verdict(READ)
        return Verdict(ENTER)

    if kind == "drag":
        return Verdict(ENTER)
    return Verdict(COMMIT, "Unrecognised action.")


def needs_approval(risk: str, allowed: set[str], forced_reason: str | None = None) -> bool:
    """Commit and person-only always stop; read and enter-data ask unless allowed."""
    if forced_reason:
        return True
    if risk in (COMMIT, PERSON):
        return True
    return risk not in allowed


def describe(action: dict[str, Any], element: dict[str, Any] | None) -> str:
    kind = action.get("kind")
    name = (element or {}).get("text") or (element or {}).get("name") or ""
    target = f" “{name[:60]}”" if name else ""
    if kind in ("click", "double_click", "triple_click", "right_click"):
        verb = {"double_click": "Double-click", "right_click": "Right-click"}.get(kind, "Click")
        return f"{verb}{target or ' the page'}"
    if kind == "type":
        n = len(str(action.get("text", "")))
        enter = " and press Enter" if action.get("press_enter") else ""
        return f"Type {n} character{'s' if n != 1 else ''} into{target or ' the field'}{enter}"
    if kind == "key":
        return f"Press {action.get('text')}"
    if kind == "scroll":
        return f"Scroll {action.get('direction', 'down')}"
    if kind == "navigate":
        return f"Open {action.get('url')}"
    if kind == "back":
        return "Go back"
    if kind == "forward":
        return "Go forward"
    if kind == "wait":
        return "Wait for the page"
    if kind == "hover":
        return f"Point at{target or ' the page'}"
    if kind == "drag":
        return "Drag on the page"
    return str(kind)
