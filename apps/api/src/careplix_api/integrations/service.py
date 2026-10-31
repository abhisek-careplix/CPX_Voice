"""Integration gateway: resolve the payer connection, call it, audit it, and record evidence.

Electronic checks run as an outbox: task creation and call finalisation mark a phase ``pending``
on ``Task.electronic_check``; ``IntegrationWorker`` picks it up. This survives restarts and keeps
payer latency out of request handlers and the audio loop (ADR05).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import Database, utcnow
from careplix_api.integrations import pas, reference
from careplix_api.integrations.fhir import AuthConfig, FhirClient, FhirError
from careplix_api.models import (
    AuditEvent,
    CallAttempt,
    Case,
    Finding,
    FindingRevision,
    IntegrationCall,
    PayerConnection,
    Task,
)

log = logging.getLogger("careplix.integrations")

# Fields a PAS ClaimResponse can confirm. Everything else stays call-evidenced.
COMPARABLE = {
    "reported_status": "reported_status",
    "authorization_reference": "authorization_reference",
}


def _norm(field: str, value: str | None) -> str:
    if value is None:
        return ""
    if field == "authorization_reference":
        return re.sub(r"[^A-Z0-9]", "", value.upper())
    return value.strip().lower()


async def find_connection(
    session: AsyncSession, workspace_id: str, payer_name: str
) -> PayerConnection | None:
    connections = list(
        await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == workspace_id,
                PayerConnection.enabled.is_(True),
                PayerConnection.kind == "pas",
            )
        )
    )
    exact = next((c for c in connections if payer_name in (c.payer_names or [])), None)
    return exact or next((c for c in connections if "*" in (c.payer_names or [])), None)


def client_for(conn: PayerConnection, transport: httpx.AsyncBaseTransport | None) -> FhirClient:
    return FhirClient(
        conn.base_url, AuthConfig.from_ref(conn.auth_type, conn.auth_ref), transport=transport
    )


async def _audit_call(
    session: AsyncSession,
    conn: PayerConnection,
    operation: str,
    phase: str,
    task_id: str | None,
    **fields: Any,
) -> IntegrationCall:
    call = IntegrationCall(
        workspace_id=conn.workspace_id,
        connection_id=conn.id,
        operation=operation,
        phase=phase,
        task_id=task_id,
        **fields,
    )
    session.add(call)
    await session.flush()
    return call


async def inquire(
    session: AsyncSession,
    conn: PayerConnection,
    case: Case,
    *,
    phase: str,
    task_id: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[IntegrationCall, pas.PasStatus | None]:
    client = client_for(conn, transport)
    tracking = f"{case.case_ref}:{task_id or 'verify'}"
    try:
        response = await client.operation(
            "Claim/$inquire", pas.build_inquiry_bundle(case.data, tracking)
        )
        status = pas.parse_inquiry_response(response.body)
        call = await _audit_call(
            session,
            conn,
            "pas.inquire",
            phase,
            task_id,
            ok=True,
            status="success" if status.found else "not_found",
            http_status=response.http_status,
            duration_ms=response.duration_ms,
            request_hash=response.request_hash,
            # Outcome only: codes and references, never patient demographics (SEC08).
            summary={
                k: v
                for k, v in status.as_dict().items()
                if k
                in {"found", "outcome", "review_action", "reported_status", "final", "synthetic"}
            },
        )
        return call, status
    except FhirError as err:
        call = await _audit_call(
            session,
            conn,
            "pas.inquire",
            phase,
            task_id,
            ok=False,
            status=err.status,
            http_status=err.http_status,
            duration_ms=0,
            error=err.message[:200],
        )
        return call, None
    finally:
        await client.aclose()


async def verify_connection(
    session: AsyncSession, conn: PayerConnection, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """A connection is verified only by a live read plus a test operation."""
    if conn.kind in reference.REFERENCE_SYSTEMS:
        return await _verify_reference(session, conn, transport)
    client = client_for(conn, transport)
    try:
        meta = await client.metadata()
    except FhirError as err:
        conn.last_error, conn.verified_at = err.message[:120], None
        await _audit_call(
            session,
            conn,
            "metadata",
            "verify",
            None,
            ok=False,
            status=err.status,
            http_status=err.http_status,
            duration_ms=0,
            error=err.message[:200],
        )
        return {"ok": False, "error": err.message}
    finally:
        await client.aclose()
    operations = sorted(
        op.get("name")
        for rest in meta.body.get("rest", [])
        for resource in rest.get("resource", [])
        if resource.get("type") == "Claim"
        for op in resource.get("operation", [])
    )
    await _audit_call(
        session,
        conn,
        "metadata",
        "verify",
        None,
        ok=True,
        status="success",
        http_status=meta.http_status,
        duration_ms=meta.duration_ms,
        summary={"operations": operations},
    )
    conn.capability = {
        "fhir_version": meta.body.get("fhirVersion"),
        "software": (meta.body.get("software") or {}).get("name"),
        "operations": operations,
        "synthetic": any(
            t.get("code") == "synthetic" for t in meta.body.get("meta", {}).get("tag", [])
        ),
    }
    if "inquire" not in operations:
        conn.last_error, conn.verified_at = "Endpoint does not advertise Claim/$inquire.", None
        return {"ok": False, "error": conn.last_error, "capability": conn.capability}

    case = await session.scalar(
        select(Case).where(Case.workspace_id == conn.workspace_id).order_by(Case.case_ref)
    )
    call, status = await inquire(
        session, conn, case, phase="verify", task_id=None, transport=transport
    )
    if status is None:
        conn.last_error, conn.verified_at = call.error, None
        return {"ok": False, "error": call.error, "capability": conn.capability}
    conn.last_error, conn.verified_at = None, utcnow()
    return {
        "ok": True,
        "capability": conn.capability,
        "test_inquiry": status.as_dict(),
        "duration_ms": call.duration_ms,
    }


async def _verify_reference(
    session: AsyncSession, conn: PayerConnection, transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    client = reference.ReferenceClient(transport)
    try:
        result = await client.ping(conn.kind)
    except Exception as exc:  # noqa: BLE001 - categorised
        conn.last_error, conn.verified_at = f"{type(exc).__name__}", None
        await _audit_call(
            session,
            conn,
            "reference.ping",
            "verify",
            None,
            ok=False,
            status="error",
            duration_ms=0,
            error=type(exc).__name__,
        )
        return {"ok": False, "error": conn.last_error}
    finally:
        await client.aclose()
    ok = result.status in {"confirmed", "skipped"}
    await _audit_call(
        session,
        conn,
        "reference.ping",
        "verify",
        None,
        ok=ok,
        status="success" if ok else "error",
        duration_ms=result.duration_ms,
        summary={"status": result.status},
    )
    conn.capability = {"operations": [result.capability], "software": result.source}
    conn.last_error, conn.verified_at = (None, utcnow()) if ok else (result.status, None)
    return {
        "ok": ok,
        "capability": conn.capability,
        "duration_ms": result.duration_ms,
        "test": result.as_dict(),
    }


async def run_readiness(
    session: AsyncSession, task: Task, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Validate the case against public systems of record before any call (D1)."""
    case = await session.get(Case, task.case_id) if task.case_id else None
    if case is None:
        return {"state": "skipped", "reason": "no_case", "at": utcnow().isoformat()}
    connections = {
        c.kind: c
        for c in await session.scalars(
            select(PayerConnection).where(
                PayerConnection.workspace_id == task.workspace_id, PayerConnection.enabled.is_(True)
            )
        )
    }
    data = case.data
    plan: list[tuple[str, Any]] = [("npi", ("npi", data["provider"]["npi"]))]
    plan += [("icd10", ("icd10", code)) for code in data.get("diagnosis_codes", [])]
    plan += [("hcpcs", ("hcpcs", data["service"]["cpt_code"]))]
    plan += [("cms_coverage", ("coverage", data.get("coverage_terms", [])))]
    client = reference.ReferenceClient(transport)
    checks: list[dict[str, Any]] = []
    try:
        for kind, (method, arg) in plan:
            conn = connections.get(kind)
            if conn is None:
                checks.append(
                    reference.CheckResult(
                        kind, "skipped", str(arg), {"reason": "connection disabled"}
                    ).as_dict()
                )
                continue
            try:
                result = await getattr(client, method)(arg)
                status = "success" if result.status in {"confirmed", "skipped"} else result.status
                call = await _audit_call(
                    session,
                    conn,
                    result.capability,
                    "readiness",
                    task.id,
                    ok=result.status != "error",
                    status=status,
                    duration_ms=result.duration_ms,
                    summary={"status": result.status},
                )
            except Exception as exc:  # noqa: BLE001 - one failing system never blocks the others
                result = reference.CheckResult(
                    kind, "error", str(arg), {"reason": type(exc).__name__}, source=kind
                )
                call = await _audit_call(
                    session,
                    conn,
                    kind,
                    "readiness",
                    task.id,
                    ok=False,
                    status="error",
                    duration_ms=0,
                    error=type(exc).__name__,
                )
            checks.append({**result.as_dict(), "integration_call_id": call.id, "system": conn.name})
    finally:
        await client.aclose()
    issues = [c for c in checks if c["status"] in {"invalid", "error"}]
    if issues and not task.next_action:
        task.next_action = "Case readiness found issues: " + "; ".join(
            f"{c['subject']} ({c['detail'].get('reason') or c['status']})" for c in issues
        )
    return {
        "state": "done",
        "at": utcnow().isoformat(),
        "checks": checks,
        "summary": {
            s: sum(1 for c in checks if c["status"] == s)
            for s in ("confirmed", "invalid", "unverifiable", "skipped", "error")
        },
    }


