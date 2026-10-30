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


#: Lance dirs whose fragments are being REWRITTEN right now (a compaction).
#: Writers of that collection stand down for these and only these — see
#: `rewrite` and worker._vector_writes_allowed.
_rewriting: set[str] = set()


def rewriting(collection) -> bool:
    with _state_lock:
        return str(collection.lance_dir) in _rewriting


#: lance dir -> commits in flight in this process (`writing`)
_writers: dict[str, int] = {}
_writers_changed = threading.Condition(_state_lock)
#: how long a compaction waits for the writes in flight before it gives up
#: its turn (a write is one delete+add commit: seconds, never minutes)
REWRITE_DRAIN_S = 60.0


class RewriteInProgress(RuntimeError):
    """A write refused while this collection's fragments are being
    rewritten. Worded as a retryable commit conflict on purpose: that is
    what the worker already takes for a deferral, never a document error."""


class writing:
    """`with writing(collection):` around one commit to the vector table:
    refused while a compaction rewrites it, counted while in flight so the
    compaction can wait for it (embeddings._write_lock)."""

    def __init__(self, collection):
        self.key = str(collection.lance_dir)

    def __enter__(self):
        with _state_lock:
            if self.key in _rewriting:
                raise RewriteInProgress(f"retryable commit conflict: {self.key} is being compacted; "
                                        f"this write waits for the next turn")
            _writers[self.key] = _writers.get(self.key, 0) + 1
        return self

    def __exit__(self, *exc):
        with _state_lock:
            n = _writers.get(self.key, 1) - 1
            if n:
                _writers[self.key] = n
            else:
                _writers.pop(self.key, None)
            _writers_changed.notify_all()
        return False


def in_flight(collection) -> int:
    with _state_lock:
        return _writers.get(str(collection.lance_dir), 0)


class rewrite(maintenance):
    """`maintenance`, plus exclusivity against this collection's OWN
    writers.

    A compaction rewrites fragments, and a writer that commits underneath
    takes the rewrite's commit away: three of wikipedia's passes died that
    way in two days — "Retryable commit conflict … Rewrite" (2026-09-16,
    twice) and "Too many concurrent writers … retry_timeout of 30 s"
    (2026-09-17) — after 40–51 minutes of work each, against an archive
    import committing about twice a second.

    Builds and refreshes deliberately do NOT use this (amendment 52: Lance
    is MVCC, a concurrent delete+add during `optimize()` is supported, and
    those run often enough that standing writers down cost the collection
    most of its life). A compaction is rare by construction — only when
    `VECTOR_INDEX_COMPACT_FRAGMENTS` small fragments have accumulated — so
    the exclusivity it needs is affordable."""

    def __init__(self, collection, *, drain_s: float | None = None):
        super().__init__(collection)
        self.drain_s = REWRITE_DRAIN_S if drain_s is None else drain_s

    def __enter__(self):
        """New writes are refused from here on; the ones in flight are
        waited for (bounded), so the rewrite's commit is the only one."""
        super().__enter__()
        deadline = time.monotonic() + self.drain_s
        with _state_lock:
            _rewriting.add(self.key)
            while _writers.get(self.key, 0):
                left = deadline - time.monotonic()
                if left <= 0:
                    n = _writers.get(self.key, 0)
                    _rewriting.discard(self.key)
                    _busy.discard(self.key)
                    raise RewriteInProgress(f"compaction deferred: {n} write(s) still in flight "
                                            f"after {self.drain_s:.0f} s")
                _writers_changed.wait(left)
        return self

    def __exit__(self, *exc):
        with _state_lock:
            _rewriting.discard(self.key)
            _writers_changed.notify_all()
        return super().__exit__(*exc)


def free_bytes(collection) -> int | None:
    """Free space on the filesystem holding this collection's vectors."""
    try:
        import shutil
        return shutil.disk_usage(collection.lance_dir).free
    except Exception:   # noqa: BLE001 — a missing dir must not stop maintenance
        return None


