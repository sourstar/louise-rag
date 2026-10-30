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
import weakref
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, CancelledError, ProcessPoolExecutor, wait
from concurrent.futures import process as _cf_process
from concurrent.futures.process import BrokenProcessPool

from . import config, cpu_limit

# config knobs a child must share with the parent: everything that shapes
# a chunk, a record or a plugin's file access
KNOBS = ("CHUNK_TOKENS", "CHUNK_OVERLAP", "CHUNK_VERSION", "CLEANING_VERSION",
         "PUBMED_PACK_DIR", "CACHE_DIR", "CORPUS_DIR", "DATA_DIR")

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_size = 0
_failures = 0
_disabled = False
#: when a caller last asked for the pool or left it (monotonic): the
#: start of its idleness for `release_idle`. Under `_held_lock`.
_last_active = time.monotonic()
#: executors this process has thrown away: a failure surfacing from one of
#: them later is not a new failure of the pool (`_note_failure`)
_discarded: "weakref.WeakSet[ProcessPoolExecutor]" = weakref.WeakSet()


class _Executor(ProcessPoolExecutor):
    """ProcessPoolExecutor with CPython's own fix for gh-115634, on an
    interpreter that lacks it: the node's 3.12 (security fixes only, so it
    never will have it; 3.13.16 has it).

    A child retires after PARSE_TASKS_PER_CHILD tasks. 3.12's manager thread
    then calls `_adjust_process_count`, which spawns a replacement only if it
    cannot take a token from `_idle_worker_semaphore`. That semaphore counts
    task COMPLETIONS, not idle children, so it holds stale tokens released by
    children that are gone, and each stale token trusted leaves the pool a
    child short. It ends with no child and parts still queued. Nothing
    answers, the watchdog throws the pool away, and the interpreter's exit
    joins that pool's manager thread for ever. München, 2026-10-25:
    wikipedia-v8's build did not exit on SIGTERM, with no parse child left.
    Reproduced on 3.12.3 (8 parts, 2 children retiring after 2 tasks each):
    4 parts answered, 0 children left. 3.13 replaces a retired child
    unconditionally (`_replace_dead_worker`). This does the same when the
    manager calls, and defers to the library everywhere else."""

    def _adjust_process_count(self):
        manager = getattr(_cf_process, "_ExecutorManagerThread", None)
        if manager is None or not isinstance(threading.current_thread(), manager):
            return super()._adjust_process_count()
        # a child retired (3.12's only call from the manager), under the
        # executor's shutdown lock, which the manager holds: 3.13's rule
        if self._processes is None or (self._shutdown_thread and not self._pending_work_items):
            return
        if len(self._processes) < self._max_workers:
            self._spawn_process()


#: how long `discard` keeps terminating children before it lets go: the
#: first terminated child normally ends the manager within milliseconds
DISCARD_WAIT_S = 5.0


