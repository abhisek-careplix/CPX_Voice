"""Warm transfer in the worker: the model never sees the dial details, a connected transfer hands
the call over, and anything else comes back as the API's call-back promise."""

import pytest

from careplix_worker import transfer

DIAL = {"number": "+12025550142", "trunk_id": "ST_x", "ringing_timeout_s": 30, "whisper": "W"}


class Plane:
    def __init__(self, answer):
        self.answer, self.reports = answer, []

    async def handoff(self, handoff_id, outcome, detail):
        self.reports.append((handoff_id, outcome, detail))
        return self.answer


class Runtime:
    def __init__(self, answer=None):
        self.plane = Plane(answer or {})
        self.handed = False

    async def handed_over(self):
        self.handed = True


def _result():
    return {"state": "transferring", "handoff_id": "H1", "message": "m", "dial": dict(DIAL)}


async def test_connected_transfer_hands_the_call_over():
    runtime, dialled = Runtime({"state": "connected"}), []

    async def ok(dial):
        dialled.append(dial)

    out = await transfer.run(runtime, _result(), ok)
    assert out == {"state": "connected"}
    assert dialled == [DIAL] and runtime.handed
    assert runtime.plane.reports == [("H1", "connected", None)]


@pytest.mark.parametrize(
    "error,outcome",
    [
        ("could not dial human agent", "no_answer"),
        ("human agent declined to connect: busy", "declined"),
        ("voicemail detected", "voicemail"),
        ("something else", "failed"),
    ],
)
async def test_a_transfer_that_fails_becomes_the_apis_call_back_promise(error, outcome):
    promise = {"state": "callback_promised", "message": "Say the front desk will call back."}
    runtime = Runtime(promise)

    async def fails(_dial):
        raise RuntimeError(error)

    result = _result()
    out = await transfer.run(runtime, result, fails)
    assert out == promise and not runtime.handed
    assert runtime.plane.reports[0][1] == outcome
    assert "dial" not in result and "number" not in str(out)


async def test_without_dial_details_nothing_is_dialled():
    runtime = Runtime()
    out = await transfer.run(runtime, {"state": "transferring"})
    assert out["state"] == "callback_promised" and runtime.plane.reports == []
