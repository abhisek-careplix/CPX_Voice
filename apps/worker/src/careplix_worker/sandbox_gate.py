"""Shared sandbox line (trials): before any agent runs, the caller keys in a workspace code.

The API refuses a claim on the sandbox number without a code (``sandbox_code_required``). The
worker then greets the caller with a short spoken prompt, reads the code from keypad presses only
(nothing the caller says counts), and claims again with it. A wrong code gets two more tries.
The greeter hears nothing: its microphone input is off, so it can't be talked into anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext
from livekit.agents.voice import room_io
from livekit.plugins import google

from careplix_worker.control_plane import ClaimRejected
from careplix_worker.gcp import load_credentials

log = logging.getLogger("careplix.worker")

CODE_LENGTH = 6
DIGIT_TIMEOUT_S = 20.0
MAX_TRIES = 3

PROMPTS = {
    "ask": "Welcome to the CarePlix sandbox line. Please enter your six digit workspace code on "
    "your keypad.",
    "again": "Sorry, that code didn't match. Please enter your six digit workspace code again.",
    "nothing": "I didn't receive a code. Please enter your six digit workspace code on your "
    "keypad.",
    "sandbox_no_agent": "This workspace hasn't chosen an agent for the sandbox line yet. Choose "
    "one in CarePlix under Connections, then call again. Goodbye.",
    "sandbox_limit": "This workspace has used its sandbox calls for today. Goodbye.",
    "give_up": "Sorry, I couldn't find that workspace. Please check the code in CarePlix and call "
    "again. Goodbye.",
    "failed": "Sorry, this call can't be connected right now. Goodbye.",
}

GREETER = (
    "You are the automated greeter of a phone line. You never answer questions or hold a "
    "conversation. When asked to say something, say exactly that sentence and nothing else."
)


class Keypad:
    """Builds one code from keypad presses: digits, ``*`` to start over, ``#`` to finish early."""

    def __init__(self, length: int = CODE_LENGTH):
        self.length = length
        self.digits: list[str] = []
        self._done = asyncio.Event()

    def press(self, digit: str) -> None:
        if self._done.is_set():
            return
        if digit == "*":
            self.digits.clear()
        elif digit == "#":
            if self.digits:
                self._done.set()
        elif digit.isdigit():
            self.digits.append(digit)
            if len(self.digits) >= self.length:
                self._done.set()

    def reset(self) -> None:
        self.digits.clear()
        self._done = asyncio.Event()

    async def read(self, timeout: float) -> str | None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._done.wait(), timeout)
        return "".join(self.digits) or None


async def ask(
    say: Callable[[str], Awaitable[None]],
    keypad: Keypad,
    claim: Callable[[str], Awaitable[dict[str, Any]]],
    digit_timeout_s: float = DIGIT_TIMEOUT_S,
) -> dict[str, Any]:
    """Prompt, read a code, claim with it; up to three tries. Raises ClaimRejected to hang up."""
    prompt = PROMPTS["ask"]
    for _ in range(MAX_TRIES):
        keypad.reset()
        await say(prompt)
        code = await keypad.read(digit_timeout_s)
        if not code:
            prompt = PROMPTS["nothing"]
            continue
        try:
            return await claim(code)
        except ClaimRejected as rejected:
            if rejected.code == "sandbox_code_invalid":
                prompt = PROMPTS["again"]
                continue
            await say(PROMPTS.get(rejected.code, PROMPTS["failed"]))
            raise
    await say(PROMPTS["give_up"])
    raise ClaimRejected(0, "sandbox_code_not_entered")


async def admit(
    ctx: JobContext,
    caller: rtc.RemoteParticipant,
    settings: Any,
    claim: Callable[[str], Awaitable[dict[str, Any]]],
    digit_timeout_s: float = DIGIT_TIMEOUT_S,
) -> dict[str, Any]:
    """Ask for the workspace code until a claim succeeds; raise ClaimRejected to hang up."""
    keypad = Keypad()

    def _on_dtmf(dtmf: rtc.SipDTMF) -> None:
        if dtmf.participant is not None and dtmf.participant.identity == caller.identity:
            keypad.press(dtmf.digit)

    ctx.room.on("sip_dtmf_received", _on_dtmf)
    greeter = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=settings.gemini_live_model,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
            voice=settings.gemini_voice,
            credentials=load_credentials(settings),
        )
    )
    await greeter.start(
        agent=Agent(instructions=GREETER),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=caller.identity,
            audio_input=False,
            text_input=False,
            close_on_disconnect=False,
        ),
    )

    async def say(text: str) -> None:
        await greeter.generate_reply(instructions=f'Say exactly this, then stop: "{text}"')

    try:
        return await ask(say, keypad, claim, digit_timeout_s)
    finally:
        ctx.room.off("sip_dtmf_received", _on_dtmf)
        with contextlib.suppress(Exception):
            await greeter.aclose()
