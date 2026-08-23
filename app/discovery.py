"""Corpus discovery — idempotent per-collection sync against the manifest.

Cheap gate: (mtime, size) from the plugin's discover() pass. Only when that
changes do we call fetch() to hash content; only when the hash actually
differs do we reset downstream stage flags for that one document. A
no-change run is just a stat scan — discover() never touches file content.
"""
import sqlite3
import time

from . import db
from .plugins.base import DiscoverState, DocRef



def _clear_cursor_if_relocated(con, collection, root) -> bool:
    """True when stored locators no longer live under the corpus root, in
    which case the scan cursor is dropped so discovery re-enumerates.

    Deliberately tolerant: a plugin whose locator is not a filesystem path
    under the root (or a collection with nothing discovered yet) simply
    reads as "not relocated" and nothing happens.
    """
    try:
        row = con.execute("SELECT path FROM documents "
                          "WHERE path IS NOT NULL LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    if row is None or not row["path"]:
        return False
    stored = str(row["path"])
    prefix = str(root)
    if stored.startswith(prefix):
        return False
    from .plugins import cursor as scan_cursor
    scan_cursor.clear(collection.manifest_path.parent, collection.plugin)
    db.log_event(con, "info", "discover",
                 f"corpus moved (stored locators point outside {prefix}); "
                 f"scan cursor cleared so locators can be refreshed")
    return True


# Refs per manifest transaction. sync_collection used to run in autocommit
# — one transaction per row — and the "COMMIT every 2000" below it never
# fired because nothing had BEGUN one. Measured live on wikivec: 1.5 M rows
# in 1,083 s, almost all of it SQLite per-statement commit overhead (the
# same economics rdf batching fixed: each dirty page hits the WAL once per
# batch instead of once per row). An interrupted batch re-yields next scan
# — the cursor only advances past containers whose rows are committed.
DISCOVER_BATCH_REFS = 2000


def sync_collection(con, collection, *, stop=None) -> dict:
    """Enumerate the corpus and upsert what changed. Streams; commits every
    DISCOVER_BATCH_REFS refs and tells the plugin (DiscoverState.committed,
    on_commit) so its scan cursor can be saved as it goes. `stop` (an
    object with is_set(), e.g. the worker's SIGTERM event) ends the scan at
    the next ref: the batch so far is committed, the cursor covers exactly
    the containers whose rows are durable, and the result carries
    `interrupted=True` — the next scan resumes from there. Invariant 2: the
    resumable unit of a scan is one batch of refs."""
    t0 = time.time()
    plugin = collection.plugin_instance()
    root = collection.corpus_dir
    if not root.is_dir():
        db.log_event(con, "error", "discover", f"corpus dir not found: {root}")
        return {"error": f"corpus dir not found: {root}"}

    # HAS THE CORPUS MOVED? Locators are absolute paths, and the scan cursor
    # is keyed on a container's BASENAME plus (mtime, size) — so a dataset
    # remounted somewhere else looks completely unchanged to it, every
    # container is skipped, and not one ref is emitted to carry the new
    # path. The stored locators then point at a mount that no longer exists
    # and every stage fails with FileNotFoundError.
    #
    # Observed live: renaming the genome collection's ZFS dataset blocked all
    # 17 of its documents, and rescanning did not help because the file
    # genuinely had not changed.
    #
    # One LIMIT 1 read answers it, so collections that have NOT moved pay a
    # single indexed row read and nothing else. Clearing the cursor is the
    # right hammer only here: re-enumeration is what regenerates the
    # locators, and the relocation branch below then updates them without
    # touching any stage flag (the bytes are the same bytes).
    _clear_cursor_if_relocated(con, collection, root)

    state = DiscoverState(corpus_root=root,
                           state_dir=collection.manifest_path.parent)
    seen = 0
    # STREAMING with last-write-wins BY ORDER. A source may yield the same
    # uid more than once in one sweep (PubMed updatefiles revising a baseline
    # record; a delete tombstone after the record it retracts) — plugins
    # enumerate containers in sorted/chronological order, so applying refs in
    # arrival order gives the same final state the old buffered de-dup did:
    # the later ref hits the ordinary mtime/size→hash change gate and wins.
    # The buffer itself had to go: a cold-cursor scan of full PubMed is ~26M
    # refs — the "revisit at millions of records" moment the previous
    # docstring named, arrived. Progress commits as the scan streams, one
    # transaction per DISCOVER_BATCH_REFS, which makes an interrupted giant
    # scan resumable: rows written so far stand, and the plugin's cursor is
    # only advanced past containers whose rows are committed.
    counts = {"seen": 0, "added": 0, "changed": 0, "missing": 0,
              "container_errors": 0, "relocated": 0}
    interrupted = False
    applied = 0                 # refs durably written so far
    batch: list[DocRef] = []
    batch_uids: set[str] = set()
    refs = plugin.discover(state)
    for ref in refs:
        if stop is not None and stop.is_set():
            interrupted = True
            if hasattr(refs, "close"):
                refs.close()
            break
        uid = f"{ref.source_id}:{ref.native_id}"
        # A batch is decided against the manifest BEFORE its writes, so two
        # refs for one uid in the same batch (a PubMed revision following
        # its baseline record, a tombstone after the record it retracts)
        # would both plan as new. Flushing first keeps last-write-wins by
        # arrival order exact: the later ref sees the earlier one committed.
        if batch and (uid in batch_uids or len(batch) >= DISCOVER_BATCH_REFS):
            if not _apply_batch(con, plugin, batch, counts, stop, wait=True):
                interrupted = True      # stop arrived while the manifest was contended
                batch = []
                break
            applied += len(batch)
            _signal_committed(state, applied)
            batch, batch_uids = [], set()
        batch.append(ref)
        batch_uids.add(uid)
        state.received = applied + len(batch)
    if batch:
        # the tail (or the batch a stop request cut short): one attempt if
        # stopping — a contended manifest must not hold SIGTERM past its
        # grace; what is not committed is simply re-enumerated next scan
        if _apply_batch(con, plugin, batch, counts, stop, wait=not interrupted):
            applied += len(batch)
            _signal_committed(state, applied)
    if not interrupted:
        apply_scope(con)
    stats = {"seen": counts["seen"], "added": counts["added"],
             "changed": counts["changed"], "missing_txt": counts["missing"],
             "secs": round(time.time() - t0, 1)}
    if counts["relocated"]:
        stats["relocated"] = counts["relocated"]
    if counts["container_errors"]:
        stats["container_errors"] = counts["container_errors"]
    if interrupted:
        stats["interrupted"] = True
        db.log_event(con, "info", "discover",
                     f"discovery interrupted (stop requested) — progress committed, "
                     f"resumes next scan {stats}")
    else:
        db.log_event(con, "info", "discover", f"discovery done {stats}")
    return stats


def _signal_committed(state, n: int) -> None:
    state.committed = n
    for hook in state.on_commit:
        hook()


def _apply_batch(con, plugin, batch: list, counts: dict, stop, *, wait: bool) -> bool:
    """Apply one batch of refs to the manifest. Two phases, so the write
    lock is held only for the writes:

    1. DECIDE, holding nothing: read each document's row, and for one whose
       (mtime, size) moved call fetch() to hash it — seconds on a gz
       container or a parquet shard, which must never sit inside a
       transaction that rdf/fts are waiting on.
    2. WRITE under `BEGIN IMMEDIATE` — the RESERVED lock taken up front so a
       concurrent commit makes us WAIT (busy_timeout) rather than fail: a
       deferred BEGIN that had already read would fail its upgrade
       instantly (WAL snapshot conflict), which is exactly what happened the
       first time this scan ran beside the pipeline thread. An embed step
       elsewhere can hold the lock longer than busy_timeout, so a lock
       failure is retried (with `wait`) until it succeeds or `stop` says
       otherwise; `counts` are merged only on commit.

    Returns False only when the batch could not be committed (stop while
    contended, or `wait=False`); the caller then treats the scan as
    interrupted — nothing half-applied, nothing in the cursor past it."""
    now = int(time.time())
    local = {k: 0 for k in counts}
    writes: list[tuple[str, tuple]] = []
    for ref in batch:
        uid = f"{ref.source_id}:{ref.native_id}"
        if ref.scope == "container":
            # A container that could not be enumerated is a scan REPORT, not
            # a document (DocRef.scope). Storing it as one minted a
            # filename-keyed row nothing ever removed, which an operator's
            # "clear errors & retry" then fed to the rdf stage forever.
            # Logging it keeps it visible (the System page merges every
            # collection's events) and lets a later clean scan heal it by
            # simply not reporting it again.
            local["container_errors"] += 1
            db.log_event(con, "warn", "discover",
                         f"container not enumerable: {ref.error}", None)
            continue      # a scan report is not a document, so it is not "seen"
        local["seen"] += 1
        if ref.error:
            local["missing"] += 1
            writes.append((
                "INSERT INTO documents(uid,source,native_id,path,error,updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET error=excluded.error",
                (uid, ref.source_id, ref.native_id, ref.locator, ref.error, now)))
            continue
        txt_name = ref.extra.get("txt_name")
        row = con.execute(
            "SELECT path,mtime,size,txt_hash FROM documents WHERE uid=?",
            (uid,)).fetchone()
        if row is None:
            writes.append((
                "INSERT INTO documents(uid,source,native_id,path,txt_name,mtime,size,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (uid, ref.source_id, ref.native_id, ref.locator, txt_name,
                 ref.mtime, ref.size, now)))
            local["added"] += 1
        elif row["mtime"] != ref.mtime or row["size"] != ref.size:
            raw = plugin.fetch(ref)                 # no lock held here
            if raw.raw_hash != row["txt_hash"]:
                writes.append((
                    "UPDATE documents SET path=?,txt_name=?,mtime=?,size=?,txt_hash=?,"
                    "rdf_done=0,fts_done=0,embed_done=0,summary_done=0,error=NULL,"
                    "retries=0,updated_at=? WHERE uid=?",
                    (ref.locator, txt_name, ref.mtime, ref.size, raw.raw_hash, now, uid)))
                local["changed"] += 1
            else:
                writes.append(("UPDATE documents SET mtime=?,size=? WHERE uid=?",
                               (ref.mtime, ref.size, uid)))
        elif row["path"] != ref.locator:
                # RELOCATED, not changed. The locator is an absolute path into
                # the corpus, so remounting a dataset somewhere else — a
                # rename, a move to another pool — invalidates every stored
                # locator while mtime and size stay exactly as they were. The
                # branch above can never notice: it is keyed on content
                # identity, and location is not content.
                #
                # Observed live: renaming the genome collection's dataset left
                # all 17 documents pointing at the old mount, and every one
                # failed the embed stage with FileNotFoundError. Rescanning did
                # not help, because the file really was unchanged.
                #
                # Stage flags are deliberately NOT reset: the bytes are the
                # same bytes, so the parse, the index and the vectors all stay
                # valid. The error and the retry count ARE cleared — the only
                # thing wrong was where we were looking, and a document
                # blocked for that reason should unblock itself rather than
                # wait for someone to press a button.
            writes.append((
                "UPDATE documents SET path=?,txt_name=?,error=NULL,retries=0,"
                "updated_at=? WHERE uid=?",
                (ref.locator, txt_name, now, uid)))
            local["relocated"] += 1

    waited = False
    while True:
        try:
            con.execute("BEGIN IMMEDIATE")
            for sql, params in writes:
                con.execute(sql, params)
            con.execute("COMMIT")
            break
        except sqlite3.OperationalError as e:
            try:
                if con.in_transaction:
                    con.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if not wait or (stop is not None and stop.is_set()):
                return False
            if not waited:
                db.log_event(con, "warn", "discover",
                             f"discovery waiting for the manifest ({e}); the batch "
                             f"is intact and will be retried")
                waited = True
            time.sleep(2)
    for k, v in local.items():
        counts[k] += v
    return True


