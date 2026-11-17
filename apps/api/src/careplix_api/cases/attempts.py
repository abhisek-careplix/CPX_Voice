"""Attempts on any channel, and contacting many people at once (operating plan D2).

A :class:`~careplix_api.cases.models.ContactAttempt` is one try to reach one person about a case
on one channel. One case may have many attempts open at once; the **first confirmed yes wins** by
an atomic claim on ``Case.winner_attempt_id`` (the same idea as the backfill opening claim), and
everyone else still waiting is told it's filled.

Channels plug in here. This module implements ``call`` (a Task started through the call runner);
``text``, ``email``, ``fax`` and ``browser`` are declared but report themselves unavailable until
their owners register a real one. **Workstream C (channels) hook:**

1. Implement :class:`Channel` (``send`` starts the attempt, ``notify`` sends a one-way notice such
   as "it's filled") and call :func:`register` at import time, e.g. ``register(TextChannel())``.
   Consent, STOP/HELP and quiet hours belong inside the channel: return
   ``SendResult("failed", detail="No text consent")`` and the playbook falls back to the next
   channel.
2. When the person answers (an inbound text, an email reply), call :func:`record_reply` with the
   attempt id (or look it up by ``message_id``) and ``"yes"``, ``"no"`` or ``"no_answer"``.
3. Delivery receipts update :class:`CaseLink` ``delivery`` through :func:`mark_delivery`.

A staff alert channel (Slack or Teams) registers under the name ``"staff"``; until then staff
notices land in the Studio review inbox (:func:`notify_staff`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.cases import contract
from careplix_api.cases.models import CaseLink, ContactAttempt, PlaybookRun
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent, Case, Task
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings, get_settings

CHANNELS = ("call", "text", "email", "fax", "browser")
ANSWERS = ("yes", "no", "no_answer")
WAITING = ("queued", "sent")
# ``notified``: a one-way notice (nothing to answer), e.g. "please call us about your visit".
SETTLED = (
    "confirmed",
    "declined",
    "no_answer",
    "failed",
    "superseded",
    "too_late",
    "cancelled",
    "notified",
)
FILLED_NOTICE = "Thanks for answering. That time has now been filled, so there's nothing to do."


@dataclass
class SendContext:
    """What a channel needs to reach one person about one case. ``address`` is unsealed here."""

    session: AsyncSession
    settings: Settings
    case: Case
    attempt: ContactAttempt
    address: str | None
    objective: str | None
    services: dict[str, Any] = field(default_factory=dict)  # gateway, runner (call channel)


@dataclass
class SendResult:
    state: str  # sent | failed | unavailable
    message_id: str | None = None
    task_id: str | None = None
    detail: str | None = None


class Channel(Protocol):
    name: str

    async def send(self, ctx: SendContext) -> SendResult: ...

    async def notify(self, ctx: SendContext, text: str) -> SendResult: ...


class UnavailableChannel:
    """A channel another workstream will implement; until then every send reports unavailable."""

    def __init__(self, name: str):
        self.name = name

    async def send(self, ctx: SendContext) -> SendResult:
        return SendResult("unavailable", detail=f"{self.name.title()} isn't available yet.")

    async def notify(self, ctx: SendContext, text: str) -> SendResult:
        return SendResult("unavailable", detail=f"{self.name.title()} isn't available yet.")


class CallChannel:
    """A voice call: a Task for the case's agent, dialled by the call runner (approved numbers)."""

    name = "call"

    async def send(self, ctx: SendContext) -> SendResult:
        from careplix_api import calls_service  # local: calls_service imports cases

        gateway, runner = ctx.services.get("gateway"), ctx.services.get("runner")
        if gateway is None or runner is None:
            return SendResult("unavailable", detail="Calling isn't running in this service.")
        agent_id = ctx.services.get("agent_id")
        if not agent_id:
            return SendResult("failed", detail="No published agent can work this case type.")
        principal = system_principal(ctx.case.workspace_id)
        try:
            task = await calls_service.create_task(
                ctx.session,
                principal,
                ctx.settings,
                agent_id,
                ctx.case.id,
                ctx.address or "",
                ctx.objective,
            )
            await ctx.session.flush()
            attempt = await calls_service.start_call(
                ctx.session,
                principal,
                ctx.settings,
                gateway,
                runner,
                task,
                f"playbook:{ctx.attempt.id}",
            )
        except ApiError as err:
            return SendResult("failed", detail=err.message[:200])
        return SendResult("sent", task_id=task.id, message_id=attempt.id)

    async def notify(self, ctx: SendContext, text: str) -> SendResult:
        # Nobody is rung back to hear "it's filled": a call not yet dialled is cancelled instead.
        task = await ctx.session.get(Task, ctx.attempt.task_id) if ctx.attempt.task_id else None
        if task is not None and task.state in ("Draft", "Ready"):
            task.state, task.outcome_reason = "Cancelled", "Filled by another patient first."
            return SendResult("sent", detail="Call cancelled before dialling")
        return SendResult("unavailable", detail="No notice by call.")


