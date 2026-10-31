"""Agent authoring lifecycle (doc 04): drafts are editable, published versions are immutable,
rollback moves the route pointer for new calls only."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_contracts as contracts
from careplix_api import connections, templates
from careplix_api.auth import Principal
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import Agent, AgentVersion, AuditEvent, Route
from careplix_api.settings import Settings


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"config": config, "policy": templates.POLICY_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _friendly(field: str, message: str) -> dict[str, str]:
    """Turn JSON Schema wording into something an agent author can act on."""
    message = message.strip()
    if field == "model/model_id":
        message = "No approved voice model is set. Configure GEMINI_LIVE_MODEL, then use it here."
    elif "should be non-empty" in message:
        message = "This field is required."
    elif "is too long" in message:
        message = "This text is too long."
    return {"field": field, "message": message}


def validation_issues(
    config: dict[str, Any], settings: Settings, approved_model: str
) -> list[dict[str, str]]:
    """Every reason this configuration cannot be published. Empty means publishable."""
    issues = [_friendly(*e.split(":", 1)) for e in contracts.errors("agent_config", config)]
    if issues:
        return issues

    template_id = config["task"]["template_id"]
    try:
        template = templates.get_template(template_id)
    except KeyError:
        return [{"field": "task/template_id", "message": f"Unknown template {template_id}."}]

    registry = contracts.tool_registry()
    allowed_tools = {t["name"] for t in template["default_config"]["tools"]}
    for ref in config["tools"]:
        tool = registry.get(ref["name"])
        if tool is None:
            issues.append({"field": "tools", "message": f"Unknown tool {ref['name']}."})
        elif tool["version"] != ref["version"]:
            issues.append(
                {
                    "field": "tools",
                    "message": f"{ref['name']} {ref['version']} is not an approved version.",
                }
            )
        elif not tool["enabled_in_demo"]:
            issues.append(
                {"field": "tools", "message": f"{ref['name']} is not available in this release."}
            )
        elif ref["name"] not in allowed_tools:
            issues.append(
                {
                    "field": "tools",
                    "message": f"{ref['name']} is not permitted for the "
                    f"{template['name']} template.",
                }
            )

    extra = set(config["guardrails"].get("allowed_disclosures", [])) - set(
        template["disclose_allowlist"]
    )
    if extra:
        issues.append(
            {
                "field": "guardrails/allowed_disclosures",
                "message": f"Not permitted by the template: {', '.join(sorted(extra))}.",
            }
        )

    approved = approved_model
    if not approved:
        issues.append(
            {
                "field": "model/model_id",
                "message": "No approved voice model is configured (GEMINI_LIVE_MODEL).",
            }
        )
    elif config["model"]["model_id"] != approved:
        issues.append(
            {
                "field": "model/model_id",
                "message": f"Only the approved model {approved} can be published.",
            }
        )

    if config["guardrails"]["max_duration_s"] > settings.max_call_duration_s:
        issues.append(
            {
                "field": "guardrails/max_duration_s",
                "message": f"Exceeds the workspace limit of {settings.max_call_duration_s} s.",
            }
        )
    return issues


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}/{k}" if prefix else k))
        return out
    return {prefix: value}


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    old, new = _flatten(before or {}), _flatten(after)
    changes = [
        {"path": path, "before": old.get(path), "after": new.get(path)}
        for path in sorted(set(old) | set(new))
        if old.get(path) != new.get(path)
    ]
    old_tools = {t["name"] for t in (before or {}).get("tools", [])}
    new_tools = {t["name"] for t in after.get("tools", [])}
    old_disc = set((before or {}).get("guardrails", {}).get("allowed_disclosures", []))
    new_disc = set(after.get("guardrails", {}).get("allowed_disclosures", []))
    return {
        "changes": changes,
        "privilege_changes": {
            "tools_added": sorted(new_tools - old_tools),
            "tools_removed": sorted(old_tools - new_tools),
            "disclosures_added": sorted(new_disc - old_disc),
            "disclosures_removed": sorted(old_disc - new_disc),
        },
    }


async def get_agent(session: AsyncSession, principal: Principal, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Agent not found.")
    return agent


async def versions(session: AsyncSession, agent: Agent) -> list[AgentVersion]:
    result = await session.scalars(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.number.desc())
    )
    return list(result)


async def latest_draft(session: AsyncSession, agent: Agent) -> AgentVersion | None:
    for version in await versions(session, agent):
        if version.state == "draft":
            return version
    return None


async def current_version(session: AsyncSession, agent: Agent) -> AgentVersion | None:
    return (
        await session.get(AgentVersion, agent.current_version_id)
        if agent.current_version_id
        else None
    )


async def _audit(
    session: AsyncSession, principal: Principal, action: str, target: str, detail: dict
) -> None:
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=target,
            detail=detail,
        )
    )


async def create_agent(
    session: AsyncSession, principal: Principal, settings: Settings, name: str, template_id: str
) -> Agent:
    try:
        template = templates.get_template(template_id)
    except KeyError as exc:
        raise ApiError(422, "unknown_template", "Unknown template.") from exc
    config = copy.deepcopy(template["default_config"])
    config["identity"]["name"] = name[:60]
    config["model"]["model_id"] = (
        await connections.load(session, principal.workspace_id, settings)
    ).approved_model
    agent = Agent(workspace_id=principal.workspace_id, name=name, template_id=template_id)
    session.add(agent)
    await session.flush()
    session.add(
        AgentVersion(
            workspace_id=principal.workspace_id,
            agent_id=agent.id,
            number=1,
            state="draft",
            config=config,
            updated_by=principal.user_id,
        )
    )
    await _audit(session, principal, "agent.created", agent.id, {"template": template_id})
    return agent


async def save_draft(
    session: AsyncSession, principal: Principal, agent: Agent, config: dict[str, Any]
) -> AgentVersion:
    if config.get("task", {}).get("template_id") != agent.template_id:
        raise ApiError(422, "template_mismatch", "An agent's template cannot be changed.")
    draft = await latest_draft(session, agent)
    if draft is None:
        number = (
            await session.scalar(
                select(func.max(AgentVersion.number)).where(AgentVersion.agent_id == agent.id)
            )
        ) or 0
        draft = AgentVersion(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            number=number + 1,
            state="draft",
            config=config,
            updated_by=principal.user_id,
        )
        session.add(draft)
    else:
        draft.config = config
        draft.updated_by = principal.user_id
        draft.updated_at = utcnow()
    if name := config.get("identity", {}).get("name"):
        agent.name = name
    return draft


async def publish_preview(
    session: AsyncSession, agent: Agent, settings: Settings
) -> dict[str, Any]:
    draft = await latest_draft(session, agent)
    if draft is None:
        raise ApiError(409, "no_draft", "There is no draft to publish.")
    current = await current_version(session, agent)
    routes = list(await session.scalars(select(Route).where(Route.agent_id == agent.id)))
    return {
        "draft_version": draft.number,
        "current_version": current.number if current else None,
        "issues": validation_issues(
            draft.config,
            settings,
            (await connections.load(session, agent.workspace_id, settings)).approved_model,
        ),
        **diff(current.config if current else None, draft.config),
        "affected_routes": [
            {"id": r.id, "name": r.name, "number": r.number_e164, "direction": r.direction}
            for r in routes
        ],
        "evaluation": {
            "status": "not_run",
            "untested": [
                "Synthetic scenario suite (simulator arrives in G1)",
                "Telephone audio quality",
            ],
        },
    }


async def publish(
    session: AsyncSession, principal: Principal, agent: Agent, settings: Settings, change_note: str
) -> AgentVersion:
    draft = await latest_draft(session, agent)
    if draft is None:
        raise ApiError(409, "no_draft", "There is no draft to publish.")
    approved = (await connections.load(session, agent.workspace_id, settings)).approved_model
    if issues := validation_issues(draft.config, settings, approved):
        raise ApiError(
            422,
            "not_publishable",
            "The draft has issues to fix before publishing.",
            {"issues": issues},
        )
    previous = agent.current_version_id
    draft.state = "published"
    draft.config_hash = config_hash(draft.config)
    draft.policy_version = templates.POLICY_VERSION
    draft.change_note = change_note.strip() or None
    draft.published_by = principal.user_id
    draft.published_at = utcnow()
    draft.evaluation = {"status": "not_run"}
    agent.current_version_id = draft.id
    await _audit(
        session,
        principal,
        "agent.published",
        agent.id,
        {"version": draft.number, "hash": draft.config_hash, "previous_version_id": previous},
    )
    return draft


async def rollback(
    session: AsyncSession, principal: Principal, agent: Agent, version_id: str
) -> AgentVersion:
    version = await session.get(AgentVersion, version_id)
    if version is None or version.agent_id != agent.id or version.state != "published":
        raise ApiError(
            422, "invalid_version", "Only a published version of this agent can be activated."
        )
    previous = agent.current_version_id
    agent.current_version_id = version.id
    await _audit(
        session,
        principal,
        "agent.rolled_back",
        agent.id,
        {"to_version": version.number, "previous_version_id": previous},
    )
    return version
