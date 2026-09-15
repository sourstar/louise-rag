"""The local VECTOR INDEX over a collection's Lance table — pack-format.md
§5 / appendix A, architecture-v2.md §8 ("ANN indexes are local, built over
imported vectors, never shared").

Without one every semantic query is a flat scan: measured 90 ms per 100k
rows and 13 s on the 15.8M-row wikivec table. A bucket (IVF) index answers
in milliseconds at 98 % of results within 0.001 cosine of the exact top-10
(IVF-FLAT), or with product-quantised codes plus re-ranking on the stored
f16 vectors (IVF-PQ: 96 B/vector in the index, 0.98 within 0.001 with
refine ×50; WITHOUT re-ranking recall drops to 0.66 on MedCPT's tight
cone, so the refine factor is not optional).

Lifecycle, all off the GPU loop:
  ensure()   the worker's entry point: prunes, then builds or refreshes;
  prune()    drops superseded Lance VERSIONS. Cheap, needs no disk
             headroom, runs on a plain interval and is gated on nothing
             else — see its docstring for what happened when it was
             neither of those things;
  refresh()  folds rows added since into the index (Lance's optimize:
             new fragments are flat-scanned until then) once
             VECTOR_INDEX_REFRESH_ROWS have accumulated. This one
             COMPACTS, so it is gated on free space;
  query()    applies nprobes / refine_factor when an index exists — the
             ONE place search learns whether to probe or scan.

Lance is a versioned store and that is the trap this module exists to
avoid: every write leaves a manifest, and a manifest pins the fragments
it referenced, so a table grows with its write COUNT and not with its
row count. Pruning and compaction are different operations with opposite
disk profiles — pruning only deletes, compaction writes before it
deletes — and they must not share a free-space guard.

Distance: L2 over unit vectors (embeddings.py L2-normalises client-side),
which orders exactly like cosine — the same metric the flat scan used.
"""
import math
import threading
import time
from datetime import timedelta

from . import config, db, embeddings

VECTOR_COLUMN = "vector"
BOOK_COLUMN = "book_id"

_state_lock = threading.Lock()
_has_index: dict[str, tuple[float, bool]] = {}     # lance dir -> (checked_at, has index)
_pruned_at: dict[str, float] = {}                  # lance dir -> monotonic of last prune (absent = never)
_HAS_INDEX_TTL_S = 60.0
# Lance dirs under maintenance right now (a build or a refresh in this
# process). The worker's vector WRITERS — the embed step, an archive pass —
# stand down for that collection meanwhile (a deferral, never an error),
# so a compaction never races a delete-then-insert; rdf/fts/discovery and
# every query continue. Process-local: the maintainer and the writers are
# threads of the one worker; the web process never writes vectors.
_busy: set[str] = set()


def busy(collection) -> bool:
    with _state_lock:
        return str(collection.lance_dir) in _busy


class maintenance:
    """`with maintenance(collection):` marks it busy for the duration."""
    def __init__(self, collection):
        self.key = str(collection.lance_dir)

    def __enter__(self):
        with _state_lock:
            _busy.add(self.key)
        return self

    def __exit__(self, *exc):
        with _state_lock:
            _busy.discard(self.key)
        return False


def free_bytes(collection) -> int | None:
    """Free space on the filesystem holding this collection's vectors."""
    try:
        import shutil
        return shutil.disk_usage(collection.lance_dir).free
    except Exception:   # noqa: BLE001 — a missing dir must not stop maintenance
        return None


def refresh_due(st: dict | None) -> bool:
    """New rows since the build reach both the absolute and the
    fractional threshold: 50k rows AND 2 % of the table by default — a
    16.8M-row table refreshes every ~340k rows, a 300k-row table every
    50k."""
    if not st or not st.get("indexed"):
        return False
    unindexed = st.get("unindexed_rows")
    if unindexed is None:
        return False
    need = max(config.VECTOR_INDEX_REFRESH_ROWS,
               int(config.VECTOR_INDEX_REFRESH_FRACTION * int(st.get("rows") or 0)))
    return unindexed >= need


def _table(collection):
    conn = embeddings._conn(collection)
    if embeddings.TABLE not in conn.table_names():
        return None
    return conn.open_table(embeddings.TABLE)