def discard(ex: ProcessPoolExecutor, wait_s: float = DISCARD_WAIT_S) -> None:
    """Stop an executor whose answers nobody will read: its children
    TERMINATED until its manager thread has ended, then its queue
    cancelled. Without this the manager ends when the children finish, or
    never if one is stuck or none is left, and the interpreter's exit joins
    it with no bound (`concurrent.futures.process._python_exit`).

    Python 3.14's `ProcessPoolExecutor.terminate_workers()` (gh-128041) is
    the library's call for this, but it shuts the executor down FIRST and
    terminates after. A child that retires at `max_tasks_per_child` in
    between is not replaced (the executor is shutting down), and if it was
    the last one, the parts already handed to the queue have nobody to run
    them: the manager waits for them for ever. Reproduced with children
    retiring after every part: 2 exits in 8 hung on 3.12.3. Here a
    terminated child's sentinel puts the manager on its own broken path
    (it fails every future, closes the queue's reader so its feeder cannot
    block, and joins), and the executor is shut down only once that has
    happened. A child only computes (this module's rule), so terminating one
    loses nothing anyone waits for: `compute` and `compute_pages` compute an
    unanswered part in-process."""
    _discarded.add(ex)
    manager = getattr(ex, "_executor_manager_thread", None)
    if manager is None and not hasattr(ex, "_processes"):
        # not the executor this was written against: the library's call
        terminate = getattr(ex, "terminate_workers", None)
        if terminate is not None:
            terminate()
        else:
            ex.shutdown(wait=False, cancel_futures=True)
        return
    deadline = time.monotonic() + wait_s
    while True:
        for p in list((getattr(ex, "_processes", None) or {}).values()):
            try:
                if p.is_alive():
                    p.terminate()
            except (ValueError, ProcessLookupError):   # closed, or gone already
                pass
        if manager is None or not manager.is_alive() or time.monotonic() >= deadline:
            break
        manager.join(0.02)
    if manager is not None and manager.is_alive():
        print(f"[parse pool] a stopped pool's manager is still waiting after {wait_s:.0f} s",
              file=sys.stderr, flush=True)
    ex.shutdown(wait=False, cancel_futures=True)


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
    old = None
    with _lock:
        if _pool is None or _pool_size != n:
            # resized: the old pool is stopped (`discard`, below, outside the
            # lock); a failure its callers see is not a new one, and they
            # compute in-process
            old = _pool
            _pool = _Executor(
                max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                initializer=_child_init, initargs=(_knobs(), _base_nice()),
                # bounded memory: a child's plugin caches grow with what it
                # parsed; it is replaced after this many sub-batches
                max_tasks_per_child=max(1, int(config.PARSE_TASKS_PER_CHILD)))
            _pool_size = n
        ex = _pool
    _touch()
    if old is not None:
        discard(old)
    return ex


def _touch() -> None:
    global _last_active
    with _held_lock:
        _last_active = time.monotonic()


def release_idle(idle_s: float, *, now: float | None = None) -> int:
    """The pool stopped (`discard`) when nobody has been inside it for
    `idle_s` seconds: no batch in `compute`/`compute_pages`, no slot held,
    and no `pool()` call meanwhile. Returns how many children it had (0 when
    nothing was released). The next `pool()` starts a fresh one.

    WHY (München, 2026-10-28): the children keep the plugin caches they grew
    while parsing — 12 of them held 10.8 GiB, idle and resident for 26-42
    minutes after the node's quiet profile stopped parsing and through a
    pause, and wikipedia-v8's first snapshot could not start beside them. A
    pause stops the work, not the memory. Called by the worker's beat thread
    (telemetry-shaped, never a pipeline thread); a prefetch in flight is
    cancelled with the pool, and the stage that needed it pays later, as a
    failed prefetch always did. loky's reusable executor shuts idle workers
    down the same way (its `timeout`); this pool stays the standard
    library's, with the gh-115634 fix above."""
    global _pool, _pool_size
    if idle_s <= 0:
        return 0
    with _lock:
        with _held_lock:
            busy = bool(_wanting) or bool(_held)
            idle_for = (time.monotonic() if now is None else now) - _last_active
        if _pool is None or busy or idle_for < idle_s:
            return 0
        ex, n, _pool, _pool_size = _pool, _pool_size, None, 0
    discard(ex)
    return n


def shutdown() -> None:
    """The pool stopped (`discard`): after a failure, and when the process
    stops."""
    global _pool, _pool_size
    with _lock:
        ex, _pool, _pool_size = _pool, None, 0
    if ex is not None:
        discard(ex)


def _stop_at_exit() -> None:
    """Interpreter exit joins every pool's manager thread with no bound
    (`concurrent.futures.process._python_exit`). Registered by the library's
    own mechanism for it, `threading._register_atexit`, after the library's
    own registration, so it runs first (the hooks run last-in, first-out):
    by then nobody reads an answer (main has returned), and a stopped build
    leaves at once instead of after its pages in flight, or never."""
    shutdown()


try:
    threading._register_atexit(_stop_at_exit)
except (AttributeError, RuntimeError):     # no such hook, or the exit has begun
    pass


def _note_success() -> None:
    global _failures
    if _failures:
        with _lock:
            _failures = 0


