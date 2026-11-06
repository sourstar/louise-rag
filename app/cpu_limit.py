"""The CPU applier: the node profile's `cpu_cores` (app/node_profile.py,
docs/notes/node-profiles-design.md, operator 2026-10-27), applied to the parse
pools — the records build's (`parallel.compute_pages`) and the worker's
(`parallel.compute`, and the pipeline's parse stages).

What it limits is the WORK IN FLIGHT, never the pool: parts submitted and not
answered are kept at `min(cores, the pool's window)`, re-read at each page or
unit boundary. The executor keeps its children — an idle child costs no CPU,
and recreating a pool is where the past hangs were (gh-115634,
docs/notes/scheduling.md pitfall 19). `0` is a pause at the end of the unit:
what is in flight finishes and is committed by its caller's own commit, then
the caller waits here, in a bounded sleep that honours its stop, until a
profile allows cores again.

Read once per TTL, not per document (CLAUDE.md: a cost never scales with the
corpus): the file is parsed and the schedule resolved at most every `TTL_S`,
and sooner only when the profile in force says it ends sooner. An unreadable
file never stops a build or a stage — it is said, and the last good limit is
kept (deferral, not failure).

The profile's MODE rides on the same read (node-profiles-design.md decision
6, operator 2026-11-02: processing suspended in the quiet windows): `Limit.mode`
is what `node_state.runs` intersects with the node's own mode, so one file
read per TTL answers both and a bad file keeps the last good mode too.

This is the reconciliation loop of the design note (Kubernetes' pattern): each
reader compares the desired limit with what it does at its next boundary. The
kernel's own mechanism for the same job is cgroups v2 `cpu.max`; the container
keeps its fixed ceiling there (decision 2), and a writable delegated cgroup per
pool would make this kernel-enforced — not done, as TrueNAS gives the app no
cgroup delegation (scheduling.md §11)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

#: how long a read of the profile is trusted: an edit to platform.toml, or a
#: schedule boundary the cache did not foresee, is seen within this
TTL_S = 30.0
#: the pause's sleep between two looks at the profile and the stop
PAUSE_TICK_S = 1.0


@dataclass(frozen=True)
class Limit:
    cores: int | None                 # None: no limit; 0: paused; N: at most N in flight
    profile: str | None = None        # the profile in force; None when the node declares none
    until: datetime | None = None     # when it ends
    error: str | None = None          # the last read failed: the rest is the last good one
    mode: str | None = None           # the profile's node mode; None when the node declares none

    @property
    def suspends(self) -> bool:
        """The profile holds the node to less than running."""
        return self.mode is not None and self.mode != "running"

    def says(self) -> str:
        """One line: "profile quiet: processing suspended until 08:00 (…)",
        "profile day: 8 cores"."""
        if self.profile is None:
            what = "no profile: no core limit"
        elif self.suspends:
            until = f" until {_when(self.until)}" if self.until else ""
            what = f"profile {self.profile}: processing suspended{until} ({_GOES_ON.get(self.mode, 'mode ' + str(self.mode))})"
        elif self.cores is None:
            what = f"profile {self.profile}: no core limit"
        elif self.cores == 0:
            what = f"profile {self.profile}: paused" + (f" until {_when(self.until)}" if self.until else "")
        else:
            what = f"profile {self.profile}: {self.cores} core{'s' if self.cores != 1 else ''}"
        if self.error:
            return f"profile unreadable ({self.error}): keeping {what}"
        return what


NO_PROFILE = Limit(None)

#: what a suspending profile's mode leaves running, in the log's words
_GOES_ON = {"acquire-only": "downloads, seeding and searches go on",
            "seed-only": "seeding and searches go on",
            "paused": "searches go on"}


def _when(t: datetime) -> str:
    return f"{t:%H:%M}" if t.date() == datetime.now().date() else f"{t:%a %H:%M}"


_lock = threading.Lock()
#: platform.toml's path -> (monotonic expiry, limit): keyed on the file, never
#: on a name (CLAUDE.md: caches key on physical identity)
_cache: dict[str, tuple[float, Limit]] = {}
_good: dict[str, Limit] = {}


def reset() -> None:
    with _lock:
        _cache.clear()
        _good.clear()


def _key() -> str:
    from . import platform_config
    return str(platform_config.platform_path())


def _read(key: str) -> tuple[Limit, float]:
    """The limit now, and how long it may be trusted."""
    from . import node_profile
    try:
        a = node_profile.active()
    except Exception as e:   # noqa: BLE001 — a bad file is said and the last limit kept
        with _lock:
            last = _good.get(key, NO_PROFILE)
        return replace(last, error=f"{type(e).__name__}: {e}"[:300]), TTL_S
    if a is None:
        got, ttl = NO_PROFILE, TTL_S
    else:
        got = Limit(a.profile.cpu_cores, a.profile.name, a.until, mode=a.profile.mode)
        ttl = TTL_S
        if a.until is not None:
            # the boundary the schedule names, on time rather than up to a TTL late
            ttl = max(0.0, min(TTL_S, a.until.timestamp() - time.time()))
    with _lock:
        _good[key] = got
    return got, ttl


def current() -> Limit:
    """The background CPU limit now, from a cache trusted for TTL_S."""
    key = _key()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now < hit[0]:
            return hit[1]
    got, ttl = _read(key)
    with _lock:
        _cache[key] = (time.monotonic() + ttl, got)
    return got


def cap(window: int, lim: Limit, floor: int = 0) -> int:
    """`window` (what the pool would admit) under the limit: at most `cores`
    in flight, and never fewer than `floor`."""
    return window if lim.cores is None else max(floor, min(window, lim.cores))


class Watch:
    """One reader's view of the limit, saying each change of it once: a
    build's log, a collection's pipeline (every log line names its
    collection, so each keeps its own watch). The first look is said only
    when there is a profile to speak of."""

    _UNSEEN = object()

    def __init__(self, say: Callable[[str], None] | None = None) -> None:
        self._say = say
        self._last: object = self._UNSEEN
        self._lock = threading.Lock()

    def see(self) -> tuple[Limit, str | None]:
        """The limit now, and the line to say when it changed since the last look."""
        lim = current()
        key = (lim.cores, lim.profile, lim.until if lim.cores == 0 or lim.suspends else None,
               lim.error, lim.mode)
        with self._lock:
            first, changed = self._last is self._UNSEEN, key != self._last
            self._last = key
        if not changed or (first and lim.profile is None and lim.error is None):
            return lim, None
        return lim, lim.says()

    def limit(self) -> Limit:
        lim, line = self.see()
        if line is not None and self._say is not None:
            self._say(line)
        return lim


def stopped(stop) -> bool:
    """An event's `is_set()`, or a callable's answer; None never stops."""
    if stop is None:
        return False
    is_set = getattr(stop, "is_set", None)
    return bool(is_set() if is_set is not None else stop())


def wait_while_paused(stop=None, watch: Watch | None = None) -> bool:
    """A paused caller's wait, at its unit boundary: True when a profile
    allows cores again, False when `stop` (SIGTERM, the node's mode, the
    collection's removal) is set first. Bounded: it looks every
    PAUSE_TICK_S, so a stop is honoured within a tick. `stop` is an event
    (anything with `is_set`) or a callable answering "stop now?"."""
    watch = watch or Watch()
    while True:
        if stopped(stop):
            return False
        if watch.limit().cores != 0:
            return True
        waiter = getattr(stop, "wait", None)
        if waiter is not None:
            waiter(PAUSE_TICK_S)
        else:
            time.sleep(PAUSE_TICK_S)
