"""The shared token bucket (architecture-v2.md §5.2, CLAUDE.md invariant 5).

Split in two on purpose:

  BucketCore  — a pure, synchronous decision core. No locks, no threads, no
                clock; `now` is a parameter. Every property worth asserting
                tightly (refill arithmetic, the reserve floor, strict-priority
                gating, rate changes, schedule flips, long-run goodput) is a
                property of this class and is tested with zero threads and
                zero elapsed seconds.

  TokenBucket — a thin blocking wrapper: one condition variable, per-class
                waiter counts, and Clock.wait. It contributes concurrency, not
                policy.

POLICY: strict priority (interactive > delta > backfill > seeding) with an
interactive reserve. A class is served only when no higher-priority class is
waiting, so seeding gets pure leftover. This is unobservable unless the link
is saturated — below budget, tokens sit at capacity and every acquire returns
immediately.

TWO ARITHMETIC RULES THAT ARE EASY TO GET WRONG, both invariant-5 relevant:

  1. Wait against the ceiling, charge the truth. A waiter blocks until
     `tokens - floor >= min(n, capacity - floor)`, but is then charged the
     FULL n, so tokens may go negative (debt repaid by refill). Requiring
     `tokens >= n + reserve` instead would deadlock forever whenever
     `n > capacity - reserve` — with 64 KiB chunks and a 20% reserve that is
     every rate below ~1.25 Mbit/s, i.e. the "else 30%" schedule branch on a
     modest link. And clamping the DEDUCTION instead would silently under-count
     bytes already written to disk, letting goodput exceed the configured rate
     by ~60% at low rates.

  2. A rate change refills at the OLD rate first. Otherwise the whole elapsed
     interval is retroactively repriced at the new rate, which mis-accounts
     every schedule-window boundary.

Accounting is pay-after: transports call budget.acquire_bytes(n) once the
bytes are already written and fsynced (that is what makes the abort path
truthful — see app/acquire/budget.py). So the guarantee is a LONG-RUN AVERAGE
within tolerance, not an instantaneous ceiling: the first chunk of each
in-flight file is unmetered, and kernel socket buffers are invisible to any
userspace shaper.
"""
import threading
from enum import IntEnum

from .clock import Clock, RealClock

# Bound on how long a single wait sleeps before re-checking. Not polling: a
# notify_all() on every state change (grant, departure, rate change, stop) is
# the primary wakeup. This only bounds the damage of a missed notify, and so
# bounds worst-case stop latency.
_MAX_WAIT = 1.0
_MIN_WAIT = 1e-4

DEFAULT_BURST_SECONDS = 0.5


class Priority(IntEnum):
    """Lower value = higher priority (architecture-v2.md §5.2's ordering)."""
    INTERACTIVE = 0    # interactive queries & lazy-p2p fetches
    DELTA = 1          # delta sync
    BACKFILL = 2       # backfill acquisition (the corpus)
    ARCHIVE = 3        # pre-embedded vector archives (amendment 38): useless
                       # without the corpus, so always behind it
    SEEDING = 4        # leftover only, never reserved


class TransferAborted(Exception):
    """The stop event fired while waiting for budget. Translated into
    app.acquire.spec.TransferAborted (a TransportError) at the acquire-layer
    boundary so the engine's existing per-file handler catches it."""