def mark_pending(task: Task, phase: str) -> None:
    check = dict(task.electronic_check or {})
    check[phase] = {"state": "pending", "queued_at": utcnow().isoformat()}
    task.electronic_check = check  # reassign: JSON columns don't track in-place mutation


async def run_phase(
    session: AsyncSession, task: Task, phase: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    if phase == "readiness":
        result = await run_readiness(session, task, transport)
        check = dict(task.electronic_check or {})
        check[phase] = result
        task.electronic_check = check
        return result
    case = await session.get(Case, task.case_id) if task.case_id else None
    result: dict[str, Any] = {"state": "skipped", "at": utcnow().isoformat()}
    if case is None:
        result["reason"] = "no_case"
    elif (
        conn := await find_connection(session, task.workspace_id, case.data["payer"]["name"])
    ) is None:
        result["reason"] = "no_payer_connection"
    else:
        call, status = await inquire(
            session, conn, case, phase=phase, task_id=task.id, transport=transport
        )
        result.update(
            state="done" if status else "failed",
            connection={"id": conn.id, "name": conn.name, "synthetic": conn.synthetic},
            integration_call_id=call.id,
            duration_ms=call.duration_ms,
            error=call.error,
            status=status.as_dict() if status else None,
        )
        if status and phase == "post_call":
            result["comparison"] = await _compare_with_call(session, task, status, call)
    check = dict(task.electronic_check or {})
    check[phase] = result
    task.electronic_check = check
    return result


async def _compare_with_call(
    session: AsyncSession, task: Task, status: pas.PasStatus, call: IntegrationCall
) -> dict[str, str]:
    """Validate call findings against the payer record and record system evidence."""
    attempt = await session.scalar(
        select(CallAttempt)
        .where(CallAttempt.task_id == task.id)
        .order_by(CallAttempt.created_at.desc())
        .limit(1)
    )
    call_findings = {
        f.field: f
        for f in await session.scalars(
            select(Finding)
            .where(
                Finding.task_id == task.id,
                or_(Finding.source.is_(None), Finding.source != "system"),
            )
            .order_by(Finding.created_at)
        )
    }
    system_values = {
        "reported_status": status.reported_status if status.found else "not_found",
        "authorization_reference": status.authorization_reference,
    }
    comparison: dict[str, str] = {}
    for field in COMPARABLE:
        system_value, heard = system_values[field], call_findings.get(field)
        if not system_value:
            comparison[field] = "system_missing"
            continue
        if heard is None or heard.value is None:
            verdict = "call_missing"
        elif _norm(field, heard.value) == _norm(field, system_value):
            verdict = "match"
        else:
            verdict = "conflict"
            if heard.status != "conflicting":
                session.add(
                    FindingRevision(
                        finding_id=heard.id,
                        prior_value=heard.value,
                        new_value=heard.value,
                        prior_status=heard.status,
                        new_status="conflicting",
                        reason=f"Payer system ({call.operation}) reports '{system_value}'.",
                        actor="system:pas",
                    )
                )
                heard.status = "conflicting"
        comparison[field] = verdict
        if attempt is not None:
            session.add(
                Finding(
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    field=field,
                    value=system_value,
                    status="conflicting" if verdict == "conflict" else "system_verified",
                    source="system",
                    integration_call_id=call.id,
                    original_phrasing=f"Payer system via {call.operation}",
                )
            )
    if "conflict" in comparison.values():
        task.next_action = (
            "The payer system disagrees with what was heard on the call. "
            "Review the conflicting fields."
        )
    session.add(
        AuditEvent(
            workspace_id=task.workspace_id,
            actor="system:pas",
            action="task.electronic_validation",
            target=task.id,
            detail=comparison,
        )
    )
    return comparison


class IntegrationWorker:
    """Processes pending electronic checks (outbox pattern)."""

    def __init__(self, db: Database, transport_getter=lambda: None):
        self.db, self.transport_getter = db, transport_getter

    async def run_once(self) -> int:
        processed = 0
        async with self.db.sessions() as session:
            tasks = list(
                await session.scalars(select(Task).where(Task.electronic_check.is_not(None)))
            )
            for task in tasks:
                for phase in ("readiness", "pre_call", "post_call"):
                    if (task.electronic_check or {}).get(phase, {}).get("state") == "pending":
                        await run_phase(session, task, phase, self.transport_getter())
                        processed += 1
            await session.commit()
        return processed

    async def run_forever(self, interval_s: float = 2.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("integration pass failed")
            await asyncio.sleep(interval_s)
