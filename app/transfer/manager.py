"""TransferManager — the one shared budget every WAN byte-mover draws from.

Owns the up/down bucket pair, composes the effective rate, enforces per-origin
etiquette as concurrency, tracks per-endpoint throughput for mirror steering,
and exposes live per-class rates for the dashboard.

EFFECTIVE RATE = min(configured ceiling x schedule factor, Tier-3 override)
                 minus any outstanding rsync rate leases.

Config tiers (CLAUDE.md invariant 10), kept strictly separate:
  Tier 2  platform.toml — the ceilings and the schedule. Desired state.
  Tier 3  the `control` table — "boost bandwidth tonight" (§14). Ephemeral:
          read here, never written back as desired state, gone on restart.

ETIQUETTE IS CONCURRENCY, NOT BANDWIDTH. A per-endpoint semaphore is held for
a whole FILE. Holding a second token bucket across a chunk would spend endpoint
budget while blocked on the global bucket, making the cap stricter than
configured and smearing it against the wire. `Endpoint.max_conns` and
`RateLimit.max_conns` are two Tier-2 declarations of one fact; both are honored
by taking the minimum, alongside the global `[transfer] per_endpoint`.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .bucket import DEFAULT_BURST_SECONDS, Priority, TokenBucket
from .clock import Clock, RealClock

if TYPE_CHECKING:                       # pragma: no cover
    from .. import platform_config


def _load_platform():
    """Imported lazily: app.platform_config depends on transfer.schedule, so a
    module-level import here would close a cycle
    (platform_config -> transfer/__init__ -> manager -> platform_config)."""
    from .. import platform_config as pc
    return pc.load()

_EWMA_ALPHA = 0.3            # weight on the newest observation
_EWMA_HEADROOM = 1.25        # lease a little above measured throughput
_MIN_LEASE_BPS = 64 * 1024   # never lease so little that --bwlimit rounds to 0
_SCHEDULE_RECHECK_SECONDS = 1.0
_RATES_CACHE_SECONDS = 0.5


class Direction(Enum):
    DOWN = "down"
    UP = "up"


@dataclass(frozen=True)
class TransferHandle:
    """What budget.acquire_bytes() needs, carried on a ContextVar.

    ContextVar rather than threading.local so `reset(token)` restores the
    PREVIOUS value: nested binds compose, and a pooled thread never leaks the
    last task's priority into the next task.
    """
    bucket: TokenBucket
    priority: Priority
    stop: threading.Event | None = None
    endpoint_key: str | None = None


# Default when nothing is bound: BACKFILL, explicitly. The enum's zero value is
# INTERACTIVE, so relying on a falsy default would silently give unbound
# callers the highest priority in the system.
_current: ContextVar[TransferHandle | None] = ContextVar("transfer_handle", default=None)


def current_handle() -> TransferHandle | None:
    return _current.get()


class TransferManager:
    def __init__(self, platform: platform_config.PlatformConfig | None = None,
                 clock: Clock | None = None,
                 burst_seconds: float = DEFAULT_BURST_SECONDS):
        self._clock = clock or RealClock()
        self._lock = threading.RLock()
        self._platform = platform if platform is not None else _load_platform()
        bw = self._platform.bandwidth
        self._override_bps: float | None = None      # Tier-3, ephemeral
        self._down = TokenBucket(bw.down_max_bps, clock=self._clock,
                                  burst_seconds=burst_seconds,
                                  reserve_fraction=bw.reserve_interactive, name="down")
        self._up = TokenBucket(bw.up_max_bps, clock=self._clock,
                                burst_seconds=burst_seconds,
                                reserve_fraction=bw.reserve_interactive, name="up")
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._slot_counts: dict[str, int] = {}
        self._ewma: dict[str, float] = {}
        self._rsync_active = 0
        self._last_schedule_check = -1e9
        self._last_factor: float | None = None
        self._rates_cache: tuple[float, dict] | None = None
        self._last_sample: tuple[float, list[float], list[float]] | None = None
        self._stop: threading.Event | None = None
        self._apply_rates_locked()

    # ---------------- configuration ----------------

    @property
    def platform(self) -> platform_config.PlatformConfig:
        return self._platform

    def bucket(self, direction: Direction = Direction.DOWN) -> TokenBucket:
        return self._down if direction is Direction.DOWN else self._up

    def reload_platform(self, platform: platform_config.PlatformConfig | None = None) -> None:
        with self._lock:
            self._platform = platform if platform is not None else _load_platform()
            self._last_factor = None
            self._apply_rates_locked()

    def set_override(self, rate_bps: float | None) -> None:
        """Tier-3 'boost bandwidth tonight'. Ephemeral by construction: it
        lives in memory (and, when the caller chooses, the control table),
        never in platform.toml."""
        with self._lock:
            self._override_bps = None if rate_bps is None else max(0.0, float(rate_bps))
            self._apply_rates_locked()

    def _ceiling(self, configured: int | None) -> float | None:
        """min(configured x schedule, override). None stays None (unlimited)."""
        factor = self._platform.bandwidth.schedule.factor_at(self._clock.wall_minutes())
        scheduled = None if configured is None else configured * factor
        candidates = [c for c in (scheduled, self._override_bps) if c is not None]
        return min(candidates) if candidates else None

    def _apply_rates_locked(self) -> None:
        bw = self._platform.bandwidth
        self._last_factor = bw.schedule.factor_at(self._clock.wall_minutes())
        self._down.set_rate(self._ceiling(bw.down_max_bps))
        self._up.set_rate(self._ceiling(bw.up_max_bps))
        self._last_schedule_check = self._clock.monotonic()

    def maybe_refresh(self) -> None:
        """Cheap enough for the per-chunk hot path: consults the wall clock at
        most once a second, and only touches the buckets when the schedule
        factor actually changed."""
        now = self._clock.monotonic()
        if now - self._last_schedule_check < _SCHEDULE_RECHECK_SECONDS:
            return
        with self._lock:
            if self._clock.monotonic() - self._last_schedule_check < _SCHEDULE_RECHECK_SECONDS:
                return
            self._last_schedule_check = self._clock.monotonic()
            factor = self._platform.bandwidth.schedule.factor_at(self._clock.wall_minutes())
            if factor != self._last_factor:
                self._apply_rates_locked()

    # ---------------- stop ----------------

    def set_stop_event(self, stop: threading.Event | None) -> None:
        self._stop = stop

    def wake_all(self) -> None:
        """Must be called whenever the stop event is set: without it the
        predicate change is inert and every waiter sleeps out its full
        timeout, which would also block interpreter exit via
        concurrent.futures' atexit join."""
        self._down.wake_all()
        self._up.wake_all()

    # ---------------- priority binding ----------------

    @contextmanager
    def bind(self, priority: Priority, direction: Direction = Direction.DOWN,
             stop: threading.Event | None = None, endpoint_key: str | None = None):
        handle = TransferHandle(bucket=self.bucket(direction), priority=priority,
                                 stop=stop, endpoint_key=endpoint_key)
        token = _current.set(handle)
        try:
            yield handle
        finally:
            _current.reset(token)

    # ---------------- per-endpoint etiquette ----------------

    def endpoint_slots(self, endpoint, spec=None) -> int:
        """min(global per_endpoint, Endpoint.max_conns, RateLimit.max_conns).

        Both max_conns fields exist and default to 4; honoring only one would
        be exactly the "etiquette as folklore" failure §5.2 names.
        """
        limits = [self._platform.transfer.per_endpoint]
        endpoint_max = getattr(endpoint, "max_conns", None)
        if endpoint_max:
            limits.append(int(endpoint_max))
        etiquette = getattr(spec, "etiquette", None) if spec is not None else None
        etiquette_max = getattr(etiquette, "max_conns", None)
        if etiquette_max:
            limits.append(int(etiquette_max))
        return max(1, min(limits))

    def endpoint_semaphore(self, key: str, slots: int) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._semaphores.get(key)
            if sem is None or self._slot_counts.get(key) != slots:
                sem = threading.BoundedSemaphore(slots)
                self._semaphores[key] = sem
                self._slot_counts[key] = slots
            return sem

    # ---------------- EWMA throughput (mirror steering) ----------------

    def record_throughput(self, key: str, byte_count: int, seconds: float) -> None:
        if not key or byte_count <= 0 or seconds <= 0:
            return
        observed = byte_count / seconds
        with self._lock:
            prior = self._ewma.get(key)
            self._ewma[key] = (observed if prior is None
                                else _EWMA_ALPHA * observed + (1 - _EWMA_ALPHA) * prior)

    def throughput(self, key: str) -> float | None:
        with self._lock:
            return self._ewma.get(key)

    def rank_endpoints(self, keyed_endpoints: list[tuple[str, object]]) -> list[tuple[str, object]]:
        """Fastest-measured first; unmeasured endpoints keep their declared
        order (spec order is mirrors-first, so a cold start is still polite)."""
        with self._lock:
            ewma = dict(self._ewma)

        def sort_key(item):
            key, _ = item
            measured = ewma.get(key)
            # measured endpoints sort ahead of unmeasured ones, fastest first
            return (0, -measured) if measured is not None else (1, 0.0)

        return sorted(keyed_endpoints, key=sort_key)

    # ---------------- rsync rate leases ----------------

    @contextmanager
    def rsync_lease(self, endpoint_key: str | None = None,
                    direction: Direction = Direction.DOWN):
        """Withdraw refill rate for an rsync subprocess we cannot meter
        per-byte, sized by MEASURED throughput rather than a naive fair share.

        Leasing a 200 Mbit/s fair share to a mirror that only delivers 3 Mbit/s
        would withhold the difference from every other transport and simply
        waste it — under-utilisation turning a safety mechanism into a
        self-inflicted throttle. Released in a finally, including on timeout,
        failure and stop: a leaked lease leaves the budget permanently short.
        """
        bucket = self.bucket(direction)
        with self._lock:
            self._rsync_active += 1
            bps = self._lease_size_locked(bucket, endpoint_key)
        lease = bucket.lease_rate(bps) if bps is not None else None
        try:
            yield bps
        finally:
            if lease is not None:
                lease.release()
            with self._lock:
                self._rsync_active = max(0, self._rsync_active - 1)

    def _lease_size_locked(self, bucket: TokenBucket, endpoint_key: str | None) -> float | None:
        rate = bucket.core.base_rate
        if rate is None:
            return None                       # unlimited budget -> no --bwlimit at all
        fair_share = rate / max(1, self._rsync_active)
        measured = self._ewma.get(endpoint_key) if endpoint_key else None
        if measured is not None:
            fair_share = min(fair_share, measured * _EWMA_HEADROOM)
        return max(float(_MIN_LEASE_BPS), fair_share)

    # ---------------- live rates for the dashboard ----------------

    def rates(self) -> dict:
        """Per-class bytes/second since the previous sample, both directions.

        Cached briefly so several SSE subscribers don't each reset the sampling
        window and see zeros.
        """
        now = self._clock.monotonic()
        with self._lock:
            if self._rates_cache is not None and now - self._rates_cache[0] < _RATES_CACHE_SECONDS:
                return self._rates_cache[1]
            _, down_granted = self._down.granted_snapshot()
            _, up_granted = self._up.granted_snapshot()
            previous = self._last_sample
            self._last_sample = (now, list(down_granted), list(up_granted))
            if previous is None or now <= previous[0]:
                payload = self._empty_rates()
            else:
                elapsed = now - previous[0]
                payload = {
                    "down": {p.name.lower(): round((down_granted[p] - previous[1][p]) / elapsed, 1)
                             for p in Priority},
                    "up": {p.name.lower(): round((up_granted[p] - previous[2][p]) / elapsed, 1)
                           for p in Priority},
                }
            payload["limits"] = {
                "down": self._down.core.rate,
                "up": self._up.core.rate,
                "schedule": self._platform.bandwidth.schedule_text or "always full",
                "schedule_factor": self._last_factor,
                "override": self._override_bps,
            }
            self._rates_cache = (now, payload)
            return payload

    @staticmethod
    def _empty_rates() -> dict:
        zeros = {p.name.lower(): 0.0 for p in Priority}
        return {"down": dict(zeros), "up": dict(zeros)}


# ---------------- process-wide singleton ----------------

_manager: TransferManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> TransferManager:
    """Lazily constructed, mirroring refs.get_store()'s single-call-site
    pattern so config is read once per process rather than per transport."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TransferManager()
    return _manager


def set_manager(manager: TransferManager | None) -> None:
    """Test seam: inject a manager with a fake clock / synthetic config."""
    global _manager
    with _manager_lock:
        _manager = manager
