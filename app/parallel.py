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
         "PUBMED_PACK_DIR", "CORPUS_DIR", "DATA_DIR")

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
            "failures": _failures, "disabled": _disabled}


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


# Which collections have used the pool lately — the denominator of the
# admission share below. A name is enough here: this decides how many
# children a submission may occupy, not what anything is keyed on.
_recent_users: dict[str, float] = {}
ACTIVE_WINDOW_S = 30.0


def active_users(name: str | None = None) -> int:
    """How many collections are currently competing for the pool."""
    now = time.monotonic()
    if name is not None:
        _recent_users[name] = now
    for stale in [k for k, t in _recent_users.items() if now - t > ACTIVE_WINDOW_S]:
        del _recent_users[stale]
    return max(1, len(_recent_users))


def forget_users() -> None:
    _recent_users.clear()


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
    # ~2 tasks per process so a slow document does not idle the others;
    # never smaller than 4 rows, or the pickling outweighs the work
    size = max(4, -(-len(plain) // (n * 2)))
    parts = [plain[i:i + size] for i in range(0, len(plain), size)]
    # ADMISSION (docs/notes/scheduling.md §10, B). The executor is FIFO, and a
    # submission is ~2 tasks per child — so whichever collection submits
    # first takes every child, and one whose documents cost 30 s each holds
    # them for the whole batch while a collection with 2 ms documents
    # waits. Each collection may occupy at most its share of the children
    # while others are active; alone, it gets all of them and nothing
    # changes.
    #
    # Note what this is NOT: it is admission control, not CPU weighting.
    # How the kernel divides CPU BETWEEN the running children is
    # cgroups' job (cpu.weight), not ours — see §11.
    share = max(1, n // active_users(getattr(collection, "name", None)))
    wave = max(1, share * 2)          # the same ~2 tasks per child, capped
    # BOUNDED: the pool is an optimisation, and a caller that waits on it
    # forever is worse than no pool at all — live 2026-08-28, pubmed's
    # pipeline thread sat in the executor with every child gone, so the
    # collection stopped and SIGTERM could not have been honoured either.
    # On a timeout or a broken pool the batch is computed in-process and
    # the pool is rebuilt; after PARSE_MAX_FAILURES in a row this process
    # stops using children until it restarts.
    #
    # THE WINDOW IS KEPT FULL, not refilled a wave at a time. It used to
    # submit `wave` tasks, wait for ALL of them, and only then submit the
    # next `wave` — so at every wave boundary the children drained to idle
    # while the last task of the wave finished alone, and a batch cut into
    # three waves paid that three times. With per-document costs that span
    # two orders of magnitude (wikipedia: median ~0 ms, tail >100 ms) the
    # last task is reliably far behind the rest, so the barrier cost most of
    # the pool. MEASURED on the node, 2026-09-22: a document costs 59 ms of
    # CPU on its own, and the pipeline thread saw 41 ms per document with
    # ELEVEN children — a speedup of 1.4 where the pool exists to give ten.
    # Now a slot is refilled the moment its result is taken, so `wave` tasks
    # are in flight from the first submission to the last: the same cap on
    # how many children one collection may occupy, without the idle gaps.
    # Results are still consumed in order, so a failing document still costs
    # itself and never its neighbours.
    futures: list = []
    try:
        deadline = time.monotonic() + config.PARSE_TIMEOUT_S
        out: list[tuple[bool, object]] = []
        nxt = 0
        while nxt < len(parts) or futures:
            while nxt < len(parts) and len(futures) < wave:
                futures.append(ex.submit(_compute, kind, collection, parts[nxt]))
                nxt += 1
            oldest = futures.pop(0)
            out.extend(oldest.result(timeout=max(1.0, deadline - time.monotonic())))
        _note_success()
        return out
    except (BrokenProcessPool, TimeoutError, OSError) as e:
        for f in futures:
            f.cancel()
        _note_failure(e)
        return _compute(kind, collection, plain)


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
