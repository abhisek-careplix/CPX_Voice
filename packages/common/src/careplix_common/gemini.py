"""Gemini Live on Vertex AI: credentials and a live connectivity probe shared by API and worker.

Credentials, in order of preference (BUILD_PLAN ADR07/R2):
1. Application Default Credentials (``gcloud auth application-default login`` locally, workload
   identity or federation when hosted).
2. A restricted service-account key passed as JSON (e.g. a LiveKit Cloud agent secret). It is parsed
   in memory only, never written to disk or logged.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
PROMPT = "Reply with exactly: CarePlix probe ready."


class CredentialConfigError(ValueError):
    pass


def credentials_from_json(raw: str):
    """Explicit service-account credentials, or None to let the Google SDK use ADC."""
    raw = (raw or "").strip()
    if not raw:
        return None
    from google.oauth2 import service_account

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    if info.get("type") != "service_account":
        raise CredentialConfigError("GOOGLE_SERVICE_ACCOUNT_JSON must be a service_account key")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


@dataclass
class ProbeResult:
    model_id: str
    location: str
    ok: bool = False
    connect_ms: int | None = None
    first_audio_ms: int | None = None
    reply_transcript: str | None = None
    resumption_handle_received: bool = False
    error: str | None = None
    error_detail: str | None = None
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


async def probe_live(
    project: str, location: str, model_id: str, credentials=None, timeout_s: float = 30.0
) -> ProbeResult:
    """Open a Live session, ask for one spoken sentence, and time it. Synthetic prompt only."""
    from google import genai
    from google.genai import types

    result = ProbeResult(model_id=model_id, location=location)
    client = genai.Client(
        vertexai=True, project=project, location=location, credentials=credentials
    )
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(),
        system_instruction="You are a connectivity probe. Follow the user's instruction exactly.",
    )
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            async with client.aio.live.connect(model=model_id, config=config) as session:
                result.connect_ms = _ms(started)
                sent = time.perf_counter()
                await session.send_realtime_input(text=PROMPT)
                transcript: list[str] = []
                async for msg in session.receive():
                    if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                        result.resumption_handle_received = True
                    content = msg.server_content
                    if not content:
                        continue
                    parts = (content.model_turn.parts if content.model_turn else None) or []
                    if result.first_audio_ms is None and any(
                        p.inline_data and p.inline_data.data for p in parts
                    ):
                        result.first_audio_ms = _ms(sent)
                    if content.output_transcription and content.output_transcription.text:
                        transcript.append(content.output_transcription.text)
                    if content.turn_complete:
                        break
                result.reply_transcript = "".join(transcript)[:200] or None
        result.ok = result.first_audio_ms is not None
        if not result.ok:
            result.error = "no_audio"
    except Exception as exc:  # noqa: BLE001 - reported as a categorised result
        result.error = type(exc).__name__
        result.error_detail = str(exc)[:300]
    return result
