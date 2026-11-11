"""Shared sandbox line: the caller keys in a workspace code before any agent runs."""

import asyncio

import pytest

from careplix_worker.control_plane import ClaimRejected
from careplix_worker.sandbox_gate import PROMPTS, Keypad, ask


def test_keypad_builds_a_code_from_digits_star_and_hash():
    pad = Keypad()
    for d in "12*4567890":  # * starts over; the first six digits after it complete the code
        pad.press(d)
    assert "".join(pad.digits) == "456789"
    short = Keypad()
    for d in "42#9":
        short.press(d)
    assert "".join(short.digits) == "42"  # # finishes early; later presses are ignored


class Caller:
    """Presses keys after each prompt, like a person on the phone."""

    def __init__(self, pad: Keypad, codes: list[str | None]):
        self.pad, self.codes, self.said = pad, list(codes), []

    async def say(self, text: str) -> None:
        self.said.append(text)
        code = self.codes.pop(0) if self.codes else None
        if code:
            for d in code:
                asyncio.get_running_loop().call_soon(self.pad.press, d)


async def test_a_wrong_code_gets_another_try():
    pad = Keypad()
    caller = Caller(pad, ["111111", "246810"])
    tried = []

    async def claim(code):
        tried.append(code)
        if code != "246810":
            raise ClaimRejected(404, "sandbox_code_invalid")
        return {"attempt_id": "a1"}

    assert await ask(caller.say, pad, claim, digit_timeout_s=0.5) == {"attempt_id": "a1"}
    assert tried == ["111111", "246810"]
    assert caller.said == [PROMPTS["ask"], PROMPTS["again"]]


async def test_silence_three_times_hangs_up_politely():
    pad = Keypad()
    caller = Caller(pad, [])

    async def claim(code):
        raise AssertionError("never claims without a code")

    with pytest.raises(ClaimRejected) as gave_up:
        await ask(caller.say, pad, claim, digit_timeout_s=0.05)
    assert gave_up.value.code == "sandbox_code_not_entered"
    assert caller.said[-1] == PROMPTS["give_up"] and len(caller.said) == 4


async def test_a_workspace_without_an_agent_is_told_why():
    pad = Keypad()
    caller = Caller(pad, ["246810"])

    async def claim(code):
        raise ClaimRejected(409, "sandbox_no_agent")

    with pytest.raises(ClaimRejected):
        await ask(caller.say, pad, claim, digit_timeout_s=0.5)
    assert caller.said[-1] == PROMPTS["sandbox_no_agent"]
