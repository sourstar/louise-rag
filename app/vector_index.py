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
  ensure()   builds the index once the table has VECTOR_INDEX_MIN_ROWS
             rows and none exists (k-means on a sample, minutes);
  refresh()  folds rows added since into the index (Lance's optimize:
             new fragments are flat-scanned until then) once
             VECTOR_INDEX_REFRESH_ROWS have accumulated;
  query()    applies nprobes / refine_factor when an index exists — the
             ONE place search learns whether to probe or scan.

Distance: L2 over unit vectors (embeddings.py L2-normalises client-side),
which orders exactly like cosine — the same metric the flat scan used.
"""
import math
import threading
import time

from . import config, db, embeddings

VECTOR_COLUMN = "vector"

_state_lock = threading.Lock()
_has_index: dict[str, tuple[float, bool]] = {}     # lance dir -> (checked_at, has index)
_HAS_INDEX_TTL_S = 60.0


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
    if not indices:
        return {"rows": rows, "indexed": False}
    name = getattr(indices[0], "name", None)
    unindexed = None
    try:
        st = tbl.index_stats(name) if name else None
        unindexed = getattr(st, "num_unindexed_rows", None) if st is not None else None
    except Exception:   # noqa: BLE001
        pass
    return {"rows": rows, "indexed": True, "name": name,
            "type": str(getattr(indices[0], "index_type", "")),
            "unindexed_rows": unindexed}


def ensure(collection, con=None, *, log=None) -> dict | None:
    """The worker's automatic path: build when the table is big enough and
    has no index; refresh when enough unindexed rows accumulated. Returns
    what it did, or None for nothing."""
    st = stats(collection)
    if st is None:
        return None
    if not st["indexed"]:
        if st["rows"] < config.VECTOR_INDEX_MIN_ROWS:
            return None
        if con is not None:
            db.log_event(con, "info", "index",
                         f"building the vector index over {st['rows']} rows "
                         f"({config.VECTOR_INDEX_TYPE}) — minutes to an hour on a large table; "
                         f"queries keep scanning meanwhile, writes continue (a write that "
                         f"collides with the build's commit is retried)")
        out = build(collection, log=log)
        if con is not None and out.get("built"):
            db.log_event(con, "info", "index",
                         f"vector index built ({out['type']}, {out['partitions']} partitions, "
                         f"{out['rows']} rows, {out['seconds']}s) — semantic queries probe instead of scan")
        return out
    unindexed = st.get("unindexed_rows")
    if unindexed is not None and unindexed >= config.VECTOR_INDEX_REFRESH_ROWS:
        return refresh(collection, con, log=log)
    return None


def refresh(collection, con=None, *, log=None) -> dict:
    """Fold the rows added since the build into the index (Lance's
    optimize: compaction + index update)."""
    tbl = _table(collection)
    if tbl is None:
        return {"refreshed": False}
    before = stats(collection) or {}
    t0 = time.monotonic()
    tbl.optimize()
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