class BucketCore:
    """Pure decision core. `rate_bps is None` means unlimited."""

    def __init__(self, rate_bps: float | None, now: float = 0.0,
                 burst_seconds: float = DEFAULT_BURST_SECONDS,
                 reserve_fraction: float = 0.0):
        if not 0.0 <= reserve_fraction < 1.0:
            raise ValueError(f"reserve_fraction must be in [0, 1), got {reserve_fraction}")
        self._burst_seconds = float(burst_seconds)
        self._reserve_fraction = float(reserve_fraction)
        self._base_rate = None if rate_bps is None else max(0.0, float(rate_bps))
        self._leased = 0.0
        self._last_refill = float(now)
        self._recompute()
        self.tokens = self.capacity
        self.granted = [0.0] * len(Priority)   # cumulative bytes, indexed by Priority

    # ---------------- rate ----------------

    def _recompute(self) -> None:
        if self._base_rate is None:
            self.rate = None
            self.capacity = 0.0
            self.reserve = 0.0
            return
        self.rate = max(0.0, self._base_rate - self._leased)
        self.capacity = self.rate * self._burst_seconds
        self.reserve = self.capacity * self._reserve_fraction
        if getattr(self, "tokens", 0.0) > self.capacity:
            self.tokens = self.capacity

    def set_rate(self, rate_bps: float | None, now: float) -> None:
        self.refill(now)                # (1) settle elapsed time at the OLD rate
        self._base_rate = None if rate_bps is None else max(0.0, float(rate_bps))
        self._recompute()

    def lease(self, bps: float, now: float) -> None:
        """Withdraw refill rate for a subprocess we cannot meter per byte
        (rsync). Not a token withdrawal: rsync consumes a RATE for its
        lifetime, not a fixed number of bytes."""
        self.refill(now)
        self._leased += max(0.0, float(bps))
        self._recompute()

    def release(self, bps: float, now: float) -> None:
        self.refill(now)
        self._leased = max(0.0, self._leased - max(0.0, float(bps)))
        self._recompute()

    @property
    def leased(self) -> float:
        return self._leased

    @property
    def base_rate(self) -> float | None:
        return self._base_rate

    # ---------------- tokens ----------------

    def refill(self, now: float) -> None:
        elapsed = float(now) - self._last_refill
        self._last_refill = float(now)
        if self.rate is None or elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def floor_for(self, priority: Priority) -> float:
        """Interactive may drain to empty; everyone else must leave the
        reserve, so a query burst finds headroom without waiting for an
        in-flight backfill chunk to drain."""
        return 0.0 if priority is Priority.INTERACTIVE else self.reserve

    def can_grant(self, n: int, priority: Priority) -> bool:
        if self.rate is None:
            return True
        if self.rate <= 0:
            return False        # schedule says off — block until the rate changes
        floor = self.floor_for(priority)
        need = min(float(n), self.capacity - floor)
        return self.tokens - floor >= need

    def wait_time(self, n: int, priority: Priority) -> float:
        """Seconds until can_grant() would be true. Never 0, never None, never
        inf — a non-advancing wait is a spin under a real clock and an infinite
        loop at zero simulated time under a fake one."""
        if self.rate is None:
            return _MIN_WAIT
        if self.rate <= 0:
            return _MAX_WAIT    # waiting on a rate change; notify_all wakes us sooner
        floor = self.floor_for(priority)
        need = min(float(n), self.capacity - floor)
        deficit = need - (self.tokens - floor)
        if deficit <= 0:
            return _MIN_WAIT
        return max(_MIN_WAIT, min(_MAX_WAIT, deficit / self.rate))

    def take(self, n: int, priority: Priority) -> None:
        """Charge the TRUE n. May drive tokens negative; the debt is repaid by
        refill, which is exactly what keeps the long-run average honest."""
        if self.rate is not None:
            self.tokens -= float(n)
        self.granted[priority] += float(n)

    # ---------------- arbitration ----------------

    def step(self, now: float, demands: dict[Priority, int]) -> dict[Priority, int]:
        """Advance to `now`, then arbitrate one round of competing demands.

        Strict priority: classes are served in order, and the first class whose
        demand cannot be met in full starves every lower class this round.
        Returns bytes granted per class.
        """
        self.refill(now)
        granted: dict[Priority, int] = {p: 0 for p in demands}
        for priority in sorted(demands):
            want = demands[priority]
            if want <= 0:
                continue
            if self.rate is None:
                granted[priority] = want
                self.granted[priority] += want
                continue
            available = self.tokens - self.floor_for(priority)
            give = int(max(0.0, min(float(want), available)))
            if give > 0:
                self.tokens -= give
                self.granted[priority] += give
                granted[priority] = give
            if give < want:
                break        # backlogged: lower classes get nothing this round
        return granted


