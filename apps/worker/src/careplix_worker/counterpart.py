"""VS2 simulated counterpart: the other side of a Voice Studio scenario, played by an AI.

The same worker image is dispatched into the scenario's room a second time with
``{"counterpart_for": attempt_id}``. It claims its role from the API (persona, script, phone menu,
hold length, voice), waits for our agent to join, and plays the call:

- an automated phone menu, with the agent's keypad presses delivered as ``[keypad: 2]``;
- a hold with generated hold music for the scenario's length (its own voice muted meanwhile);
- a live representative following the scenario's script.

It reports nothing: the agent's worker records the call (transcript, events, scorecard), exactly as
for a real call. The counterpart only ends the call when its role says so, or at its time limit.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterator
from typing import Any

from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, RunContext
from livekit.agents.llm import function_tool
from livekit.agents.voice import room_io
from livekit.plugins import google

from careplix_worker.control_plane import ClaimRejected, ControlPlane
from careplix_worker.gcp import load_credentials
from careplix_worker.settings import WorkerSettings

log = logging.getLogger("careplix.worker.counterpart")

AGENT_KIND = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
SAMPLE_RATE = 24000
FRAME_MS = 20
DTMF_SETTLE_S = 1.2  # keys pressed within this gap are delivered together ("1", then "2" → "12")

# A gentle, looping hold tune: (frequency Hz, beats). 0 is a rest.
_TUNE = [(523.25, 1), (659.25, 1), (783.99, 2), (659.25, 1), (587.33, 1), (523.25, 2), (0, 1)]
_BEAT_S = 0.42


def hold_music_frames(seconds: float) -> Iterator[rtc.AudioFrame]:
    """Soft sine-tone hold music, 20 ms frames of 16-bit mono PCM (no audio files needed)."""
    per_frame = SAMPLE_RATE * FRAME_MS // 1000
    notes: list[float] = []
    for freq, beats in _TUNE:
        notes += [freq] * int(beats * _BEAT_S * SAMPLE_RATE)
    total = int(seconds * SAMPLE_RATE)
    pos = 0
    while pos < total:
        n = min(per_frame, total - pos)
        data = bytearray()
        for i in range(n):
            t = pos + i
            freq = notes[t % len(notes)]
            # Fade each note in and out a little so it doesn't click.
            env = 0.0 if freq == 0 else 0.12
            sample = int(env * 32767 * math.sin(2 * math.pi * freq * t / SAMPLE_RATE))
            data += sample.to_bytes(2, "little", signed=True)
        yield rtc.AudioFrame(bytes(data), SAMPLE_RATE, 1, n)
        pos += n


class KeypadBuffer:
    """Collects DTMF digits and hands them over once the caller stops pressing."""

    def __init__(self, deliver, settle_s: float = DTMF_SETTLE_S):
        self._deliver, self._settle_s = deliver, settle_s
        self._digits: list[str] = []
        self._timer: asyncio.TimerHandle | None = None

    def press(self, digit: str) -> None:
        self._digits.append(digit)
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(self._settle_s, self._flush)

    def _flush(self) -> None:
        digits, self._digits = "".join(self._digits), []
        self._timer = None
        if digits:
            self._deliver(digits)


async def run(ctx: JobContext, plane: ControlPlane, settings: WorkerSettings, attempt_id: str):
    ids = {"room": ctx.room.name, "job_id": ctx.job.id, "role": "counterpart"}
    try:
        role = await plane.counterpart_claim(attempt_id, ctx.room.name, ctx.job.id)
    except ClaimRejected as rejected:
        log.warning("counterpart claim rejected: %s", rejected.code, extra=ids)
        ctx.shutdown(reason=f"counterpart_rejected:{rejected.code}")
        return
    finally:
        await plane.aclose()

    # Our agent joins as an agent participant; wait for it, ignoring the Studio listener.
    agent = await ctx.wait_for_participant(kind=AGENT_KIND)
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=role["model_id"],
            vertexai=True,
            project=role.get("vertex", {}).get("project") or settings.gcp_project,
            location=role.get("vertex", {}).get("location") or settings.vertex_location,
            voice=role.get("voice") or "Charon",
            credentials=load_credentials(settings),
        )
    )
    music = rtc.AudioSource(SAMPLE_RATE, 1)
    music_track = rtc.LocalAudioTrack.create_audio_track("hold-music", music)
    on_hold = asyncio.Event()

    async def _play_hold(seconds: float) -> None:
        publication = await ctx.room.local_participant.publish_track(
            music_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        try:
            for frame in hold_music_frames(seconds):
                await music.capture_frame(frame)
        finally:
            await ctx.room.local_participant.unpublish_track(publication.sid)

    async def put_on_hold(seconds: int, context: RunContext) -> str:
        """Put the caller on hold with music for the given number of seconds."""
        await context.wait_for_playout()
        seconds = max(1, min(int(seconds), int(role.get("hold_s") or seconds)))
        on_hold.set()
        session.output.set_audio_enabled(False)
        try:
            await _play_hold(seconds)
        finally:
            session.output.set_audio_enabled(True)
            on_hold.clear()
        return "You are back from hold. Greet the caller as a live representative."

    async def end_call(context: RunContext) -> str:
        """End the call after saying goodbye."""
        await context.wait_for_playout()
        await ctx.delete_room()
        ctx.shutdown(reason="counterpart_ended")
        return "ended"

    keypad = KeypadBuffer(
        lambda digits: asyncio.create_task(session.generate_reply(user_input=f"[keypad: {digits}]"))
    )

    @ctx.room.on("sip_dtmf_received")
    def _on_dtmf(dtmf: rtc.SipDTMF) -> None:
        from_agent = dtmf.participant is None or dtmf.participant.identity == agent.identity
        if from_agent and not on_hold.is_set():  # keys pressed during hold music are ignored
            keypad.press(dtmf.digit)

    @ctx.room.on("participant_disconnected")
    def _on_left(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == agent.identity:
            ctx.shutdown(reason="agent_left")

    await session.start(
        agent=Agent(
            instructions=role["instructions"],
            tools=[function_tool(put_on_hold), function_tool(end_call)],
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=agent.identity, participant_kinds=[AGENT_KIND]
        ),
    )
    # The callee answers first: the menu, or a greeting.
    await session.generate_reply(instructions="The call has just connected. Answer it now.")

    async def _limit() -> None:
        await asyncio.sleep(float(role.get("max_duration_s") or 900))
        await ctx.delete_room()
        ctx.shutdown(reason="counterpart_time_limit")

    limit = asyncio.create_task(_limit())

    async def _cancel() -> None:
        limit.cancel()

    ctx.add_shutdown_callback(_cancel)
    log.info("counterpart playing %s", role.get("counterpart", "?"), extra=ids)


def describe(role: dict[str, Any]) -> str:
    """Short, PHI-free description for logs and tests."""
    return f"{role.get('counterpart')} · voice {role.get('voice')} · hold {role.get('hold_s')}s"
