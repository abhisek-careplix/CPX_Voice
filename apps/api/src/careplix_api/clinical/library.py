"""The approved instructions library: clinician-approved words, delivered word for word.

- Only ``approved`` instructions reach a patient, and only exactly as written (two placeholders,
  both administrative: ``{first_name}`` and ``{practice_phone}``).
- A clinician approves (``clinical_signoff``); staff may draft. Editing approved words makes a new
  draft version, so nothing changed is sent until a clinician approves it again.
- Text bodies must pass the text channel's minimum-necessary check (no diagnoses, medicines,
  doses or identifiers), so an approved text never bounces at send time.
- Anything a clinician wants to say that isn't in the library goes to a person to say.

A workspace starts with draft starter wording (synthetic, for the clinical lead to edit and
approve); nothing in it is usable until a clinician approves it.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.channels import content
from careplix_api.clinical.models import ApprovedInstruction
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent

TOPICS = ("refill", "discharge", "previsit", "general")
OUTCOMES = ("approved", "denied", "needs_visit")
CHANNELS = ("text", "call", "any")
PLACEHOLDERS = ("first_name", "practice_phone")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
RED_FLAG_KEY = "red_flag_response"

STARTERS: list[dict[str, Any]] = [
    {
        "key": "refill_approved",
        "title": "Refill approved",
        "topic": "refill",
        "outcome": "approved",
        "channel": "any",
        "body": "Your care team reviewed your refill request and approved it. It has been sent "
        "to your pharmacy. Questions? Call {practice_phone}.",
    },
    {
        "key": "refill_needs_visit",
        "title": "Refill: a visit is needed first",
        "topic": "refill",
        "outcome": "needs_visit",
        "channel": "any",
        "body": "Your care team reviewed your refill request and would like to see you before "
        "renewing it. Please call {practice_phone} to book a visit.",
    },
    {
        "key": "refill_denied",
        "title": "Refill not renewed",
        "topic": "refill",
        "outcome": "denied",
        "channel": "any",
        "body": "Your care team reviewed your refill request and can't renew it as requested. "
        "Please call {practice_phone} to talk with them.",
    },
    {
        "key": RED_FLAG_KEY,
        "title": "Red-flag answer (read on the call)",
        "topic": "general",
        "outcome": None,
        "channel": "call",
        "body": "Thank you for telling me. I'm letting your care team know right now, and someone "
        "will call you back today. If this feels like an emergency, please hang up and call 911.",
    },
    {
        "key": "discharge_check_done",
        "title": "Discharge check: answers received",
        "topic": "discharge",
        "outcome": None,
        "channel": "any",
        "body": "Thank you for answering our questions after your hospital stay. Your care team "
        "will review your answers. If anything changes, call {practice_phone}.",
    },
]

# Used only until a clinician approves the red-flag wording: the platform's own emergency rule
# (the clinical-scope guardrail pack), which every patient-facing agent already runs under.
PLATFORM_RED_FLAG = (
    "Thank you for telling me. I'm letting your care team know now. If this feels like an "
    "emergency, please hang up and call 911."
)


def placeholders(body: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(body)


def validate(body: str, channel: str) -> None:
    body = (body or "").strip()
    if len(body) < 10:
        raise ApiError(422, "too_short", "Write the instruction in full.")
    bad = [p for p in placeholders(body) if p not in PLACEHOLDERS]
    if bad:
        raise ApiError(
            422,
            "bad_placeholder",
            f"Only {{first_name}} and {{practice_phone}} can be filled in; remove {{{bad[0]}}}.",
        )
    if channel in ("text", "any"):
        sample = render(body, first_name="Alex", practice_phone="+1 202 555 0100")
        why = content.check(f"Practice: {sample}")
        if why:
            raise ApiError(
                422,
                "not_textable",
                f"A text can't carry this ({why.replace('_', ' ')}): texts sit on lock screens. "
                "Keep clinical detail out, or make it a call-only instruction.",
                {"reason": why},
            )


def render(body: str, **values: str) -> str:
    """The words exactly as approved, with the administrative placeholders filled in."""
    return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1)) or ""), body).strip()


async def ensure_starters(session: AsyncSession, workspace_id: str) -> None:
    have = set(
        await session.scalars(
            select(ApprovedInstruction.key).where(ApprovedInstruction.workspace_id == workspace_id)
        )
    )
    for s in STARTERS:
        if s["key"] not in have:
            session.add(ApprovedInstruction(workspace_id=workspace_id, state="draft", **s))
    await session.flush()


async def listing(session: AsyncSession, workspace_id: str) -> list[ApprovedInstruction]:
    await ensure_starters(session, workspace_id)
    return list(
        await session.scalars(
            select(ApprovedInstruction)
            .where(ApprovedInstruction.workspace_id == workspace_id)
            .order_by(ApprovedInstruction.topic, ApprovedInstruction.title)
        )
    )


async def get(session: AsyncSession, workspace_id: str, ref: str) -> ApprovedInstruction:
    row = await session.get(ApprovedInstruction, ref)
    if row is None:
        row = await session.scalar(
            select(ApprovedInstruction).where(
                ApprovedInstruction.workspace_id == workspace_id, ApprovedInstruction.key == ref
            )
        )
    if row is None or row.workspace_id != workspace_id:
        raise ApiError(404, "not_found", "No such instruction.")
    return row


async def approved(
    session: AsyncSession, workspace_id: str, key: str
) -> ApprovedInstruction | None:
    row = await session.scalar(
        select(ApprovedInstruction).where(
            ApprovedInstruction.workspace_id == workspace_id, ApprovedInstruction.key == key
        )
    )
    return row if row is not None and row.state == "approved" else None


def _audit(session: AsyncSession, p: Principal, action: str, row: ApprovedInstruction) -> None:
    session.add(
        AuditEvent(
            workspace_id=p.workspace_id,
            actor=p.user_id,
            action=action,
            target=row.id,
            detail={"key": row.key, "version": row.version},
        )
    )


def _check_fields(topic: str, outcome: str | None, channel: str) -> None:
    if topic not in TOPICS:
        raise ApiError(422, "bad_topic", f"Topic is one of {', '.join(TOPICS)}.")
    if outcome is not None and outcome not in OUTCOMES:
        raise ApiError(422, "bad_outcome", f"Outcome is one of {', '.join(OUTCOMES)}.")
    if channel not in CHANNELS:
        raise ApiError(422, "bad_channel", f"Channel is one of {', '.join(CHANNELS)}.")


async def create(
    session: AsyncSession, p: Principal, values: dict[str, Any]
) -> ApprovedInstruction:
    key = (values.get("key") or "").strip().lower()
    if not KEY_RE.match(key):
        raise ApiError(422, "bad_key", "Use a short key: lowercase letters, digits, underscores.")
    _check_fields(values["topic"], values.get("outcome"), values["channel"])
    validate(values["body"], values["channel"])
    exists = await session.scalar(
        select(ApprovedInstruction.id).where(
            ApprovedInstruction.workspace_id == p.workspace_id, ApprovedInstruction.key == key
        )
    )
    if exists:
        raise ApiError(409, "duplicate_key", "An instruction with that key exists.")
    row = ApprovedInstruction(
        workspace_id=p.workspace_id,
        key=key,
        title=values["title"].strip()[:120],
        topic=values["topic"],
        outcome=values.get("outcome"),
        channel=values["channel"],
        body=values["body"].strip(),
        state="draft",
        created_by=p.user_id,
    )
    session.add(row)
    await session.flush()
    _audit(session, p, "instruction.drafted", row)
    return row


async def edit(
    session: AsyncSession, p: Principal, row: ApprovedInstruction, values: dict[str, Any]
) -> ApprovedInstruction:
    if row.state == "retired":
        raise ApiError(409, "retired", "This instruction is retired.")
    topic = values.get("topic") or row.topic
    channel = values.get("channel") or row.channel
    outcome = values.get("outcome", row.outcome)
    body = (values.get("body") or row.body).strip()
    _check_fields(topic, outcome, channel)
    validate(body, channel)
    changed = (body, channel, outcome, topic) != (row.body, row.channel, row.outcome, row.topic)
    row.title = (values.get("title") or row.title).strip()[:120]
    if changed:
        if row.state == "approved":
            row.version += 1  # approved words never change silently
        row.body, row.channel, row.outcome, row.topic = body, channel, outcome, topic
        row.state, row.approved_by, row.approved_by_name, row.approved_at = (
            "draft",
            None,
            None,
            None,
        )
    _audit(session, p, "instruction.edited", row)
    return row


async def approve(
    session: AsyncSession, p: Principal, row: ApprovedInstruction
) -> ApprovedInstruction:
    if not p.can("clinical_signoff"):
        raise ApiError(403, "forbidden", "A clinician approves clinical wording.")
    if row.state == "retired":
        raise ApiError(409, "retired", "This instruction is retired.")
    validate(row.body, row.channel)
    row.state, row.approved_by, row.approved_by_name, row.approved_at = (
        "approved",
        p.user_id,
        p.display_name,
        utcnow(),
    )
    _audit(session, p, "instruction.approved", row)
    return row


async def retire(
    session: AsyncSession, p: Principal, row: ApprovedInstruction
) -> ApprovedInstruction:
    if not p.can("clinical_signoff"):
        raise ApiError(403, "forbidden", "A clinician retires clinical wording.")
    row.state = "retired"
    _audit(session, p, "instruction.retired", row)
    return row


def view(row: ApprovedInstruction) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.key,
        "title": row.title,
        "topic": row.topic,
        "outcome": row.outcome,
        "channel": row.channel,
        "body": row.body,
        "version": row.version,
        "state": row.state,
        "approved_by": row.approved_by_name,
        "approved_at": row.approved_at,
        "updated_at": row.updated_at,
    }
