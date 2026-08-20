"""Measured throughput per collection — how fast this node actually fetches
and embeds, as opposed to how fast anyone hoped it would.

Everything here is a MEASUREMENT of completed work. Nothing is modelled, and
nothing is extrapolated from a constant: the acquisition planner's
`_DRY_RUN_ASSUMED_RATE_BPS` exists because there was nowhere to read a real
rate from, and this module is that place.

TWO RATES, DELIBERATELY, because they answer different questions:

  encoder throughput  = work / time spent INSIDE the call
      What the embedding endpoint is worth. Independent of whether the
      worker had anything else to do, so it is the number to compare a CPU
      encoder against a GPU one — the reason this module exists.

  wall-clock rate     = work / elapsed time since the oldest sample
      What actually determines when a backfill finishes. Always lower,
      because embedding is GPU-serial across collections by doctrine and a
      collection spends most of its life waiting its turn.

The gap between them is not noise: it is exactly the cost of contention, and
an operator deciding whether to buy a second encoder should be able to see
that the first one is idle half the time before spending money on it.

MEASURED IN CHARACTERS, not chunks. Chunk size is per collection
(genome runs ctx=1024, gutenberg ctx=8192), so chunks/second would rank a
small-context encoder ahead of a large-context one for doing less work per
chunk. Characters are the same unit everywhere.

In-process and deliberately not persisted: a rate is a statement about now.
After a restart the honest answer is "no measurement yet", not a number
inherited from a run that used a different endpoint.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Bounds on the rolling window. Whichever is reached first wins, so a fast
# encoder is measured over a short recent window and a slow one still has
# enough samples to say anything at all.
MAX_EVENTS = 400
MAX_AGE_S = 2 * 3600.0
# Below this many samples the numbers are too jumpy to publish. Reporting
# "measuring" is more useful than reporting one document's luck.
MIN_EVENTS = 3


@dataclass
class _Event:
    at: float
    seconds: float          # time spent inside the call
    units: dict[str, float]  # docs / chunks / chars, or bytes


@dataclass
class _Stream:
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))

    def add(self, seconds: float, units: dict) -> None:
        self.events.append(_Event(time.time(), max(0.0, seconds), units))

    def prune(self, now: float) -> None:
        while self.events and now - self.events[0].at > MAX_AGE_S:
            self.events.popleft()

    def summary(self, now: float) -> dict | None:
        self.prune(now)
        if len(self.events) < MIN_EVENTS:
            return None
        totals: dict[str, float] = {}
        busy = 0.0
        for e in self.events:
            busy += e.seconds
            for k, v in e.units.items():
                totals[k] = totals.get(k, 0.0) + v
        span = self.events[-1].at - self.events[0].at
        return {
            "totals": totals,
            "busy_seconds": round(busy, 3),
            # The span covers the gaps BETWEEN samples, which is the point:
            # it is wall-clock progress, contention included.
            "span_seconds": round(max(span, 0.0), 3),
            "samples": len(self.events),
            "last_at": self.events[-1].at,
            "age_seconds": round(now - self.events[-1].at, 1),
        }


class Recorder:
    """Thread-safe: embed runs on the main loop, fetch on per-collection
    acquire threads, and the snapshot is taken from a third."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._embed: dict[str, _Stream] = {}
        self._fetch: dict[str, _Stream] = {}
        # Documents inside the encoder at this instant. A document commits
        # only when every one of its chunks is done, so on a slow CPU encoder
        # several can be in flight for many minutes — and counting those as
        # "awaiting the encoder" tells an operator nothing is happening while
        # the encoder is at 300% CPU.
        self._in_flight: dict[str, int] = {}

    def record_embed(self, collection: str, *, seconds: float, docs: int,
                     chunks: int, chars: int) -> None:
        with self._lock:
            self._embed.setdefault(collection, _Stream()).add(
                seconds, {"docs": docs, "chunks": chunks, "chars": chars})

    def record_fetch(self, collection: str, *, seconds: float, bytes_moved: int) -> None:
        """Bytes that actually crossed the network. Adopted files are
        deliberately NOT recorded: counting a verify-only pass as transfer
        would report an infinite download speed and poison every ETA on the
        page."""
        if bytes_moved <= 0:
            return
        with self._lock:
            self._fetch.setdefault(collection, _Stream()).add(
                seconds, {"bytes": bytes_moved})

    def set_in_flight(self, collection: str, n: int) -> None:
        with self._lock:
            if n:
                self._in_flight[collection] = n
            else:
                self._in_flight.pop(collection, None)

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            names = set(self._embed) | set(self._fetch) | set(self._in_flight)
            out = {}
            for name in names:
                entry = {}
                if self._in_flight.get(name):
                    entry["in_flight"] = self._in_flight[name]
                embed = self._embed.get(name)
                fetch = self._fetch.get(name)
                if embed is not None and (s := embed.summary(now)):
                    entry["embed"] = s
                if fetch is not None and (s := fetch.summary(now)):
                    entry["fetch"] = s
                if entry:
                    out[name] = entry
            return out

    def reset(self) -> None:
        with self._lock:
            self._embed.clear()
            self._fetch.clear()
            self._in_flight.clear()


_recorder = Recorder()


def recorder() -> Recorder:
    return _recorder


# ---------------- derived figures, shared by the page and its tests ----------

def rates(entry: dict | None) -> dict:
    """Turn one collection's raw sample summary into the numbers a human
    reads. Returns {} when there is nothing measured — never a zero, which
    would be indistinguishable from a genuinely stalled stage."""
    out: dict = {}
    if not entry:
        return out

    if entry.get("in_flight"):
        out["in_flight"] = entry["in_flight"]

    embed = entry.get("embed")
    if embed:
        totals = embed["totals"]
        busy = embed["busy_seconds"]
        span = embed["span_seconds"]
        out["embed"] = {
            "samples": embed["samples"],
            "age_seconds": embed["age_seconds"],
            "docs": int(totals.get("docs", 0)),
            "chunks": int(totals.get("chunks", 0)),
            # what the ENDPOINT is worth
            "chars_per_s": _div(totals.get("chars"), busy),
            "chunks_per_s": _div(totals.get("chunks"), busy),
            "ms_per_chunk": _div(busy * 1000.0, totals.get("chunks")),
            # what the QUEUE actually drains at
            "docs_per_hour": _div(totals.get("docs", 0) * 3600.0, span),
            # how much of the time this collection had the encoder at all
            "duty": _div(busy, span),
        }

    fetch = entry.get("fetch")
    if fetch:
        totals = fetch["totals"]
        out["fetch"] = {
            "samples": fetch["samples"],
            "age_seconds": fetch["age_seconds"],
            "bytes_per_s": _div(totals.get("bytes"), fetch["busy_seconds"]),
            "bytes_per_s_wall": _div(totals.get("bytes"), fetch["span_seconds"]),
        }
    return out


def eta_seconds(remaining: float, per_second: float | None) -> float | None:
    """None when it cannot be known — an unmeasured rate, a zero rate, or
    nothing left to do. A page that prints "0s remaining" for a stage it has
    never seen run is worse than one that prints nothing."""
    if not per_second or per_second <= 0 or remaining <= 0:
        return None
    return remaining / per_second


def _div(numerator, denominator) -> float | None:
    if not numerator or not denominator:
        return None
    return numerator / denominator
