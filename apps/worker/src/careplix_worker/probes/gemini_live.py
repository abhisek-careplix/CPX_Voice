"""G0 probe: verify Gemini Live access on Vertex AI and measure session behaviour.

    make probe-gemini                               # connect + one spoken reply
    make probe-gemini ARGS="--soak-seconds 900"     # then stream silence to observe session limits

Run it from the same credential environment the worker will use (OD04, BUILD_PLAN R1/R2).
Records: connect time, time to first audio for a text turn, whether session-resumption handles are
issued, and when the server sends GoAway or closes during a long silent stream (what a payer hold
looks like to the model). Soak runs bill input audio for their full duration.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from careplix_common.evidence import EvidenceRecord
from careplix_worker.gcp import credential_source, load_credentials
from careplix_worker.settings import get_settings

EVIDENCE_DIR = Path("evidence/g0")
CHUNK_MS = 100
SILENCE = b"\x00\x00" * (16000 * CHUNK_MS // 1000)  # 16 kHz, 16-bit mono PCM
PROMPT = "Reply with exactly: CarePlix probe ready."


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


async def _first_reply(session, observed: dict[str, Any]) -> None:
    sent = time.perf_counter()
    await session.send_realtime_input(text=PROMPT)
    audio_bytes, transcript = 0, []
    async for msg in session.receive():
        if msg.session_resumption_update and msg.session_resumption_update.new_handle:
            observed["resumption_handle_received"] = True
        content = msg.server_content
        if not content:
            continue
        for part in (content.model_turn.parts if content.model_turn else []) or []:
            if part.inline_data and part.inline_data.data:
                if "first_audio_ms" not in observed:
                    observed["first_audio_ms"] = _ms(sent)
                audio_bytes += len(part.inline_data.data)
        if content.output_transcription and content.output_transcription.text:
            transcript.append(content.output_transcription.text)
        if content.turn_complete:
            break
    observed["reply_audio_bytes"] = audio_bytes
    observed["reply_transcript"] = "".join(transcript)[:200]


async def _soak(session, seconds: int, observed: dict[str, Any]) -> None:
    started = time.perf_counter()
    soak: dict[str, Any] = {"requested_s": seconds}
    observed["soak"] = soak

    async def sender() -> None:
        while time.perf_counter() - started < seconds:
            await session.send_realtime_input(
                audio=types.Blob(data=SILENCE, mime_type="audio/pcm;rate=16000")
            )
            await asyncio.sleep(CHUNK_MS / 1000)

    async def receiver() -> None:
        while True:
            async for msg in session.receive():
                if msg.go_away and "go_away_at_s" not in soak:
                    soak["go_away_at_s"] = round(time.perf_counter() - started, 1)
                    soak["go_away_time_left"] = str(msg.go_away.time_left)
                if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                    observed["resumption_handle_received"] = True

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    soak["ended_after_s"] = round(time.perf_counter() - started, 1)
    for task in done:
        if task.exception():
            soak["ended_by"] = type(task.exception()).__name__
            break
    else:
        soak["ended_by"] = "requested_duration"


async def run(soak_seconds: int) -> int:
    settings = get_settings()
    observed: dict[str, Any] = {
        "model_id": settings.gemini_live_model,
        "location": settings.vertex_location,
        "credential_source": credential_source(settings),
        "resumption_handle_received": False,
    }
    if missing := settings.missing_model_config():
        print(f"blocked: set {', '.join(missing)} in .env")
        return 1

    client = genai.Client(
        vertexai=True,
        project=settings.gcp_project,
        location=settings.vertex_location,
        credentials=load_credentials(settings),
    )
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(),
        system_instruction="You are a connectivity probe. Follow the user's instruction exactly.",
    )

    result = "fail"
    started = time.perf_counter()
    try:
        async with client.aio.live.connect(
            model=settings.gemini_live_model, config=config
        ) as session:
            observed["connect_ms"] = _ms(started)
            await _first_reply(session, observed)
            if soak_seconds:
                await _soak(session, soak_seconds, observed)
        result = "pass" if observed.get("first_audio_ms") is not None else "fail"
    except Exception as exc:  # noqa: BLE001 - recorded as evidence, not swallowed
        observed["error"] = type(exc).__name__
        observed["error_detail"] = str(exc)[:300]

    record = EvidenceRecord(
        test_id="G0-GEMINI-LIVE",
        gate="G0",
        expected="Vertex Gemini Live session connects and returns spoken audio for a text turn.",
        observed=observed,
        result=result,
        model_id=settings.gemini_live_model,
    )
    path = record.write(EVIDENCE_DIR)
    for key, value in observed.items():
        print(f"{key:<28} {value}")
    print(f"\nresult: {result}\nevidence: {path}")
    return 0 if result == "pass" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--soak-seconds", type=int, default=0, help="stream silence after the reply (0 = skip)"
    )
    raise SystemExit(asyncio.run(run(parser.parse_args().soak_seconds)))


if __name__ == "__main__":
    main()
