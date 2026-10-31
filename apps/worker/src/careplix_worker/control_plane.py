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


class ControlPlane:
    def __init__(self, base_url: str, service_token: str):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(8.0, connect=4.0))
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
        self, room: str, job_id: str, build: str, called: str, caller: str | None
    ) -> dict[str, Any]:
        return await self._claim(
            "/internal/inbound/claim",
            {
                "room": room,
                "job_id": job_id,
                "runtime_build": build,
                "called_number": called,
                "caller_number": caller,
            },
        )

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._attempt_token}"}

    async def tool(
        self, name: str, call_id: str, arguments: dict[str, Any], evidence: list[str]
    ) -> dict[str, Any]:
        body = {"call_id": call_id, "arguments": arguments, "evidence_segment_ids": evidence}
        for attempt in range(3):
            try:
                response = await self._http.post(
                    f"/internal/attempts/{self.attempt_id}/tools/{name}",
                    json=body,
                    headers=self._auth(),
                )
                response.raise_for_status()
                return response.json()
            except httpx.TransportError:
                # Safe to retry: the API keys executions on the model's call_id (FR11).
                await asyncio.sleep(0.3 * (attempt + 1))
        return {"state": "unknown", "result": {"error": "control_plane_unreachable"}}

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