_registry: dict[str, Channel] = {name: UnavailableChannel(name) for name in CHANNELS}
_registry["call"] = CallChannel()
_staff: Channel | None = None


def register(channel: Channel) -> None:
    """Install a channel implementation (``text``, ``email``, ``fax``, ``browser`` or ``staff``)."""
    global _staff
    if channel.name == "staff":
        _staff = channel
        return
    if channel.name not in CHANNELS:
        raise ValueError(f"unknown channel {channel.name}")
    _registry[channel.name] = channel


def unregister(name: str) -> None:
    """Back to the placeholder (tests use this to remove a fake)."""
    global _staff
    if name == "staff":
        _staff = None
    elif name == "call":
        _registry["call"] = CallChannel()
    else:
        _registry[name] = UnavailableChannel(name)


def channel(name: str) -> Channel:
    return _registry[name]


def available(name: str) -> bool:
    return not isinstance(_registry.get(name), UnavailableChannel)


def system_principal(workspace_id: str):
    from careplix_api.auth import Principal

    return Principal(
        user_id="playbook", workspace_id=workspace_id, role="operator", display_name="Playbook"
    )


def mask(address: str | None) -> str | None:
    return f"…{address[-4:]}" if address and len(address) >= 4 else None


def new_attempt(
    session: AsyncSession,
    box: PhiBox,
    case: Case,
    *,
    channel_name: str,
    person_label: str,
    address: str | None,
    person_ref: str | None = None,
    audience: str = "patient",
    round_: int = 1,
) -> ContactAttempt:
    if channel_name not in CHANNELS:
        raise ApiError(422, "bad_channel", f"Channel is one of {', '.join(CHANNELS)}.")
    attempt = ContactAttempt(
        workspace_id=case.workspace_id,
        case_id=case.id,
        round=round_,
        person_ref=person_ref,
        person_label=person_label[:60],
        audience=audience,
        channel=channel_name,
        address=box.seal({"address": address}) if address else None,
        address_hint=mask(address),
        state="queued",
    )
    session.add(attempt)
    return attempt


async def dispatch(
    session: AsyncSession,
    settings: Settings,
    case: Case,
    attempt: ContactAttempt,
    *,
    objective: str | None = None,
    services: dict[str, Any] | None = None,
) -> SendResult:
    """Send one queued attempt on its channel and record what happened."""
    box = PhiBox(settings)
    ctx = SendContext(
        session,
        settings,
        case,
        attempt,
        box.open(attempt.address).get("address"),
        objective,
        services or {},
    )
    result = await channel(attempt.channel).send(ctx)
    attempt.detail = (result.detail or None) and result.detail[:200]
    attempt.task_id = result.task_id or attempt.task_id
    attempt.message_id = result.message_id or attempt.message_id
    attempt.state = "sent" if result.state == "sent" else "failed"
    attempt.updated_at = utcnow()
    return result


async def for_case(session: AsyncSession, case: Case) -> list[ContactAttempt]:
    return list(
        await session.scalars(
            select(ContactAttempt)
            .where(ContactAttempt.case_id == case.id)
            .order_by(ContactAttempt.created_at)
        )
    )