def apply_scope(con):
    """Recompute documents.in_scope from the current scope config.

    native_id is TEXT (generic across plugins); Gutenberg's ids are numeric,
    so ordering/comparison here casts to INTEGER to match pre-refactor
    int-`id` behavior exactly. A future non-numeric-id plugin will need a
    per-plugin ordering hook — out of scope for P0.1 (single Gutenberg
    collection).
    """
    scope = db.get_scope(con)
    con.execute("UPDATE documents SET in_scope=1")
    if scope["languages"]:
        marks = ",".join("?" for _ in scope["languages"])
        con.execute(
            f"UPDATE documents SET in_scope=0 WHERE lang IS NOT NULL "
            f"AND lang NOT IN ({marks})", scope["languages"])
    if scope["book_ids"]:
        marks = ",".join("?" for _ in scope["book_ids"])
        con.execute(
            f"UPDATE documents SET in_scope=0 WHERE CAST(native_id AS INTEGER) "
            f"NOT IN ({marks})", scope["book_ids"])
    if scope["sample_per_lang"]:
        n = int(scope["sample_per_lang"])
        con.execute("""
            UPDATE documents SET in_scope=0 WHERE in_scope=1 AND uid NOT IN (
              SELECT uid FROM (
                SELECT uid, ROW_NUMBER() OVER (
                  PARTITION BY lang ORDER BY CAST(native_id AS INTEGER)) rn
                FROM documents WHERE in_scope=1) WHERE rn<=?)""", (n,))
    if scope["limit"]:
        con.execute("""
            UPDATE documents SET in_scope=0 WHERE in_scope=1 AND uid NOT IN (
              SELECT uid FROM documents WHERE in_scope=1
              ORDER BY CAST(native_id AS INTEGER) LIMIT ?)""",
            (int(scope["limit"]),))
