"""CarePlix agent worker — G0 spike.

Proves the chain: LiveKit explicit dispatch -> worker -> Gemini Live on Vertex AI -> two-way audio.
It deliberately has no tools and no case data. G1 replaces the fixed instructions with the claim
flow (ADR09): fetch the immutable call brief and pinned AgentVersion, then build the layered prompt.

    make worker-dev            # local process, registers with LiveKit Cloud
"""

from __future__ import annotations

import json
import logging
import time

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import google

from careplix_worker.gcp import credential_source, load_credentials
from careplix_worker.settings import get_settings

log = logging.getLogger("careplix.worker")

G0_INSTRUCTIONS = """\
You are a CarePlix automated voice assistant running a connectivity test.
This is a test using synthetic data only. Say clearly that you are an automated assistant.
Do not ask for, repeat, or invent any personal, medical or payment information.
Keep replies to one or two short sentences. If asked to do anything other than chat briefly
about this test, say that this test line cannot help with that.
"""

server = AgentServer()


def _metadata_keys(raw: str) -> list[str]:
    # Log the shape of dispatch metadata, never its values (I4).
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ["<invalid-json>"]
    return sorted(data) if isinstance(data, dict) else ["<non-object>"]


@server.rtc_session(agent_name=get_settings().livekit_agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    ids = {"room": ctx.room.name, "job_id": ctx.job.id}
    if missing := settings.missing_model_config():
        log.error("model not configured: %s", ", ".join(missing), extra=ids)
        ctx.shutdown(reason="model_not_configured")
        return

    log.info("job accepted, metadata keys=%s", _metadata_keys(ctx.job.metadata), extra=ids)
    started = time.perf_counter()

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=settings.gemini_live_model,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
            voice=settings.gemini_voice,
            credentials=load_credentials(settings),
        )
    )

    @session.on("metrics_collected")
    def _on_metrics(event) -> None:
        metrics = event.metrics
        fields = {
            k: getattr(metrics, k)
            for k in ("ttft", "duration", "input_tokens", "output_tokens")
            if getattr(metrics, k, None) is not None
        }
        log.info("metrics %s %s", type(metrics).__name__, fields, extra=ids)

    @session.on("close")
    def _on_close(event) -> None:
        log.info(
            "session closed reason=%s error=%s",
            event.reason,
            type(event.error).__name__ if event.error else None,
            extra=ids,
        )

    await session.start(agent=Agent(instructions=G0_INSTRUCTIONS), room=ctx.room)
    log.info(
        "session started in %.0f ms model=%s credentials=%s",
        (time.perf_counter() - started) * 1000,
        settings.gemini_live_model,
        credential_source(settings),
        extra=ids,
    )
    await session.generate_reply(
        instructions="Greet the tester, say you are the CarePlix automated assistant and that this "
        "is a synthetic-data connectivity test, then ask them to say something."
    )


if __name__ == "__main__":
    cli.run_app(server)
