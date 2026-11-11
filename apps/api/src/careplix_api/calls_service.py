"""Tasks, call attempts and their lifecycle (doc 03 sequences, ADR05/ADR08/ADR09).

Ordering rule: the attempt is committed before any external side effect, so a crash or timeout
always leaves a record the reconciler can repair. Dialing happens only after a worker has claimed
the attempt (readiness deadline), and only from the API.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_contracts as contracts
from careplix_api import agents_service, connections, events, templates
from careplix_api.auth import Principal, issue_attempt_token
from careplix_api.db import Database, as_utc, new_id, utcnow
from careplix_api.errors import ApiError
from careplix_api.integrations import service as integration_service
from careplix_api.livekit_gateway import LiveKitGateway, room_name_for
from careplix_api.models import (
    Agent,
    AgentVersion,
    CallAttempt,
    Case,
    Route,
    Scenario,
    Task,
    TranscriptSegment,
    Workspace,
)
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

log = logging.getLogger("careplix.calls")

ACTIVE_STATES = ("Created", "Queued", "Dialing", "Connected", "Ending")
TERMINAL_TASK_STATES = ("Completed", "Partial", "Unsuccessful", "Cancelled")
PROVIDER_FALLBACK = "the provider's office"  # no case and no scenario: nobody named
RESTRICTED_ACTIONS = [
    "Do not transfer the call or promise a transfer.",
    "Do not dial or repeat back any phone number spoken on the call.",
    "Do not disclose case details outside the allowed list.",
    "Do not give clinical advice or make financial commitments.",
]


# --- Validation helpers --------------------------------------------------------------------------


def normalize_e164(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ApiError(
        422, "invalid_destination", "Enter a phone number in E.164 format, e.g. +12025550142."
    )


def mask_number(number: str | None) -> str | None:
    return f"•••• {number[-4:]}" if number and len(number) >= 4 else number


async def _workspace_guard(session: AsyncSession, principal: Principal) -> None:
    workspace = await session.get(Workspace, principal.workspace_id)
    if workspace and workspace.emergency_stop:
        raise ApiError(423, "emergency_stop", "Calling is stopped for this workspace.")


async def sandbox_calls_today(session: AsyncSession, workspace_id: str) -> int:
    """Calls on the shared sandbox line in the last 24 hours, inbound and outbound."""
    since = utcnow() - timedelta(hours=24)
    recent = await session.scalars(
        select(CallAttempt).where(
            CallAttempt.workspace_id == workspace_id, CallAttempt.created_at >= since
        )
    )
    return sum(1 for a in recent if a.pins.get("sandbox_line"))


async def _sandbox_guard(session: AsyncSession, workspace_id: str, settings: Settings) -> None:
    """Trial calls on the shared sandbox line: a daily allowance per workspace."""
    if await sandbox_calls_today(session, workspace_id) >= settings.sandbox_daily_calls:
        raise ApiError(
            429,
            "sandbox_limit",
            f"This workspace has used its {settings.sandbox_daily_calls} sandbox calls for today. "
            "Connect your own phone number for more.",
        )


async def _capacity_guard(session: AsyncSession, workspace_id: str, settings: Settings) -> None:
    active = list(
        await session.scalars(
            select(CallAttempt)
            .where(CallAttempt.workspace_id == workspace_id, CallAttempt.state.in_(ACTIVE_STATES))
            .order_by(CallAttempt.created_at)
        )
    )
    # Suites and shadow replays are simulations with their own lane.
    active = [a for a in active if not _own_lane(a.pins)]
    if len(active) >= settings.max_concurrent_calls:
        # Name the call that holds the slot, so the Studio can open or end it: a session that
        # went quiet otherwise blocks every new call until the reconciler's limits expire.
        blocking = active[0]
        raise ApiError(
            409,
            "capacity_exhausted",
            "Another call is in progress. This workspace runs one call at a time.",
            {
                "attempt_id": blocking.id,
                "state": blocking.state,
                "direction": blocking.direction,
                "started_at": as_utc(blocking.created_at).isoformat(),
            },
        )


def _own_lane(pins: dict[str, Any]) -> bool:
    return bool(pins.get("eval_case_id") or (pins.get("shadow_item_id") and pins.get("simulated")))


def _pins(version: AgentVersion, eff: connections.Effective) -> dict[str, Any]:
    return {
        "agent_version_id": version.id,
        "agent_version": version.number,
        "version_state": version.state,
        "config_hash": version.config_hash or agents_service.config_hash(version.config),
        "model_id": version.config["model"]["model_id"] or eff.approved_model,
        "policy_version": templates.POLICY_VERSION,
        "tool_versions": {t["name"]: t["version"] for t in version.config["tools"]},
    }


# --- Browser test --------------------------------------------------------------------------------


async def start_browser_session(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    gateway: LiveKitGateway,
    agent: Agent,
    use: str,
    case_id: str | None,
    *,
    scenario_id: str | None = None,
    simulated: bool = False,
    version: AgentVersion | None = None,
    eval_case_id: str | None = None,
    shadow_item_id: str | None = None,
) -> dict[str, Any]:
    """A sandbox session in a LiveKit room. ``simulated``: the other side is an AI counterpart
    playing the scenario (VS2), dispatched into the same room, instead of a person's browser.
    A regression suite passes the exact ``version`` it evaluates and its case id; suite runs have
    their own concurrency lane, so they neither wait for nor block the workspace's calls. A shadow
    replay passes its row (``shadow_item_id``); the row's facts replace the scenario's."""
    if not gateway.configured:
        raise ApiError(503, "voice_not_configured", "Voice connection is not configured.")
    if version is None and use == "draft":
        version = await agents_service.latest_draft(session, agent)
        if version is None:
            raise ApiError(409, "no_draft", "This agent has no draft. Test the published version.")
    elif version is None:
        version = await agents_service.current_version(session, agent)
        if version is None:
            raise ApiError(409, "not_published", "This agent has no published version yet.")
    eff = await connections.load(session, principal.workspace_id, settings)
    if not (version.config["model"]["model_id"] or eff.approved_model):
        raise ApiError(503, "model_not_configured", "No approved voice model is configured.")

    case = await _resolve_case(session, principal.workspace_id, case_id, required=False)
    scenario = await session.get(Scenario, scenario_id) if scenario_id else None
    await _workspace_guard(session, principal)
    if eval_case_id is None and not (shadow_item_id and simulated):
        await _capacity_guard(session, principal.workspace_id, settings)

    attempt_id = new_id()
    attempt = CallAttempt(
        id=attempt_id,
        workspace_id=principal.workspace_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        case_id=case.id if case else None,
        direction="browser",
        sandbox=True,
        room_name=room_name_for(attempt_id),
        state="Created",
        pins={
            **_pins(version, eff),
            **({"simulated": True} if simulated else {}),
            **({"eval_case_id": eval_case_id} if eval_case_id else {}),
            **({"shadow_item_id": shadow_item_id} if shadow_item_id else {}),
        },
        created_by=principal.user_id,
        scenario_id=scenario_id,
    )
    session.add(attempt)
    await events.append(session, attempt, "attempt.created", "api", {"direction": "browser"})
    await session.commit()

    try:
        dispatch_id = await gateway.create_room_and_dispatch(attempt.room_name, attempt.id)
        if simulated:
            await gateway.dispatch_counterpart(attempt.room_name, attempt.id)
    except Exception as exc:
        attempt.disposition = "voice_unavailable"
        await events.set_state(session, attempt, "Failed", "api", reason=type(exc).__name__)
        await session.commit()
        raise ApiError(
            503, "voice_unavailable", "The voice service could not start the session."
        ) from exc

    attempt.provider_ids = {"dispatch_id": dispatch_id}
    await events.set_state(session, attempt, "Queued", "api")
    await session.commit()
    return {
        "attempt_id": attempt.id,
        "url": settings.livekit_url,
        "token": gateway.browser_token(
            attempt.room_name,
            f"{'lsn' if simulated else 'usr'}_{principal.user_id[:8]}_{attempt.id[:8]}",
            principal.display_name,
            listen_only=simulated,
        ),
        "listen_only": simulated,
        "version": {"number": version.number, "state": version.state},
        "case_ref": case.case_ref if case else None,
        "acting_for": acting_for(case, scenario),
        "persona": version.config["identity"]["name"],
    }


def acting_for(case: Case | None, scenario: Scenario | None) -> str:
    """Who the agent says it represents: the scenario's party, else the case's provider."""
    if scenario is not None:
        return scenario.on_behalf_of
    if case is not None:
        return templates.case_context(case.data)["provider_name"]
    return PROVIDER_FALLBACK


async def _resolve_case(
    session: AsyncSession, workspace_id: str, case_id: str | None, *, required: bool
) -> Case | None:
    if case_id:
        case = await session.get(Case, case_id)
        if case is None or case.workspace_id != workspace_id:
            # Indistinguishable from "no access" (doc 05: no match never exposes other cases).
            raise ApiError(404, "case_not_found", "No accessible case matches that reference.")
        return case
    if required:
        raise ApiError(422, "case_required", "Select a case for this task.")
    # No silent default: a test without a chosen case gets no patient or provider details
    # (it used to take the first synthetic case, whose provider the agent then claimed to call for).
    return None


# --- Outbound tasks ------------------------------------------------------------------------------


async def create_task(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    agent_id: str,
    case_id: str,
    destination: str,
    additional_instructions: str | None,
) -> Task:
    agent = await agents_service.get_agent(session, principal, agent_id)
    version = await agents_service.current_version(session, agent)
    template = templates.get_template(agent.template_id)
    if version is None:
        raise ApiError(422, "agent_not_published", "Publish this agent before creating tasks.")
    if template["direction"] != "outbound":
        raise ApiError(422, "agent_not_outbound", "This agent's template answers inbound calls.")
    case = await _resolve_case(session, principal.workspace_id, case_id, required=True)
    number = normalize_e164(destination)
    eff = await connections.load(session, principal.workspace_id, settings)
    if number not in eff.approved_destinations:
        raise ApiError(
            422,
            "destination_not_approved",
            "Only approved test destinations can be called in this release.",
        )
    note = (additional_instructions or "").strip()
    if len(note) > 500:
        raise ApiError(
            422, "instructions_too_long", "Keep additional instructions under 500 characters."
        )
    task = Task(
        workspace_id=principal.workspace_id,
        kind="outbound",
        created_by=principal.user_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        case_id=case.id,
        destination_e164=number,
        objective=template["objective"],
        additional_instructions=note or None,
        state="Ready",
    )
    # API first: check the payer's Prior Authorization API before anyone dials (outbox).
    integration_service.mark_pending(task, "readiness")
    integration_service.mark_pending(task, "pre_call")
    session.add(task)
    return task


async def start_call(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    gateway: LiveKitGateway,
    runner: CallRunner,
    task: Task,
    idempotency_key: str,
    *,
    extra_pins: dict[str, Any] | None = None,
) -> CallAttempt:
    if not idempotency_key or len(idempotency_key) > 100:
        raise ApiError(400, "idempotency_key_required", "An Idempotency-Key header is required.")
    key = f"{principal.workspace_id}:{idempotency_key}"
    existing = await session.scalar(select(CallAttempt).where(CallAttempt.idempotency_key == key))
    if existing is not None:
        if existing.task_id != task.id:
            raise ApiError(409, "idempotency_conflict", "That key was used for another task.")
        return existing  # FR11: a repeated Start Call returns the same attempt
    if task.state in TERMINAL_TASK_STATES:
        raise ApiError(409, "task_closed", "This task is closed.")
    eff = await connections.load(session, principal.workspace_id, settings)
    if not gateway.configured or not eff.outbound_trunk_id:
        raise ApiError(503, "outbound_not_configured", "Phone connection missing.")
    await _workspace_guard(session, principal)
    await _capacity_guard(session, principal.workspace_id, settings)
    if eff.sandbox_outbound:
        await _sandbox_guard(session, principal.workspace_id, settings)

    version = await session.get(AgentVersion, task.agent_version_id)
    attempt_id = new_id()
    attempt = CallAttempt(
        id=attempt_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        agent_id=task.agent_id,
        agent_version_id=task.agent_version_id,
        case_id=task.case_id,
        scenario_id=task.scenario_id,
        direction="outbound",
        room_name=room_name_for(attempt_id),
        state="Created",
        idempotency_key=key,
        destination_e164=task.destination_e164,
        pins={
            **_pins(version, eff),
            "outbound_trunk_id": eff.outbound_trunk_id,
            **({"sandbox_line": True} if eff.sandbox_outbound else {}),
            **(extra_pins or {}),
        },
        created_by=principal.user_id,
    )
    session.add(attempt)
    task.state = "Running"
    await events.append(session, attempt, "attempt.created", "api", {"direction": "outbound"})
    await session.commit()
    runner.spawn(runner.run_outbound(attempt.id))
    return attempt


async def start_test_call(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    gateway: LiveKitGateway,
    runner: CallRunner,
    agent: Agent,
    destination: str,
) -> CallAttempt:
    """Route qualification: a sandbox outbound call with a published agent to an approved number.

    The admin listens for two-way audio and keypad tones, then confirms; nothing is qualified
    automatically from a connected call alone.
    """
    version = await agents_service.current_version(session, agent)
    if version is None or templates.get_template(agent.template_id)["direction"] != "outbound":
        raise ApiError(422, "agent_not_ready", "Choose a published outbound agent.")
    eff = await connections.load(session, principal.workspace_id, settings)
    number = normalize_e164(destination)
    if number not in eff.approved_destinations:
        raise ApiError(
            422, "destination_not_approved", "Add this number to approved test destinations first."
        )
    if not gateway.configured or not eff.outbound_trunk_id:
        raise ApiError(503, "outbound_not_configured", "Select a carrier trunk first.")
    await _workspace_guard(session, principal)
    await _capacity_guard(session, principal.workspace_id, settings)
    if eff.sandbox_outbound:
        await _sandbox_guard(session, principal.workspace_id, settings)
    case = await _resolve_case(session, principal.workspace_id, None, required=False)
    attempt_id = new_id()
    attempt = CallAttempt(
        id=attempt_id,
        workspace_id=principal.workspace_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        case_id=case.id if case else None,
        direction="outbound",
        sandbox=True,
        room_name=room_name_for(attempt_id),
        state="Created",
        destination_e164=number,
        pins={
            **_pins(version, eff),
            "outbound_trunk_id": eff.outbound_trunk_id,
            "qualification": True,
            **({"sandbox_line": True} if eff.sandbox_outbound else {}),
        },
        created_by=principal.user_id,
    )
    session.add(attempt)
    await events.append(session, attempt, "attempt.created", "api", {"direction": "outbound"})
    await session.commit()
    runner.spawn(runner.run_outbound(attempt.id))
    return attempt


# --- Worker claim (ADR09) ------------------------------------------------------------------------


async def claim(
    session: AsyncSession, settings: Settings, attempt_id: str, room: str, job_id: str, build: str
) -> dict[str, Any]:
    attempt = await session.get(CallAttempt, attempt_id)
    if attempt is None or attempt.room_name != room:
        raise ApiError(404, "attempt_not_found", "No attempt for this room.")
    if attempt.claimed_at is not None:
        raise ApiError(409, "already_claimed", "This attempt was already claimed.")
    if attempt.state not in ("Created", "Queued"):
        raise ApiError(409, "attempt_not_claimable", "This attempt is no longer active.")
    attempt.claimed_at = attempt.last_heartbeat_at = utcnow()
    attempt.pins = {**attempt.pins, "runtime_build": build}
    attempt.provider_ids = {**attempt.provider_ids, "job_id": job_id}
    await events.append(session, attempt, "worker.claimed", "worker", {"runtime_build": build})
    payload = await _claim_payload(session, settings, attempt)
    await session.commit()
    return payload


def phone_digits(number: str | None) -> str:
    """Comparable form of a phone number: digits only, US 10-digit numbers given country code 1."""
    digits = re.sub(r"\D", "", number or "")
    return f"1{digits}" if len(digits) == 10 else digits


async def resolve_inbound_route(
    session: AsyncSession, called_number: str, route_id: str | None, rule_id: str | None
) -> Route:
    """Pick the route from trusted signals only; nothing the caller says or sends counts (I5).

    Signals: the called number reported by LiveKit SIP, and the route ID our own dispatch rule
    attached, cross-checked against the rule LiveKit says it matched. A reported called number
    always decides; the rule signal is used only when LiveKit does not report the number.
    """
    by_rule = None
    if route_id and rule_id:
        candidate = await session.get(Route, route_id)
        if (
            candidate is not None
            and candidate.direction == "inbound"
            and (candidate.provider_refs or {}).get("dispatch_rule_id") == rule_id
        ):
            by_rule = candidate
    by_number = None
    if phone_digits(called_number):
        routes = await session.scalars(select(Route).where(Route.direction == "inbound"))
        by_number = next(
            (r for r in routes if phone_digits(r.number_e164) == phone_digits(called_number)), None
        )
    if phone_digits(called_number):
        # A reported called number decides; a project-wide rule must not answer other numbers.
        if by_rule and by_number and by_rule.id != by_number.id:
            raise ApiError(403, "route_mismatch", "Called number and dispatch rule disagree.")
        route = by_number
    else:
        route = by_rule
    if route is None or route.agent_id is None:
        raise ApiError(404, "no_route", "No inbound route for this number.")
    return route


def is_sandbox_number(settings: Settings, number: str | None) -> bool:
    return bool(settings.sandbox_number) and phone_digits(number) == phone_digits(
        settings.sandbox_number
    )


async def _sandbox_inbound(
    session: AsyncSession, settings: Settings, code: str | None
) -> tuple[Workspace, Agent]:
    """The shared sandbox line: the caller keys in a workspace code (DTMF, via the worker); the
    workspace's chosen inbound agent answers. Nothing else the caller says picks the workspace."""
    digits = re.sub(r"\D", "", code or "")
    if not digits:
        raise ApiError(428, "sandbox_code_required", "Ask the caller for their workspace code.")
    workspace = await session.scalar(select(Workspace).where(Workspace.sandbox_code == digits))
    if workspace is None:
        raise ApiError(404, "sandbox_code_invalid", "No workspace has that code.")
    agent = await session.get(Agent, workspace.sandbox_agent_id or "")
    if agent is None or agent.workspace_id != workspace.id:
        raise ApiError(409, "sandbox_no_agent", "This workspace hasn't chosen a sandbox agent.")
    if templates.get_template(agent.template_id)["direction"] != "inbound":
        raise ApiError(409, "sandbox_no_agent", "The sandbox agent must answer inbound calls.")
    return workspace, agent