async def claim_winner(session: AsyncSession, case: Case, attempt: ContactAttempt) -> bool:
    """Atomically make ``attempt`` the case's winner. Exactly one caller ever gets True."""
    claimed = await session.execute(
        update(Case)
        .where(Case.id == case.id, Case.winner_attempt_id.is_(None))
        .values(winner_attempt_id=attempt.id)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(case, ["winner_attempt_id"])
    return claimed.rowcount == 1


async def record_reply(
    session: AsyncSession,
    settings: Settings,
    attempt: ContactAttempt,
    answer: str,
    actor: str,
    *,
    message_id: str | None = None,
) -> ContactAttempt:
    """A person's answer on one attempt. A yes claims the case; a yes after that is too late.

    Called by staff in the Studio, by the task-outcome hook for calls, and by channels (an inbound
    text "YES"). Commit is left to the caller.
    """
    if answer not in ANSWERS:
        raise ApiError(422, "bad_answer", "Answer yes, no or no_answer.")
    case = await session.get(Case, attempt.case_id)
    if attempt.state in ("confirmed", "declined", "too_late"):
        raise ApiError(409, "already_answered", f"This attempt is already {attempt.state}.")
    attempt.answered_at, attempt.updated_at = utcnow(), utcnow()
    if message_id:
        attempt.message_id = message_id[:80]
    if answer == "no":
        attempt.state = "declined"
    elif answer == "no_answer":
        attempt.state = "no_answer"
    elif await claim_winner(session, case, attempt):
        attempt.state = "confirmed"
        await _evidence_and_told(session, case, attempt, actor)
        await notify_others(session, settings, case, attempt)
    else:
        # Someone else's yes won. They may already have been told it's filled while this reply
        # was on its way; tell them once.
        await session.refresh(attempt, ["notice"])
        attempt.state = "too_late"
        if not attempt.notice:
            await _notice(session, settings, case, attempt)
    # Wake the case's playbook: its next pass looks at the answer now, not at its next poll.
    await session.execute(
        update(PlaybookRun)
        .where(PlaybookRun.case_id == case.id, PlaybookRun.state == "waiting")
        .values(next_run_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    session.add(
        AuditEvent(
            workspace_id=case.workspace_id,
            actor=actor,
            action=f"case.attempt_{attempt.state}",
            target=case.id,
            detail={"attempt": attempt.id, "channel": attempt.channel},
        )
    )
    return attempt


async def _evidence_and_told(
    session: AsyncSession, case: Case, attempt: ContactAttempt, actor: str
) -> None:
    """The winning exchange is evidence, and it told the person their answer was taken."""
    ref = attempt.task_id or attempt.message_id or attempt.id
    kind = "task" if attempt.task_id else "attempt"
    if not await contract.has_link(session, case, "evidence", kind, ref):
        contract.add_link(
            session,
            case,
            part="evidence",
            kind=kind,
            ref=ref,
            label=f"{attempt.person_label} said yes by {attempt.channel}",
            actor=actor,
        )
    contract.add_link(
        session,
        case,
        part="informed",
        kind="message",
        ref=attempt.message_id or attempt.task_id or attempt.id,
        label=f"{attempt.person_label} told by {attempt.channel}",
        audience=attempt.audience,
        channel=attempt.channel,
        delivery="delivered",
        actor=actor,
    )


async def _notice(
    session: AsyncSession, settings: Settings, case: Case, attempt: ContactAttempt
) -> None:
    box = PhiBox(settings)
    ctx = SendContext(
        session, settings, case, attempt, box.open(attempt.address).get("address"), None
    )
    result = await channel(attempt.channel).notify(ctx, FILLED_NOTICE)
    attempt.notice = {"sent": "sent", "failed": "failed"}.get(result.state, "skipped")
    attempt.notice_ref = result.message_id


async def notify_others(
    session: AsyncSession, settings: Settings, case: Case, winner: ContactAttempt
) -> int:
    """Everyone still waiting is superseded and told it's filled."""
    told = 0
    for other in await for_case(session, case):
        if other.id == winner.id or other.state not in WAITING:
            continue
        other.state, other.updated_at = "superseded", utcnow()
        await _notice(session, settings, case, other)
        told += 1
    return told


async def cancel_waiting(session: AsyncSession, settings: Settings, case: Case) -> None:
    for other in await for_case(session, case):
        if other.state in WAITING:
            other.state = "cancelled"
            await _notice(session, settings, case, other)


async def on_task_outcome(session: AsyncSession, settings: Settings, task: Task) -> None:
    """A reviewed call outcome answers the call's attempt (Completed counts as a yes)."""
    attempt = await session.scalar(select(ContactAttempt).where(ContactAttempt.task_id == task.id))
    if attempt is None or attempt.state not in WAITING:
        return
    answer = {"Completed": "yes", "Unsuccessful": "no_answer", "Partial": "no_answer"}.get(
        task.state
    )
    if answer:
        await record_reply(session, settings, attempt, answer, task.outcome_by or "reviewer")
    elif task.state == "Cancelled":
        attempt.state = "cancelled"


async def notify_staff(
    session: AsyncSession,
    case: Case,
    text: str,
    *,
    kind: str = "notice",
    settings: Settings | None = None,
) -> None:
    """Tell the case's staff owner. Through a registered staff channel when there is one;
    otherwise the review inbox carries it. Either way it counts as staff informed when it's
    about the outcome (``kind="notice"``)."""
    delivery, ref, channel_name = "delivered", None, "studio"
    if _staff is not None:
        dummy = ContactAttempt(
            workspace_id=case.workspace_id,
            case_id=case.id,
            person_label="Staff",
            audience="staff",
            channel="staff",
        )
        result = await _staff.send(
            SendContext(session, settings or get_settings(), case, dummy, None, text)
        )
        delivery = "sent" if result.state == "sent" else "failed"
        ref, channel_name = result.message_id, "staff"
    if kind == "notice":
        contract.add_link(
            session,
            case,
            part="informed",
            kind="notice",
            ref=ref,
            label=text[:200],
            audience="staff",
            channel=channel_name,
            delivery=delivery,
            actor="playbook",
        )


async def mark_delivery(session: AsyncSession, message_id: str, delivery: str) -> int:
    """A channel's delivery receipt for a message linked to a case."""
    if delivery not in contract.DELIVERY:
        raise ApiError(422, "bad_delivery", "Unknown delivery status.")
    changed = 0
    for link in await session.scalars(
        select(CaseLink).where(CaseLink.ref == message_id, CaseLink.part == "informed")
    ):
        link.delivery = delivery
        changed += 1
    return changed


def view(attempt: ContactAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "round": attempt.round,
        "person": attempt.person_label,
        "person_ref": attempt.person_ref,
        "audience": attempt.audience,
        "channel": attempt.channel,
        "address": attempt.address_hint,
        "state": attempt.state,
        "task_id": attempt.task_id,
        "message_id": attempt.message_id,
        "detail": attempt.detail,
        "notice": attempt.notice,
        "answered_at": attempt.answered_at,
        "created_at": attempt.created_at,
    }
