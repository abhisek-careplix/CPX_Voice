"""Hold mode (BUILD_PLAN ADR10): sit through a payer's hold queue without spending the call.

When the model marks the call as on hold (``set_call_state(hold)``), the worker:
- mutes the agent's voice, so it never talks to hold music or recorded announcements; it is
  unmuted when the model marks a person as back (``conversation``) or a menu (``ivr``);
- stops counting talk time: the call budget (``max_duration_s``) covers only time off hold, and
  time on hold has its own limit (``max_hold_s``, from the agent's hold policy);
- ends the call if one hold lasts longer than ``max_hold_s`` (reason ``hold_timeout``).

Gemini Live keeps listening throughout, with context-window compression so a long hold doesn't
end the session, and the plugin reconnects with a resumption handle when Google rotates the
connection. Listening costs audio input tokens only (cents per hour).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class HoldClock:
    """Talk time and hold time for one call. ``now`` is injectable for tests."""

    max_duration_s: float
    max_hold_s: float
    now: Callable[[], float] = time.monotonic
    _talk_s: float = 0.0
    _hold_total_s: float = 0.0
    _segment_start: float | None = None
    _hold_start: float | None = None
    holds: int = 0
    _started: bool = field(default=False, repr=False)

    def start(self) -> None:
        self._started = True
        self._segment_start = self.now()

    @property
    def on_hold(self) -> bool:
        return self._hold_start is not None

    def enter_hold(self) -> bool:
        """Returns True if this started a new hold (False if already on hold)."""
        if self.on_hold:
            return False
        t = self.now()
        if self._segment_start is not None:
            self._talk_s += t - self._segment_start
            self._segment_start = None
        self._hold_start = t
        self.holds += 1
        return True

    def leave_hold(self) -> float:
        """Ends the hold; returns how long it lasted (0 if not on hold)."""
        if not self.on_hold:
            return 0.0
        t = self.now()
        lasted = t - self._hold_start
        self._hold_total_s += lasted
        self._hold_start = None
        self._segment_start = t
        return lasted

    def talk_s(self) -> float:
        running = self.now() - self._segment_start if self._segment_start is not None else 0.0
        return self._talk_s + running

    def current_hold_s(self) -> float:
        return self.now() - self._hold_start if self.on_hold else 0.0

    def hold_total_s(self) -> float:
        return self._hold_total_s + self.current_hold_s()

    def exceeded(self) -> str | None:
        """Why the call must end now, or None."""
        if not self._started:
            return None
        if self.on_hold and self.current_hold_s() >= self.max_hold_s:
            return "hold_timeout"
        if self.talk_s() >= self.max_duration_s:
            return "budget_exhausted"
        return None
