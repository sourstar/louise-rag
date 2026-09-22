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

HOW. The cost of a batch is AFFINE, not proportional: `fixed + marginal x n`.
The fixed part is real and large — measured on the node, 2026-09-22, over 64
logged batches of wikipedia's rdf and fts: 2.7 s and 4.5 s per batch before
the first document is touched (a tantivy commit, a writer built, a
transaction, a reference store opened), against 41 and 35 ms per document.

THIS MATTERS BECAUSE THE OBVIOUS MODEL DIVERGES. Charging the whole batch to
a per-document rate (`seconds / units`) reports `marginal + fixed/n`, and
sizing from that gives `n' = target x n / (fixed + marginal x n)`. When the
fixed cost approaches the target the next batch is smaller, which makes the
reported rate worse, which makes the batch smaller again — a feedback loop
that ends at the floor, paying the fixed cost once per document it can. With
a 3 s target and a 2.7 s fixed cost that is where it went: batches of 36 to
234 documents where the thread could have taken thousands.

So both terms are fitted, by least squares over the last few batches, and the
size is `(budget - fixed) / marginal`. When the fixed cost alone eats the
target, the target is UNREACHABLE by shrinking — the only way to spend less
of the thread's time on it is to pay it less often — so the budget widens to
`ceiling_s`, which is what actually bounds the unit (and so SIGTERM, and so
how fine-grained a slot hand-back can be). Clamped at both ends, because a
measurement can be wrong and the clamp is what stops it being wrong
expensively. Before there are enough measurements the configured default
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
#: how many (size, duration) pairs the affine fit looks at
SAMPLES_KEPT = 16
#: the fixed cost is "dominant" at this share of the target — above it,
#: shrinking the batch cannot buy the target back, it only pays the fixed
#: cost more often
FIXED_SHARE = 0.5
#: how far past the target a unit may run when the fixed cost dominates.
#: This is the real bound on the unit, and so on SIGTERM and on how
#: fine-grained a slot hand-back can be (scheduling.md §10, B3).
CEILING_MULTIPLE = 4.0


def _affine(samples) -> tuple[float, float | None]:
    """(fixed seconds per batch, seconds per unit) by least squares over
    recent batches. The marginal term is None when every batch was the same
    size — then the two terms cannot be told apart and the caller falls back
    to the plain rate. Both are clamped non-negative: a fit that says a
    bigger batch is cheaper, or that a batch costs less than nothing before
    it starts, is noise, not a discovery."""
    n = len(samples)
    if n < MIN_SAMPLES:
        return 0.0, None
    sx = sum(u for u, _ in samples)
    sy = sum(s for _, s in samples)
    sxx = sum(u * u for u, _ in samples)
    sxy = sum(u * s for u, s in samples)
    denom = n * sxx - sx * sx
    if denom <= 0:                      # every batch the same size
        return 0.0, None
    marginal = (n * sxy - sx * sy) / denom
    fixed = (sy - marginal * sx) / n
    if marginal <= 0:
        return 0.0, None                # noise: fall back to the plain rate
    return max(0.0, fixed), marginal


class Sizer:
    """Thread-safe: pipeline threads size their own batches, the stats
    thread reads the snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rate: dict[str, float] = {}        # key -> seconds per unit
        self._samples: dict[str, int] = {}
        self._window: dict[str, float] = {}      # key -> current width
        #: key -> the last few (units, seconds), for the affine fit
        self._recent: dict[str, list[tuple[int, float]]] = {}

    # ------------------------------------------------------------ batches

    def record(self, key: str, units: int, seconds: float) -> None:
        """What a batch of this kind actually cost — its SIZE and its whole
        duration, because the two terms can only be told apart across batches
        of different sizes."""
        if units <= 0 or seconds < 0:
            return
        per = seconds / units
        with self._lock:
            prev = self._rate.get(key)
            self._rate[key] = per if prev is None else (1 - ALPHA) * prev + ALPHA * per
            self._samples[key] = self._samples.get(key, 0) + 1
            seen = self._recent.setdefault(key, [])
            seen.append((int(units), float(seconds)))
            if len(seen) > SAMPLES_KEPT:
                del seen[0]

    def size(self, key: str, *, target_s: float, default: int,
             lo: int = 1, hi: int | None = None,
             ceiling_s: float | None = None) -> int:
        """How many units to take so the batch lasts about `target_s` — or,
        when the batch's FIXED cost already eats that target, as many as
        `ceiling_s` allows, since paying it less often is then the only way
        to spend less of the thread on it (see the header)."""
        with self._lock:
            per = self._rate.get(key)
            enough = self._samples.get(key, 0) >= MIN_SAMPLES
            seen = list(self._recent.get(key, ()))
        if not per or not enough:
            return default
        hi = hi if hi is not None else default * 20
        fixed, marginal = _affine(seen)
        if marginal is None:                      # every batch the same size:
            n = int(target_s / per) if per > 0 else default   # no way to separate
        else:
            budget = target_s
            if fixed >= target_s * FIXED_SHARE:
                budget = ceiling_s if ceiling_s else target_s * CEILING_MULTIPLE
            n = int((budget - fixed) / marginal) if budget > fixed else lo
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
                "affine": {k: {"fixed_s": round(f, 3), "per_unit_s": round(m, 6)}
                           for k, (f, m) in ((k, _affine(v)) for k, v in self._recent.items())
                           if m is not None},
            }

    def reset(self) -> None:
        with self._lock:
            self._rate.clear()
            self._samples.clear()
            self._window.clear()
            self._recent.clear()


_sizer = Sizer()


def sizer() -> Sizer:
    return _sizer
