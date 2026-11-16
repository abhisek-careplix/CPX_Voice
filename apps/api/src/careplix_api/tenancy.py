"""Tenant isolation: every query in a signed-in request is limited to that request's workspace.

Routes already filter by ``principal.workspace_id``; this is the second wall, so a route that
forgets the filter (or fetches a row by id) still cannot read or write another customer's data.

- ``set_tenant`` is called once the caller is known (Studio session, worker attempt token).
- Every ORM SELECT then carries ``workspace_id = <tenant>`` for each tenant-owned table, and
  child tables without their own column are limited through their parent.
- A flush that would write a row tagged with another workspace is refused.
- Background loops, webhooks and sign-in run with no tenant and see every workspace, by design;
  ``unscoped()`` lifts the wall explicitly inside a request (workspace switcher, uniqueness checks).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from careplix_api.errors import ApiError

_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "careplix_tenant", default=None
)
_installed = False


def set_tenant(workspace_id: str | None) -> None:
    _tenant.set(workspace_id)


def current_tenant() -> str | None:
    return _tenant.get()


@contextmanager
def unscoped() -> Iterator[None]:
    """Run the enclosed queries across all workspaces (callers must filter themselves)."""
    token = _tenant.set(None)
    try:
        yield
    finally:
        _tenant.reset(token)


def _mapped_classes() -> list[type]:
    from careplix_api.db import Base

    return [m.class_ for m in Base.registry.mappers]


def _criteria(tenant: str) -> list[Any]:
    from careplix_api.models import (
        CallAttempt,
        Finding,
        FindingRevision,
        ToolExecution,
        TranscriptSegment,
        Workspace,
    )

    options: list[Any] = []
    for cls in _mapped_classes():
        if cls is Workspace:
            options.append(with_loader_criteria(Workspace, Workspace.id == tenant))
        elif "workspace_id" in cls.__table__.c:
            options.append(
                # A plain expression, not a lambda: lambda criteria are cached with the first
                # value they saw, which would pin every later request to that workspace.
                with_loader_criteria(cls, cls.workspace_id == tenant, include_aliases=True)
            )
    attempts = select(CallAttempt.id).where(CallAttempt.workspace_id == tenant)
    findings = select(Finding.id).where(Finding.workspace_id == tenant)
    options += [
        with_loader_criteria(TranscriptSegment, TranscriptSegment.attempt_id.in_(attempts)),
        with_loader_criteria(ToolExecution, ToolExecution.attempt_id.in_(attempts)),
        with_loader_criteria(FindingRevision, FindingRevision.finding_id.in_(findings)),
    ]
    return options


def _scope_select(state: ORMExecuteState) -> None:
    tenant = _tenant.get()
    if tenant is None or not state.is_select or state.execution_options.get("tenant_unscoped"):
        return
    state.statement = state.statement.options(*_criteria(tenant))


def _guard_flush(session: Session, _ctx, _instances) -> None:
    tenant = _tenant.get()
    if tenant is None:
        return
    from careplix_api.models import Workspace

    for obj in list(session.new) + list(session.dirty):
        owner = obj.id if isinstance(obj, Workspace) else getattr(obj, "workspace_id", tenant)
        if isinstance(obj, Workspace) and obj in session.new:
            continue  # creating a workspace is a platform action, checked by its route
        if owner is not None and owner != tenant:
            raise ApiError(403, "forbidden", "That belongs to another workspace.")


def install() -> None:
    """Register the hooks once per process (all sessions share them)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "do_orm_execute", _scope_select)
    event.listen(Session, "before_flush", _guard_flush)
    _installed = True