async def inbound_claim(
    session: AsyncSession,
    settings: Settings,
    room: str,
    job_id: str,
    build: str,
    called_number: str,
    caller_number: str | None,
    route_id: str | None = None,
    rule_id: str | None = None,
    workspace_code: str | None = None,
) -> dict[str, Any]:
    route: Route | None = None
    if is_sandbox_number(settings, called_number):
        workspace, agent = await _sandbox_inbound(session, settings, workspace_code)
    else:
        route = await resolve_inbound_route(session, called_number, route_id, rule_id)
        agent = await session.get(Agent, route.agent_id)
        workspace = await session.get(Workspace, route.workspace_id)
    version = await agents_service.current_version(session, agent)
    if version is None:
        raise ApiError(409, "not_published", "The routed agent has no published version.")
    if workspace.emergency_stop:
        raise ApiError(423, "emergency_stop", "Calling is stopped for this workspace.")
    await _capacity_guard(session, workspace.id, settings)
    if route is None:
        await _sandbox_guard(session, workspace.id, settings)

    template = templates.get_template(agent.template_id)
    eff = connections.effective(workspace, settings)
    if route is not None:
        route.last_call_at = utcnow()
    task = Task(
        workspace_id=workspace.id,
        kind="inbound",
        created_by="system",
        agent_id=agent.id,
        agent_version_id=version.id,
        objective=template["objective"],
        state="Running",
    )
    session.add(task)
    await session.flush()
    attempt = CallAttempt(
        workspace_id=workspace.id,
        task_id=task.id,
        agent_id=agent.id,
        agent_version_id=version.id,
        route_id=route.id if route else None,
        direction="inbound",
        room_name=room,
        state="Connected",
        caller_e164=caller_number,
        pins={
            **_pins(version, eff),
            "runtime_build": build,
            **({} if route else {"sandbox_line": True}),
        },
        provider_ids={"job_id": job_id},
        claimed_at=utcnow(),
        last_heartbeat_at=utcnow(),
        connected_at=utcnow(),
    )
    session.add(attempt)
    await session.flush()
    await events.append(session, attempt, "attempt.created", "worker", {"direction": "inbound"})
    payload = await _claim_payload(session, settings, attempt)
    await session.commit()
    return payload


