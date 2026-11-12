"""Cases: the work an agent is given. See :mod:`careplix_api.cases.types` for the contract.

These helpers read a case row without touching the database, so call and tool code can import them
without import cycles; :mod:`careplix_api.cases.service` holds the queries and the Studio views.
"""

from __future__ import annotations

from typing import Any

from careplix_api.cases import types


def type_id(case: Any) -> str:
    """The case's type. Rows from before case types existed are prior authorizations."""
    tid = getattr(case, "case_type", None) or (case.data or {}).get("case_type")
    return tid if tid in types.CASE_TYPES else "prior_authorization"


def context(case: Any) -> dict[str, Any]:
    return types.flatten(type_id(case), case.data)


def on_behalf_of(case: Any) -> str | None:
    return types.value(case.data, types.get(type_id(case))["on_behalf_of"])


def packs(case: Any | None) -> list[str]:
    """Guardrail packs every call on this case runs under (on top of the agent's own)."""
    return list(types.get(type_id(case))["packs"]) if case is not None else []


def shareable(case: Any) -> list[str]:
    """Fact keys the agent may tell the other side."""
    return [f["key"] for f in types.get(type_id(case))["fields"] if f["share"]]


def objective(case: Any) -> str | None:
    """The type's objective with this case's facts, for general calling agents."""
    return types.objective(type_id(case), case.data)
