"""The PARSE POOL — the GIL-bound per-document work (fetch, parse, chunk,
metadata, pack records) in child processes, so a 12-core node indexes on
twelve cores instead of one (architecture amendment 47).

Measured on the reference node before this: the worker process at 232 %
of twelve cores — ONE Python thread at 87 % (whichever pipeline held the
interpreter lock), Lance's native threads for the rest — and eleven idle
cores, with the operator asking for "close to 100 % when there is not
much else to do".

What stays in the worker: every WRITER (the tantivy index writer, the
Lance table, the manifest transactions, the marks) and the selection —
the pool only computes. A batch of rows is cut into sub-batches, each a
task for one child; results come back in order, one (ok, value) per row,
so a failing document costs itself and never its neighbours, exactly as
the serial path treats a peer that cannot load.

Children are SPAWNED (never forked: the worker holds SQLite connections
and threads, and a forked copy of those is undefined), run at `nice 10`
so encoders, queries and the UI keep priority ("when there is not much
else to do"), and receive the parent's config knobs that shape chunking
so a test's or an operator's override is honoured in the child.
`PARSE_PROCESSES=1` is the in-process serial path — identical results,
no children — used by the test suite except where the pool itself is
under test.
"""
import multiprocessing
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from . import config

# config knobs a child must share with the parent: everything that shapes
# a chunk, a record or a plugin's file access
KNOBS = ("CHUNK_TOKENS", "CHUNK_OVERLAP", "CHUNK_VERSION", "CLEANING_VERSION",
         "PUBMED_PACK_DIR", "CACHE_DIR", "CORPUS_DIR", "DATA_DIR")

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_size = 0
_failures = 0
_disabled = False


def _child_init(knobs: dict, base: int | None = None) -> None:
    """A child sits at the WORKER's base nice + 10, whichever thread started
    the pool. A child inherits its starter's nice, and since the pipeline
    threads lower themselves (docs/notes/scheduling.md §12) a relative
    `nice(10)` landed at 15 (live, 2026-09-20) — below the index
    maintainer, whose rule is that it yields to parsing, not the reverse."""
    try:
        if base is None:
            os.nice(10)
        else:
            target = min(19, base + 10)
            if os.getpriority(os.PRIO_PROCESS, 0) < target:
                os.setpriority(os.PRIO_PROCESS, 0, target)
    except (AttributeError, OSError):
        pass
    for k, v in knobs.items():
        setattr(config, k, v)


def _base_nice() -> int | None:
    """The worker's base: its main thread's nice (the main thread's tid is
    the pid)."""
    try:
        return os.getpriority(os.PRIO_PROCESS, os.getpid())
    except (AttributeError, OSError):
        return None


def _knobs() -> dict:
    return {k: getattr(config, k) for k in KNOBS if hasattr(config, k)}


def processes() -> int:
    return max(1, int(config.PARSE_PROCESSES))


def pool() -> ProcessPoolExecutor | None:
    """The executor, created on first use; None when serial or DISABLED."""
    global _pool, _pool_size
    n = processes()
    if n <= 1 or _disabled:
        return None
    with _lock:
        if _pool is None or _pool_size != n:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            _pool = ProcessPoolExecutor(
                max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                initializer=_child_init, initargs=(_knobs(), _base_nice()),
                # bounded memory: a child's plugin caches grow with what it
                # parsed; it is replaced after this many sub-batches
                max_tasks_per_child=max(1, int(config.PARSE_TASKS_PER_CHILD)))
            _pool_size = n
        return _pool


def shutdown() -> None:
    global _pool, _pool_size
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None
            _pool_size = 0


def _note_success() -> None:
    global _failures
    if _failures:
        with _lock:
            _failures = 0