def _vector_indices(tbl) -> list:
    try:
        return [ix for ix in tbl.list_indices()
                if VECTOR_COLUMN in (getattr(ix, "columns", None) or [])
                and "IVF" in str(getattr(ix, "index_type", "")).upper()]
    except Exception:   # noqa: BLE001 — an older table/format simply has none
        return []


def _scalar_indices(tbl) -> list:
    try:
        return [ix for ix in tbl.list_indices()
                if BOOK_COLUMN in (getattr(ix, "columns", None) or [])]
    except Exception:   # noqa: BLE001
        return []


def build_scalar(collection, *, log=None) -> dict:
    """A BTREE index on `book_id` — the column EVERY write filters on.

    Every vector write is delete-then-insert keyed on the document
    (invariant 2), so each one evaluates `book_id IN (...)` over the whole
    table: measured on the reference node at 0.90 s per 500-document
    genesis flush on wikivec (35M rows) and 0.59 s on pubmed (16M) — about
    a fifth of an archive pass, paid again by every local embed and by
    every reader lookup. Minutes to build, then those predicates are index
    probes."""
    tbl = _table(collection)
    if tbl is None:
        return {"built": False, "reason": "no vector table"}
    rows = tbl.count_rows()
    t0 = time.monotonic()
    tbl.create_scalar_index(BOOK_COLUMN, index_type="BTREE", replace=True)
    dt = time.monotonic() - t0
    out = {"built": True, "kind": "btree", "column": BOOK_COLUMN, "rows": rows,
           "seconds": round(dt, 1)}
    if log:
        log(f"book_id index built over {rows} rows in {dt:.0f}s")
    return out


def has_index(collection) -> bool:
    """Cached per Lance dir for a minute: the query path asks per query."""
    key = str(collection.lance_dir)
    now = time.monotonic()
    with _state_lock:
        hit = _has_index.get(key)
        if hit is not None and now - hit[0] < _HAS_INDEX_TTL_S:
            return hit[1]
    tbl = _table(collection)
    value = bool(tbl is not None and _vector_indices(tbl))
    with _state_lock:
        _has_index[key] = (now, value)
    return value


def forget(collection) -> None:
    with _state_lock:
        _has_index.pop(str(collection.lance_dir), None)


def partitions_for(rows: int) -> int:
    """About sqrt(N), clamped: 256 at 100k rows, 4096 at 16M."""
    return int(min(4096, max(256, 2 ** round(math.log2(max(1.0, math.sqrt(rows)))))))


