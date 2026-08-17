"""Injectable clock — the seam that makes the scheduler testable.

The bucket's *decisions* live in a pure function (bucket.step), so most tests
need no clock at all. What remains here is the thin blocking wrapper: a
condition-variable wait in production, and a deterministic time-advance under
test.

Two distinct time bases, deliberately:
  - monotonic()    — elapsed seconds, for token refill. Never wall time: a
                     clock step (NTP, DST) must not mint or destroy tokens.
  - wall_minutes() — minutes since local midnight, for schedule windows
                     ("01:00-07:00 full; else 30%"), which are wall-clock by
                     definition.

FakeClock derives BOTH from one counter, so a test that time-travels three
simulated hours through waits genuinely crosses a schedule boundary. Deriving
them independently would make window tests impossible to write.
"""
import threading
import time
from typing import Protocol

_MINUTES_PER_DAY = 24 * 60


class Clock(Protocol):
    def monotonic(self) -> float:
        """Elapsed seconds from an arbitrary origin. Only differences matter."""
        ...

    def wall_minutes(self) -> int:
        """Minutes since local midnight, 0..1439."""
        ...

    def wait(self, cv: threading.Condition, timeout: float) -> None:
        """Block on `cv` for at most `timeout` seconds. The caller MUST hold
        cv's lock and MUST re-check its predicate afterwards (spurious and
        early wakeups are both normal)."""
        ...


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_minutes(self) -> int:
        lt = time.localtime()
        return lt.tm_hour * 60 + lt.tm_min

    def wait(self, cv: threading.Condition, timeout: float) -> None:
        # A genuine CV wait: no CPU burn, and notify_all() wakes it early when
        # the rate changes, a waiter departs, or the stop event fires.
        cv.wait(timeout)


class FakeClock:
    """Deterministic time travel for tests.

    `wait()` advances time and returns immediately WITHOUT releasing the lock,
    so it is only meaningful single-threaded — concurrency tests use RealClock
    with tiny byte counts instead. That split is deliberate: the properties
    worth asserting tightly (refill arithmetic, priority gating, the reserve,
    schedule flips) all live in bucket.step, which needs no clock at all.

    Guards against the two ways a fake clock silently hangs a suite: a
    non-positive or non-finite timeout (which would advance zero simulated
    time forever), and an unbounded number of waits in a single acquire.
    There is no pytest-timeout in this repo, so an infinite loop here is a
    permanently hung CI job rather than a failed test.
    """

    def __init__(self, start: float = 0.0, start_wall_minutes: int = 0,
                 max_waits: int = 100_000):
        self._now = float(start)
        self._start_wall_seconds = float(start_wall_minutes) * 60.0
        self._max_waits = max_waits
        self.waits = 0

    def monotonic(self) -> float:
        return self._now

    def wall_minutes(self) -> int:
        total = self._start_wall_seconds + self._now
        return int(total // 60) % _MINUTES_PER_DAY

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        self._now += float(seconds)

    def wait(self, cv: threading.Condition, timeout: float) -> None:
        if timeout is None:
            raise TypeError(
                "FakeClock.wait requires a finite timeout; waiting forever on a "
                "fake clock can never be woken (nothing else advances time)")
        if not (timeout > 0) or timeout == float("inf"):
            raise ValueError(
                f"FakeClock.wait needs a positive finite timeout, got {timeout!r} — "
                "a non-advancing wait would spin at zero simulated time forever")
        self.waits += 1
        if self.waits > self._max_waits:
            raise AssertionError(
                f"FakeClock exceeded {self._max_waits} waits — the caller is almost "
                "certainly not making progress (livelock or an unsatisfiable predicate)")
        self.advance(timeout)
