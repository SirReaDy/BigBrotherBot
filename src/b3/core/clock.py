"""Injectable time source.

The legacy code reached for ``time.time()`` (and, in the event loop, the long-removed
``time.clock()``) directly everywhere, which made time-dependent behaviour — penalty
expiry, auth back-off, cron — impossible to test deterministically. Here time is a
dependency: pass a :class:`FakeClock` in tests, :class:`SystemClock` in production.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Something that can tell the current epoch time in seconds."""

    def now(self) -> float:
        """Return the current time as epoch seconds (float)."""
        ...

    def epoch(self) -> int:
        """Return the current time as integer epoch seconds (DB/penalty timestamps)."""
        ...


class SystemClock:
    """Real wall-clock time."""

    def now(self) -> float:
        return time.time()

    def epoch(self) -> int:
        return int(time.time())


class FakeClock:
    """Deterministic clock for tests. Time only advances when you tell it to."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def epoch(self) -> int:
        return int(self._t)

    def advance(self, seconds: float) -> None:
        self._t += seconds