def build(collection, *, index_type: str | None = None, partitions: int | None = None,
          log=None) -> dict:
    """Train and build the index now (CLI, or ensure()). Replaces an
    existing one. Returns what was built."""
    from lancedb.index import IvfFlat, IvfPq
    tbl = _table(collection)
    if tbl is None:
        return {"built": False, "reason": "no vector table"}
    rows = tbl.count_rows()
    if rows == 0:
        return {"built": False, "reason": "empty table"}
    kind = (index_type or config.VECTOR_INDEX_TYPE).lower()
    k = int(partitions or partitions_for(rows))
    dim = int(tbl.schema.field(VECTOR_COLUMN).type.list_size)
    t0 = time.monotonic()
    if kind == "ivf_flat":
        cfg = IvfFlat(distance_type="l2", num_partitions=k)
    else:
        kind = "ivf_pq"
        # 8 dims per sub-vector: 768 -> 96, 1024 -> 128, 4096 -> 512
        sub = max(1, dim // 8)
        while dim % sub:
            sub -= 1
        cfg = IvfPq(distance_type="l2", num_partitions=k, num_sub_vectors=sub)
    tbl.create_index(VECTOR_COLUMN, config=cfg, replace=True)
    forget(collection)
    dt = time.monotonic() - t0
    out = {"built": True, "type": kind, "partitions": k, "rows": rows, "seconds": round(dt, 1)}
    if log:
        log(f"vector index built: {kind}, {k} partitions over {rows} rows in {dt:.0f}s")
    return out


def stats(collection) -> dict | None:
    tbl = _table(collection)
    if tbl is None:
        return None
    rows = tbl.count_rows()
    indices = _vector_indices(tbl)
    book_id_indexed = bool(_scalar_indices(tbl))
    if not indices:
        return {"rows": rows, "indexed": False, "book_id_indexed": book_id_indexed}
    name = getattr(indices[0], "name", None)
    unindexed = None
    try:
        st = tbl.index_stats(name) if name else None
        unindexed = getattr(st, "num_unindexed_rows", None) if st is not None else None
    except Exception:   # noqa: BLE001
        pass
    return {"rows": rows, "indexed": True, "name": name,
            "type": str(getattr(indices[0], "index_type", "")),
            "unindexed_rows": unindexed,
            "book_id_indexed": book_id_indexed}


def prune(collection, con=None, *, log=None) -> dict:
    """Delete Lance versions older than the retention window.

    Lance is a versioned store: every write — every `add`, every
    delete-then-insert — leaves a manifest, and a manifest pins the data
    fragments it referenced. So a table grows with its write COUNT and not
    with its row count, and something has to drop the old versions.

    MEASURED on the reference node 2026-09-10, with the app stopped:

        table     rows         versions   data      _versions   _indices
        pubmed    35,457,919     29,007   76.2 GiB   36.6 GiB    3.7 GiB
        wikivec   91,238,804     13,656  208.7 GiB    2.9 GiB   63.0 GiB

    The `data` columns are honest — 91 M chunks at 1024 f16 dimensions IS
    about 209 GiB — so this is not a story about fragments piling up. It
    is about **29,007 retained manifests costing 36.6 GiB on one table**,
    and about whatever share of wikivec's 63 GiB of indices belongs to
    superseded generations.

    WHY IT NEVER RAN — two independent reasons, both fixed by calling this
    from `ensure` on its own interval:

      1. it was reachable only through `refresh`, which `refresh_due`
         gates on UNINDEXED ROWS. A table with an index and few new rows
         never pruned; a table with NO index never pruned at all, because
         `refresh_due` returns False without one. Version growth is caused
         by writes and has nothing to do with either condition.
      2. it shared a FLAT free-space floor with compaction, which
         deadlocks: under the floor nothing ran, so nothing was freed, so
         free space never climbed back over it. That floor is now
         proportional to the table — see `headroom_ok`.

    A NOTE ON THE CALL. Pruning and compaction have opposite disk
    profiles: pruning only deletes, compaction writes before it deletes.
    They ought to be separable and in this lancedb they are not —
    `cleanup_old_versions` is deprecated as of 0.21 and needs `pylance`,
    which this image does not carry, so `optimize` (compact + prune +
    index, one call) is the only entry point. That is why this function
    still checks headroom at all; if a prune-only call becomes available,
    the check belongs on `refresh` alone and this one can drop it.
    """
    tbl = _table(collection)
    if tbl is None:
        return {"pruned": False, "reason": "no table"}
    if not headroom_ok(collection, tbl):
        # `optimize` compacts as well as prunes (see the docstring), so it
        # does need headroom. What it must not have is a flat floor no
        # table can climb back over.
        free = free_bytes(collection)
        msg = (f"version prune deferred: {(free or 0) / 2**30:.0f} GiB free is "
               f"under what a compaction of this table needs")
        if con is not None:
            db.log_event(con, "warn", "index", msg)
        elif log:
            log(msg)
        return {"pruned": False, "reason": "low disk"}
    keep = timedelta(hours=max(0.0, config.VECTOR_INDEX_KEEP_VERSIONS_H))
    before = _versions(tbl)
    v0 = _version(tbl)
    t0 = time.monotonic()
    try:
        # `delete_unverified` stays FALSE by omission: it would remove
        # files Lance cannot prove are unreferenced, which is exactly the
        # file a concurrent writer has just written and not yet committed.
        tbl.optimize(cleanup_older_than=keep)
    except Exception as e:                              # noqa: BLE001
        # A prune that fails is not a failure of the caller: the table is
        # intact and larger than it needs to be, which is a cost and not
        # a fault. Never let it break an embed pass.
        msg = f"version prune skipped: {type(e).__name__}: {e}"
        if con is not None:
            db.log_event(con, "warn", "index", msg)
        elif log:
            log(msg)
        return {"pruned": False, "reason": str(e)}
    dt = time.monotonic() - t0
    latest = _table(collection) or tbl
    after, v1 = _versions(latest), _version(latest)
    # Writers keep committing while optimize runs (pubmed's embed adds a
    # version per document), and every version they add is inside the
    # window, so before - after undercounts by that many: on 2026-09-15 it
    # logged "pruned -1656" for a 146 s run. The version number counts them.
    written = None if (v0 is None or v1 is None) else max(0, v1 - v0)
    removed = None if None in (before, after, written) else before + written - after
    out = {"pruned": True, "seconds": round(dt, 1),
           "versions_before": before, "versions_after": after,
           "versions_written": written, "versions_removed": removed}
    if removed and removed > 0:
        msg = (f"pruned {removed} superseded Lance version(s), "
               f"{after} kept ({dt:.0f}s)")
        if con is not None:
            db.log_event(con, "info", "index", msg)
        elif log:
            log(msg)
    return out


def _versions(tbl) -> int | None:
    try:
        return len(tbl.list_versions())
    except Exception:                                   # noqa: BLE001
        return None


def _version(tbl) -> int | None:
    """The table's latest version number; it grows by one per commit."""
    try:
        return int(tbl.version)
    except Exception:                                   # noqa: BLE001
        return None


def table_bytes(collection) -> int | None:
    """Bytes on disk under the table directory."""
    import os
    root = str(collection.lance_dir)
    if not os.path.isdir(root):
        return None
    total = 0
    for base, _, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def headroom_ok(collection, tbl=None) -> bool:
    """Is there room for a compaction of THIS table?

    The guard this replaces was a flat 150 GiB of free space, and it
    deadlocked: below the floor nothing ran, so nothing was ever freed,
    so free space never climbed back over the floor. A collection could
    only get further from being able to fix itself.

    A proportional test cannot deadlock the same way, because the thing
    it scales with is the thing the operation shrinks. The fraction comes
    from the one measurement on record — a compaction of a 291 GiB table
    took free space down by 23 GiB before releasing anything, which is
    about 8 % — with `VECTOR_INDEX_COMPACT_HEADROOM` at 0.25 for margin,
    and an absolute floor underneath for the case where the pool is in
    trouble for reasons that have nothing to do with this table.
    """
    free = free_bytes(collection)
    if free is None:
        return True                    # cannot tell: do not block maintenance
    size = table_bytes(collection)
    need = config.VECTOR_INDEX_MIN_FREE_GB * 2**30
    if size:
        need = max(need, int(size * config.VECTOR_INDEX_COMPACT_HEADROOM))
    return free >= need


def prune_due(collection) -> bool:
    """Once per VECTOR_INDEX_PRUNE_S per collection, in this process."""
    key = str(collection.lance_dir)
    now = time.monotonic()
    with _state_lock:
        last = _pruned_at.get(key)
        # None means NEVER, and that is not the same as "long ago": under a
        # monotonic clock that starts near zero — a fresh container — a
        # sentinel of 0.0 reads as "pruned moments ago" and suppresses the
        # first prune of the process. Caught by the interval test.
        if last is not None and now - last < config.VECTOR_INDEX_PRUNE_S:
            return False
        _pruned_at[key] = now
    return True


def ensure(collection, con=None, *, log=None, may_continue=None) -> dict | None:
    """The worker's automatic path: build when the table is big enough and
    has no index; refresh when enough unindexed rows accumulated. Returns
    what it did, or None for nothing.

    `may_continue` is asked before each heavy step — the prune, then the
    index work. Each is a Lance call of minutes that cannot be interrupted
    once started, so a pause is honoured BETWEEN them; the worker passes
    its `_maintenance_allowed`. Absent, every step runs (the CLI)."""
    go = may_continue or (lambda: True)
    if not go():
        return None
    st = stats(collection)
    if st is None:
        return None
    # BEFORE any of the index work, and independent of all of it: drop
    # superseded versions. This is what keeps the table from growing
    # without bound, it costs no disk headroom, and it must not be
    # reachable only through a path that a table without an index — or
    # without new rows — never takes. See `prune`.
    if prune_due(collection):
        pruned = prune(collection, con, log=log)
        if pruned.get("bytes_freed"):
            st = stats(collection) or st
        if not go():
            return pruned               # paused while the prune ran
    # the book_id index FIRST: it is cheap and every write pays for its
    # absence (build_scalar explains), including the writes the vector
    # index build itself waits behind
    if st["rows"] >= config.VECTOR_INDEX_MIN_ROWS and not st.get("book_id_indexed", False):
        with maintenance(collection):
            out = build_scalar(collection, log=log)
        if con is not None and out.get("built"):
            db.log_event(con, "info", "index",
                         f"book_id index built ({out['rows']} rows, {out['seconds']}s) — "
                         f"delete-then-insert writes probe instead of scanning")
        return out
    if not st["indexed"]:
        if st["rows"] < config.VECTOR_INDEX_MIN_ROWS:
            return None
        if con is not None:
            db.log_event(con, "info", "index",
                         f"building the vector index over {st['rows']} rows "
                         f"({config.VECTOR_INDEX_TYPE}) — minutes to an hour on a large table; "
                         f"queries keep scanning meanwhile; this collection's vector writes "
                         f"wait (rdf/fts continue)")
        with maintenance(collection):
            out = build(collection, log=log)
        if con is not None and out.get("built"):
            db.log_event(con, "info", "index",
                         f"vector index built ({out['type']}, {out['partitions']} partitions, "
                         f"{out['rows']} rows, {out['seconds']}s) — semantic queries probe instead of scan")
        return out
    if refresh_due(st):
        with maintenance(collection):
            return refresh(collection, con, log=log)
    return None


def refresh(collection, con=None, *, log=None) -> dict:
    """Fold the rows added since the build into the index (Lance's
    optimize: compaction + index update) AND prune superseded versions.

    The pruning window is explicit because the default is not a tuning
    choice, it is a disk bill: `optimize()` with no argument keeps seven
    days of versions, and a collection fed by archive import makes a version
    per flush. Measured on the reference node: wikivec at 522,650 retained
    versions, 177 GB of `_versions` beside 291 GB of live data, on a pool
    with 116 GB free. `delete_unverified` stays False so a file that might
    still be referenced by an in-flight reader is never removed.
    """
    tbl = _table(collection)
    if tbl is None:
        return {"refreshed": False}
    free = free_bytes(collection)
    if not headroom_ok(collection, tbl):
        # optimize() COMPACTS before it prunes: it writes the new fragments
        # first and only then deletes the old ones, so it needs headroom and
        # temporarily makes a full disk fuller. Observed on the reference
        # node at 92% full — a compaction of wikivec's 291 GB table took
        # free space DOWN from 116 GB to 93 GB before it had freed anything.
        # A maintenance pass must never be the thing that fills the pool.
        #
        # The test is PROPORTIONAL to the table (headroom_ok) rather than a
        # flat floor, because a flat floor deadlocks: under it nothing runs,
        # so nothing is freed, so free space never rises back over it.
        msg = (f"vector index refresh SKIPPED: {(free or 0) / 2**30:.0f} GiB free is "
               f"under what compacting this table needs "
               f"({config.VECTOR_INDEX_COMPACT_HEADROOM:.0%} of it, floor "
               f"{config.VECTOR_INDEX_MIN_FREE_GB:.0f} GiB); "
               f"compaction needs headroom before it releases any")
        if con is not None:
            db.log_event(con, "warn", "index", msg)
        elif log:
            log(msg)
        return {"refreshed": False, "reason": "low disk", "free_gb": free / 2**30}
    before = stats(collection) or {}
    t0 = time.monotonic()
    tbl.optimize(cleanup_older_than=timedelta(
        hours=max(0.0, config.VECTOR_INDEX_KEEP_VERSIONS_H)))
    forget(collection)
    dt = time.monotonic() - t0
    after = stats(collection) or {}
    msg = (f"vector index refreshed: {before.get('unindexed_rows')} unindexed rows folded in "
           f"({dt:.0f}s); {after.get('unindexed_rows')} remain")
    if con is not None:
        db.log_event(con, "info", "index", msg)
    elif log:
        log(msg)
    return {"refreshed": True, "seconds": round(dt, 1), "before": before, "after": after}


def query(q, collection):
    """Apply the probing parameters to a Lance query builder when the
    collection has an index; leave a flat scan alone otherwise."""
    if has_index(collection):
        q = q.nprobes(config.VECTOR_INDEX_NPROBES)
        if (config.VECTOR_INDEX_TYPE or "").lower() != "ivf_flat" and config.VECTOR_INDEX_REFINE > 0:
            q = q.refine_factor(config.VECTOR_INDEX_REFINE)
    return q
