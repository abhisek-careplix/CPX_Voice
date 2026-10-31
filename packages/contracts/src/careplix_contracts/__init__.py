"""CarePlix Voice data contracts.

JSON Schema files under ``schemas/`` are the source of truth (BUILD_PLAN ADR15). Pydantic and
TypeScript types are generated from them starting in G1; until then, validate with this module.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

_SCHEMAS = files(__package__) / "schemas"
_TOOLS = files(__package__) / "tools"

SCHEMA_URN_PREFIX = "urn:careplix:voice:"


def schema_names() -> list[str]:
    return sorted(
        p.name.removesuffix(".json") for p in _SCHEMAS.iterdir() if p.name.endswith(".json")
    )


@cache
def load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMAS / f"{name}.json").read_text())


@cache
def _registry() -> Registry:
    resources = [
        (load_schema(n)["$id"], Resource.from_contents(load_schema(n))) for n in schema_names()
    ]
    return Registry().with_resources(resources)


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        load_schema(name), registry=_registry(), format_checker=FormatChecker()
    )


def errors(name: str, instance: Any) -> list[str]:
    """Return human-readable validation errors (empty when valid)."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]


def validate(name: str, instance: Any) -> None:
    validator(name).validate(instance)


@cache
def tool_registry() -> dict[str, dict[str, Any]]:
    """Tool definitions keyed by name. Each entry is validated against ``tool_definition``."""
    tools: dict[str, dict[str, Any]] = {}
    for path in sorted(_TOOLS.iterdir(), key=lambda p: p.name):
        if path.name.endswith(".json"):
            tool = json.loads(path.read_text())
            validate("tool_definition", tool)
            tools[tool["name"]] = tool
    return tools


def demo_tools() -> list[str]:
    return sorted(name for name, t in tool_registry().items() if t["enabled_in_demo"])