def _note_failure(exc: BaseException, ex: ProcessPoolExecutor | None = None) -> None:
    """A pool that did not answer: rebuild it, and give up on children
    entirely if it keeps happening. Printed, not log_event'd — this module
    has no manifest connection, and the worker's stdout IS the log.

    `ex`, the executor that failed: one already thrown away (by another
    caller's failure, a resize, or the process stopping) is not a new
    failure. Its answers were never coming, and counting it would throw away
    the pool that replaced it."""
    global _failures, _disabled
    if ex is not None and ex in _discarded:
        print(f"[parse pool] {type(exc).__name__} from a pool already stopped: "
              f"computing this batch in-process", file=sys.stderr, flush=True)
        return
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
    if kind == "chunks+names":
        # one parse for both: the chunks the text index cuts, and the names a
        # record is found by (fts.index_names_of; wikidata)
        from . import fts
        doc = plugin_io.canonical_doc(plugin, row)
        return {"chunks": layers.chunks_for_document(collection, row, doc.sections),
                "names": fts.index_names_of(collection, doc.extra)}
    if kind == "metadata":
        return plugin_io.metadata(plugin, row)
    if kind in ("record", "held-record"):
        # the line encoded here too, in the child (`with_line`)
        from .pack.build import _record_of, held_or_parsed, with_line
        return with_line(collection, (held_or_parsed if kind == "held-record" else _record_of)(
            collection, plugin, row))
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
    if err.get("type") in ("PlatformMissing", "TokenizerMissing"):
        # the node's condition, deferred by the worker (`worker.platform_missing`
        # keys on the CLASS): a RuntimeError naming it in its message was an
        # error on every document that crossed the pool (2026-10-20)
        from .chunking import PlatformMissing
        raise PlatformMissing(f"{err.get('type')}: {err.get('msg')}")
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
    global _last_active
    with _held_lock:
        _last_active = time.monotonic()     # entering or leaving: not idle now
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
    # THE NODE PROFILE'S CORES (app/cpu_limit.py), read once per unit: at
    # most that many parts in flight. A unit is never stalled half-way: under
    # a pause (0) the one already begun finishes on one child, and the pause
    # itself is its caller's, at the unit boundary (worker._cpu_paused)
    lim = cpu_limit.current()
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
            cap = cpu_limit.cap(slots(name, budget), lim, floor=1)
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
    except (RuntimeError, TimeoutError, OSError, CancelledError) as e:
        # RuntimeError: BrokenProcessPool, or "cannot schedule new futures
        # after shutdown" from a pool another caller stopped meanwhile
        for f in futures:
            f.cancel()
        _note_failure(e, ex)
        return _compute(kind, collection, plain)
    finally:
        # whatever is still held on the way out — the timeout and the broken
        # pool leave a window's worth — or the next collection inherits a
        # budget that has been eaten by a batch that is long over.
        if mine:
            _occupy(name, -mine)
        _want(name, -1)


def _finished(f):
    """A future's result when it finished cleanly, else None."""
    try:
        return f.result(timeout=0) if f.done() and not f.cancelled() and f.exception(timeout=0) is None else None
    except Exception:   # noqa: BLE001 — a part that did not finish is computed again
        return None


#: pages `compute_pages` may read ahead of the one its caller takes next: the
#: memory bound of the stream (a wikipedia page is ≤500 records). 2026-10-25,
#: München's profile at 12 children: at two pages ahead, one long article in
#: the oldest page held back every later page, and the cpu sat at one core
#: for stretches between 800 % readings.
PAGES_AHEAD = 16


def _running(inflight) -> int:
    """Parts submitted and not answered yet: the children they occupy."""
    return sum(1 for *_x, f in inflight if f is not None and not f.done())


class Interrupted(Exception):
    """`compute_pages` was paused by the node profile and its `stop` was set
    meanwhile: what was in flight was handed over, nothing more is coming.
    The caller handles it as its stop at a page boundary (invariant 2)."""


def _submit(ex, kind, collection, part):
    """A part to the pool, or None — broken, or stopped by another caller
    meanwhile — and then it is computed in-process."""
    try:
        return ex.submit(_compute, kind, collection, part)
    except RuntimeError as e:
        _note_failure(e, ex)
        return None


