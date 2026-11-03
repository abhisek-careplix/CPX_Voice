"""Client for the CarePlix internal API, plus the event reporter that streams call state to it.

The worker holds no authority of its own: it claims an attempt once, then every tool call and
event goes to the API under an attempt-scoped token (ADR09, ADR12).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from typing import Any

import httpx

log = logging.getLogger("careplix.worker")


class ClaimRejected(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class _CloudRunIdentity(httpx.Auth):
    """Adds a Google ID token for a private Cloud Run API without touching Authorization."""

    def __init__(self, audience: str):
        self.audience, self._token, self._expires = audience, "", 0.0

    async def async_auth_flow(self, request):
        if time.time() > self._expires:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token

            self._token = await asyncio.to_thread(id_token.fetch_id_token, Request(), self.audience)
            self._expires = time.time() + 3000
        request.headers["X-Serverless-Authorization"] = f"Bearer {self._token}"
        yield request


class ControlPlane:
    def __init__(self, base_url: str, service_token: str, audience: str = ""):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(8.0, connect=4.0),
            auth=_CloudRunIdentity(audience) if audience else None,
        )
        self._service_token = service_token
        self.attempt_id: str | None = None
        self._attempt_token: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _claim(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(
            path, json=body, headers={"Authorization": f"Bearer {self._service_token}"}
        )
        if response.status_code != 200:
            code = response.json().get("error", {}).get("code", "claim_failed")
            raise ClaimRejected(response.status_code, code)
        data = response.json()
        self.attempt_id, self._attempt_token = data["attempt_id"], data["attempt_token"]
        return data

    async def claim(self, attempt_id: str, room: str, job_id: str, build: str) -> dict[str, Any]:
        return await self._claim(
            f"/internal/attempts/{attempt_id}/claim",
            {"room": room, "job_id": job_id, "runtime_build": build},
        )

    async def claim_inbound(
        self,
        room: str,
        job_id: str,
        build: str,
        called: str,
        caller: str | None,
        route_id: str | None,
        rule_id: str | None,
    ) -> dict[str, Any]:
        return await self._claim(
            "/internal/inbound/claim",
            {
                "room": room,
                "job_id": job_id,
                "runtime_build": build,
                "called_number": called,
                "caller_number": caller,
                "route_id": route_id,
                "rule_id": rule_id,
            },
        )

    async def counterpart_claim(self, attempt_id: str, room: str, job_id: str) -> dict[str, Any]:
        """VS2: claim the simulated other side of a scenario run (once per run)."""
        response = await self._http.post(
            f"/internal/attempts/{attempt_id}/counterpart",
            json={"room": room, "job_id": job_id},
            headers={"Authorization": f"Bearer {self._service_token}"},
        )
        if response.status_code != 200:
            code = response.json().get("error", {}).get("code", "claim_failed")
            raise ClaimRejected(response.status_code, code)
        return response.json()

    async def ack_probe(self, probe_id: str, room: str, build: str) -> None:
        response = await self._http.post(
            f"/internal/probes/{probe_id}/ack",
            json={"room": room, "runtime_build": build},
            headers={"Authorization": f"Bearer {self._service_token}"},
        )
        response.raise_for_status()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._attempt_token}"}

    async def tool(
        self,
        name: str,
        call_id: str,
        arguments: dict[str, Any],
        evidence: list[str],
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        body = {"call_id": call_id, "arguments": arguments, "evidence_segment_ids": evidence}
        # EHR tools (find_slots, book_appointment…) can take longer than the 8 s default; give
        # them their registry timeout plus a margin for the API's own work.
        timeout = httpx.Timeout(max(8.0, (timeout_ms or 0) / 1000 + 5), connect=4.0)
        for attempt in range(3):
            try:
                response = await self._http.post(
                    f"/internal/attempts/{self.attempt_id}/tools/{name}",
                    json=body,
                    headers=self._auth(),
                    timeout=timeout,
                )
                if response.status_code < 500:
                    response.raise_for_status()
                    return response.json()
            except httpx.TransportError:
                pass
            # Safe to retry: the API keys executions on the model's call_id (FR11).
            await asyncio.sleep(0.3 * (attempt + 1))
        # Never raise into the conversation: the model gets an explicit "unknown" outcome.
        return {"state": "unknown", "result": {"message": "The system could not complete that."}}

    async def post_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        response = await self._http.post(
            f"/internal/attempts/{self.attempt_id}/events",
            json={"events": events},
            headers=self._auth(),
        )
        response.raise_for_status()
        return response.json()


class EventReporter:
    """Batches events to the API, retries on failure and sends heartbeats."""

    def __init__(self, plane: ControlPlane, heartbeat_s: float = 10.0):
        self._plane = plane
        self._queue: deque[dict[str, Any]] = deque(maxlen=2000)
        self._wake = asyncio.Event()
        self._heartbeat_s = heartbeat_s
        self._task: asyncio.Task | None = None
        self._segments: deque[str] = deque(maxlen=6)
        self.remote_state: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        self._queue.append({"event_id": str(uuid.uuid4()), "type": type_, "payload": payload or {}})
        self._wake.set()

    def segment(self, segment_id: str, speaker: str, text: str, playout: str) -> None:
        self._segments.append(segment_id)
        self.emit(
            "transcript.segment",
            {"segment_id": segment_id, "speaker": speaker, "text": text, "playout": playout},
        )

    def evidence_window(self) -> list[str]:
        """Recent segments on both sides: a read-back and its confirmation span two turns."""
        return list(self._segments)

    async def _flush(self) -> None:
        while self._queue:
            batch = [self._queue[i] for i in range(min(len(self._queue), 100))]
            result = await self._plane.post_events(batch)
            self.remote_state = result.get("state")
            for _ in batch:
                self._queue.popleft()

    async def _run(self) -> None:
        last_beat = 0.0
        backoff = 0.5
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            self._wake.clear()
            if time.monotonic() - last_beat >= self._heartbeat_s:
                self.emit("worker.heartbeat")
                last_beat = time.monotonic()
            try:
                await self._flush()
                backoff = 0.5
            except Exception as exc:  # noqa: BLE001 - keep events and retry
                log.warning("event flush failed: %s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        for _ in range(3):
            try:
                await self._flush()
                return
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