def _started(con, log, msg: str) -> None:
    """The line a long Lance call writes BEFORE it runs."""
    if con is not None:
        db.log_event(con, "info", "index", msg)
    elif log:
        log(msg)


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


def _dataset(tbl):
    """The Lance dataset behind a lancedb table, or None where the Lance
    package is absent.

    `Table.optimize()` welds three jobs into one call — drop superseded
    versions, compact fragments, fold new rows into the index — and they
    earn their cost on different triggers. `cleanup_old_versions` and
    `optimize.compact_files` reach two of them apart, and both route
    through this handle, which raises without `pylance`. Measured on a
    clone of wikipedia's 155 GiB table (2026-09-17): the cleanup alone took
    18.5 s and dropped 2,176 versions and 479 MB, where the welded call had
    been costing 40–51 minutes of ~10 cores every hour."""
    try:
        return tbl.to_lance()
    except Exception:   # noqa: BLE001 — no pylance: the welded call still works
        return None


def purge_deleted(collection, lock_con=None) -> dict:
    """Make the table's deletions physical, for an erasable collection
    (02 §2.9: an erase leaves no trace).

    Lance deletes by writing a deletion file beside the data file, and keeps
    every older version of the table until it is cleaned up — so a deleted
    row's text and vector stay on disk twice over. This rewrites every
    fragment that has a deletion (`materialize_deletions_threshold=0`),
    drops every version but the current one, and, where a vector index was
    built (its partitions were trained on the rows now gone), trains it
    again. Called after an erase, never by the maintainer: a large curated
    table would pay this per document."""
    import datetime

    tbl = _table(collection)
    if tbl is None:
        return {"purged": False, "reason": "no vector table"}
    ds = _dataset(tbl)
    if ds is None:
        raise RuntimeError("the Lance dataset API (pylance) is required to make "
                           "an erase physical (02 §2.9)")
    with embeddings._write_lock(lock_con):
        metrics = ds.optimize.compact_files(materialize_deletions=True,
                                            materialize_deletions_threshold=0.0)
        stats = ds.cleanup_old_versions(older_than=datetime.timedelta(0),
                                        delete_unverified=True)
    retrained = False
    if _vector_indices(embeddings._conn(collection).open_table(embeddings.TABLE)):
        build(collection)
        with embeddings._write_lock(lock_con):
            _dataset(_table(collection)).cleanup_old_versions(
                older_than=datetime.timedelta(0), delete_unverified=True)
        retrained = True
    return {"purged": True,
            "fragments_rewritten": getattr(metrics, "fragments_removed", None),
            "bytes_removed": getattr(stats, "bytes_removed", None),
            "index_retrained": retrained}


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
    # fragments decide whether a COMPACTION is worth its cost (compact_due)
    fragments = small_fragments = None
    try:
        fs = dict(tbl.stats())["fragment_stats"]
        fragments, small_fragments = fs.get("num_fragments"), fs.get("num_small_fragments")
    except Exception:   # noqa: BLE001 — an older table simply does not say
        pass
    if not indices:
        return {"rows": rows, "indexed": False, "book_id_indexed": book_id_indexed,
                "fragments": fragments, "small_fragments": small_fragments}
    name = getattr(indices[0], "name", None)
    unindexed = n_indices = None
    try:
        st = tbl.index_stats(name) if name else None
        unindexed = getattr(st, "num_unindexed_rows", None) if st is not None else None
        # the base and its deltas (`refresh` writes one per call)
        n_indices = getattr(st, "num_indices", None) if st is not None else None
    except Exception:   # noqa: BLE001
        pass
    return {"rows": rows, "indexed": True, "name": name,
            "type": str(getattr(indices[0], "index_type", "")),
            "unindexed_rows": unindexed, "indices": n_indices,
            "book_id_indexed": book_id_indexed,
            "fragments": fragments, "small_fragments": small_fragments}


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
    They ought to be separable, and since 2026-09-17 (`pylance`, operator)
    they are: `cleanup_old_versions` drops versions and touches nothing
    else — 18.5 s on wikipedia's 155 GiB table against 40–51 minutes for
    the welded `optimize()`, which was running EVERY HOUR and starving the
    index refresh behind it. Compaction moved to `compact`, on its own
    trigger. Without the Lance package this falls back to the welded call,
    and only that path needs headroom.
    """
    tbl = _table(collection)
    if tbl is None:
        return {"pruned": False, "reason": "no table"}
    keep = timedelta(hours=max(0.0, config.VECTOR_INDEX_KEEP_VERSIONS_H))
    ds = _dataset(tbl)
    if ds is not None:
        t0 = time.monotonic()
        try:
            # `delete_unverified` stays FALSE by omission: it would remove
            # files Lance cannot prove are unreferenced, which is exactly
            # the file a concurrent writer has written and not yet
            # committed. This only deletes, so it needs no headroom — and
            # on a tight pool it is the work that relieves it.
            st = ds.cleanup_old_versions(older_than=keep)
        except Exception as e:                          # noqa: BLE001
            msg = f"version prune skipped: {type(e).__name__}: {e}"
            if con is not None:
                db.log_event(con, "warn", "index", msg)
            elif log:
                log(msg)
            return {"pruned": False, "reason": str(e)}
        dt = time.monotonic() - t0
        # NO VERSION LISTING HERE. `list_versions()` walks every retained
        # manifest — 10.3 s on wikipedia's 7,891 of them (2026-09-18) —
        # which would have doubled the cost of an 18.5 s cleanup to fill in
        # a log line. What the call returns already says what went.
        removed = int(getattr(st, "old_versions", 0) or 0)
        freed = int(getattr(st, "bytes_removed", 0) or 0)
        out = {"pruned": True, "seconds": round(dt, 1),
               "versions_removed": removed, "bytes_freed": freed}
        if removed:
            msg = (f"pruned {removed} superseded Lance version(s) "
                   f"({dt:.0f}s, {freed / 2**30:.1f} GiB freed)")
            if con is not None:
                db.log_event(con, "info", "index", msg)
            elif log:
                log(msg)
        return out

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


def compact_due(st: dict | None) -> bool:
    """Enough small fragments for a rewrite to earn its cost. One commit
    leaves one fragment, and a stream of small writes makes them faster
    than any pass retires them (wikipedia's import: ~1/s); every read and
    write then pays for them. But compacting that table cost 40–51 minutes
    of ~10 cores, so a handful is never worth it."""
    if not st:
        return False
    small = st.get("small_fragments")
    return small is not None and small >= config.VECTOR_INDEX_COMPACT_FRAGMENTS


def light_compact_due(st: dict | None) -> bool:
    """Small fragments enough for the LIGHT compaction (`compact(light=True)`),
    and not yet so many that the full one is due."""
    if not st:
        return False
    small = st.get("small_fragments")
    return (small is not None and small >= config.VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS
            and not compact_due(st))


def compact(collection, con=None, *, log=None, light: bool = False) -> dict:
    """Merge the small fragments a stream of small writes leaves behind.

    LIGHT (2026-10-30): the small fragments alone, `materialize_deletions`
    off, so no large fragment is rewritten — seconds, not the full pass's
    40–51 minutes — and then the book_id index's new rows folded in as a
    delta (`_refresh_scalar`). Lance's own two calls; the full compaction is
    unchanged.

    IT TAKES THE TABLE EXCLUSIVELY (`rewrite`), which `prune` never did
    and a refresh deliberately does not. A compaction rewrites fragments,
    and a writer that commits underneath takes its commit away: three of
    wikipedia's passes died that way in two days — twice as "Retryable
    commit conflict … Rewrite" (2026-09-16) and once as "Too many
    concurrent writers" (2026-09-17) — after 40–51 minutes of work each.
    So this collection's embed step and archive pass defer while it runs
    (a deferral, never an error), and it is rare enough by construction to
    make that affordable.
    """
    tbl = _table(collection)
    if tbl is None:
        return {"compacted": False, "reason": "no table"}
    if not headroom_ok(collection, tbl):
        free = free_bytes(collection)
        msg = (f"compaction deferred: {(free or 0) / 2**30:.0f} GiB free is under "
               f"what compacting this table needs "
               f"({config.VECTOR_INDEX_COMPACT_HEADROOM:.0%} of it); it writes the "
               f"new fragments before it drops the old")
        if con is not None:
            db.log_event(con, "warn", "index", msg)
        elif log:
            log(msg)
        return {"compacted": False, "reason": "low disk"}
    before = stats(collection) or {}
    what = "light compaction" if light else "compaction"
    _started(con, log, f"{what} started: {before.get('small_fragments')} small fragment(s) "
                       f"of {before.get('fragments')}, {before.get('rows')} rows; this "
                       f"collection's vector writes wait until it ends")
    ds = _dataset(tbl)
    t0 = time.monotonic()
    try:
        with rewrite(collection):
            if ds is not None and light:
                ds.optimize.compact_files(
                    target_rows_per_fragment=config.VECTOR_INDEX_FRAGMENT_ROWS,
                    materialize_deletions=False)
                _refresh_scalar(_dataset(_table(collection)))
            elif ds is not None:
                ds.optimize.compact_files(
                    target_rows_per_fragment=config.VECTOR_INDEX_FRAGMENT_ROWS)
            elif light:
                return {"compacted": False, "reason": "no lance dataset"}
            else:
                tbl.optimize(cleanup_older_than=timedelta(
                    hours=max(0.0, config.VECTOR_INDEX_KEEP_VERSIONS_H)))
    except Exception as e:                              # noqa: BLE001
        # A compaction that loses its commit is not the caller's failure:
        # the table is intact and merely more fragmented than it could be.
        msg = f"{what} skipped: {type(e).__name__}: {e}"
        if con is not None:
            db.log_event(con, "warn", "index", msg)
        elif log:
            log(msg)
        return {"compacted": False, "reason": str(e)}
    forget(collection)
    dt = time.monotonic() - t0
    after = stats(collection) or {}
    msg = (f"{'light-' if light else ''}compacted {before.get('small_fragments')} small fragment(s) of "
           f"{before.get('fragments')} into {after.get('fragments')} ({dt:.0f}s)"
           + ("; the book_id index's new rows indexed" if light else ""))
    if con is not None:
        db.log_event(con, "info", "index", msg)
    elif log:
        log(msg)
    return {"compacted": True, "seconds": round(dt, 1), "before": before, "after": after}


def _refresh_scalar(ds) -> None:
    """The book_id index's new rows folded in as a DELTA (Lance's
    `optimize_indices` for that index alone), its deltas merged into one
    past VECTOR_INDEX_MAX_INDICES. Cheap (0.2 s for 100k rows on the bench),
    and what keeps every `book_id IN (...)` an index probe."""
    if ds is None:
        return
    # one entry per delta segment: the names, once each
    names = sorted({ix.get("name") for ix in ds.list_indices()
                    if BOOK_COLUMN in (ix.get("fields") or []) and ix.get("name")})
    if not names:
        return
    ds.optimize.optimize_indices(index_names=names, num_indices_to_merge=0)
    try:
        n = ds.stats.index_stats(names[0]).get("num_indices")
    except Exception:   # noqa: BLE001 — an older dataset simply does not say
        n = None
    if n is not None and n > config.VECTOR_INDEX_MAX_INDICES:
        ds.optimize.optimize_indices(index_names=names, num_indices_to_merge=n - 1)


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
    # THE INDEX BEFORE THE FRAGMENTS. An unindexed row is scanned by every
    # query; a small fragment only makes reads and writes dearer. Run as one
    # welded `optimize()` on a clock, the compaction always went first and
    # the refresh starved: wikipedia sat at 4.7M unindexed rows against a
    # 1.34M threshold while the hourly pass spent 40–51 minutes compacting
    # (2026-09-17).
    # AND THE FRAGMENTS IN THE SAME PASS (2026-10-14). Returning after the
    # refresh starved the compaction instead: normattiva embeds faster than
    # the refresh threshold, so every pass found a refresh due and none ever
    # compacted — 33,701 fragments, 19,347 of them outside the book_id index,
    # every per-document read 1–1.7 s, embedding by reuse down to ~17
    # documents a minute. The index still goes first; the compaction follows
    # when it is due, between the two `may_continue` is asked again.
    did = None
    if refresh_due(st):
        with maintenance(collection):
            did = refresh(collection, con, log=log)
        if not go():
            return did
        st = stats(collection) or st
    if compact_due(st):
        out = compact(collection, con, log=log)
        return {"refresh": did, "compact": out} if did is not None else out
    # THE LIGHT COMPACTION (2026-10-30): the small fragments merged and the
    # book_id index caught up long before the full pass is due — every
    # delete-then-insert and held-chunks read scans each one it does not
    # cover (wikipedia: 2,368 small fragments, a third of its pipeline
    # thread in genesis's delete)
    if light_compact_due(st):
        if not go():
            return did
        out = compact(collection, con, log=log, light=True)
        return {"refresh": did, "compact": out} if did is not None else out
    return did


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
    # SAID BEFORE IT RUNS (München, 2026-10-23): normattiva's night of
    # 01:00–06:00 logged nothing, and the one refresh line of 10-22 read
    # 20,838 s. A call that cannot be interrupted and logs only when it ends
    # is indistinguishable from a hang for hours.
    _started(con, log, f"vector index refresh started: {before.get('unindexed_rows')} "
                       f"unindexed rows of {before.get('rows')}, {before.get('fragments')} "
                       f"fragment(s); this collection's maintenance waits until it ends")
    ds = _dataset(tbl)
    t0 = time.monotonic()
    merged = None
    if ds is not None:
        # THE INDEX ONLY (2026-09-17): folding new rows in is what a
        # refresh is for, and `optimize()` would compact the whole table
        # on the way — the cost that kept this from running at all.
        #
        # AS A DELTA (2026-10-23). With no argument Lance folds the new rows
        # into the NEWEST index — on normattiva the 7.5M-row base, rewritten
        # whole (713 -> 719 partitions): 404.9 s for 591,591 rows on a clone,
        # against 93.1 s for a delta over the new rows alone (München; the
        # night's 20,838 s was that call starved at nice 10 under load 11.6
        # on 12 cores). A delta costs with the NEW rows, never with the table.
        ds.optimize.optimize_indices(num_indices_to_merge=0)
        # and the deltas are merged into one once there are more than
        # VECTOR_INDEX_MAX_INDICES (the base never: rebuilding it is `build`).
        # Measured here (IVF_PQ, nprobes 30, refine 30): 24 ms a query up to 5
        # indices, 36 ms at 9, 41 ms at 13; merging four deltas 0.4 s over a
        # 300k or a 900k base alike, merging the base too 0.7 s / 1.1 s.
        forget(collection)
        mid = stats(collection) or {}
        n = mid.get("indices")
        if n is not None and n > config.VECTOR_INDEX_MAX_INDICES:
            again = _dataset(_table(collection))
            if again is not None:
                again.optimize.optimize_indices(num_indices_to_merge=n - 1)
                merged = n - 1
    else:
        tbl.optimize(cleanup_older_than=timedelta(
            hours=max(0.0, config.VECTOR_INDEX_KEEP_VERSIONS_H)))
    forget(collection)
    dt = time.monotonic() - t0
    after = stats(collection) or {}
    msg = (f"vector index refreshed: {before.get('unindexed_rows')} unindexed rows folded in "
           f"({dt:.0f}s); {after.get('unindexed_rows')} remain"
           + (f"; {after.get('indices')} index(es)" if after.get("indices") is not None else "")
           + (f", {merged} delta(s) merged into one" if merged else ""))
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