class TokenBucket:
    """Blocking wrapper over BucketCore. Contributes concurrency, not policy."""

    def __init__(self, rate_bps: float | None, clock: Clock | None = None,
                 burst_seconds: float = DEFAULT_BURST_SECONDS,
                 reserve_fraction: float = 0.0, name: str = ""):
        self._clock = clock or RealClock()
        self._cv = threading.Condition()
        self._core = BucketCore(rate_bps, now=self._clock.monotonic(),
                                 burst_seconds=burst_seconds,
                                 reserve_fraction=reserve_fraction)
        self._waiting = [0] * len(Priority)
        self.name = name

    # ---------------- introspection ----------------

    @property
    def core(self) -> BucketCore:
        """Direct core access for tests and for the manager's stats sampling.
        Callers that mutate it must hold `lock`."""
        return self._core

    @property
    def lock(self) -> threading.Condition:
        return self._cv

    def granted_snapshot(self) -> tuple[float, list[float]]:
        with self._cv:
            return self._clock.monotonic(), list(self._core.granted)

    def note_granted(self, n: int, priority: Priority) -> None:
        """Count bytes that MOVED OUTSIDE the token flow — rsync's subprocess,
        which is paced by a rate lease (--bwlimit) rather than per-chunk
        draws. Tokens are untouched (the lease already withdrew budget; with
        an unlimited budget there is nothing to withdraw): this is
        accounting, not arbitration. Without it the Network page's per-class
        graph read zero whenever rsync was the thing moving bytes — which on
        the reference node is gutenberg, the biggest mover."""
        if n <= 0:
            return
        with self._cv:
            self._core.granted[priority] += float(n)

    # ---------------- rate control ----------------

    def set_rate(self, rate_bps: float | None) -> None:
        with self._cv:
            self._core.set_rate(rate_bps, self._clock.monotonic())
            self._cv.notify_all()      # capacity may have shrunk below a waiter's need

    def lease_rate(self, bps: float) -> "RateLease":
        with self._cv:
            self._core.lease(bps, self._clock.monotonic())
            self._cv.notify_all()
        return RateLease(self, bps)

    def _release_lease(self, bps: float) -> None:
        with self._cv:
            self._core.release(bps, self._clock.monotonic())
            self._cv.notify_all()

    def wake_all(self) -> None:
        """Called when a stop event is set. Without this the predicate change
        is inert and every waiter sleeps out its full timeout — and
        concurrent.futures' atexit hook would then block interpreter exit."""
        with self._cv:
            self._cv.notify_all()

    # ---------------- the hot path ----------------

    def acquire(self, n: int, priority: Priority = Priority.BACKFILL,
                stop: threading.Event | None = None) -> None:
        """Block until `n` bytes of budget are available at `priority`.

        Raises TransferAborted if `stop` is set. There is deliberately NO fast
        path that skips the priority gate: a late low-priority arrival must not
        take tokens a sleeping interactive waiter is owed, or preemption
        becomes nondeterministic under real concurrency.
        """
        if n <= 0:
            return
        # THE STOP IS CHECKED BEFORE THE UNLIMITED BRANCH, not only inside
        # the wait loop below. With no rate set — the default when no
        # platform.toml exists — the branch below returned without ever
        # looking at `stop`, so every transport that sees a stop ONLY through
        # this bucket (HTTPS, a segmented range fetch, the genesis archive
        # fetcher) was blind to it: a 3 GB archive shard or a Wikipedia dump
        # streamed to completion through a node pause, and through SIGTERM,
        # and was recorded fetched. Found by adversarial review 2026-09-13;
        # a probe in the dev image confirmed it (unlimited + stop set:
        # returned; limited + stop set: raised). Every consumer already
        # handles TransferAborted, because the limited path always raised it.
        if stop is not None and stop.is_set():
            raise TransferAborted(
                f"transfer aborted before budget ({n} bytes, "
                f"{priority.name.lower()})")
        with self._cv:
            if self._core.rate is None:
                # Unlimited: the default when no platform.toml exists, so
                # existing deployments see no behavior change. This is not the
                # "fast path" B-6 warns about — with no scarce tokens there is
                # nothing to arbitrate and nobody can ever be waiting.
                self._core.take(n, priority)
                return
            self._waiting[priority] += 1
            try:
                while True:
                    if stop is not None and stop.is_set():
                        raise TransferAborted(
                            f"transfer aborted while waiting for budget "
                            f"({n} bytes, {priority.name.lower()})")
                    self._core.refill(self._clock.monotonic())
                    if not self._waiting_above(priority) and self._core.can_grant(n, priority):
                        self._core.take(n, priority)
                        self._cv.notify_all()
                        return
                    self._clock.wait(self._cv, self._wait_timeout(n, priority))
            finally:
                # A departure for ANY reason (grant, abort, exception) is what
                # unblocks lower classes. notify_all, never notify: the
                # predicate is heterogeneous, so notify() is a lost wakeup.
                self._waiting[priority] -= 1
                self._cv.notify_all()

    def _waiting_above(self, priority: Priority) -> bool:
        return any(self._waiting[q] for q in range(int(priority)))

    def _wait_timeout(self, n: int, priority: Priority) -> float:
        if self._waiting_above(priority):
            # Blocked purely by priority: no token-derived deadline exists, so
            # cap it. notify_all() on the higher waiter's departure is the real
            # wakeup; this is only a backstop.
            return 0.05
        return self._core.wait_time(n, priority)


class RateLease:
    """A slice of refill rate withdrawn for a subprocess we cannot meter
    per-byte. MUST be released in a finally — a leaked lease leaves the budget
    permanently short for the life of the process."""

    __slots__ = ("_bucket", "bps", "_released")

    def __init__(self, bucket: TokenBucket, bps: float):
        self._bucket = bucket
        self.bps = bps
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._bucket._release_lease(self.bps)

    def __enter__(self) -> "RateLease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