async def _claim_payload(
    session: AsyncSession, settings: Settings, attempt: CallAttempt
) -> dict[str, Any]:
    version = await session.get(AgentVersion, attempt.agent_version_id)
    task = await session.get(Task, attempt.task_id) if attempt.task_id else None
    case = await session.get(Case, attempt.case_id) if attempt.case_id else None
    eff = await connections.load(session, attempt.workspace_id, settings)
    config = version.config
    template = templates.get_template(config["task"]["template_id"])
    from careplix_api import scenarios  # local: scenarios imports this module

    scenario = await scenarios.for_attempt(session, attempt)
    plan = None
    if scenario is not None:
        from careplix_api.shadow import service as shadow  # local: shadow imports this module

        box = PhiBox(settings)
        item = await shadow.item_for_attempt(session, attempt)
        facts = shadow.item_facts(item, box) if item else None
        plan = scenarios.brief_inputs(scenario, box, facts=facts)
    brief = build_brief(attempt, task, config, template, case, settings, scenario=plan)
    attempt.brief = PhiBox(settings).seal(brief)  # what this run was told, for the call detail
    registry = contracts.tool_registry()
    return {
        "attempt_id": attempt.id,
        "attempt_token": issue_attempt_token(settings, attempt.id, attempt.workspace_id),
        "brief": brief,
        "instructions": {
            "platform_policy": templates.PLATFORM_POLICY,
            "template": template["instructions"],
            "agent": config["instructions"]["agent_text"],
        },
        "identity": config["identity"],
        "conversation": config["conversation"],
        "model_id": attempt.pins["model_id"],
        "vertex": {"project": eff.gcp_project, "location": eff.vertex_location},
        # The callee speaks first. In a simulation the AI counterpart is the callee.
        "speak_first": attempt.direction != "outbound" and not attempt.pins.get("simulated"),
        # Who the agent talks to: a person (browser/phone) or the simulated counterpart agent.
        "counterpart_kind": "agent" if attempt.pins.get("simulated") else "person",
        "expect_sip": attempt.direction == "outbound",
        "sip_identity": f"pstn_{attempt.id.replace('-', '')}"
        if attempt.direction == "outbound"
        else None,
        "tools": [_model_tool(registry[ref["name"]]) for ref in config["tools"]],
        "max_duration_s": talk_budget(attempt, config, settings),
        "max_hold_s": max_hold_s(config, settings),
    }


