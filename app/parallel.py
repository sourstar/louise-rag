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
import threading
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


def _child_init(knobs: dict) -> None:
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    for k, v in knobs.items():
        setattr(config, k, v)


def _knobs() -> dict:
    return {k: getattr(config, k) for k in KNOBS if hasattr(config, k)}


def processes() -> int:
    return max(1, int(config.PARSE_PROCESSES))


def pool() -> ProcessPoolExecutor | None:
    """The executor, created on first use; None when serial."""
    global _pool, _pool_size
    n = processes()
    if n <= 1:
        return None
    with _lock:
        if _pool is None or _pool_size != n:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            _pool = ProcessPoolExecutor(
                max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                initializer=_child_init, initargs=(_knobs(),))
            _pool_size = n
        return _pool


def shutdown() -> None:
    global _pool, _pool_size
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None
            _pool_size = 0


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
    try:
        results = list(ex.map(_compute, [kind] * len(parts), [collection] * len(parts), parts))
    except BrokenProcessPool:
        # a child died (OOM, a native crash): this batch runs serially and
        # the next call rebuilds the pool
        shutdown()
        return _compute(kind, collection, plain)
    return [item for part in results for item in part]


def chunks_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("chunks", collection, rows)


def metadata_for_rows(collection, rows) -> list[tuple[bool, object]]:
    return compute("metadata", collection, rows)