def _note_failure(exc: BaseException) -> None:
    """A pool that did not answer: rebuild it, and give up on children
    entirely if it keeps happening. Printed, not log_event'd — this module
    has no manifest connection, and the worker's stdout IS the log."""
    global _failures, _disabled
    shutdown()
    with _lock:
        _failures += 1
        failures = _failures
        if failures >= max(1, config.PARSE_MAX_FAILURES) and not _disabled:
            _disabled = True
    print(f"[parse pool] {type(exc).__name__}: computing this batch in-process "
          f"(failure {failures} of {config.PARSE_MAX_FAILURES})"
          + ("; children DISABLED until restart" if _disabled else ""),
          file=sys.stderr, flush=True)


def reset_failures() -> None:
    global _failures, _disabled
    with _lock:
        _failures = 0
        _disabled = False


def health() -> dict:
    return {"processes": processes(), "pool": _pool is not None,
            "failures": _failures, "disabled": _disabled,
            # read WITHOUT resetting: health is a question, not a meter
            # read — `_log_shares` owns the reset (see occupancy).
            "slots_in_flight": occupancy(reset=False)["slots_in_flight"]}


# ---- the work ---------------------------------------------------------------

def _one(kind: str, collection, row: dict):
    from . import layers, plugin_io
    plugin = collection.plugin_instance()
    if kind == "chunks":
        sections = plugin_io.sections(plugin, row)
        return layers.chunks_for_document(collection, row, sections)
    if kind == "metadata":
        return plugin_io.metadata(plugin, row)
    if kind == "record":
        from .pack.build import _record_of
        return _record_of(collection, plugin, row)
    if kind == "prepare":
        return plugin_io.prepare(plugin, row)
    raise ValueError(f"unknown work kind {kind!r}")


def _compute(kind: str, collection, rows: list[dict]) -> list[tuple[bool, object]]:
    """Runs in a child (or inline): one (ok, value) per row, in order.
    A failure is (False, {"type", "msg"}) — never an exception out of the
    task, so one bad document never poisons its sub-batch."""
    out: list[tuple[bool, object]] = []
    for row in rows:
        try:
            out.append((True, _one(kind, collection, row)))
        except Exception as e:   # noqa: BLE001 — recorded per document by the caller
            out.append((False, {"type": type(e).__name__, "msg": str(e)[:500]}))
    return out


def raise_for(err: dict):
    """Re-raise a child's failure with the class the worker's error
    handling keys on (an UnindexableDocument stays a corpus fact)."""
    from .plugins.base import UnindexableDocument
    if err.get("type") == "UnindexableDocument":
        raise UnindexableDocument(err.get("msg", ""))
    raise RuntimeError(f"{err.get('type')}: {err.get('msg')}")