def talk_budget(attempt: CallAttempt, config: dict[str, Any], settings: Settings) -> int:
    """The agent's talk time: its own limit, capped by the workspace and the sandbox line."""
    limit = min(config["guardrails"]["max_duration_s"], settings.max_call_duration_s)
    if attempt.pins.get("sandbox_line"):
        limit = min(limit, settings.sandbox_max_call_s)
    return limit


_MODEL_SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items"}
_MODEL_SCHEMA_LIMITS = {"minItems", "maxItems"}


def model_safe_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce a registry JSON Schema to the subset Gemini function declarations accept.

    Gemini takes one type per field (``nullable`` instead of ``["string", "null"]``) and rejects
    some keywords. Constraints dropped here (pattern, format, lengths, additionalProperties) are
    still enforced by the API against the full registry schema before any tool runs (SEC06).
    """
    out: dict[str, Any] = {
        k: schema[k] for k in _MODEL_SCHEMA_KEYS | _MODEL_SCHEMA_LIMITS if k in schema
    }
    if isinstance(out.get("type"), list):
        types_ = [t for t in out["type"] if t != "null"]
        out["type"] = types_[0] if types_ else "string"
        if "null" in schema["type"]:
            out["nullable"] = True
    hints = []
    if schema.get("format") == "date":
        hints.append("Use YYYY-MM-DD.")
    if "pattern" in schema:
        hints.append(f"Must match {schema['pattern']}.")
    if hints:
        out["description"] = " ".join([out.get("description", ""), *hints]).strip()
    if "properties" in out:
        out["properties"] = {k: model_safe_schema(v) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = model_safe_schema(out["items"])
    return out


def _model_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Model-facing tool definition. The worker, not the model, attaches evidence references."""
    schema = dict(tool["arguments_schema"])
    if tool["name"] == "record_finding":
        props = {k: v for k, v in schema["properties"].items() if k != "evidence_segment_ids"}
        schema = {
            **schema,
            "properties": props,
            "required": [r for r in schema["required"] if r != "evidence_segment_ids"],
        }
    return {
        "name": tool["name"],
        "version": tool["version"],
        "description": tool["description"],
        "parameters": model_safe_schema(schema),
        "execution": tool["execution"],
        "timeout_ms": tool["timeout_ms"],
    }


