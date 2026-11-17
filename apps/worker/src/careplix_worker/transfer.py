"""Warm transfer in the worker (OPERATING_PLAN §5.1 answering, §5.4 human transfers).

The API has already authorised the transfer (``transfer_to_staff``) and handed the worker an
approved staff number, the trunk and the whisper; the model never sees those. Here:

1. LiveKit's warm transfer workflow holds the caller (hold music, agent muted), dials staff into
   a side room, and a second model session briefs them with the whisper.
2. Staff say yes: they are moved into the caller's room. The agent's own session closes but the
   job stays in the room, so the call keeps going until either side hangs up.
3. No answer, voicemail, declined or an error: the caller is back with the agent, and the API
   turns the hand-off into a call-back promise whose wording the model then says.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("careplix.worker")

TransferFn = Callable[[dict[str, Any]], Awaitable[Any]]


def classify(error: BaseException) -> str:
    """The workflow fails with a ToolError whose text says why; map it to the API's outcomes."""
    text = str(error).lower()
    if "voicemail" in text:
        return "voicemail"
    if "declined" in text:
        return "declined"
    if "could not dial" in text or "room closed" in text:
        return "no_answer"
    return "failed"


async def warm_transfer(dial: dict[str, Any]) -> Any:
    """LiveKit's WarmTransferTask, awaited inside the tool call (it becomes the active task)."""
    from livekit.agents.beta.workflows import WarmTransferTask

    return await WarmTransferTask(
        sip_call_to=dial["number"],
        sip_trunk_id=dial["trunk_id"],
        ringing_timeout=float(dial.get("ringing_timeout_s") or 30),
        instructions=dial["whisper"],
    )


async def run(runtime: Any, result: dict[str, Any], transfer: TransferFn = warm_transfer) -> dict:
    """Carry out an authorised transfer and return what the model should see."""
    dial = result.pop("dial", None)
    handoff_id = result.get("handoff_id")
    if not dial or not handoff_id:
        return {"state": "callback_promised", "message": "Offer to take a message instead."}
    try:
        await transfer(dial)
        outcome, detail = "connected", None
    except Exception as err:  # noqa: BLE001 - every failure falls back to a call-back
        outcome, detail = classify(err), type(err).__name__
        log.info("warm transfer did not connect: %s", outcome)
    report = await runtime.plane.handoff(handoff_id, outcome, detail)
    if outcome == "connected":
        await runtime.handed_over()
        return {"state": "connected"}
    return {
        "state": report.get("state", "callback_promised"),
        "message": report.get("message")
        or "Nobody can take the call right now. Tell the caller the office will call them back.",
    }