# ----------------------------------------------------------------- admission
#
# WORK-CONSERVING, by what is HELD. The rule here used to be a fair share by
# PRESENCE: divide the children by however many collections had touched the
# pool in the last 30 s. That is wrong in the one way that matters — it hands
# half the machine to a collection that has no work for the pool at all.
# Live on the node, 2026-09-23: gutenberg only EMBEDS, and the embed path
# chunks IN-PROCESS on its own thread (worker._load_chunks) and never enters
# the pool; wikipedia is the only collection parsing and indexing, and it was
# sized for half the children anyway, so six of twelve cores had nothing to
# run on them.
#
# So a collection takes every task slot nobody else is occupying, recomputed
# each time the window is refilled. Alone — the live case — it takes all of
# them.
#
# The old fair share survives as the FLOOR, because it was protecting
# something real: a collection whose documents cost 30 s each would
# otherwise hold every child for its whole batch, and the collection waiting
# behind it holds `BEGIN IMMEDIATE` on its manifest the whole time it
# crawls. So a collection that is inside `compute` counts as wanting the
# pool, and is never admitted below `budget // wanting` however much the
# others are already holding — the surplus queues and the incumbent stops
# refilling past its own share, so the two converge within one wave.
#
# What this is still NOT: CPU weighting. How the kernel divides CPU BETWEEN
# running children is cgroups' job (§11), and it says nothing about the GPU
# feed — the embed thread does not use the pool, and children sit at the
# worker's nice + 10, so feeding the encoder outranks parsing by
# construction (docs/notes/scheduling.md §12).
_held: dict[str, int] = {}                 # collection name -> slots in flight
_wanting: dict[str, int] = {}              # collection name -> batches inside compute
_held_lock = threading.Lock()
#: task slots in flight per child: one running, one queued behind it, so a
#: child never idles while the parent notices the previous result.
IN_FLIGHT_PER_CHILD = 2
#: never fewer than one child and a task queued behind it, whatever the
#: arithmetic says — a share of zero is a stalled collection.
MIN_SLOTS = 2
#: how many tasks a batch is cut into per child (see `compute`).
#:
#: TWO, and it was measured back DOWN to two. Eight was deployed on
#: 2026-09-23 to stop a dear document idling a child, and it cost 4.6x:
#: wikipedia's rdf went from 6.3 to 28.8 ms/doc and fts from 10.2 to 27.2,
#: with the CPU UP (77% -> 193% of 1200%) and throughput DOWN (54.2 -> 24.3
#: docs/s). More CPU for less work is REDUNDANT COMPUTATION — that much the
#: numbers settle. Which redundancy was not isolated; the likeliest is
#: locality, since the parts are cut by equal COUNT and wikipedia's
#: documents come out of shared multistream blocks, so every child holding
#: one of a block's documents decompresses that whole block and four times
#: the parts is four times the re-decompression. (`PARSE_TASKS_PER_CHILD`
#: — a different knob with a confusingly similar name — recycles a child
#: every 500 tasks, so finer parts also recycle more often; that was ~45
#: minutes apart and too rare to be the main term.)
#:
#: Either way the lesson holds and is the reason for the ceiling: a part's
#: cost is NOT independent of the other parts, so balance cannot be bought
#: with granularity. The cut has to follow the blocks first
#: (docs/notes/scheduling.md §11).
PARTS_PER_CHILD = 2
#: rows below which pickling a part outweighs the work in it
MIN_PART = 4