def compute_pages(kind: str, collection, pages, *, stop=None, say=None):
    """`compute` over a STREAM of pages: one result list per page, in order,
    with the pool's window kept full ACROSS pages — the parts of page k+1
    are submitted while page k's are still running and while the caller
    works on page k's results.

    WHY (München's profile of wikipedia-v8's records build, 2026-10-24):
    `compute` per page of ≤500 documents waits for its slowest part before
    the next page is even read, so every page ended on one long article with
    the other children idle — 47.8 % of all samples were children waiting
    for work, two children did the parsing, ~1.3 of 4 cpus busy — and its
    deadline covered the WHOLE batch, so a page past PARSE_TIMEOUT_S was
    thrown away and parsed again serially (15 times in 18 h).

    Here the timeout bounds the wait for the NEXT result (no progress for
    PARSE_TIMEOUT_S), the standard watchdog; on a timeout or a broken pool
    the parts not yet answered — and only they — are computed in-process,
    the finished ones kept, and the next pages go to a fresh pool. Parts are
    cut per page by `_partition` (unit boundaries, `work_span`), as before.
    A generator: closing it cancels what is in flight.

    THE NODE PROFILE (app/cpu_limit.py, operator 2026-10-27): parts are
    submitted one at a time while the parts RUNNING are below
    `min(the window, the profile's cores)`, the cores re-read at each page
    boundary (`say` gets each change, once). `0` pauses: nothing new is
    submitted or read, what is in flight finishes and its pages are handed
    over (the caller commits what they complete), and then the stream waits,
    bounded, until a profile allows cores again — or raises `Interrupted`
    when `stop` is set meanwhile. Results never depend on the limit: a part
    is the same part whenever it runs, and pages come out in order."""
    from collections import deque
    name = getattr(collection, "name", None)
    watch = cpu_limit.Watch(say)
    lim = watch.limit()                   # re-read at each page boundary, below
    pages = iter(pages)
    inflight: deque = deque()            # (page_no, part_no, part_rows, future or None = in-process)
    queued: deque = deque()              # (page_no, part_no, part_rows): cut, waiting for a slot
    owner: dict = {}                      # future -> the executor it was submitted to
    results: dict[int, list] = {}         # page_no -> list of per-part results, in part order
    next_page = 0                         # pages read so far
    emit = 0                              # the next page to yield
    exhausted = False
    _want(name, 1)
    mine = 0
    deadline = time.monotonic() + config.PARSE_TIMEOUT_S

    def submit_queued(ex, cap: int) -> None:
        """What waits, oldest first, while the parts RUNNING are below the
        cap — a part that answered but waits behind a slower one for its
        page's turn holds no child, so it holds no slot either."""
        nonlocal mine
        while queued and (ex is None or _running(inflight) < cap):
            page_no, i, part = queued.popleft()
            fut = _submit(ex, kind, collection, part) if ex is not None else None
            if fut is not None:
                owner[fut] = ex
                mine += 1
                _occupy(name, 1)
            inflight.append((page_no, i, part, fut))

    try:
        while True:
            ex = pool()
            n = processes()
            cap = cpu_limit.cap(slots(name, n * IN_FLIGHT_PER_CHILD), lim)
            if lim.cores != 0:
                submit_queued(ex, cap)
            # read pages ahead, once what was read is all submitted
            while not exhausted and not queued and lim.cores != 0 \
                    and (ex is None or _running(inflight) < cap) and next_page - emit <= PAGES_AHEAD:
                page = next(pages, None)
                if page is None:
                    exhausted = True
                    break
                rows = [dict(r) if not isinstance(r, dict) else r for r in page]
                parts = ([rows] if ex is None or len(rows) <= 1
                         else _partition(collection, rows, n * PARTS_PER_CHILD)) if rows else []
                results[next_page] = [None] * len(parts)
                if ex is None or len(rows) <= 1:
                    inflight.extend((next_page, i, part, None) for i, part in enumerate(parts))
                else:
                    queued.extend((next_page, i, part) for i, part in enumerate(parts))
                    submit_queued(ex, cap)
                next_page += 1
                if ex is None:
                    break                 # serial: one page at a time
            # yield every page whose parts are all in
            while emit < next_page and all(r is not None for r in results[emit]):
                out: list = []
                for r in results.pop(emit):
                    out.extend(r)
                emit += 1
                yield out
                # a page boundary: the profile's cores, again (cached, TTL)
                lim = watch.limit()
                # the caller's time with a page is not the pool's silence
                deadline = time.monotonic() + config.PARSE_TIMEOUT_S
            if not inflight:
                if exhausted and not queued:
                    return
                if lim.cores == 0:
                    # PAUSED, with nothing left in flight: wait at this
                    # boundary, bounded, honouring the caller's stop
                    if not cpu_limit.wait_while_paused(stop, watch):
                        raise Interrupted("stopped while the node profile paused the pool")
                    lim = watch.limit()
                    deadline = time.monotonic() + config.PARSE_TIMEOUT_S
                continue
            page_no, i, part, fut = inflight[0]
            if fut is None:
                inflight.popleft()
                results[page_no][i] = _compute(kind, collection, part)
                continue
            try:
                # THE WATCHDOG: no part answered for PARSE_TIMEOUT_S, not
                # "this part took longer" — one long article on a busy core
                # is progress as long as its neighbours keep finishing
                if not fut.done():
                    waiting = [f for *_x, f in inflight if f is not None and not f.done()]
                    done, _ = wait(waiting, timeout=max(0.0, deadline - time.monotonic()),
                                   return_when=FIRST_COMPLETED)
                    if done:
                        # progress, though perhaps not the oldest part: back to
                        # the top, which refills the slots the finished ones freed
                        # (2026-10-25: waiting on the oldest alone left 11 of 12
                        # children idle behind one long article)
                        deadline = time.monotonic() + config.PARSE_TIMEOUT_S
                        continue
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"no part answered for {config.PARSE_TIMEOUT_S:.0f} s")
                    continue
                inflight.popleft()
                deadline = time.monotonic() + config.PARSE_TIMEOUT_S
                got = fut.result()
                owner.pop(fut, None)
                mine -= 1
                _occupy(name, -1)
                _note_success()
                results[page_no][i] = got
            except (BrokenProcessPool, TimeoutError, OSError, CancelledError) as e:
                if inflight and inflight[0][3] is fut:
                    inflight.popleft()        # the oldest part, computed in-process below
                mine -= 1
                _occupy(name, -1)
                for _p, _i, _part, f in inflight:
                    if f is not None:
                        f.cancel()
                        mine -= 1
                        _occupy(name, -1)
                _note_failure(e, owner.get(fut))
                owner.clear()
                # what was not answered, in-process; what finished is kept;
                # what was still queued goes to the next pool
                results[page_no][i] = _compute(kind, collection, part)
                for p_no, j, rest, f in inflight:
                    results[p_no][j] = (_finished(f) if f is not None else None) \
                        or _compute(kind, collection, rest)
                inflight.clear()
    finally:
        for _p, _i, _part, f in inflight:
            if f is not None:
                f.cancel()
        for _ in range(max(0, mine)):
            _occupy(name, -1)
        _want(name, -1)


def chunks_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("chunks", collection, rows)


def chunks_names_for_rows(collection, rows) -> list[tuple[bool, object]]:
    """`chunks_for_rows` plus each record's names: one `{chunks, names}` per
    row, for a collection whose plugin gives names (fts.names_capable)."""
    return compute("chunks+names", collection, rows)


def metadata_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("metadata", collection, rows)


# ---- prefetch: prepare what the NEXT batches will need, in the background --

_prefetched: OrderedDict = OrderedDict()      # (collection, key) -> True
_MAX_PREFETCH_MEMO = 512


def prefetch_allowed() -> bool:
    """A pool, and no node-profile limit below its size. A prefetch is fired
    and forgotten, outside any window, so it is not counted in flight: under
    a limit it is not sent, and the stage that needs the preparation pays
    for it inside its own limited window, as it did before prefetch existed
    (app/cpu_limit.py)."""
    if pool() is None:
        return False
    cores = cpu_limit.current().cores
    return cores is None or cores >= processes()


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
    if ex is None or not prefetch_allowed():
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