def max_hold_s(config: dict[str, Any], settings: Settings) -> int:
    """The agent's hold allowance (ADR10), capped by the workspace limit."""
    wanted = config["conversation"].get("hold_policy", {}).get("max_hold_s", 0)
    return max(0, min(int(wanted), settings.max_hold_s))


def build_brief(
    attempt: CallAttempt,
    task: Task | None,
    config: dict[str, Any],
    template: dict[str, Any],
    case: Case | None,
    settings: Settings,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = templates.case_context(case.data) if case else {}
    required = template["required_context"]
    objective = task.objective if task else template["objective"]
    if scenario is not None:
        # Voice Studio: the scenario is the job (goal, limits, done-when) and its facts are the
        # only context the agent has and may share.
        objective = scenario["objective"]
    if task and task.additional_instructions:
        objective += f" Additional operator note: {task.additional_instructions}"
    issued = utcnow()
    brief = {
        "attempt_id": attempt.id,
        "task_id": attempt.task_id,
        "workspace_id": attempt.workspace_id,
        "direction": attempt.direction,
        "channel": "browser" if attempt.direction == "browser" else "phone",
        "sandbox": attempt.sandbox,
        "pins": {
            k: attempt.pins[k]
            for k in (
                "agent_version_id",
                "config_hash",
                "model_id",
                "policy_version",
                "tool_versions",
            )
        },
        "authority": {
            "statement": "Act only on behalf of the provider for the purpose of this task.",
            "on_behalf_of": context.get("provider_name", PROVIDER_FALLBACK),
            "disclose_allowlist": config["guardrails"].get("allowed_disclosures", []),
        },
        "objective": objective,
        "desired_findings": list(template["output_fields"]),
        "restricted_actions": RESTRICTED_ACTIONS,
        "budget": {
            "max_duration_s": talk_budget(attempt, config, settings),
            "max_hold_s": max_hold_s(config, settings),
            "max_menu_repeats": config["conversation"]
            .get("ivr_policy", {})
            .get("max_menu_repeats", 3),
        },
        "context": {k: context[k] for k in required if k in context},
        "issued_at": issued.isoformat(),
        "expires_at": (
            issued + timedelta(seconds=settings.max_call_duration_s + settings.max_hold_s + 600)
        ).isoformat(),
    }
    if scenario is not None:
        brief["authority"] = {
            "statement": "Act only for the person or organisation named, for this goal, within the "
            "limits given.",
            "on_behalf_of": scenario["on_behalf_of"],
            "disclose_allowlist": scenario["disclose"],
        }
        brief["context"] = scenario["context"]
    contracts.validate("call_brief", brief)
    return brief


# --- Events and termination ----------------------------------------------------------------------

WORKER_EVENT_TYPES = {
    "worker.ready",
    "worker.heartbeat",
    "worker.done",
    "participant.joined",
    "participant.left",
    "transcript.segment",
    "sip.call_status",
    "amd.result",
    "model.error",
    "audio.missing",
    "model.session_renewed",
    "hold.started",
    "hold.ended",
    "hold.timeout",
    "budget.exceeded",
}


async def finalize(
    session: AsyncSession, attempt: CallAttempt, reason: str, disposition: str | None = None
) -> None:
    if attempt.state in ("Ended", "Failed", "Cancelled"):
        return
    attempt.ended_at = utcnow()
    attempt.end_reason = reason
    if attempt.connected_at is None and attempt.direction != "inbound":
        attempt.disposition = disposition or attempt.disposition or "not_connected"
        await events.set_state(session, attempt, "Failed", "api", reason=reason)
    else:
        attempt.disposition = disposition or attempt.disposition or "completed"
        await events.set_state(session, attempt, "Ended", "api", reason=reason)
    if attempt.task_id:
        task = await session.get(Task, attempt.task_id)
        if task and task.state not in TERMINAL_TASK_STATES:
            # A business outcome needs evidence and review; a finished call is not success (I1).
            task.state = "Review"
            task.next_action = task.next_action or _suggested_next_action(attempt)
            if task.case_id:
                # Validate what was heard against the payer system (outbox).
                integration_service.mark_pending(task, "post_call")


def _suggested_next_action(attempt: CallAttempt) -> str:
    return {
        "busy": "Destination was busy. Review and explicitly retry.",
        "no_answer": "No answer from destination. Review and explicitly retry.",
        "interrupted": "Call was interrupted. Review partial findings and decide whether to retry.",
        "agent_unavailable": "No agent was available. Check readiness, then retry.",
    }.get(attempt.disposition or "", "Review findings and record the outcome.")


async def ingest_worker_events(
    session: AsyncSession, attempt: CallAttempt, items: list[dict[str, Any]]
) -> int:
    accepted = 0
    for item in items:
        type_ = item.get("type")
        if type_ not in WORKER_EVENT_TYPES:
            continue
        payload = dict(item.get("payload") or {})
        if type_ == "transcript.segment":
            segment_id = str(payload.get("segment_id", ""))[:64]
            text = str(payload.pop("text", ""))[:4000]
            if not segment_id or await session.get(TranscriptSegment, segment_id):
                continue
            session.add(
                TranscriptSegment(
                    id=segment_id,
                    attempt_id=attempt.id,
                    speaker="agent" if payload.get("speaker") == "agent" else "counterpart",
                    text=text,
                    playout=payload.get("playout", "unverified"),
                    occurred_at=utcnow(),
                )
            )
        event = await events.append(
            session, attempt, type_, "worker", payload, event_id=item.get("event_id")
        )
        if event is None:
            continue
        accepted += 1
        attempt.last_heartbeat_at = utcnow()
        if type_ == "participant.joined" and attempt.state in ("Queued", "Dialing", "Created"):
            attempt.connected_at = attempt.connected_at or utcnow()
            await events.set_state(session, attempt, "Connected", "worker")
            if attempt.substate is None:
                attempt.substate = "Conversation" if attempt.direction != "outbound" else "IVR"
        elif type_ == "sip.call_status" and payload.get("status") == "hangup":
            if attempt.state == "Connected":
                await events.set_state(
                    session, attempt, "Ending", "worker", reason="counterpart_hangup"
                )
        elif type_ == "amd.result" and payload.get("category") in ("machine", "voicemail"):
            attempt.disposition = "voicemail"
        elif type_ == "worker.done":
            await finalize(session, attempt, payload.get("reason", "worker_done"))
    return accepted


async def end_by_operator(
    session: AsyncSession,
    principal: Principal,
    gateway: LiveKitGateway,
    attempt: CallAttempt,
) -> CallAttempt:
    if attempt.state not in ACTIVE_STATES:
        return attempt  # idempotent
    await events.set_state(
        session, attempt, "Ending", "api", actor=principal.user_id, reason="operator_end"
    )
    await session.commit()
    if gateway.configured:
        await gateway.delete_room(attempt.room_name)
    await finalize(session, attempt, "operator_ended")
    await session.commit()
    return attempt


# --- Background runner and reconciler ------------------------------------------------------------


class CallRunner:
    """In-process executor for dial commands and reconciliation.

    Development stand-in for the outbox -> Cloud Tasks relay (BUILD_PLAN 5.3); handlers are written
    to be safe under repeat delivery so the swap does not change their logic.
    """

    def __init__(self, db: Database, gateway: LiveKitGateway, settings: Settings):
        self.db, self.gateway, self.settings = db, gateway, settings
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def run_outbound(self, attempt_id: str) -> None:
        try:
            await self._run_outbound(attempt_id)
        except Exception:
            log.exception("outbound run failed", extra={"attempt_id": attempt_id})

    async def _run_outbound(self, attempt_id: str) -> None:
        async with self.db.sessions() as session:
            attempt = await session.get(CallAttempt, attempt_id)
            try:
                dispatch_id = await self.gateway.create_room_and_dispatch(
                    attempt.room_name, attempt.id
                )
            except Exception as exc:
                attempt.disposition = "voice_unavailable"
                await finalize(session, attempt, f"dispatch_failed:{type(exc).__name__}")
                await session.commit()
                return
            attempt.provider_ids = {**attempt.provider_ids, "dispatch_id": dispatch_id}
            await events.set_state(session, attempt, "Queued", "api")
            await session.commit()

        # Readiness deadline: never dial into a room without a functioning agent (doc 03 step 3).
        deadline = asyncio.get_running_loop().time() + self.settings.worker_claim_timeout_s
        while True:
            async with self.db.sessions() as session:
                attempt = await session.get(CallAttempt, attempt_id)
                if attempt.state not in ACTIVE_STATES:
                    return  # cancelled meanwhile
                if attempt.claimed_at is not None:
                    break
                if asyncio.get_running_loop().time() > deadline:
                    attempt.disposition = "agent_unavailable"
                    await finalize(session, attempt, "worker_claim_timeout")
                    await session.commit()
                    await self._safe_delete(attempt.room_name)
                    return
            await asyncio.sleep(0.5)

        async with self.db.sessions() as session:
            attempt = await session.get(CallAttempt, attempt_id)
            await events.set_state(session, attempt, "Dialing", "api")
            await session.commit()
            try:
                outcome = await self.gateway.dial(
                    attempt.room_name,
                    attempt.id,
                    attempt.destination_e164,
                    attempt.pins.get("outbound_trunk_id", ""),
                )
            except Exception as exc:
                # Ambiguous: the call may exist. Reconcile instead of dialing again (TR04).
                await events.append(
                    session,
                    attempt,
                    "tool.unknown",
                    "api",
                    {"action": "dial", "error": type(exc).__name__},
                )
                present = await self._participant_present(attempt)
                if present:
                    attempt.connected_at = utcnow()
                    await events.set_state(session, attempt, "Connected", "reconciler")
                else:
                    attempt.disposition = "failed"
                    await finalize(session, attempt, "dial_error")
                    await self._safe_delete(attempt.room_name)
                await session.commit()
                return
            if outcome.connected:
                attempt.connected_at = attempt.connected_at or utcnow()
                attempt.provider_ids = {
                    **attempt.provider_ids,
                    "sip_participant_id": outcome.participant_id,
                    "sip_call_id": outcome.sip_call_id,
                }
                await events.set_state(session, attempt, "Connected", "api")
            else:
                attempt.disposition = outcome.disposition
                await events.append(
                    session,
                    attempt,
                    "sip.call_status",
                    "api",
                    {"status": outcome.disposition, "sip_status_code": outcome.sip_status_code},
                )
                await finalize(session, attempt, f"dial_{outcome.disposition}", outcome.disposition)
                await self._safe_delete(attempt.room_name)
            await session.commit()

    async def _participant_present(self, attempt: CallAttempt) -> bool:
        try:
            return await self.gateway.sip_participant_present(attempt.room_name, attempt.id)
        except Exception:
            return False

    async def _safe_delete(self, room: str) -> None:
        if not self.gateway.configured:
            return
        try:
            await self.gateway.delete_room(room)
        except Exception:
            log.warning("room delete failed", extra={"room": room})

    async def reconcile_once(self) -> int:
        """Repair stale attempts with auditable transitions (doc 03 reconciliation service)."""
        now = utcnow()
        repaired = 0
        async with self.db.sessions() as session:
            stale = list(
                await session.scalars(
                    select(CallAttempt).where(CallAttempt.state.in_(ACTIVE_STATES))
                )
            )
            for attempt in stale:
                age = (now - as_utc(attempt.created_at)).total_seconds()
                beat = (
                    (now - as_utc(attempt.last_heartbeat_at)).total_seconds()
                    if attempt.last_heartbeat_at
                    else None
                )
                reason = None
                if attempt.claimed_at is None and age > self.settings.worker_claim_timeout_s + 15:
                    attempt.disposition, reason = "agent_unavailable", "reconciler_unclaimed"
                elif beat is not None and beat > self.settings.heartbeat_stale_s:
                    attempt.disposition, reason = "interrupted", "reconciler_stale_heartbeat"
                elif attempt.state == "Ending" and beat is not None and beat > 20:
                    reason = "reconciler_ending_timeout"
                elif age > self.settings.max_call_duration_s + self.settings.max_hold_s + 60:
                    attempt.disposition, reason = "budget_exhausted", "reconciler_budget"
                if reason:
                    await events.append(
                        session, attempt, "attempt.reconciled", "reconciler", {"reason": reason}
                    )
                    await finalize(session, attempt, reason)
                    await self._safe_delete(attempt.room_name)
                    repaired += 1
            await session.commit()
        return repaired

    async def reconcile_forever(self, interval_s: float = 10.0) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                log.exception("reconcile pass failed")
            await asyncio.sleep(interval_s)