def slots(name: str | None, budget: int) -> int:
    """How many task slots this collection may hold right now: the budget
    less what the OTHERS are actually holding, and never less than its share
    among the collections that are currently asking."""
    with _held_lock:
        others = sum(v for k, v in _held.items() if k != name)
        asking = len(_wanting) + (0 if name in _wanting else 1)
    return max(MIN_SLOTS, budget // max(1, asking), budget - others)


# ---------------------------------------------------------------- occupancy
#
# THE MEASUREMENT THIS MODULE DID NOT HAVE. A starved pool and a slow pool
# look identical from outside: both show long batches. The batch log has
# duration and size, `docker stats` has the container, and the sizer will
# faithfully fit a cost model to a machine running on one core — so on
# 2026-09-23 admission handed wikipedia 3 of 11 children for a day and
# every instrument agreed things were merely slow.
#
# WHAT THIS IS, EXACTLY — read this before quoting it. It is the time
# average of TASKS IN FLIGHT (submitted and not yet collected), capped at
# the pool size. It answers the ADMISSION question: did the pool have work
# it was allowed to run? It does NOT say how many children were running,
# and the two diverge badly, because `compute` submits a whole batch at
# once (parts ~= PARTS_PER_CHILD x n = the cap) and then drains it: the
# slots stay "in flight" all the way through the straggler tail, while the
# children that finished early sit idle.
#
# Measured on the node the night this was written: this metric read 73-81 %
# while children actually in state R averaged 3.7 of 11 (34 %) — sampled at
# 1 Hz, `tools/sched_measure.sh`. Both numbers are true and they are
# answers to different questions. It was first shipped labelled
# "busy_children", which was simply wrong, and the log row it produced
# overstated the change it was measuring.
#
# Integrated over time, not sampled — a sample lands between batches as
# often as inside one.
_busy_slots = 0                    # tasks in flight, all collections
_busy_integral = 0.0               # child-seconds accrued since _busy_read_at
_busy_since = time.monotonic()     # last point the integral was advanced to
_busy_read_at = time.monotonic()   # last occupancy(reset=True)


def _advance(now: float, n: int) -> None:
    """Accrue child-seconds up to `now`. Caller holds _held_lock."""
    global _busy_integral, _busy_since
    if now > _busy_since:
        _busy_integral += min(_busy_slots, n) * (now - _busy_since)
    _busy_since = now


def occupancy(reset: bool = True) -> dict:
    """How busy the pool has been SINCE THE LAST READ: the time-average
    number of children with work, and that as a share of the pool.

    Published by the beat thread, never by a pipeline thread (CLAUDE.md:
    telemetry is published by someone other than the thread doing the work).
    """
    global _busy_integral, _busy_read_at
    n = processes()
    with _held_lock:
        now = time.monotonic()
        _advance(now, n)
        span = now - _busy_read_at
        child_s = _busy_integral
        if reset:
            _busy_integral = 0.0
            _busy_read_at = now
    busy = child_s / span if span > 0 else 0.0
    return {"slots_in_flight": round(busy, 2), "processes": n,
            "utilisation": round(busy / n, 4) if n else 0.0,
            "elapsed_s": round(span, 1)}


def _occupy(name: str | None, delta: int) -> None:
    global _busy_slots
    n = processes()
    with _held_lock:
        _advance(time.monotonic(), n)
        _busy_slots = max(0, _busy_slots + delta)
        got = _held.get(name, 0) + delta
        if got > 0:
            _held[name] = got
        else:
            _held.pop(name, None)


def _want(name: str | None, delta: int) -> None:
    with _held_lock:
        got = _wanting.get(name, 0) + delta
        if got > 0:
            _wanting[name] = got
        else:
            _wanting.pop(name, None)


def held() -> dict[str, int]:
    with _held_lock:
        return dict(_held)


def wanting() -> dict[str, int]:
    with _held_lock:
        return dict(_wanting)


def forget_users() -> None:
    global _busy_slots, _busy_integral, _busy_since, _busy_read_at
    with _held_lock:
        _held.clear()
        _wanting.clear()
        _busy_slots = 0
        _busy_integral = 0.0
        _busy_since = _busy_read_at = time.monotonic()


def _spans(collection, rows) -> list[tuple[str, int]] | None:
    """`plugin_io.work_span` for every row, or None when this plugin has no
    shared work units (then the plain equal-count cut is equal work)."""
    try:
        from . import plugin_io
        plugin = collection.plugin_instance()
        out = [plugin_io.work_span(plugin, r) for r in rows]
    except Exception:   # noqa: BLE001 — a hint, never a reason to fail a batch
        return None
    return out if any(s is not None for s in out) else None


def _partition(collection, rows: list[dict], want: int) -> list[list[dict]]:
    """Cut `rows` into at most `want` CONTIGUOUS parts.

    Contiguous because the rows arrive in the corpus's own order, which for
    a dump is stream order — so neighbouring rows already share work, and
    any reordering would scatter it.

    By COST and on UNIT BOUNDARIES when the plugin says what the units are
    (`plugin_io.work_span`), because equal COUNT is not equal WORK:
    wikipedia's pages come in multistream chunks of ~100, a chunk costs its
    decompression whoever touches it, and `_stream_pages` caches one chunk
    per process. Cutting mid-chunk decompresses it twice; cutting by count
    leaves parts of wildly different cost, and the batch then ends with ten
    children idle behind one. Both were measured on the node 2026-09-23 —
    4.6x for the first, 3.7 of 11 children running for the second.

    Equal count otherwise, which is what gutenberg and pubmed want: their
    documents are independent files and a row IS the unit.

    SIMULATED against real locators sampled from the node's manifest before
    it was deployed (55 pages per chunk; chunk bytes p10 6.9k, p50 26k, max
    186k — a 27x spread, which is the straggler), cutting into ~22 parts for
    eleven children and scheduling the parts onto them:

        rows/chunks      total work      makespan      worst part
        2200 /  48         0.72x          0.64x        242k -> 92k
        6000 / 109         0.89x          0.74x        452k -> 186k
        3000 /  39         0.70x          0.46x        412k -> 169k

    Work falls because chunks stop being decompressed by two children;
    makespan falls further because the worst part is what the batch waits
    for. A simulation is not the node — this must still be measured there.
    """
    spans = _spans(collection, rows)
    if spans is None:
        size = max(MIN_PART, -(-len(rows) // want))
        return [rows[i:i + size] for i in range(0, len(rows), size)]

    # RUNS: the contiguous rows sharing one unit. A run is indivisible — the
    # whole point — so the cut happens between runs and never inside one. A
    # unit is charged ONCE, to the run, plus the cheap per-row parse.
    runs: list[tuple[list[dict], float]] = []
    for i, row in enumerate(rows):
        key = spans[i][0] if spans[i] else None
        prev_key = spans[i - 1][0] if i and spans[i - 1] else None
        if runs and key is not None and key == prev_key:
            runs[-1][0].append(row)
            runs[-1] = (runs[-1][0], runs[-1][1] + 1.0)
        else:
            runs.append(([row], float(spans[i][1]) if spans[i] else 1.0))
    target = sum(c for _, c in runs) / max(1, want)

    parts: list[list[dict]] = []
    cur: list[dict] = []
    acc = 0.0

    def close() -> None:
        nonlocal cur, acc
        if cur:
            parts.append(cur)
            cur, acc = [], 0.0

    # `want` IS A TARGET SIZE, NOT A CAP — and that distinction was worth
    # 2x. Refusing to open part number `want` forces every remaining run
    # into the last one: simulated on real locators, the final part cost
    # 607k against equal-count's worst of 242k, and the makespan across
    # eleven children came out 2.03x WORSE even though total work fell to
    # 0.72x. Extra parts are free here in the way they were not before,
    # because a part never splits a unit: nothing is decompressed twice
    # however many parts there are. Overshooting `want` by a third beats
    # one part carrying the tail.
    for rows_of, cost in runs:
        # CLOSE BEFORE OVERSHOOTING, not after. Closing only once the budget
        # is already spent bundles a dear unit with everything ahead of it:
        # with one unit costing 100x its neighbours the part that gets it
        # carried five others too, and that part IS the straggler tail.
        if cur and acc + cost > target:
            close()
        cur.extend(rows_of)
        acc += cost
        if acc >= target:
            close()
    close()
    return parts


def compute(kind: str, collection, rows) -> list[tuple[bool, object]]:
    """chunks | metadata | record for every row, in order. `rows` may be
    sqlite3.Row objects (converted; only plain fields travel)."""
    plain = [dict(r) if not isinstance(r, dict) else r for r in rows]
    if not plain:
        return []
    ex = pool()
    if ex is None or len(plain) == 1:
        return _compute(kind, collection, plain)
    n = processes()
    parts = _partition(collection, plain, n * PARTS_PER_CHILD)
    # ADMISSION (docs/notes/scheduling.md §10, B) — see `slots` above. The
    # budget is the whole pool; the cap is recomputed on every refill, so a
    # collection alone holds all of it and gives slots up only to a
    # collection that is really using them.
    name = getattr(collection, "name", None)
    budget = n * IN_FLIGHT_PER_CHILD
    # BOUNDED: the pool is an optimisation, and a caller that waits on it
    # forever is worse than no pool at all — live 2026-08-28, pubmed's
    # pipeline thread sat in the executor with every child gone, so the
    # collection stopped and SIGTERM could not have been honoured either.
    # On a timeout or a broken pool the batch is computed in-process and
    # the pool is rebuilt; after PARSE_MAX_FAILURES in a row this process
    # stops using children until it restarts.
    #
    # THE WINDOW IS KEPT FULL, not refilled a wave at a time. It used to
    # submit a wave of tasks, wait for ALL of them, and only then submit the
    # next — so at every wave boundary the children drained to idle while the
    # last task of the wave finished alone, and a batch cut into three waves
    # paid that three times. MEASURED on the node, 2026-09-22: a document
    # costs 59 ms of CPU on its own, and the pipeline thread saw 41 ms per
    # document with ELEVEN children — a speedup of 1.4 where the pool exists
    # to give ten. Now a slot is refilled the moment its result is taken, so
    # the window is full from the first submission to the last. Results are
    # still consumed in order, so a failing document still costs itself and
    # never its neighbours.
    futures: list = []
    mine = 0
    _want(name, 1)
    try:
        deadline = time.monotonic() + config.PARSE_TIMEOUT_S
        out: list[tuple[bool, object]] = []
        nxt = 0
        while nxt < len(parts) or futures:
            cap = slots(name, budget)
            while nxt < len(parts) and len(futures) < cap:
                futures.append(ex.submit(_compute, kind, collection, parts[nxt]))
                nxt += 1
                mine += 1
                _occupy(name, 1)
            oldest = futures.pop(0)
            out.extend(oldest.result(timeout=max(1.0, deadline - time.monotonic())))
            mine -= 1
            _occupy(name, -1)
        _note_success()
        return out
    except (BrokenProcessPool, TimeoutError, OSError) as e:
        for f in futures:
            f.cancel()
        _note_failure(e)
        return _compute(kind, collection, plain)
    finally:
        # whatever is still held on the way out — the timeout and the broken
        # pool leave a window's worth — or the next collection inherits a
        # budget that has been eaten by a batch that is long over.
        if mine:
            _occupy(name, -mine)
        _want(name, -1)


def chunks_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("chunks", collection, rows)


def metadata_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("metadata", collection, rows)


# ---- prefetch: prepare what the NEXT batches will need, in the background --

_prefetched: OrderedDict = OrderedDict()      # (collection, key) -> True
_MAX_PREFETCH_MEMO = 512


def prefetch(collection, rows) -> list[str]:
    """Fire-and-forget: ask the pool to PREPARE what documents ahead of the
    current batch will need — a plugin's derived cache, built once and
    reused (pubmed: the per-container pack, 17 s of every ~38 s of its
    pipeline before this existed, paid by the batch that first touched a
    container while eleven pool children idled).

    Deduplicated on the plugin's `prepare_key` (the container, not the
    document), so a container is submitted once. A no-op when the pool is
    serial: there is no background to do it in, and doing it inline would
    just move the same cost earlier. Never waits, never raises — a failed
    preparation simply means the stage pays for it later, as before."""
    from . import plugin_io
    ex = pool()
    if ex is None:
        return []
    try:
        plugin = collection.plugin_instance()
    except Exception:   # noqa: BLE001
        return []
    submitted = []
    for row in rows:
        row = dict(row) if not isinstance(row, dict) else row
        key = plugin_io.prepare_key(plugin, row)
        if not key:
            continue
        memo_key = (collection.name, key)
        with _lock:
            if memo_key in _prefetched:
                _prefetched.move_to_end(memo_key)
                continue
            _prefetched[memo_key] = True
            while len(_prefetched) > _MAX_PREFETCH_MEMO:
                _prefetched.popitem(last=False)
        try:
            ex.submit(_compute, "prepare", collection, [row])
        except Exception:   # noqa: BLE001 — a broken pool is handled by the next compute()
            with _lock:
                _prefetched.pop(memo_key, None)
            break
        submitted.append(key)
    return submitted


def forget_prefetched() -> None:
    with _lock:
        _prefetched.clear()
