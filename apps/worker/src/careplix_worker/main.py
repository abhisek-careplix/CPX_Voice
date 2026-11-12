"""CarePlix agent worker: one generic runtime for every published agent (ADR04, ADR09).

On dispatch the worker claims its attempt from the CarePlix API, receives the immutable call brief
and pinned configuration, then runs the conversation on Gemini Live. Every tool call is authorized
by the API; transcript segments, state and heartbeats stream back as events.

    make worker-dev            # local process, registers with LiveKit Cloud for explicit dispatch
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from google.genai import types as genai_types
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli
from livekit.agents.llm import function_tool
from livekit.agents.voice import room_io
from livekit.plugins import google

from careplix_common.safe_logging import quiet_httpx, scrub, tag_severity
from careplix_worker import counterpart as counterpart_role
from careplix_worker import prompt, sandbox_gate
from careplix_worker.control_plane import ClaimRejected, ControlPlane, EventReporter
from careplix_worker.gcp import load_credentials
from careplix_worker.hold import HoldClock
from careplix_worker.settings import get_settings

log = logging.getLogger("careplix.worker")
# Before LiveKit sets up logging, so its JSON lines carry a Cloud Logging severity.
tag_severity()
quiet_httpx()

RUNTIME_BUILD = os.environ.get("CAREPLIX_RUNTIME_BUILD", "dev")
DTMF_CODES = {**{str(d): d for d in range(10)}, "*": 10, "#": 11}
SIP_KIND = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
AGENT_KIND = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT

# Cloud Run sends health checks to $PORT; locally the SDK picks its own port.
server = AgentServer(port=int(os.environ["PORT"])) if os.environ.get("PORT") else AgentServer()


def _metadata(raw: str) -> dict[str, Any]:
    # Dispatch metadata holds opaque references only (I4). Never log its values.
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_keys(raw: str) -> list[str]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ["<invalid-json>"]
    return sorted(data) if isinstance(data, dict) else ["<non-object>"]


class CallRuntime:
    """Per-call state shared by tool proxies and session hooks."""

    def __init__(self, ctx: JobContext, plane: ControlPlane, reporter: EventReporter, claim: dict):
        self.ctx, self.plane, self.reporter, self.claim = ctx, plane, reporter, claim
        self.ending = False
        self.session: AgentSession | None = None
        self.clock = HoldClock(
            max_duration_s=claim["max_duration_s"],
            # Older API builds don't send max_hold_s: no hold allowance beyond the budget.
            max_hold_s=claim.get("max_hold_s") or 0,
        )

    def substate_changed(self, substate: str) -> None:
        """Mute on hold, unmute when a menu or a person is back (ADR10)."""
        if substate == "Hold":
            if self.clock.enter_hold():
                self._voice(False)
                self.reporter.emit("hold.started", {"hold": self.clock.holds})
        elif self.clock.on_hold:
            lasted = self.clock.leave_hold()
            self._voice(True)
            self.reporter.emit("hold.ended", {"hold_s": round(lasted), "to": substate})

    def _voice(self, enabled: bool) -> None:
        if self.session is not None:
            self.session.output.set_audio_enabled(enabled)

    def build_tools(self) -> list:
        return [self._proxy(spec) for spec in self.claim["tools"]]

    def _proxy(self, spec: dict[str, Any]):
        name = spec["name"]

        async def call(raw_arguments: dict[str, Any], context: RunContext) -> Any:
            evidence = self.reporter.evidence_window() if name == "record_finding" else []
            response = await self.plane.tool(
                name,
                context.function_call.call_id,
                raw_arguments,
                evidence,
                spec.get("timeout_ms"),
            )
            if response["state"] != "succeeded":
                # The model sees the refusal and continues; it cannot work around it (SEC06).
                return {"refused": response["result"].get("message", "Not permitted.")}
            if name == "set_call_state":
                self.substate_changed(response["result"].get("substate", ""))
            elif name == "send_dtmf":
                await self._press(raw_arguments["digits"])
            elif name == "end_call":
                await context.wait_for_playout()
                await self.hang_up(raw_arguments.get("reason", "objective_complete"))
            return response["result"]

        return function_tool(
            call,
            raw_schema={
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        )

    async def _press(self, digits: str) -> None:
        for digit in digits:
            await self.ctx.room.local_participant.publish_dtmf(code=DTMF_CODES[digit], digit=digit)
            await asyncio.sleep(0.25)

    async def hang_up(self, reason: str) -> None:
        if self.ending:
            return
        self.ending = True
        self.reporter.emit("worker.done", {"reason": reason})
        await self.reporter.close()
        try:
            await self.ctx.delete_room()
        finally:
            self.ctx.shutdown(reason=reason)


async def _claim(ctx: JobContext, plane: ControlPlane) -> dict[str, Any]:
    meta = _metadata(ctx.job.metadata)
    if attempt_id := meta.get("attempt_id"):
        return await plane.claim(attempt_id, ctx.room.name, ctx.job.id, RUNTIME_BUILD)
    # Inbound: route by signals from LiveKit and our dispatch rule, never by the caller (I5).
    caller = await ctx.wait_for_participant(kind=SIP_KIND)
    attrs = caller.attributes
    called = attrs.get("sip.trunkPhoneNumber", "")
    log.info(
        "inbound sip attributes keys=%s called_len=%d called_last4=%s rule=%s",
        sorted(k for k in attrs if k.startswith("sip.")),
        len(called),
        called[-4:] or "-",
        attrs.get("sip.ruleID", "-"),
        extra={"job_id": ctx.job.id},
    )

    async def claim(code: str | None = None) -> dict[str, Any]:
        return await plane.claim_inbound(
            ctx.room.name,
            ctx.job.id,
            RUNTIME_BUILD,
            called,
            attrs.get("sip.phoneNumber"),
            meta.get("route_id"),
            attrs.get("sip.ruleID"),
            workspace_code=code,
        )

    try:
        return await claim()
    except ClaimRejected as rejected:
        if rejected.code != "sandbox_code_required":
            raise
    # The shared sandbox line: the caller keys in their workspace code first.
    return await sandbox_gate.admit(ctx, caller, get_settings(), claim)


@server.rtc_session(agent_name=get_settings().livekit_agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    ids = {"room": ctx.room.name, "job_id": ctx.job.id}
    log.info("job accepted, metadata keys=%s", _metadata_keys(ctx.job.metadata), extra=ids)
    await ctx.connect()

    plane = ControlPlane(
        settings.careplix_api_url, settings.service_token(), settings.careplix_api_audience
    )
    if probe_id := _metadata(ctx.job.metadata).get("probe_id"):
        # Connections > Test agent: prove this worker answers dispatch; no model session.
        try:
            await plane.ack_probe(probe_id, ctx.room.name, RUNTIME_BUILD)
            log.info("probe acknowledged", extra=ids)
        finally:
            await plane.aclose()
            ctx.shutdown(reason="probe_acknowledged")
        return
    if attempt_for := _metadata(ctx.job.metadata).get("counterpart_for"):
        # VS2: this job plays the other side of a simulated scenario run.
        await counterpart_role.run(ctx, plane, settings, str(attempt_for))
        return

    try:
        claim = await _claim(ctx, plane)
    except ClaimRejected as rejected:
        # No silent open line: without a claim there is no authorised conversation (doc 04).
        log.warning("claim rejected: %s", rejected.code, extra=ids)
        await plane.aclose()
        await ctx.delete_room()
        ctx.shutdown(reason=f"claim_rejected:{rejected.code}")
        return

    ids["attempt_id"] = plane.attempt_id
    reporter = EventReporter(plane)
    reporter.start()
    runtime = CallRuntime(ctx, plane, reporter, claim)
    ctx.add_shutdown_callback(plane.aclose)

    # Outbound calls bind audio input to the dialled SIP participant only. In a simulation the
    # other side is the counterpart agent (VS2); a Studio listener in the room is ignored.
    simulated = claim.get("counterpart_kind") == "agent"
    if claim["expect_sip"]:
        counterpart = await ctx.wait_for_participant(identity=claim["sip_identity"])
    elif simulated:
        counterpart = await ctx.wait_for_participant(kind=AGENT_KIND)
    else:
        counterpart = await ctx.wait_for_participant()
    reporter.emit(
        "participant.joined",
        {
            "kind": "sip"
            if counterpart.kind == SIP_KIND
            else ("simulated" if simulated else "browser")
        },
    )

    @ctx.room.on("participant_attributes_changed")
    def _on_attrs(changed: dict[str, str], participant: rtc.Participant) -> None:
        if participant.identity == counterpart.identity and "sip.callStatus" in changed:
            reporter.emit("sip.call_status", {"status": changed["sip.callStatus"]})

    @ctx.room.on("participant_disconnected")
    def _on_left(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == counterpart.identity:
            asyncio.create_task(runtime.hang_up("counterpart_ended"))

    conversation = claim["conversation"]
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=claim["model_id"],
            vertexai=True,
            # Workspace choice from the Studio first, worker environment as fallback.
            project=claim.get("vertex", {}).get("project") or settings.gcp_project,
            location=claim.get("vertex", {}).get("location") or settings.vertex_location,
            voice=conversation.get("voice") or settings.gemini_voice,
            credentials=load_credentials(settings),
            # Long payer holds: a sliding window keeps the session under Gemini's context limit,
            # so it isn't cut off at ~15 minutes of audio. The plugin resumes on GoAway.
            **(
                {
                    "context_window_compression": genai_types.ContextWindowCompressionConfig(
                        sliding_window=genai_types.SlidingWindow()
                    )
                }
                if settings.gemini_context_compression
                else {}
            ),
        )
    )
    runtime.session = session

    spoke = asyncio.Event()  # the agent has said something (for the silence alarm)

    @session.on("conversation_item_added")
    def _on_item(event) -> None:
        item = event.item
        if getattr(item, "role", None) == "assistant":
            spoke.set()
        text = getattr(item, "text_content", None)
        if getattr(item, "role", None) not in ("user", "assistant") or not text:
            return
        reporter.segment(
            f"{plane.attempt_id[:8]}-{item.id}"[:64],
            "agent" if item.role == "assistant" else "counterpart",
            text,
            # Said while muted on hold: the other side never heard it.
            "muted"
            if item.role == "assistant" and runtime.clock.on_hold
            else "interrupted"
            if getattr(item, "interrupted", False)
            else "played",
        )

    @session.on("error")
    def _on_error(event) -> None:
        # The message says *why* (bad model ID, permission, rejected setting…); scrubbed and capped.
        detail = scrub(str(getattr(event.error, "message", None) or event.error))[:240]
        log.warning("model error %s: %s", type(event.error).__name__, detail, extra=ids)
        reporter.emit("model.error", {"error": type(event.error).__name__, "detail": detail})

    @session.on("close")
    def _on_close(event) -> None:
        asyncio.create_task(runtime.hang_up(f"session_closed:{event.reason}"))

    await session.start(
        agent=Agent(instructions=prompt.compose(claim), tools=runtime.build_tools()),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=counterpart.identity,
            **({"participant_kinds": [AGENT_KIND]} if simulated else {}),
        ),
    )
    reporter.emit("worker.ready", {"runtime_build": RUNTIME_BUILD})

    async def _silence_alarm() -> None:
        # A session that connects but never speaks looks like "no audio" to the person testing.
        try:
            await asyncio.wait_for(spoke.wait(), timeout=settings.silence_alarm_s)
        except TimeoutError:
            log.warning("agent silent %.0fs after joining", settings.silence_alarm_s, extra=ids)
            reporter.emit(
                "audio.missing",
                {
                    "after_s": round(settings.silence_alarm_s),
                    "compression": settings.gemini_context_compression,
                },
            )

    if claim["speak_first"]:
        silence = asyncio.create_task(_silence_alarm())

        async def _cancel_silence() -> None:
            silence.cancel()

        ctx.add_shutdown_callback(_cancel_silence)

    async def _budget() -> None:
        # Talk time counts against max_duration_s; hold time has its own limit (ADR10).
        runtime.clock.start()
        while (reason := runtime.clock.exceeded()) is None:
            await asyncio.sleep(1)
        payload = {
            "max_duration_s": claim["max_duration_s"],
            "talk_s": round(runtime.clock.talk_s()),
            "hold_s": round(runtime.clock.hold_total_s()),
        }
        reporter.emit(
            "budget.exceeded" if reason == "budget_exhausted" else "hold.timeout", payload
        )
        await runtime.hang_up(reason)

    budget = asyncio.create_task(_budget())

    async def _cancel_budget() -> None:
        budget.cancel()

    ctx.add_shutdown_callback(_cancel_budget)

    if claim["speak_first"]:
        await session.generate_reply(
            instructions=f'Greet the other person with: "{claim["identity"]["greeting"]}"'
        )


if __name__ == "__main__":
    cli.run_app(server)
