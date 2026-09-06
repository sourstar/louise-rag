"""Units sized by measured DURATION, not by a constant count.

docs/notes/scheduling.md §10 proposal 4, and the precondition for the rest of
the scheduling model: a slot can only be handed back at a unit boundary,
so how long a unit takes decides how fine-grained "release early" (B3)
and "reclaim on your next turn" (B2) can possibly be.

WHY COUNTS ARE THE WRONG UNIT. `FTS_BATCH_DOCS=200` means 0.4 s of work
for pubmed and 14-37 s for gutenberg — the same setting buying two
different things, and neither one chosen. Every fixed count in a pipeline
that serves corpora of different shapes has this property. A target
DURATION is the same thing everywhere: it says what the scheduler
actually cares about, which is how long the thread is committed before it
can yield.

HOW. An exponentially weighted average of seconds-per-unit per key, and
the size that would hit the target at that rate — clamped, because a
measurement can be wrong and the clamp is what stops it being wrong
expensively. Before there is any measurement, the configured default
stands: this never GUESSES a size, it only corrects one that has been
observed.

TWO SHAPES, because the pipeline has two:

  size()   for work measured in items — a batch of N documents. The rate
           is seconds per document.
  window() for work measured in a RANGE that may be mostly empty — the
           re-index walk over a rowid window, where cost depends on how
           many rows the window happens to contain, not on its width.
           A rate is meaningless there, so the width is adjusted
           multiplicatively from the last probe: over target, halve;
           comfortably under, grow by a quarter. AIMD, for the same
           reason TCP uses it — it converges without needing a model of
           what makes the window expensive.

In-process and not persisted, like app/stats.py: a rate is a statement
about now, and after a restart the honest starting point is the
configured default rather than a number inherited from a run that had a
different corpus in front of it.
"""
from __future__ import annotations

import threading

# Weight of the newest sample. Low enough that one unlucky batch does not
# resize the next, high enough to follow a corpus that changes shape
# (gutenberg's documents get bigger as the ids get higher).
ALPHA = 0.25
# Below this many samples the average is one batch's luck (stats.MIN_EVENTS
# reasoning). The default stands until then.
MIN_SAMPLES = 3


class Sizer:
    """Thread-safe: pipeline threads size their own batches, the stats
    thread reads the snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rate: dict[str, float] = {}        # key -> seconds per unit
        self._samples: dict[str, int] = {}
        self._window: dict[str, float] = {}      # key -> current width

    # ------------------------------------------------------------ batches

    def record(self, key: str, units: int, seconds: float) -> None:
        """What a unit of this kind actually cost."""
        if units <= 0 or seconds < 0:
            return
        per = seconds / units
        with self._lock:
            prev = self._rate.get(key)
            self._rate[key] = per if prev is None else (1 - ALPHA) * prev + ALPHA * per
            self._samples[key] = self._samples.get(key, 0) + 1

    def size(self, key: str, *, target_s: float, default: int,
             lo: int = 1, hi: int | None = None) -> int:
        """How many units to take so the batch lasts about `target_s`."""
        with self._lock:
            per = self._rate.get(key)
            enough = self._samples.get(key, 0) >= MIN_SAMPLES
        if not per or not enough:
            return default
        n = int(target_s / per) if per > 0 else default
        hi = hi if hi is not None else default * 20
        return max(lo, min(hi, n))

    # ------------------------------------------------------------- windows

    def window(self, key: str, *, default: int, lo: int, hi: int) -> int:
        with self._lock:
            return int(self._window.get(key, default))

    def note_window(self, key: str, *, seconds: float, target_s: float,
                    default: int, lo: int, hi: int) -> int:
        """Report what the last probe of this window cost and get the next
        width. Multiplicative on both sides: a window that is too expensive
        is halved at once (the point is to stop paying it), one that is
        cheap grows gently (the point is not to thrash back)."""
        with self._lock:
            width = self._window.get(key, float(default))
            if seconds > target_s:
                width = width / 2 if seconds > 2 * target_s else width * 0.75
            elif seconds < target_s / 2:
                width = width * 1.25
            width = max(float(lo), min(float(hi), width))
            self._window[key] = width
            return int(width)

    # ----------------------------------------------------------- telemetry

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "rates": {k: round(v, 6) for k, v in self._rate.items()},
                "samples": dict(self._samples),
                "windows": {k: int(v) for k, v in self._window.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._rate.clear()
            self._samples.clear()
            self._window.clear()


_sizer = Sizer()


def sizer() -> Sizer:
    return _sizer
