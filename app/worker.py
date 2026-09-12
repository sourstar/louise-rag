"""Ingestion worker — a round-robin loop over ALL configured collections, one
document-stage-step per collection per round.

Stage order per document: rdf -> fts -> embed (embed only when embed_mode says
so). Every round re-reads the control table, so pause/resume/scope changes
from the web UI take effect within one document. SIGTERM between documents
loses nothing; SIGTERM mid-document loses at most that document's partial
work, which re-runs cleanly because every write is delete-then-insert keyed
on uid.

Single-threaded, not concurrent: a full round visits every collection once,
each getting at most one document's worth of work, so a busy collection
can't starve an idle one and vice versa. worker_state (pause/resume) stays a
single GLOBAL toggle read from the first collection's manifest — today's
dashboard only ever drives that one control, and introducing genuine
per-collection pause/resume is separate, not-yet-built API surface, not a
P0.5 deliverable. This still fixes the real bug P0.5 needed fixed: before
this, only collections[0] was ever discovered/ingested at all — a second
configured collection was silently, permanently never touched.
"""
import errno
import signal
import sqlite3
import threading
import time
import traceback
from pathlib import Path

import httpx

from . import (chunking, collections as collections_mod, config, db, discovery, layers as layers_mod, parallel,
                embeddings, entities, fts, plugin_io, refs, sched, sizing, spaces, stats)
from .acquire import engine as acquire_engine
from .acquire.spec import DiskPreflightError, LicenseAckRequiredError
from .acquire.specs import ACQUIRE_REGISTRY
from .bundles import genesis as bundle_genesis
from .bundles import genesis_fetch as bundle_genesis_fetch
from .bundles import importer as bundle_importer
from .bundles import ledger as bundle_ledger
from .plugins import base as plugin_base
from .verify import backend_gate
from .transfer.manager import get_manager

# A threading.Event rather than a bool, because it now has to be *observed* by
# acquisition worker threads parked inside the bandwidth bucket. Before P0.7
# a SIGTERM during a fetch was noticed only after the whole acquisition
# returned, which on a large corpus could be many minutes.
DEFERRED = "__deferred__"   # a work() result that must not be recorded

_stop = threading.Event()

def _handle_sigterm(signum, frame):
    _stop.set()
    # Wake anything parked in a token-bucket wait. Without this the stop check
    # in the wait predicate is inert and waiters sleep out their timeout; it is
    # reentrant-safe because Condition uses an RLock, and the bucket's own wait
    # cap bounds shutdown latency even if this call is skipped.
    try:
        get_manager().wake_all()
    except Exception:
        pass

def _load_chunks(row, collection):
    plugin = collection.plugin_instance()
    chapters = plugin_io.sections(plugin, row)
    # the document's LAYER decides the chunker (amendment 41); one layer =
    # the plugin's chunker, exactly as before
    return layers_mod.chunks_for_document(collection, row, chapters)

_RDF_BATCH_PEERS = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=0 AND error IS NULL "
    "AND retries<? AND uid<>? LIMIT ?")

# One event line per batch would be ~86k rows/day at full speed — log spam
# that buries real events. At most one line per collection+stage per 30s;
# the counters on the Library/Stats pages are the real progress signal.
_batch_log_ts: dict[tuple[str, str], float] = {}


def _batch_log(con, stage: str, collection, msg: str) -> None:
    key = (collection.name, stage)
    now = time.time()
    if now - _batch_log_ts.get(key, 0.0) >= 30:
        _batch_log_ts[key] = now
        db.log_event(con, "info", stage, msg)


def _rdf_one(con, row, collection, plugin, now: int, meta: dict | None = None) -> None:
    if meta is None:
        meta = plugin_io.metadata(plugin, row)
    con.execute(
        "UPDATE documents SET title=?,authors=?,lang=?,subjects=?,bookshelves=?,"
        "rdf_done=1,updated_at=? WHERE uid=?",
        (meta["title"], meta["authors"], meta["lang"], meta["subjects"],
         meta["bookshelves"], now, row["uid"]))
    # Keyed on the document's rowid, REPLACE instead of delete-then-insert:
    # `uid UNINDEXED` means a DELETE ... WHERE uid=? scans the whole shadow
    # table — 14.02 of rdf's 14.25 ms/doc at just 36k catalog rows, growing
    # linearly with the catalog (O(N²) for a whole corpus). REPLACE by rowid
    # is one b-tree probe and keeps the same idempotence.
    # db.ensure_catalog_rowids realigned pre-existing rows to this scheme.
    con.execute(
        "INSERT OR REPLACE INTO catalog_fts"
        "(rowid,uid,title,authors,subjects,bookshelves) "
        "VALUES((SELECT rowid FROM documents WHERE uid=?),?,?,?,?,?)",
        (row["uid"], row["uid"], meta["title"], meta["authors"],
         meta["subjects"], meta["bookshelves"]))


def _batch_key(collection, stage: str) -> str:
    """Sizing is per collection AND per stage, keyed on the manifest PATH:
    two collections can share a name and point at different corpora, and a
    document costs what its own corpus makes it cost."""
    key = f"{stage}:{collection.manifest_path}"
    _remember_label(key, f"{stage} {collection.name}")
    return key


def batch_docs(collection, stage: str, default: int) -> int:
    """How many documents this stage should take, from what they have
    actually cost here (app/sizing.py). The configured count is the
    starting point and the floor of the clamp — never a guess replaced by
    another guess."""
    return sizing.sizer().size(
        _batch_key(collection, stage), target_s=config.BATCH_TARGET_S,
        default=default, lo=1, hi=max(default, 1) * 20)


def stage_rdf(con, row, collection) -> None:
    """Extract metadata for `row`, and for as many other rdf-pending
    documents as RDF_BATCH_DOCS allows, inside the ONE transaction the
    caller (_execute_step) already opened.

    Profiled live on pubmed: 95% of a warm rdf step was SQLite, not parsing
    (0.8 ms) — the UPDATE against a 33M-row table plus the catalog_fts FTS5
    delete/insert, committed per document. Batching commits each dirty page
    once per batch and lets FTS5 buffer, the same economics as stage_fts.

    Invariant 2 holds because the enclosing transaction is all-or-nothing:
    a crash mid-batch marks nothing, and a re-run repeats delete-then-insert
    keyed on uid. Only `row` may fail the round — a peer that cannot be
    parsed is skipped, keeps its pending state, and records its own error
    when its own turn comes. Peers arrive in insertion order (no ORDER BY),
    which is discovery order, which is container order — so the container
    cache stays hot across the batch.
    """
    plugin = collection.plugin_instance()
    now = int(time.time())
    t0 = time.monotonic()
    peers = []
    want = batch_docs(collection, "rdf", config.RDF_BATCH_DOCS)
    if want > 1:
        peers = con.execute(
            _RDF_BATCH_PEERS,
            (config.MAX_RETRIES, row["uid"], want - 1)).fetchall()
    _prefetch_ahead(con, collection, "rdf", row)
    # the batch's parse goes to the PARSE POOL (amendment 47); the writes
    # stay here, inside the caller's transaction
    metas = parallel.metadata_for_rows(collection, [row, *peers])
    ok, first = metas[0]
    if not ok:
        parallel.raise_for(first)            # unguarded: its failure IS the round's
    _rdf_one(con, row, collection, plugin, now, meta=first)
    n = 1
    for peer, (ok, meta) in zip(peers, metas[1:]):
        if not ok:
            continue                        # a peer never fails the round
        try:
            _rdf_one(con, peer, collection, plugin, now, meta=meta)
            n += 1
        except Exception:   # noqa: BLE001
            continue
    took = time.monotonic() - t0
    sizing.sizer().record(_batch_key(collection, "rdf"), n, took)
    if n > 1:
        _batch_log(con, "rdf", collection,
                   f"extracted {n} documents in one transaction "
                   f"({took:.1f}s)")

_FTS_BATCH_PEERS = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND error IS NULL "
    "AND retries<? AND (fts_done=0 OR fts_version<? OR entity_version<?) "
    "AND uid<>? LIMIT ?")

# The batch's peers, split for the same reason the SELECTION is split: the
# `OR` above cannot use fts_done, so on a collection whose fresh work has
# just run out it walked the whole (in_scope, rdf_done) range to fill a
# batch — 19.3 s on wikivec, 15.9 s on wikipedia, measured. Fresh peers are
# an index prefix (0.4 ms); re-index peers stay inside the window the
# selection is walking (_next_reindex).
_FTS_FRESH_PEERS = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=0 "
    "AND error IS NULL AND retries<? AND uid<>? LIMIT ?")
_FTS_REINDEX_PEERS = (
    "SELECT * FROM documents WHERE rowid>? AND rowid<=? "
    "AND +in_scope=1 AND +rdf_done=1 AND +fts_done=1 "
    "AND error IS NULL AND retries<? AND (fts_version<? OR entity_version<?) "
    "AND uid<>? LIMIT ?")


def _fts_peers(con, collection, row, limit: int):
    """Batch peers for the selected fts document — fresh or re-index."""
    if limit <= 0:
        return []
    keys = row.keys() if hasattr(row, "keys") else row
    if not ("fts_done" in keys and row["fts_done"]):
        return con.execute(_FTS_FRESH_PEERS,
                           (config.MAX_RETRIES, row["uid"], limit)).fetchall()
    state = _reindex_cursor.get(str(collection.manifest_path) if collection is not None else "")
    if state is None:
        # a direct caller (tests, tools) that did not come through the
        # windowed selection: the unwindowed query is correct, just slower
        return con.execute(_FTS_BATCH_PEERS,
                           (config.MAX_RETRIES, config.FTS_VERSION, config.ENTITY_VERSION,
                            row["uid"], limit)).fetchall()
    start = state[2]
    end = min(start + _REINDEX_WINDOW, state[3])
    return con.execute(_FTS_REINDEX_PEERS,
                       (start, end, config.MAX_RETRIES, config.FTS_VERSION,
                        config.ENTITY_VERSION, row["uid"], limit)).fetchall()

# PREFETCH (amendment 48): what the stage will need in a few thousand
# documents' time — for a container-based plugin, the next container's
# derived pack. The probe is ANCHORED on the current document's rowid and
# walks FORWARD from it: `OFFSET k` alone made SQLite start at rowid 1 and
# re-filter the 28.6M documents already done to find the frontier — three
# full scans per call, measured live as 10.5 s rdf batches (the engineering
# rule this breaks is the first one: a per-round cost that scales with the
# corpus). With `rowid > ?` it is k index steps from where the stage is.
_PREFETCH_OFFSETS = (5_000, 20_000, 40_000)
_PREFETCH_INTERVAL_S = 10.0
_prefetch_last: dict[tuple[str, str], float] = {}
# Both probes constrain the WHOLE `idx_documents_scope` prefix
# (in_scope, rdf_done, fts_done, embed_done) by equality, so the index's
# implicit rowid tail already gives `ORDER BY rowid` — the same trick the
# candidate walk uses. Constraining only part of it made SQLite sort the
# whole pending set in a TEMP B-TREE instead (caught by the plan check in
# tests/test_worker_selection_cost.py, which is why that check exists).
_RDF_AHEAD = (
    "SELECT uid, native_id, path, lang FROM documents "
    "WHERE in_scope=1 AND rdf_done=0 AND fts_done=0 AND embed_done=0 AND rowid>? "
    "AND error IS NULL AND retries<? ORDER BY rowid LIMIT 1 OFFSET ?")
_FTS_AHEAD = (
    "SELECT uid, native_id, path, lang FROM documents "
    "WHERE in_scope=1 AND rdf_done=1 AND fts_done=0 AND embed_done=0 AND rowid>? "
    "AND error IS NULL AND retries<? ORDER BY rowid LIMIT 1 OFFSET ?")


def _prefetch_ahead(con, collection, stage: str, row) -> None:
    """Submit the preparation of what this stage will read next, to the
    parse pool. Anchored on `row` (the document the stage is on), throttled
    per (collection, stage), and never fatal: a prefetch that does not
    happen costs exactly what it cost before."""
    key = (collection.name, stage)
    now = time.monotonic()
    if now - _prefetch_last.get(key, 0.0) < _PREFETCH_INTERVAL_S:
        return
    _prefetch_last[key] = now
    # Nothing to prepare, or nowhere to prepare it: do not pay for the
    # probes at all. Four of five plugins have no derived cache, and a
    # serial node has no background — running the lookahead for them would
    # be pure cost on the hot path.
    if parallel.pool() is None:
        return
    try:
        if not hasattr(collection.plugin_instance(), "prepare"):
            return
    except Exception:   # noqa: BLE001
        return
    sql = _RDF_AHEAD if stage == "rdf" else _FTS_AHEAD
    try:
        anchor = con.execute("SELECT rowid FROM documents WHERE uid=?",
                             (row["uid"],)).fetchone()
        if anchor is None:
            return
        rows = []
        for offset in _PREFETCH_OFFSETS:
            r = con.execute(sql, (anchor[0], config.MAX_RETRIES, offset)).fetchone()
            if r is not None:
                rows.append(dict(r))
        if rows:
            parallel.prefetch(collection, rows)
    except Exception:   # noqa: BLE001 — an optimisation, never a round's problem
        pass


def _mark_indexed(con, row, chunks, now: int) -> None:
    con.execute(
        "UPDATE documents SET fts_done=1,fts_version=?,entity_version=?,chunks=?,tokens=?,"
        "updated_at=? WHERE uid=?",
        (config.FTS_VERSION, config.ENTITY_VERSION, len(chunks),
         sum(c["tokens"] for c in chunks), now, row["uid"]))


def stage_fts(con, row, collection) -> None:
    """Index `row`, and as many other fts-pending documents as the batch caps
    allow, through ONE tantivy commit.

    A writer costs a 64 MB allocation, a commit and a merge wait no matter
    how little was written, so one writer per document is a fixed toll on
    every document in the corpus. Batching pays it once per batch instead.

    The ORDER is what keeps invariant 2: every document is indexed, THEN the
    single commit lands (inside fts.index_books), THEN the rows are marked.
    A crash anywhere before the mark leaves those documents unmarked, and
    re-running re-indexes over the same delete-then-insert keyed on book_id —
    lossless, and truthful about what is actually done. Marking before the
    commit is the one ordering that would lose data.

    Only `row` may fail the round. A peer that cannot load its chunks, or
    cannot be entity-tagged, is dropped rather than blamed on `row`: it keeps
    its pending state and records its own error when its own turn comes.
    """
    # LOADS AND THE TANTIVY COMMIT RUN OUTSIDE ANY SQLITE TRANSACTION
    # (CLAUDE.md: long non-DB work stays outside the transaction). Chunking
    # is pure compute — a genome chromosome takes the better part of an
    # hour — and holding BEGIN IMMEDIATE across it wedged genome-hg38's
    # manifest solid (observed live: every discovery/event/acquire write
    # "database is locked" for 68+ minutes). Invariant 2's ordering is
    # untouched: index, THEN the tantivy commit, THEN the marks — only the
    # marks are SQLite writes, so the transaction opens just before them
    # (see the BEGIN IMMEDIATE below; _execute_step commits it).
    t0 = time.monotonic()
    peers = []
    want = batch_docs(collection, "fts", config.FTS_BATCH_DOCS)
    if want > 1:
        peers = _fts_peers(con, collection, row, want - 1)
    _prefetch_ahead(con, collection, "fts", row)
    # the batch's fetch/parse/chunk goes to the PARSE POOL (amendment 47):
    # one (ok, chunks) per row, in order; the selected row's failure IS the
    # round's, a peer's failure drops the peer
    results = parallel.chunks_for_rows(collection, [row, *peers])
    ok, first = results[0]
    if not ok:
        parallel.raise_for(first)
    chunks = first
    batch = [(row, chunks)]
    n_chunks = len(chunks)
    for peer, (ok, peer_chunks) in zip(peers, results[1:]):
        if n_chunks >= config.FTS_BATCH_CHUNKS:
            break
        if not ok:
            continue            # a peer never fails the round
        batch.append((peer, peer_chunks))
        n_chunks += len(peer_chunks)

    fts.index_books(((r["lang"], int(r["native_id"]), ch) for r, ch in batch),
                     collection)

    # the tantivy commit landed; only the tags and marks remain — SQLite
    # writes, seconds for a batch. THIS is where the write lock begins
    # (unless a caller already opened one — direct callers in tests and
    # legacy paths wrap the whole stage; their commit covers these writes).
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    ref_store = refs.get_store()
    now = int(time.time())
    # the selected document first and unguarded: its failure IS the round's
    entities.tag_document(con, row["uid"], row["title"], [c["text"] for c in chunks],
                           refs.RESOLVER, ref_store)
    _mark_indexed(con, row, chunks, now)
    for peer, peer_chunks in batch[1:]:
        try:
            entities.tag_document(con, peer["uid"], peer["title"],
                                   [c["text"] for c in peer_chunks],
                                   refs.RESOLVER, ref_store)
            _mark_indexed(con, peer, peer_chunks, now)
        except Exception:   # noqa: BLE001 — indexed but left unmarked, so it
            continue        # simply re-runs; never poisons the selected row
    took = time.monotonic() - t0
    sizing.sizer().record(_batch_key(collection, "fts"), len(batch), took)
    if len(batch) > 1:
        _batch_log(con, "fts", collection,
                   f"indexed {len(batch)} documents / {n_chunks} chunks "
                   f"in one commit ({took:.1f}s)")

def stage_embed(con, row, collection) -> None:
    chunks = _load_chunks(row, collection)
    # lock_con serializes the delete+add pair against a concurrent bundle
    # import in another process (app/bundles/writer_lock.py). Passing it here
    # is what makes the importer's lock mean anything.
    started = time.time()
    stats.recorder().set_in_flight(collection.name, 1)
    try:
        # `stop` bounds SIGTERM to one slice: a 5,335-chunk document is
        # 5.3 h of encoder time, and "finish the current unit" has to mean
        # something shorter than that (invariant 2 — smaller where a
        # document is not possible). What is written stays written.
        n = embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks,
                                    collection, lock_con=con, stop=_stop)
    finally:
        stats.recorder().set_in_flight(collection.name, 0)
    # Timed around the encoder call only — not around _load_chunks, and not
    # around the manifest write below — so the figure on the Stats page is
    # the endpoint's throughput and nothing else. Comparing a CPU encoder
    # with a GPU one is the whole point of measuring it.
    stats.recorder().record_embed(
        collection.name, seconds=time.time() - started, docs=1, chunks=n,
        chars=sum(len(c.get("text") or "") for c in chunks))
    bundle_ledger.clear_verification(con, row["uid"])   # no longer imported provenance
    con.execute(
        "UPDATE documents SET embed_done=1,embed_version=?,updated_at=? WHERE uid=?",
        (config.EMBED_VERSION, int(time.time()), row["uid"]))
    db.log_event(con, "info", "embed", f"embedded {n} chunks", row["uid"])

# The lazy queue's eligibility predicate and its cleanup, shared by every
# selector that serves the queue (_next_book, _next_embed, _embed_candidates)
# so the three cannot drift apart — they carried three copies, and the
# missing filters below were missing from all three.
#
# `error IS NULL AND retries<?` is here for the same reason the rdf and fts
# selections have it: a queued document that CANNOT be embedded was
# otherwise re-picked every round forever, and because the queue is consulted
# before rdf/fts, one such document starves the whole collection.
# CROSS JOIN is load-bearing: it pins the join order to queue -> documents
# (SQLite treats CROSS JOIN as "do not reorder"). With a plain JOIN the
# planner inverted it — SEARCH documents (in_scope, rdf_done) probing the
# queue per row — which scans every rdf-done document to serve a queue
# that is usually EMPTY. Measured live on pubmed: 4.9 s per call at 3.6M
# rdf-done rows, paid at the top of EVERY _next_book, and growing with
# rdf progress — this one query was the pipeline's ~10x brake. Queue-first
# is one tiny scan (the lazy queue holds at most a few hundred rows) plus
# one uid probe per row.
_QUEUE_ELIGIBLE = """
    SELECT d.* FROM embed_queue q CROSS JOIN documents d ON d.uid=q.uid
    WHERE d.in_scope=1 AND d.rdf_done=1 AND d.error IS NULL AND d.retries<?
      AND (d.embed_done=0 OR d.embed_version < ?)
    ORDER BY q.priority DESC, q.ts LIMIT ?"""

# NOT EXISTS, never `uid NOT IN (SELECT ...)`: the IN form materialises EVERY
# not-yet-embedded uid as a list in order to test a handful of queue rows.
# Measured live on pubmed (33.4M documents, 17 queue rows): 22.3s to delete
# ZERO. This form is one index probe per queue row. It also retires rows the
# selection above can no longer serve, so an errored or retry-exhausted
# document does not sit in the queue forever.
_QUEUE_CLEANUP = (
    "DELETE FROM embed_queue WHERE NOT EXISTS ("
    " SELECT 1 FROM documents d WHERE d.uid=embed_queue.uid"
    "  AND d.embed_done=0 AND d.error IS NULL AND d.retries<?)")

# Why no ORDER BY on the rdf/fts selections: they used to end
# `ORDER BY CAST(native_id AS INTEGER)`, and the CAST makes every index
# unusable — SQLite builds a TEMP B-TREE over the whole candidate set to
# return one row. Measured live on pubmed: 10.0s with it, 0.000s without.
# Nothing depends on the order. Each stage flips its own done flag, so the
# document leaves the candidate set and the next call returns the next one;
# every document is still processed exactly once. Index order (from
# idx_documents_scope) replaces numeric native_id order, which was only ever
# cosmetic. The backfill keeps `ORDER BY (lang='en')` — non-English first is
# a deliberate priority (rarest knowledge) — and drops only the CAST
# tiebreaker.
_RDF_PENDING = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=0 AND error IS NULL "
    "AND retries<? LIMIT 1")

# Split in two so the common case is an index prefix on idx_documents_scope
# (in_scope, rdf_done, fts_done) instead of an OR that cannot use the
# fts_done column at all. See the interleaving note in _next_book.
_FTS_FRESH = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=0 "
    "AND error IS NULL AND retries<? LIMIT 1")

_FTS_REINDEX = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=1 "
    "AND error IS NULL AND retries<? AND (fts_version<? OR entity_version<?) "
    "LIMIT 1")

# The reindex selection above is the ONE query here that no index can
# answer: `fts_version < ?` is not in idx_documents_scope, so SQLite walks
# the whole (in_scope, rdf_done, fts_done=1) range to decide there is
# nothing to re-index. Measured on the live node: 16.7 s on wikivec, 18.3 s
# on wikipedia, 127.9 s on pubmed — PER ROUND, on exactly the collections
# that are caught up and should be cheap to skip.
#
# It is therefore walked in WINDOWS of rowid, from a per-collection cursor,
# so no round pays more than one window; when the walk reaches the end
# without a candidate the collection is marked done for this (fts_version,
# entity_version) pair and costs nothing at all until a version bumps —
# which is the only thing that can create work here.
# The window is a ROWID range, and the `+` prefixes are what make it one:
# with the scope columns indexable SQLite prefers idx_documents_scope
# (in_scope, rdf_done, fts_done, embed_done, rowid) and — because
# embed_done sits between the constrained prefix and the rowid tail — it
# cannot use `rowid>?` as a range at all, so every window re-walked the
# whole 28M-row (1,1,1) range: 5.4 s per window, measured, which is the
# same defect one level down. Forced onto the integer primary key a window
# visits exactly its own rows (~0.3 s per 200k), and the walk ends.
_REINDEX_WINDOW = 200_000        # the starting width; sizing.py adjusts it
_REINDEX_WINDOW_MIN = 1_000      # below this the walk would never finish
_REINDEX_WINDOW_MAX = 2_000_000
_FTS_REINDEX_WINDOWED = (
    "SELECT * FROM documents WHERE rowid>? AND rowid<=? "
    "AND +in_scope=1 AND +rdf_done=1 AND +fts_done=1 "
    "AND error IS NULL AND retries<? AND (fts_version<? OR entity_version<?) LIMIT 1")
# path -> (fts_version, entity_version, cursor rowid, max rowid, done)
#: Node-level control values (worker_state, the bandwidth override) read
#: out of a collection's control table as that collection leaves the node,
#: so the next owner can adopt them even when every sibling left at once.
#: Process-lived on purpose: it is a hand-off within one run, not state.
_orphaned_node_control: dict[str, str] = {}

_reindex_cursor: dict[str, tuple[int, int, int, int, bool]] = {}


def reset_reindex_cursors() -> None:
    _reindex_cursor.clear()


def _reindex_key(con, collection) -> str:
    """The cursor's identity: the manifest PATH (two collections can share
    a name and point at different files — the rule the residual sets and
    the counts cache each learned separately). Callers that pass no
    collection still get their own entry from the connection, which knows
    which collection it belongs to; keying those on "" made ONE finished
    walk suppress every other collection's re-index."""
    if collection is not None:
        key = str(collection.manifest_path)
        _remember_label(key, collection.name)
        return key
    return str(getattr(con, "collection_name", "") or id(con))


def _next_reindex(con, collection):
    """One window of the version-driven re-index walk, or None."""
    key = _reindex_key(con, collection)
    versions = (config.FTS_VERSION, config.ENTITY_VERSION)
    state = _reindex_cursor.get(key)
    if state is None or state[:2] != versions:
        top = con.execute("SELECT MAX(rowid) FROM documents").fetchone()[0] or 0
        state = (*versions, 0, int(top), False)
    if state[4]:
        return None                      # nothing to re-index at these versions
    start = state[2]
    # The window is a rowid RANGE, and what it costs depends on how many
    # rows it happens to hold — not on its width, so there is no rate to
    # size it from. It is adjusted from the last probe instead
    # (app/sizing.py `note_window`). Measured live at 498 ms on pubmed with
    # a fixed 200k window, on a query that runs every round of a
    # caught-up collection.
    width = sizing.sizer().window(key, default=_REINDEX_WINDOW,
                                  lo=_REINDEX_WINDOW_MIN, hi=_REINDEX_WINDOW_MAX)
    end = min(start + width, state[3])
    probe = time.monotonic()
    row = con.execute(_FTS_REINDEX_WINDOWED,
                      (start, end, config.MAX_RETRIES, *versions)).fetchone()
    sizing.sizer().note_window(
        key, seconds=time.monotonic() - probe,
        target_s=config.REINDEX_PROBE_TARGET_S, default=_REINDEX_WINDOW,
        lo=_REINDEX_WINDOW_MIN, hi=_REINDEX_WINDOW_MAX)
    if row is not None:
        # stay in this window: the batch takes its peers from it, and the
        # next round re-enters here until the window is clean
        _reindex_cursor[key] = (*versions, start, state[3], False)
        return row
    done = end >= state[3]
    _reindex_cursor[key] = (*versions, end, state[3], done)
    return None

# The inter-phase queue BELOW fts: documents that have cleared the index
# and are waiting for a vector. Bounded by the same LIMIT trick as
# _FTS_BACKLOG — the question is "are there at least N", never "how many".
_EMBED_BACKLOG = (
    "SELECT COUNT(*) FROM (SELECT 1 FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=1 AND embed_done=0 AND error IS NULL AND retries<? LIMIT ?)")


# Bounded on purpose: the inner LIMIT stops the count at one batch, so this
# never walks a 33M-row backlog to answer "is there a batch yet".
_FTS_BACKLOG = (
    "SELECT COUNT(*) FROM (SELECT 1 FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=0 AND error IS NULL AND retries<? LIMIT ?)")


_PHASE_POLL_S = 2.0
_phase_seen: dict[str, tuple[float, bool]] = {}


def reset_phase_memo() -> None:
    _phase_seen.clear()


def _archive_yields(con, collection) -> bool:
    """Should the archive pass stand aside and let fts have the thread?

    THE PHASE CONTROLLER (docs/notes/scheduling.md §10, A2 and A4). A collection's
    stages share one thread, so whichever holds it decides what every stage
    downstream gets to do. An archive pass holds it for up to
    GENESIS_PASS_BUDGET_S — 300 s — and during that time no document
    reaches `fts_done`, so nothing new becomes embeddable and the encoder
    runs dry. That is exactly the burstiness A4 is about: 1,900 embeds/s
    during a pass, zero during the parse phases.

    The rule is deliberately one-directional and cheap: **if the encoder's
    queue is below its low watermark and fts has work ready, fts goes
    first.** Nothing else is inferred, and it cannot rob the archive of its
    thread in the long run, because genesis imports vectors ONLY for
    documents with `fts_done=1` — fts is the archive's upstream as well as
    the encoder's. Running fts is never wasted work for either of them,
    which is what makes this safe without modelling who needs what.
    """
    if config.PHASE_EMBED_LOW <= 0:
        return False
    ready = con.execute(_FTS_BACKLOG, (config.MAX_RETRIES, 1)).fetchone()[0]
    if not ready:
        return False                 # nothing to feed it with; let the archive run
    # MEASURED on the node (pubmed 40M, wikivec 22M): this count costs
    # 46.9 ms cold and 3.5 ms warm, because `error IS NULL AND retries<?`
    # are not in idx_documents_scope (in_scope, rdf_done, fts_done,
    # embed_done) and so cost one table lookup per row counted.
    #
    # Dropping those two filters makes it a COVERING index scan at 0.1 ms —
    # 470x cheaper — and that is the WRONG trade, which is worth writing
    # down because the plan output makes it look free. Errored documents
    # would then count as "waiting for a vector", inflating the queue, and
    # an inflated queue is the one direction that hurts: the archive keeps
    # the thread while the encoder actually starves. Undercounting merely
    # delays the archive by one fts batch, and that batch is never wasted.
    #
    # So: keep the exact count, and bound what it can cost by asking it at
    # most every PHASE_POLL_S. The queue moves far more slowly than that.
    key = str(getattr(con, "collection_name", "") or id(con))
    now = time.monotonic()
    seen = _phase_seen.get(key)
    if seen is not None and now - seen[0] < _PHASE_POLL_S:
        return seen[1]
    waiting = con.execute(
        _EMBED_BACKLOG, (config.MAX_RETRIES, config.PHASE_EMBED_LOW)).fetchone()[0]
    verdict = waiting < config.PHASE_EMBED_LOW
    _phase_seen[key] = (now, verdict)
    return verdict


# ---- amendment 38: coverage-aware dense backfill ---------------------------
# Under "all" the local encoder takes only what a declared archive CANNOT
# cover: documents outside every non-final shard's id range. The selection
# walks the scope index in rowid order (its own order — no ORDER BY over
# the candidate set, which sorted millions of rows per round) from a
# per-collection CURSOR kept in memory, keyed on the manifest PATH, so a
# round never re-walks the covered prefix (8M rows on pubmed). The cursor
# expires after a short TTL (one index pass) and resets when a shard goes
# final (genesis.coverage_epoch): the documents in that range the archive
# did not cover sit BELOW the cursor and are now the encoder's. Without an
# archive there is no cursor at all — a document that clears fts must be
# embeddable in the same round.
_BACKFILL_SQL = (
    "SELECT rowid AS rid, * FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=1 AND embed_done=0 AND error IS NULL AND retries<? AND rowid>? "
    "{covered} ORDER BY rowid LIMIT ?")
_REEMBED_SQL = (
    "SELECT rowid AS rid, * FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=1 AND embed_done=1 AND embed_version<? AND error IS NULL "
    "AND retries<? AND rowid>? ORDER BY rowid LIMIT ?")
_BACKFILL_CURSOR_TTL_S = 120
_backfill_cursor: dict[tuple[str, str], tuple[int, float, int]] = {}  # (path, kind) -> (epoch, since, rowid)
_backfill_cursor_lock = threading.Lock()


def _covered_ranges(con, collection) -> list[tuple[int, int]]:
    """Merged id ranges of the archive's shards that are not final — what
    the archive can still deliver, so the local encoder skips them. A
    REJECTED archive covers nothing (it is not this function)."""
    if collection is None or not collection.genesis_archive:
        return []
    archive = bundle_genesis.ARCHIVES.get(collection.genesis_archive)
    if archive is None or archive.local or not archive.ids_per_shard:
        return []
    rows = con.execute("SELECT shard, state, proven_at, docs FROM genesis_imports "
                       "WHERE archive=?", (archive.archive_id,)).fetchall()
    if bundle_genesis.archive_stopped(rows):
        return []           # nothing proven, something rejected: not this function
    # a final shard delivered all it can; a rejected one (under a proven
    # archive) never will — both release their range to the encoder
    done = {r["shard"] for r in rows if r["state"] in ("final", "rejected")}
    ranges = sorted(rng for s in archive.shards if s not in done
                    for rng in (archive.id_range_of(s),) if rng)
    merged: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def _backfill_rows(con, collection, limit: int) -> list:
    """Dense backfill candidates under "all": new documents first, then
    version-driven re-embeds — both cursor-paged when an archive is
    declared, both walked from the start otherwise."""
    ranges = _covered_ranges(con, collection)
    key = str(collection.manifest_path) if collection is not None else ""
    covered, params = "", []
    if ranges:
        covered = "AND NOT (" + " OR ".join(
            "CAST(native_id AS INTEGER) BETWEEN ? AND ?" for _ in ranges) + ")"
        for lo, hi in ranges:
            params += [lo, hi]
    rows = _cursor_page(
        con, key, "new", bool(ranges),
        lambda start, n: con.execute(_BACKFILL_SQL.format(covered=covered),
                                     (config.MAX_RETRIES, start, *params, n)).fetchall(),
        limit)
    if len(rows) < limit:
        rows += _cursor_page(
            con, key, "ver", bool(ranges),
            lambda start, n: con.execute(_REEMBED_SQL, (config.EMBED_VERSION, config.MAX_RETRIES,
                                                        start, n)).fetchall(),
            limit - len(rows))
    return rows


def _cursor_page(con, key: str, kind: str, use_cursor: bool, query, limit: int) -> list:
    if not use_cursor:
        return query(-1, limit)
    epoch, now = bundle_genesis.coverage_epoch(), time.time()
    with _backfill_cursor_lock:
        cur = _backfill_cursor.get((key, kind))
    live = cur is not None and cur[0] == epoch and now - cur[1] < _BACKFILL_CURSOR_TTL_S
    rows = query(cur[2] if live else -1, limit)
    if rows:
        # resume FROM the first returned row: if it is deferred rather than
        # embedded it is selected again; everything before it was covered
        nxt = int(rows[0]["rid"]) - 1
    else:
        # nothing eligible below the end: skip to it until the cursor expires
        nxt = int(con.execute("SELECT COALESCE(MAX(rowid), -1) FROM documents").fetchone()[0])
    with _backfill_cursor_lock:
        _backfill_cursor[(key, kind)] = (epoch, cur[1] if live else now, nxt)
    return rows


def reset_backfill_cursors() -> None:
    with _backfill_cursor_lock:
        _backfill_cursor.clear()


def _ingest_pending(con) -> bool:
    """Does this collection still have rdf or fts work? Two LIMIT-1 index
    probes. Genesis uses it to decide whether a shard read may wait for a
    batch (rdf/fts would otherwise starve behind it) or should happen now
    (nothing else to do on this thread)."""
    return con.execute(_RDF_PENDING, (config.MAX_RETRIES,)).fetchone() is not None \
        or con.execute(_FTS_FRESH, (config.MAX_RETRIES,)).fetchone() is not None


def _next_book(con, scope, embed_allowed=True, collection=None):
    # THE ROUND IS GATED, not only the scan. rdf/fts/embed ran over
    # whatever was already in the manifest with no licence check, so a
    # withheld collection still got indexed and embedded and "withheld"
    # meant only "scan withheld, everything else proceeds". Invariant 9
    # gates the USE. `_license_allows` keeps its own once-an-hour log, so
    # asking every round does not become per-round spam.
    if collection is not None and not _license_allows(con, collection, "indexing"):
        return None, None
    # 1. priority embeds (lazy queue), regardless of backfill mode
    if embed_allowed and scope["embed_mode"] in ("lazy", "all"):
        r = con.execute(_QUEUE_ELIGIBLE,
                        (config.MAX_RETRIES, config.EMBED_VERSION, 1)).fetchone()
        if r:
            return "embed", r
        con.execute(_QUEUE_CLEANUP, (config.MAX_RETRIES,))
    # 2/3. rdf and fts INTERLEAVE, one document at a time.
    #
    # This module's contract is "stage order PER DOCUMENT: rdf -> fts ->
    # embed", but selecting rdf first made that order GLOBAL: every document
    # in the collection finished rdf before any document reached fts. On a
    # large corpus that is indistinguishable from fts never running — live,
    # wikivec sat at rdf 172,538 of 8,495,470 with fts exactly 0 — and
    # everything downstream of an fts commit waits for the whole corpus
    # instead of for one document. That includes genesis, which imports
    # published vectors only for documents with fts_done=1, so a collection
    # whose vectors need no encoder at all still could not use them.
    #
    # They interleave a BATCH at a time, not a document at a time. fts commits
    # one tantivy writer for up to FTS_BATCH_DOCS documents (see stage_fts),
    # so taking fts the instant a single document is ready would pay the full
    # writer cost per document and batching would never happen. Instead rdf
    # runs ahead until a batch is worth committing, then fts takes it.
    #
    # fts therefore lags rdf by at most one batch — against the whole corpus
    # under the old stage-global ordering. With FTS_BATCH_DOCS=1 this degrades
    # exactly to one-for-one alternation.
    fts_row = con.execute(_FTS_FRESH, (config.MAX_RETRIES,)).fetchone()
    if fts_row is not None:
        # the watermark is what fts will ACTUALLY take (app/sizing.py), not
        # the configured count: sized batches would otherwise wait for a
        # watermark of 200 and then commit 4,000
        want = (batch_docs(collection, "fts", config.FTS_BATCH_DOCS)
                if collection is not None else config.FTS_BATCH_DOCS)
        ready = con.execute(
            _FTS_BACKLOG, (config.MAX_RETRIES, want)).fetchone()[0]
        if ready >= want:
            return "fts", fts_row
    r = con.execute(_RDF_PENDING, (config.MAX_RETRIES,)).fetchone()
    if r:
        return "rdf", r
    # rdf is exhausted, so drain whatever the tail holds even if it is short
    # of a full batch — otherwise the last documents of a corpus never index.
    if fts_row is not None:
        return "fts", fts_row
    # Version-driven RE-indexing (FTS_VERSION / ENTITY_VERSION, each able to
    # force a re-run independently) comes last on purpose: a bump makes every
    # document a candidate at once, and taking those ahead of rdf would stall
    # ingestion of new documents for the length of a full re-index. It is
    # also the one selection here that cannot use the fts_done column, so
    # keeping it last means it is only reached on a caught-up collection.
    r = _next_reindex(con, collection)
    if r:
        return "fts", r
    # 4. dense backfill only in 'all' mode — coverage-aware (amendment 38):
    # what a declared archive can still deliver is not the encoder's
    if embed_allowed and scope["embed_mode"] == "all":
        rows = _backfill_rows(con, collection, 1)
        if rows:
            return "embed", rows[0]
    return None, None


def _next_embed(con, scope, collection=None):
    """Embed-stage selection ONLY — the main loop's share of the pipeline
    when per-collection threads own rdf/fts. Exactly _next_book's parts 1
    (lazy queue) and 4 (backfill under 'all'), so the two selectors can
    never claim the same document: a document is embed-eligible only after
    its fts commit, and a pipeline thread never touches it again after that."""
    if scope["embed_mode"] not in ("lazy", "all"):
        return None, None
    r = con.execute(_QUEUE_ELIGIBLE,
                    (config.MAX_RETRIES, config.EMBED_VERSION, 1)).fetchone()
    if r:
        return "embed", r
    con.execute(_QUEUE_CLEANUP, (config.MAX_RETRIES,))
    if scope["embed_mode"] == "all":
        rows = _backfill_rows(con, collection, 1)
        if rows:
            return "embed", rows[0]
    return None, None

STAGES = {"rdf": stage_rdf, "fts": stage_fts, "embed": stage_embed}


def _embed_candidates(con, scope, limit: int, collection=None):
    """Up to `limit` embed-stage candidates, queue priority first — the same
    two selections _next_book makes, widened for the parallel path."""
    rows = []
    if scope["embed_mode"] in ("lazy", "all"):
        rows += con.execute(
            _QUEUE_ELIGIBLE,
            (config.MAX_RETRIES, config.EMBED_VERSION, limit)).fetchall()
    if len(rows) < limit and scope["embed_mode"] == "all":
        seen = {r["uid"] for r in rows}
        rows += [r for r in _backfill_rows(con, collection, limit)
                 if r["uid"] not in seen][: limit - len(rows)]
    return rows


def _embed_parallel(con, collection, recipe) -> bool:
    """CPU-encoder collections (recipe.concurrency > 1) embed several WHOLE
    DOCUMENTS concurrently. Per-doc request concurrency cannot help a corpus
    whose documents are single chunks (PubMed abstracts); the unit of
    parallelism must be the document. Each thread derives chunks and writes
    vectors on its own manifest connection (the Lance writer lock is a real
    SQLite lock, cross-connection); manifest row updates and logging happen
    back on this thread, which owns `con`.

    GPU llama-server collections never come here (concurrency defaults 1) —
    document-serial embedding is a decision, not an accident: parallel multi-
    sequence processing is precisely the failure mode the batch gate guards.
    """
    scope = db.get_scope(con)
    rows = _embed_candidates(con, scope, recipe.concurrency * 2, collection)
    if not rows:
        return False

    def work(row):
        if _stop.is_set():
            return (row["uid"], None, "stopped")
        try:
            chunks = _load_chunks(row, collection)
            tcon = db.connect(collection)
            started = time.time()
            try:
                n = embeddings.upsert_book(int(row["native_id"]), row["lang"],
                                            chunks, collection, lock_con=tcon)
            finally:
                tcon.close()
            # Measured HERE as well as in stage_embed, because every CPU
            # encoder in the deployment takes this path and only the GPU one
            # takes the other. Recording in stage_embed alone left the Stats
            # page permanently blank for exactly the encoders it exists to
            # compare. The recorder is lock-guarded, so several document
            # threads reporting at once is fine.
            stats.recorder().record_embed(
                collection.name, seconds=time.time() - started, docs=1, chunks=n,
                chars=sum(len(c.get("text") or "") for c in chunks))
            return (row["uid"], n, None)
        except Exception as e:  # noqa: BLE001 — isolated per document
            if endpoint_unavailable(e):
                return (row["uid"], None, DEFERRED)
            return (row["uid"], None, f"{type(e).__name__}: {e}"[:400])

    # A document only commits when ALL of its chunks are done, so a large one
    # is a long silence — a yeast chromosome is ~200 chunks and DNABERT-2 on
    # CPU answers in ~5s each, which is a quarter of an hour with nothing in
    # the log. Announcing the batch is the difference between "slow" and
    # "stuck", which an operator cannot otherwise tell apart.
    pending_chunks = sum(r["chunks"] or 0 for r in rows)
    db.log_event(con, "info", "embed",
                 f"embedding {len(rows)} document(s)"
                 + (f" / {pending_chunks} chunks" if pending_chunks else "")
                 + f" with {recipe.concurrency} workers")

    # A SLIDING WINDOW, not a batch-and-join. Measured live on the AMD
    # instance (60 s GPU trace at 1 Hz): the batch form ran ~20 s at
    # 90-100 % and then sat ~9 s idle — every batch ended with a tail where
    # one or two workers embedded the longest book alone, then a full stop
    # while the main loop turned around. 30 % of the GPU wasted, in a
    # rhythm the operator could see. Now a worker that finishes is handed
    # the next candidate immediately, results commit on THIS thread as they
    # land, and the call keeps refilling for EMBED_STREAM_S before draining
    # — the round loop still gets control back, just less often. Nothing
    # about the requests changes: one document per worker, one text per
    # request (the batch gate's rule), document-serial inside a worker.
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    deadline = time.time() + getattr(config, "EMBED_STREAM_S", 120.0)
    in_flight: dict = {}          # future -> uid
    seen: set[str] = {r["uid"] for r in rows}
    queue: list = list(rows)
    done = deferred = 0

    def commit(uid, n, err) -> None:
        nonlocal done, deferred
        now = int(time.time())
        if err == "stopped":
            return
        if err == DEFERRED:
            # the encoder was not answering: leave the row exactly as it was
            deferred += 1
            return
        if err is None:
            con.execute(
                "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? "
                "WHERE uid=?", (config.EMBED_VERSION, now, uid))
            bundle_ledger.clear_verification(con, uid)
            con.execute("DELETE FROM embed_queue WHERE uid=?", (uid,))
            done += 1
        else:
            con.execute(
                "UPDATE documents SET retries=retries+1, error=?, updated_at=? "
                "WHERE uid=?", (f"embed: {err}"[:500], now, uid))
            db.log_event(con, "error", "embed", err, uid)

    def refill(pool) -> None:
        nonlocal queue
        while len(in_flight) < recipe.concurrency and not _stop.is_set() \
                and time.time() < deadline:
            if not queue:
                # candidates committed by THIS thread are already out of
                # the selection; the ones in flight are not, hence `seen`
                fresh = [r for r in _embed_candidates(con, scope, recipe.concurrency * 2, collection)
                         if r["uid"] not in seen]
                if not fresh:
                    return
                seen.update(r["uid"] for r in fresh)
                queue = fresh
            row = queue.pop(0)
            in_flight[pool.submit(work, row)] = row["uid"]
        stats.recorder().set_in_flight(collection.name, len(in_flight))

    try:
        with ThreadPoolExecutor(max_workers=recipe.concurrency) as pool:
            refill(pool)
            while in_flight:
                finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in finished:
                    in_flight.pop(fut)
                    commit(*fut.result())
                # a dead encoder must not be walked document by document:
                # stop refilling as soon as a deferral shows up
                if deferred == 0:
                    refill(pool)
                stats.recorder().set_in_flight(collection.name, len(in_flight))
    finally:
        stats.recorder().set_in_flight(collection.name, 0)
    if done:
        db.log_event(con, "info", "embed",
                     f"embedded {done} document(s) at concurrency "
                     f"{recipe.concurrency}")
    if deferred:
        db.log_event(con, "warn", "embed",
                     f"{deferred} document(s) deferred — encoder unreachable; "
                     f"untouched, and retried when it answers again")
    # Nothing embedded and something deferred means the endpoint is down:
    # report no work so the caller yields the round rather than spinning
    # through the rest of the corpus against a dead encoder.
    return bool(done)

# Acquisition runs in a BACKGROUND THREAD per collection (§5.2's "download ∥
# checksum ∥ parse" axis). Observed live: a `grow full` on PubMed put a
# multi-hour 45 GB fetch inside the round loop and froze every collection's
# pipeline for the duration — the single-threaded worker must keep rounding
# while bytes move. One thread per collection at most; run_acquisition gets
# its OWN manifest connection (its threading contract: the calling thread
# owns the con exclusively; SQLite WAL + busy_timeout handle cross-connection
# writes). Completion raises a flag the main loop turns into a discovery
# scan, so newly fetched files enter the pipeline without waiting for the
# next interval.
#: Per-collection removal signal, independent of WORKER_PIPELINE_THREADS —
#: `_sync_pipeline_threads` mints its own only in pipeline mode and runs
#: LATER in the round than `_sync_collections`, so neither could serve the
#: byte-moving threads. They stopped only on SIGTERM: a retired collection
#: went on fetching from its origin and draining its archive.
_gone: dict[str, threading.Event] = {}


def _gone_event(name: str) -> threading.Event:
    return _gone.setdefault(name, threading.Event())

_acquire_threads: dict[str, threading.Thread] = {}
_acquire_done: dict[str, bool] = {}      # collection -> did it fetch anything
_acquire_lock = threading.Lock()


def _acquire_worker(collection, spec, profile: str, reason: str) -> None:
    con = db.connect(collection)
    fetched_any = False
    try:
        result = acquire_engine.run_acquisition(
            con, collection, spec, profile,
            stop=_AnyStop(_stop, _gone_event(collection.name)))
        fetched_any = bool(result.get("fetched"))
        db.log_event(con, "info", "acquire", f"corpus fetch ({reason}): {result}")
    except (LicenseAckRequiredError, DiskPreflightError) as e:
        db.log_event(con, "error", "acquire", str(e))
    except sqlite3.OperationalError as e:
        # The manifest was busy past the busy timeout — a discovery pass
        # streaming millions of rows can starve this thread. NOTHING moved
        # and nothing is inconsistent: the claim was never taken, so this
        # is a deferral, not a failure. The completion flag below turns
        # into a discovery request, which starts the fetch again.
        db.log_event(con, "warn", "acquire",
                     f"corpus fetch ({reason}) deferred — manifest busy "
                     f"({e}); retrying next round")
    except Exception as e:   # noqa: BLE001 — a fetch must never kill its thread silently
        db.log_event(con, "error", "acquire",
                     f"corpus fetch ({reason}) failed: {type(e).__name__}: {e}"[:400])
    finally:
        con.close()
        with _acquire_lock:
            # only a fetch that actually LANDED files needs a rescan; a
            # no-op fetch requesting one is how the acquire/discover loop
            # sustained itself
            _acquire_done[collection.name] = fetched_any


def _acquire(con, collection, reason: str) -> None:
    """Starts (or skips, if one is already running) the background fetch for
    a collection that has opted in. Off by default (acquire_enabled False) so
    a hand-populated deployment sees no surprise outbound traffic."""
    if not collection.acquire_enabled:
        return
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        return
    if not collection.profile:
        # `collection.profile or "demo"` used to stand below. A collection
        # with acquisition ON and no profile declared is a configuration
        # error, and answering it by fetching the DEMO slice is the wrong
        # kind of answer: the operator asked for a corpus and silently got
        # a sample, with the Library's "fetched N of M" counting against
        # the sample's denominator so it even looked complete.
        #
        # Observed live 2026-09-11: gutenberg's `profile = "full"` was lost
        # in a TOML rewrite and nothing said so — the card read
        # "(hand-populated)" and a background fetch would have pulled demo.
        now = time.time()
        if now - _profile_nagged.get(collection.name, 0.0) > 3600:
            _profile_nagged[collection.name] = now
            db.log_event(con, "warn", "acquire",
                         f"{collection.name}: acquisition is enabled but no "
                         f"profile is declared, so there is nothing to fetch. "
                         f"Set `profile` in collections/{collection.name}.toml "
                         f"(offered: {', '.join(sorted(spec.profiles))})")
        return
    with _acquire_lock:
        existing = _acquire_threads.get(collection.name)
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_acquire_worker,
            args=(collection, spec, collection.profile or "demo", reason),
            name=f"acquire-{collection.name}", daemon=True)
        _acquire_threads[collection.name] = thread
    db.log_event(con, "info", "acquire",
                 f"corpus fetch ({reason}) started in the background")
    thread.start()


_archive_threads: dict[str, threading.Thread] = {}
_archive_log_state: dict[str, dict] = {}


def _archive_worker(collection, reason: str) -> None:
    con = db.connect(collection)
    try:
        state = _archive_log_state.setdefault(collection.name, {})
        result = bundle_genesis_fetch.run_fetcher(
            con, collection, stop=_AnyStop(_stop, _gone_event(collection.name)),
            log_state=state)
        if result.get("fetched"):
            db.log_event(con, "info", "genesis",
                         f"archive fetch ({reason}): landed {len(result['fetched'])} "
                         f"shard(s), stopped: {result['reason']}")
    except LicenseAckRequiredError as e:
        db.log_event(con, "error", "genesis", str(e))
    except sqlite3.OperationalError as e:
        db.log_event(con, "warn", "genesis",
                     f"archive fetch ({reason}) deferred — manifest busy ({e})")
    except Exception as e:   # noqa: BLE001 — a fetch must never kill its thread silently
        db.log_event(con, "error", "genesis",
                     f"archive fetch ({reason}) failed: {type(e).__name__}: {e}"[:400])
    finally:
        con.close()


_archive_last_kick: dict[str, float] = {}
_ARCHIVE_KICK_INTERVAL_S = 600


def _acquire_archive(con, collection, reason: str, force: bool = False) -> None:
    """Amendment 38: a REMOTE genesis archive is the collection's second
    acquisition stream — its shards download on their own thread, under
    the `archive` budget class (behind the corpus), whenever the corpus
    fetch would run. Same opt-in (acquire_enabled), same license gate.
    Called every round from _run_one_step: restarts are throttled to one
    per _ARCHIVE_KICK_INTERVAL_S unless `force` (a shard just went final
    and freed a cache slot)."""
    if not collection.acquire_enabled or not collection.genesis_archive:
        return
    if _paused.is_set():
        return
    archive = bundle_genesis.ARCHIVES.get(collection.genesis_archive)
    if archive is None or archive.local:
        return
    now = time.time()
    with _acquire_lock:
        existing = _archive_threads.get(collection.name)
        if existing is not None and existing.is_alive():
            return
        if not force and now - _archive_last_kick.get(collection.name, 0.0) \
                < _ARCHIVE_KICK_INTERVAL_S:
            return
        _archive_last_kick[collection.name] = now
        thread = threading.Thread(target=_archive_worker, args=(collection, reason),
                                  name=f"archive-{collection.name}", daemon=True)
        _archive_threads[collection.name] = thread
    thread.start()


def _remote_archive(collection) -> bool:
    archive = bundle_genesis.ARCHIVES.get(collection.genesis_archive or "")
    return archive is not None and not archive.local


_vindex_last: dict[str, float] = {}     # lance dir -> last maintenance attempt


def _maintain_vector_index(con, collection) -> bool:
    """The local vector index (pack-format.md §5): build once the table is
    big enough, fold new rows in when enough accumulated. Throttled per
    collection; runs on the INDEX MAINTAINER thread — never on the GPU loop,
    never on a pipeline thread: a refresh compacts the table, which took
    41 minutes on the 16.8M-row wikivec table (HDD) and idled that
    collection's rdf/fts/archive for the duration. NO manifest lock is
    held either (that blocked every event and commit of the collection);
    instead vector_index.maintenance() marks the collection busy and the
    worker's vector writers stand down for it — a deferral, never a
    conflict, never lost data. Returns True when it did work."""
    from . import vector_index
    key = str(collection.lance_dir)
    now = time.time()
    if now - _vindex_last.get(key, 0.0) < config.VECTOR_INDEX_REFRESH_S:
        return False
    _vindex_last[key] = now
    try:
        out = vector_index.ensure(collection, con)
    except Exception as e:   # noqa: BLE001 — an index is an optimisation, never a crash
        db.log_event(con, "warn", "index",
                     f"vector index maintenance deferred: {type(e).__name__}: {e}"[:300])
        return False
    return bool(out)


def _vector_writes_allowed(collection) -> bool:
    """Whether this collection's vector writers may run.

    They ALWAYS may. This used to answer "not while the index maintainer
    is working", which was over-cautious and expensive: a compaction of
    wikivec's 70 GB table takes ~41 minutes, and the collection did no
    embedding and no archive import for the whole of it — measured live,
    and with the refresh threshold reached every few minutes of work the
    collection would have spent most of its life standing down.

    Lance is MVCC: a concurrent delete+add during `optimize()` is
    supported (measured: 210 writes during a maintenance pass, zero
    errors), and a rare commit conflict surfaces as an exception that
    `vector_store_contended` turns into a deferral — the document is
    untouched and retried, never errored. The maintainer's `busy` flag
    survives for what it is actually for: not running two maintenance
    operations on one table at once."""
    return True


def vector_store_contended(exc: BaseException) -> bool:
    """Is this failure the vector store's optimistic concurrency (another
    writer or a maintenance pass committing first) rather than the
    document's fault? Then it is a DEFERRAL: nothing is written to the
    document and it is retried, exactly as for a busy manifest."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(s in text for s in (
        "commit conflict", "commitconflict", "concurrent update",
        "retryable commit", "conflicting transaction", "version conflict"))


INDEX_MAINTAINER_INTERVAL_S = 60.0


def _index_maintainer() -> None:
    """One low-priority thread for every collection's vector index. Its
    own connections (SQLite is thread-affine); idles while the node is
    paused; one collection at a time, so two hour-long builds never run
    together."""
    cons: dict[str, object] = {}
    while not _stop.wait(INDEX_MAINTAINER_INTERVAL_S):
        try:
            collections = collections_mod.load_all()
            if not collections:
                continue
            control = db.connect(collections[0])
            try:
                state = db.worker_state(control)
            finally:
                control.close()
            if state in ("paused", "seed-only"):
                continue
            # Served by the SAME fair-share rotation as the encoder
            # (app/sched.py), not in list order. One collection at a time is
            # deliberate — two hour-long builds must never run together —
            # which is exactly what makes the order matter: whoever is
            # served last waits for everyone ahead. DRR gives
            # longest-waiting-first and starvation-freedom without a second
            # ordering heuristic to get wrong, and a collection that is not
            # due drops out of the rotation instead of holding a place.
            s = sched.scheduler()
            by_name = {c.name: c for c in collections}
            s.sync(sched.RES_INDEX, by_name)
            for name in by_name:
                s.declare(sched.RES_INDEX, name)
            for _ in range(len(by_name)):
                if _stop.is_set():
                    break
                turn = s.acquire(sched.RES_INDEX)
                if turn is None:
                    break
                c = by_name.get(turn.collection)
                if c is None:
                    turn.release(idle=True)
                    continue
                key = str(c.manifest_path)
                if key not in cons:
                    cons[key] = db.connect(c)
                t0 = time.monotonic()
                did = _maintain_vector_index(cons[key], c)
                turn.spent(time.monotonic() - t0, units=1 if did else 0)
                # not due, or nothing to fold in: give the slot back rather
                # than sit on it, and rejoin at the tail when there is work
                turn.release(idle=not did)
        except Exception:   # noqa: BLE001 — maintenance must never kill the worker
            pass
    for con in cons.values():
        con.close()


def _drain_remote_archive(con, collection, stop) -> bool:
    """One genesis round for the collection's declared archive: kick the
    fetcher (throttled inside), apply the next shard with work, and — when
    a shard went final — land the next one into the freed cache slot.
    Returns True when a shard was worked (the caller skips its idle sleep
    / its own selection this round). Failures are ledgered inside
    drain_next_shard; anything unexpected is logged, never raised."""
    if not collection.genesis_archive:
        return False
    if not _vector_writes_allowed(collection):
        return False            # the index maintainer holds the table; next round
    if not _license_allows(con, collection, "archive import"):
        return False            # vectors are USE of the corpus they describe
    _acquire_archive(con, collection, "round")
    try:
        result = bundle_genesis.drain_next_shard(
            con, collection, stop=stop, eager=not _ingest_pending(con))
    except Exception as e:   # noqa: BLE001 — an archive must never crash the worker
        db.log_event(con, "error", "genesis",
                     f"unexpected genesis failure: {type(e).__name__}: {e}"[:400])
        return False
    if result is None:
        return False
    if result.get("state") == "final":
        _acquire_archive(con, collection, "shard final", force=True)
    return True


def _finish_completed_acquires(cons, all_collections, last_discover,
                                pipeline: bool = False) -> None:
    """Turn finished background fetches into discovery scans: on the
    collection's pipeline thread when one exists, otherwise inline on the
    main thread (which owns the per-collection cons)."""
    with _acquire_lock:
        done = dict(_acquire_done)
        _acquire_done.clear()
    if pipeline:
        # Filtered on the live set, exactly as the non-pipeline branch below
        # already does. A fetch that finished AFTER its collection was
        # retired re-armed a discovery request for a dead name; the retired
        # collection's pipeline thread is still polling until
        # `_sync_pipeline_threads` runs later in the round, so the request
        # could be taken and a scan started against a collection just taken
        # out of service.
        live = {c.name for c in all_collections}
        for name, fetched in done.items():
            if fetched and name in live:   # nothing landed → nothing to rescan
                _request_discovery(name, "background fetch finished",
                                    acquire=False)
        return
    for collection in all_collections:
        if done.get(collection.name) and collection.name in cons:
            db.log_event(cons[collection.name], "info", "discover",
                         "background fetch finished — scanning")
            stats = discovery.sync_collection(cons[collection.name], collection)
            if stats.get("added") or stats.get("changed"):
                db.log_event(cons[collection.name], "info", "discover",
                             f"picked up {stats.get('added', 0)} new, "
                             f"{stats.get('changed', 0)} changed books")
            last_discover[collection.name] = time.time()

def _drain_bundle_inbox(con, collection) -> int:
    """Import any bundles waiting in this collection's inbox.

    This is the alternative implementation of the embed stage
    (architecture-v2.md §5): a document covered by an accepted bundle reaches
    embed_done=1 without the embed stage ever computing a vector for it.

    Outcomes move the directory so the inbox drains:
      accepted / rejected -> processed, moved aside with the verdict recorded
      retryable           -> LEFT IN PLACE (the embed endpoint being down says
                             nothing about the bundle, so it is retried next
                             round rather than discarded)

    A bundle never crashes the worker: import_bundle catches its own errors and
    returns a summary, and anything unexpected is caught here.
    """
    inbox = bundle_importer.inbox_dir(collection)
    if not inbox.is_dir():
        return 0
    processed = 0
    for entry in sorted(inbox.iterdir()):
        if _stop.is_set():
            break
        if not entry.is_dir():
            continue
        try:
            result = bundle_importer.import_bundle(con, collection, entry, stop=_stop)
        except Exception as e:   # noqa: BLE001 - defense in depth; must never escape
            db.log_event(con, "error", "bundle",
                         f"unexpected import failure for {entry.name}: "
                         f"{type(e).__name__}: {e}"[:400])
            continue
        if result.get("retryable"):
            db.log_event(con, "warn", "bundle",
                         f"{entry.name}: {result['error']} — will retry")
            continue
        destination = "done" if result.get("ok") else "rejected"
        if result.get("ok"):
            db.log_event(con, "info", "bundle",
                         f"{entry.name}: imported {result.get('chunks', 0)} chunks "
                         f"across {result.get('uids', 0)} document(s)")
        else:
            db.log_event(con, "warn", "bundle",
                         f"{entry.name}: rejected — {result['error']}"[:400])
        try:
            target = inbox.parent / destination
            target.mkdir(parents=True, exist_ok=True)
            entry.rename(target / entry.name)
        except OSError:
            pass   # leaving it in place is safe: re-import is a ledger no-op
        processed += 1
    return processed


def _scan_only(con, collection, reason: str, stop=None) -> None:
    """Scan WITHOUT starting a fetch — see _request_discovery(acquire=False)."""
    # The same gate `_discover` has. This path had none, and it is reachable
    # for any collection whose discovery request carried acquire=False — so
    # a yellow source with no acknowledgement was scanned and indexed
    # through it while the acquiring path correctly refused. Invariant 9
    # binds the USE, not the route that reached it.
    if not _license_allows(con, collection, "corpus scan"):
        return
    db.log_event(con, "info", "discover", f"corpus scan ({reason})")
    stats = discovery.sync_collection(con, collection, stop=stop)
    if stats.get("added") or stats.get("changed"):
        db.log_event(con, "info", "discover",
                     f"picked up {stats.get('added', 0)} new, "
                     f"{stats.get('changed', 0)} changed books")


_license_nagged: dict[str, float] = {}
#: last "no profile declared" warning per collection — see _acquire
_profile_nagged: dict[str, float] = {}


def _license_allows(con, collection, use: str) -> bool:
    """Invariant 9 / amendment 36, for EVERY way data arrives: a yellow-
    or red-class collection without a valid acknowledgement of its terms
    (the origin's, or the pack's byte-exact copy) is not scanned and its
    archive is not applied. Logged once an hour per collection."""
    from . import licensing
    if licensing.state_for(collection) != licensing.MISSING:
        return True
    now = time.time()
    if now - _license_nagged.get(collection.name, 0.0) > 3600:
        _license_nagged[collection.name] = now
        db.log_event(con, "warn", "control",
                     f"{use} withheld: license_class={licensing.license_class_for(collection)} "
                     f"needs an acknowledgement of the terms (Library → acknowledge license…) "
                     f"— a hand-populated or packed folder is not a bypass (invariant 9)")
    return False


def _discover(con, collection, reason: str, stop=None) -> None:
    _acquire(con, collection, reason)
    _acquire_archive(con, collection, reason)
    if not _license_allows(con, collection, "corpus scan"):
        return
    db.log_event(con, "info", "discover", f"corpus scan ({reason})")
    stats = discovery.sync_collection(con, collection, stop=stop)
    if stats.get("added") or stats.get("changed"):
        db.log_event(con, "info", "discover",
                     f"picked up {stats.get('added',0)} new, "
                     f"{stats.get('changed',0)} changed books")


# Discovery scans, one thread per collection at most, OFF the pipeline
# thread. A scan used to run on the pipeline thread itself, so a collection
# did no rdf, fts or genesis for the scan's whole duration — measured live
# on wikivec: 1,083 s after every restart (the corpus is still being
# enumerated), and restarts are frequent. The scan is the one stage whose
# unit is the corpus; everything else is per document. Now it runs beside
# the pipeline: its writes are short batched transactions
# (discovery.DISCOVER_BATCH_REFS) against the same WAL manifest, which the
# pipeline's busy_timeout and `manifest_contended` deferral already
# tolerate, and it stops at the next ref on SIGTERM or collection removal
# with its progress committed and its cursor advanced exactly that far.
_scan_threads: dict[str, threading.Thread] = {}
_scan_lock = threading.Lock()


class _AnyStop:
    """is_set() when any of the given events is — the scan must stop both
    on SIGTERM and when its collection is removed mid-run."""
    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def scan_running(name: str) -> bool:
    with _scan_lock:
        t = _scan_threads.get(name)
        return t is not None and t.is_alive()


def _start_scan(collection, reason: str, acquire: bool, gone: threading.Event) -> bool:
    """Start this collection's scan on its own thread, unless one is already
    running (then the caller keeps the request queued). Returns whether a
    thread was started."""
    with _scan_lock:
        current = _scan_threads.get(collection.name)
        if current is not None and current.is_alive():
            return False
        thread = threading.Thread(target=_scan_worker,
                                  args=(collection, reason, acquire, gone),
                                  name=f"scan-{collection.name}", daemon=True)
        _scan_threads[collection.name] = thread
        thread.start()
        return True


def _scan_worker(collection, reason: str, acquire: bool, gone: threading.Event) -> None:
    con = db.connect(collection)            # connections are thread-affine
    stop = _AnyStop(_stop, gone)
    try:
        if acquire:
            _discover(con, collection, reason, stop=stop)
        else:
            _scan_only(con, collection, reason, stop=stop)
        _last_discover[collection.name] = time.time()
    except Exception:   # noqa: BLE001 — a scan must never take a thread down silently
        try:
            db.log_event(con, "error", "discover",
                         f"scan failed:\n{traceback.format_exc()[:1200]}")
        except Exception:
            print(traceback.format_exc()[:1200], flush=True)
    finally:
        con.close()
        with _scan_lock:
            if _scan_threads.get(collection.name) is threading.current_thread():
                del _scan_threads[collection.name]

# Errors that mean "the endpoint is not there", never "this document is
# bad". Deliberately narrow: only connection ESTABLISHMENT failures. A read
# timeout is excluded because an oversized document can genuinely cause one,
# and mislabelling that as a deferral would retry it forever.
_ENDPOINT_DOWN_ERRNOS = frozenset(
    {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH})


def manifest_contended(exc: BaseException) -> bool:
    """Is this failure another writer holding the manifest, not the document?

    Same principle as endpoint_unavailable below, and the acquire path has
    always drawn it (see its docstring: "a manifest busy past its timeout is
    logged as a deferral"). The document stages never did, so a lock timeout
    recorded a permanent error and dropped a perfectly good document out of
    the pipeline until a human pressed "clear errors & retry".

    It went unnoticed while every stage held the write lock for microseconds.
    It stopped being unnoticeable when fts began committing a whole batch and
    gutenberg was on embed_mode=all: an embed runs inside _execute_step's
    transaction and can take minutes, busy_timeout is 60s, so the fts batch
    behind it lost the lock and errored documents that had nothing wrong with
    them (observed live: six in the first minutes, still accruing).
    """
    return (isinstance(exc, sqlite3.OperationalError)
            and "locked" in str(exc).lower())


def endpoint_unavailable(exc: BaseException) -> bool:
    """Is this failure the encoder's absence rather than the document's fault?

    It matters because of what recording an error DOES: the backfill query
    requires `error IS NULL`, so an errored document drops out of embedding
    even with retries left, and only comes back when a human presses "clear
    errors & retry". Observed live — three documents accumulated across one
    morning of restarts, each one a connection refused against an encoder
    that was simply being restarted, and none of them would ever have
    retried on their own.

    The acquire path already draws this distinction: a manifest busy past
    its timeout is logged as a deferral and retried next round rather than
    counted as a failure, because nothing was wrong with the work.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    if isinstance(exc, ConnectionError):      # ConnectionRefusedError et al
        return True
    return isinstance(exc, OSError) and exc.errno in _ENDPOINT_DOWN_ERRNOS


def _execute_step(con, collection, stage, row) -> bool:
    """One document's stage, transactional, errors recorded on the row —
    shared verbatim by the main loop and the per-collection pipeline
    threads (each caller passes a con its own thread owns).

    Returns False when the step was DEFERRED rather than attempted: the
    document is left completely untouched, and the caller stops working this
    collection for the round instead of marching through every remaining
    document against an endpoint that is not answering.

    TRANSACTIONS. rdf and fts run under `BEGIN IMMEDIATE`: they read, then
    write, and a deferred `BEGIN` that reads first can fail its upgrade to
    write INSTANTLY (WAL snapshot conflict — the busy handler never runs)
    whenever another connection committed in between. That was invisible
    while every writer of a manifest sat on one thread; with the discovery
    scan on its own thread (committing a batch every second or so) fts was
    deferred six times for every success. The RESERVED lock is held for the
    step — seconds for a batch — and contenders wait busy_timeout instead of
    failing. The embed step opens NO outer transaction: its long part is the
    encoder call, and holding the manifest's write lock across minutes of
    network I/O would stall the web process and every other writer; instead
    `upsert_book` embeds outside any lock and takes `vector_write_lock`
    (`BEGIN IMMEDIATE`) for the Lance delete+add alone — which it silently
    did NOT do before, because the outer deferred transaction made
    `vector_write_lock` yield without taking a lock at all. The SQL writes
    that follow autocommit; a crash between them re-embeds the document
    (delete-then-insert, invariant 2).
    """
    t0 = time.time()
    if stage not in ("embed", "fts"):
        # fts opens its own BEGIN IMMEDIATE late — after chunk loading and
        # the tantivy commit — so hours of pure compute on a huge document
        # never hold the manifest's write lock (stage_fts explains). The
        # commit/rollback below covers it either way (in_transaction).
        con.execute("BEGIN IMMEDIATE")
    try:
        STAGES[stage](con, row, collection)
        if stage == "embed":
            con.execute("DELETE FROM embed_queue WHERE uid=?", (row["uid"],))
        if con.in_transaction:
            con.execute("COMMIT")
    except Exception as e:
        try:
            if con.in_transaction:
                con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass        # nothing to roll back, or still contended: not fatal
        if isinstance(e, embeddings.EmbedInterrupted):
            # shutdown between slices of a long document: the vectors already
            # written are durable and the document itself is untouched, so
            # this is a deferral like any other — never an error on the row
            db.log_event(con, "info", stage,
                         f"{stage} interrupted by shutdown ({e})")
            return False
        if manifest_contended(e):
            # Another writer had the manifest. Nothing is wrong with this
            # document, so nothing is written to it — and in particular we do
            # NOT try to record an error, which is itself a write and would
            # be just as likely to lose the lock.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — manifest busy ({e}); "
                         f"the document is untouched and will be retried")
            return False
        if vector_store_contended(e):
            # the vector store's optimistic concurrency: another writer or
            # the index maintainer committed first. Same rule as above —
            # a deferral, never an error on the document.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — vector store busy ({e}); "
                         f"the document is untouched and will be retried")
            return False
        if endpoint_unavailable(e):
            # Nothing is wrong with this document, so nothing is written to
            # it: no error, no retry increment. The backend gate owns the
            # endpoint's health; this stage simply yields the round.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — encoder unreachable ({e}); "
                         f"the document is untouched and will be retried")
            return False
        if isinstance(e, plugin_base.UnindexableDocument):
            # a CORPUS fact surfaced mid-stage (the mirror ships no text for
            # this entry): recorded UNPREFIXED, like discovery writes it, so
            # it lands in the Library's 'not indexable' bucket — not as a
            # stage-prefixed error that reads as a blocked pipeline and that
            # "clear errors & retry" would bounce forever
            con.execute(
                "UPDATE documents SET error=?, updated_at=? WHERE uid=?",
                (str(e)[:500], int(time.time()), row["uid"]))
            db.log_event(con, "info", stage,
                         f"not indexable: {e}", row["uid"])
            return True
        con.execute(
            "UPDATE documents SET retries=retries+1, error=?, updated_at=? WHERE uid=?",
            (f"{stage}: {e}"[:500], int(time.time()), row["uid"]))
        db.log_event(con, "error", stage, f"{type(e).__name__}: {e}", row["uid"])
        return True
    dt = round(time.time() - t0, 2)
    if stage not in ("rdf", "fts") or int(row["native_id"]) % 500 == 0:
        db.log_event(con, "info", stage, f"done in {dt}s", row["uid"])
    return True


def _run_one_step(con, collection, last_idle_log: dict,
                  pipeline_active: bool = False) -> bool:
    """Runs at most one document's worth of work for this collection.
    Returns True if work was done (so the caller can skip the idle sleep).

    pipeline_active=True means this collection's rdf/fts stages belong to a
    dedicated pipeline thread — the main loop then only selects embed work
    (plus bundles/genesis, which write embed_done and are kept here so the
    GPU-serial-across-collections doctrine has a single owner)."""
    # Imported vectors are cheaper than computed ones, so drain the inbox
    # before looking for local work: a bundle may satisfy the very document
    # the embed stage was about to pick up.
    if _drain_bundle_inbox(con, collection):
        return True
    # Same economics, declarative source: a collection with a genesis
    # archive declared (genesis_archive in its TOML) reuses published
    # vectors before any GPU-hour is spent. One shard per round; failures
    # are ledgered and backed off inside drain_next_shard, never fatal.
    #
    # Genesis comes BEFORE the selection below because imported vectors
    # beat computed ones — but it must never come before rdf/fts for long:
    # observed live, a local archive on a pipeline thread that owns its
    # whole pipeline claimed every round re-reading shards, and the
    # collection advanced one fts batch per full sweep (200 documents per
    # 3.2 hours on 959 shards). drain_next_shard now reads only shards its
    # candidates name; `eager` tells it whether rdf/fts are waiting on this
    # thread, so a read is amortised over a batch rather than paid per
    # fts commit (config.GENESIS_MIN_CANDIDATES / GENESIS_MAX_WAIT_SEC).
    # A REMOTE archive whose collection has a pipeline thread is drained
    # on THAT thread (_drain_remote_archive in the pipeline loop), never
    # here on the GPU-serial main loop; a local archive, or any archive of
    # a collection that runs its whole pipeline here, is drained here.
    if collection.genesis_archive and not (pipeline_active and _remote_archive(collection)):
        # same phase rule as the pipeline thread (_archive_yields): a pass
        # that would starve the encoder waits for one fts batch first
        if not _archive_yields(con, collection) and \
                _drain_remote_archive(con, collection, _stop):
            return True
    scope = db.get_scope(con)
    if pipeline_active:
        stage, row = _next_embed(con, scope, collection)
        if stage == "embed" and not backend_gate.embed_allowed(con, collection):
            return False        # rdf/fts continue on the pipeline thread
    else:
        stage, row = _next_book(con, scope, collection=collection)
        if stage == "embed" and not backend_gate.embed_allowed(con, collection):
            # The endpoint failed its behavioral baseline (or is down).
            # Embedding pauses — the document is untouched, never errored —
            # while rdf/fts work continues. embed_allowed() logged the
            # transition and caches the verdict, so this costs nothing per
            # round until the TTL expires.
            stage, row = _next_book(con, scope, embed_allowed=False, collection=collection)
    if stage == "embed" and not _vector_writes_allowed(collection):
        return False            # index build/refresh in progress: deferred, not an error
    if stage == "embed":
        recipe = collections_mod.recipe_for(collection)
        if recipe.concurrency > 1:
            return _embed_parallel(con, collection, recipe)
    if row is None:
        now = time.time()
        if not pipeline_active and \
                now - last_idle_log.get(collection.name, 0.0) > 300:
            db.log_event(con, "info", "worker", "idle — pipeline is caught up")
            last_idle_log[collection.name] = now
        return False
    # an ATTEMPTED (even failed) step counts as work this round; a DEFERRED
    # one does not, so a dead encoder makes the loop move on instead of
    # walking the whole collection document by document
    return _execute_step(con, collection, stage, row)

# Per-collection pipeline threads (rdf/fts). ONE thread per collection —
# never two writers on one tantivy index, and every plugin cache that spans
# collections (pubmed's container cache, embedded's shard cache) is already
# lock-guarded for the parallel embed stage. Each thread owns its OWN
# manifest connection (SQLite connections are thread-affine; WAL +
# busy_timeout arbitrate against the main loop's embed/discovery writes on
# the same manifest). The main loop keeps embed: GPU-serial ACROSS
# collections is doctrine, and one owner enforces it.
_pipeline_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
_paused = threading.Event()          # set = worker paused via the control UI
# set = "acquire-only": keep the corpus growing (discovery still runs, so
# finished fetches are picked up and new ones start) but stand down the
# CPU-heavy stages. Moving bytes is nearly free; rdf/fts/embed run every
# core hot, which is a real consideration when the node lives in a room
# someone sits in.
_acquire_only = threading.Event()

# Discovery requests, drained by the owning collection's pipeline thread.
#
# Startup discovery used to run SERIALLY ON THE MAIN THREAD for every
# collection before the round loop began — so after each restart nothing
# processed (no rdf, no fts, no embed, no pipeline threads at all) until
# the slowest scan finished. Observed live at five collections, two of
# them ~19M documents: minutes of dead time per restart, and it defeated
# the point of per-collection threads. Now the main loop only REQUESTS a
# scan and each thread runs its own, so a slow scan costs its own
# collection and nobody else.
_discover_requests: dict[str, tuple[str, bool]] = {}   # name -> (reason, acquire)
_discover_req_lock = threading.Lock()

#: The CURRENT Collection object per name, republished by `_sync_collections`
#: every round. A pipeline thread is started once and lives for the life of
#: the worker, so the object it was handed at start is frozen — and every
#: Tier-2 field the thread reads froze with it: `profile`, `acquire_enabled`,
#: `license_ack`, `extras`, `peer_source`, the `[encoder]` recipe. Hot reload
#: then worked only for the main loop's own list, while `_sync_collections`
#: promised in its own docstring that "profile/acquire toggles apply
#: immediately".
#:
#: Observed live 2026-09-12: gutenberg's `profile = "full"` was added to its
#: TOML, the main loop SAW the change and requested a discovery — and the
#: pipeline thread ran that discovery against its startup object, whose
#: profile was still None, so `_acquire` returned early and the fetch never
#: started. Nothing said so: the "no profile declared" warning is rate
#: limited to once an hour and had already been spent.
_live_collections: dict[str, object] = {}
_live_lock = threading.Lock()


def _publish_collections(collections) -> None:
    with _live_lock:
        _live_collections.clear()
        _live_collections.update({c.name: c for c in collections})


def _live_collection(collection):
    """The current object for this collection, or the one given.

    PATHS ARE NOT HOT-RELOADED. A thread holds an open connection to the
    manifest it was started against, so a TOML that repointed the data
    directories mid-run would leave the thread writing to one database and
    the main loop to another. That case still needs a restart — it always
    did, and `_sync_collections` says so — and it is refused here rather
    than half-applied.
    """
    with _live_lock:
        fresh = _live_collections.get(collection.name)
    if fresh is None:
        return collection
    if fresh.manifest_path != collection.manifest_path:
        return collection
    return fresh
# When each collection was last scanned; module-level because both the
# main loop (interval trigger) and the pipeline threads (completion) write
# it. run_forever passes this same object to _sync_collections.
_last_discover: dict[str, float] = {}
# idle-log throttle for collections whose pipeline thread owns everything
_pipeline_idle_log: dict[str, float] = {}


def _request_discovery(name: str, reason: str, *, acquire: bool = True) -> None:
    """First reason wins: a queued 'startup' must not be relabeled by a
    later 'interval' before the thread has run it.

    `acquire=False` asks for a PLAIN scan. The post-fetch scan must use it:
    its job is to pick up what a fetch just landed, and if it started
    another fetch, that fetch's completion would request another scan —
    which is exactly the tight acquire↔discover loop this grew when
    discovery moved onto the pipeline threads (observed live: one cycle
    every ~5 seconds, starving rdf/fts/embed entirely)."""
    with _discover_req_lock:
        _discover_requests.setdefault(name, (reason, acquire))


def _take_discovery_request(name: str) -> tuple[str, bool] | None:
    with _discover_req_lock:
        return _discover_requests.pop(name, None)


def owns_own_embed(collection) -> bool:
    """Does this collection embed on its OWN pipeline thread?

    The "GPU-serial across collections" doctrine exists because one GPU
    cannot serve two collections at once. It says nothing about CPU
    encoders, which are separate containers on separate cores and do not
    contend with the GPU at all — the Library card has always said so
    ("runs alongside the GPU without competing").

    Making the main loop the single embed owner for EVERYONE turned that
    doctrine inside out. Observed live: _embed_parallel took a batch of four
    yeast chromosomes and blocked the loop for a quarter of an hour, and
    because collections are visited in sort order, `genome-sacCer3` sits
    ahead of `gutenberg` — so the GPU sat at 0% CPU waiting for a CPU
    encoder to finish. The scarce resource was starved by the abundant one.

    So: a CPU collection owns its whole pipeline on its own thread, and the
    main loop keeps exactly what the doctrine is about — the GPU.
    """
    return collections_mod.recipe_for(collection).spends == "cpu"


def servable_collections(all_collections, pipeline: bool) -> list:
    """Who the encoder rotation may serve.

    A collection that owns its own embed is excluded and the exclusion
    happens HERE, before any work is selected: serving it on this loop
    would put two embed writers on one collection, and would let a CPU
    encoder hold the GPU's turn for the length of a batch — the starvation
    that the per-collection thread split exists to fix.
    """
    return [c for c in all_collections
            if not (pipeline and owns_own_embed(c))]


def embed_resource(collection) -> str:
    """The scheduler's name for what this collection's embedding contends
    for. Per ENDPOINT, not per collection: two collections pointed at one
    GPU contend and must share it; two pointed at different containers do
    not, and the name is the only thing that tells them apart."""
    return sched.encoder(collections_mod.recipe_for(collection).url)


_QUERY_PRIORITY_POLL_S = 0.5
_query_priority_seen: dict[str, tuple[float, bool]] = {}


def query_waiting(con) -> bool:
    """Is a human waiting on this encoder right now (A5)?

    Memoised for _QUERY_PRIORITY_POLL_S: the answer is read between
    documents, and at 175 embeds/s an unmemoised read would put a needless
    SELECT on the control table for every document. Half a second is far
    below the cost of the document it protects, so the memo cannot make a
    query wait measurably longer."""
    key = str(getattr(con, "collection_name", "") or id(con))
    now = time.monotonic()
    seen = _query_priority_seen.get(key)
    if seen is not None and now - seen[0] < _QUERY_PRIORITY_POLL_S:
        return seen[1]
    try:
        active = db.query_priority_claimed(con)
    except Exception:   # noqa: BLE001 — a reservation is an optimisation
        active = False
    _query_priority_seen[key] = (now, active)
    return active


def reset_query_priority_memo() -> None:
    _query_priority_seen.clear()


def _serve_turn(con, collection, turn, last_idle_log, pipeline) -> bool:
    """One collection's slot on the encoder: work until the slot is spent,
    or until the collection has nothing left to do — whichever is first.

    A step that returns False is a DEFERRAL or an empty queue, and either
    way this collection cannot use the resource now. It releases at once
    (`idle=True`) instead of sitting on a slot it cannot spend, and rejoins
    the rotation at the tail when its work reappears — the operator's "a
    plugin can release a resource without waiting for the end of its time
    slot", and the reason releasing honestly is safe: re-entry is at the
    tail, so an early release can never be used to cut back in.
    """
    worked = False
    try:
        while not _stop.is_set():
            t0 = time.monotonic()
            did = _run_one_step(con, collection, last_idle_log,
                                pipeline_active=pipeline)
            dt = time.monotonic() - t0
            if not did:
                # charged for the time it took to find out, but not as a
                # unit: nothing was processed, so it must not pollute the
                # measured cost of this collection's units
                turn.spent(dt, units=0)
                turn.release(idle=True)
                return worked
            worked = True
            turn.spent(dt)
            if query_waiting(con):
                # A5: a human is on the encoder. Yield BETWEEN documents —
                # never mid-call, which would waste the work already done —
                # and keep our place in the rotation (idle=False), because
                # standing aside for a query is not the same as having
                # nothing to do.
                turn.release(idle=False)
                return worked
            if not turn.may_continue():
                break
    finally:
        turn.release(idle=False)        # a no-op if already released above
    return worked


def _serve_rotation(cons, servable, last_idle_log, pipeline) -> bool:
    """The encoder rotation across collections, as deficit round-robin
    (app/sched.py, docs/notes/scheduling.md §10).

    What this replaces: a fixed budget per collection, spent in list order,
    on every collection whether or not it had work. That gave a collection
    with nothing to do the same place in the queue as one with millions of
    documents pending, and it made the worst-case wait for a collection
    that just became ready the sum of everyone else's FULL budgets.

    One pass hands each flow at most one turn and returns. The bound
    matters: the caller owns telemetry, the control state (pause, SIGTERM)
    and discovery, and a rotation that never came back up here would
    make all three wait for it.
    """
    s = sched.scheduler()
    by_resource: dict[str, dict] = {}
    for c in servable:
        by_resource.setdefault(embed_resource(c), {})[c.name] = c
    worked = False
    for resource, members in by_resource.items():
        s.sync(resource, members)
        for name in members:
            # no floor: a flow's minimum efficient unit is MEASURED from what
            # its units actually cost (sched.Scheduler._learn), never set
            # here — and passing one every pass would overwrite the
            # measurement before it could ever take effect
            s.declare(resource, name)
        for _ in range(len(members)):
            if _stop.is_set():
                break
            turn = s.acquire(resource)
            if turn is None:
                break
            collection = members.get(turn.collection)
            if collection is None:
                turn.release(idle=True)
                continue
            if _serve_turn(cons[collection.name], collection, turn,
                           last_idle_log, pipeline):
                worked = True
    return worked


def _serve_in_order(cons, servable, last_idle_log, pipeline) -> bool:
    """The pre-scheduler behaviour, kept behind SCHED_FAIR_SHARE=0 as an
    escape hatch: a fixed time budget per collection, in list order."""
    worked = False
    for collection in servable:
        if _stop.is_set():
            break
        con = cons[collection.name]
        round_deadline = time.time() + config.WORKER_ROUND_BUDGET_S
        while not _stop.is_set():
            if not _run_one_step(con, collection, last_idle_log,
                                 pipeline_active=pipeline):
                break
            worked = True
            if time.time() >= round_deadline:
                break
    return worked


def _pipeline_worker(collection, gone: threading.Event) -> None:
    con = db.connect(collection)
    db.log_event(con, "info", "worker",
                 f"pipeline thread started for {collection.name!r}")
    last_idle = 0.0
    try:
        while not _stop.is_set() and not gone.is_set():
            if _paused.is_set():
                time.sleep(2)
                continue
            # Tier-2 is DESIRED STATE (invariant 10) and this thread outlives
            # every edit to it, so the object is re-read each round rather
            # than held from thread start — see `_live_collection`.
            collection = _live_collection(collection)
            try:
                # this collection's scans are REQUESTED here (acquisition
                # kickoff, startup scan, interval rescans, post-fetch
                # rescans all arrive from the main loop) and run on their
                # own thread beside this one — see _start_scan. One at a
                # time: while a scan runs the request stays queued, and the
                # pipeline below keeps working. Discovery runs in EVERY
                # non-paused mode: it is what starts fetches and picks up
                # what they land.
                if not scan_running(collection.name):
                    request = _take_discovery_request(collection.name)
                    if request:
                        reason, acquire = request
                        _start_scan(collection, reason, acquire, gone)
                if _acquire_only.is_set():
                    time.sleep(config.WORKER_IDLE_SLEEP)
                    continue
                if owns_own_embed(collection):
                    # The full pipeline, embed and genesis included: this
                    # collection's encoder is its own container, so nothing
                    # here competes with the GPU or with another collection.
                    if not _run_one_step(con, collection, _pipeline_idle_log,
                                          pipeline_active=False):
                        time.sleep(config.WORKER_IDLE_SLEEP)
                    continue
                # A REMOTE archive (amendment 38) is applied HERE, on the
                # collection's own thread, never on the GPU-serial main
                # loop: a pass is CPU/IO work (a million row lookups, text
                # loads, Lance writes — the encoder is touched only for
                # the 16-sample proof), and on the main loop it blocked
                # every other collection's embedding for its whole budget
                # (observed live: gutenberg's embeds "done in 478 s"). Its
                # budget/yield alternation is with THIS thread's rdf/fts.
                # the phase controller: an archive pass can hold this thread
                # for 300 s, and while it does nothing new becomes
                # embeddable. If the encoder is running dry and fts has
                # work, fts goes first (_archive_yields).
                if not _archive_yields(con, collection) and \
                        _drain_remote_archive(con, collection, gone):
                    continue
                scope = db.get_scope(con)
                stage, row = _next_book(con, scope, embed_allowed=False, collection=collection)
                if row is None:
                    if time.time() - last_idle > 300:
                        db.log_event(con, "info", "worker",
                                     "rdf/fts caught up — pipeline thread idle")
                        last_idle = time.time()
                    time.sleep(config.WORKER_IDLE_SLEEP)
                    continue
                if _execute_step(con, collection, stage, row) and stage == "fts":
                    # A document just became embeddable. ANNOUNCE it (§10,
                    # B2) instead of leaving the encoder rotation to find
                    # out on its next sweep: a collection that becomes
                    # runnable just after being skipped would otherwise wait
                    # a whole rotation to say so.
                    sched.scheduler().demand(embed_resource(collection),
                                             collection.name)
            except Exception as e:   # noqa: BLE001 — a pipeline thread must outlive any step
                if manifest_contended(e):
                    # a writer outside _execute_step's own handling (queue
                    # cleanup in _next_book, a control write) lost the lock
                    # to a long holder — a deferral, not an error: nothing
                    # is wrong, the round retries
                    db.log_event(con, "warn", "worker",
                                 f"round deferred — manifest busy ({e})")
                    time.sleep(2)
                    continue
                try:
                    db.log_event(con, "error", "worker",
                                 f"pipeline thread error:\n"
                                 f"{traceback.format_exc()[:1200]}")
                except Exception:
                    print(traceback.format_exc()[:1200], flush=True)
                time.sleep(5)
        db.log_event(con, "info", "worker",
                     f"pipeline thread stopped for {collection.name!r}")
    finally:
        con.close()


def _sync_pipeline_threads(all_collections) -> None:
    """Reconcile pipeline threads with the live collection set: start one
    per new collection, signal the thread of a removed collection to stop
    (it finishes its current document first — invariant 2)."""
    if not config.WORKER_PIPELINE_THREADS:
        return
    live = {c.name: c for c in all_collections}
    for name in list(_pipeline_threads):
        thread, gone = _pipeline_threads[name]
        if name not in live:
            gone.set()
            del _pipeline_threads[name]
        elif not thread.is_alive():
            del _pipeline_threads[name]     # crashed/finished: restart below
    for name, collection in live.items():
        if name not in _pipeline_threads:
            gone = threading.Event()
            thread = threading.Thread(target=_pipeline_worker,
                                       args=(collection, gone),
                                       name=f"pipeline-{name}", daemon=True)
            _pipeline_threads[name] = (thread, gone)
            thread.start()


def _join_pipeline_threads(timeout: float = 90.0) -> None:
    """Shutdown: each thread finishes its current document and exits (the
    stop event is checked between documents). The deadline is shared, and
    stragglers are daemon threads — a hung step cannot block SIGTERM's
    exit-0 contract past the container's grace period."""
    deadline = time.time() + timeout
    for thread, _ in list(_pipeline_threads.values()):
        thread.join(timeout=max(0.0, deadline - time.time()))
    # scans stop at their next ref and commit the batch so far; give them
    # the same deadline so that commit lands before the process exits
    with _scan_lock:
        scans = list(_scan_threads.values())
    for thread in scans:
        thread.join(timeout=max(0.0, deadline - time.time()))


def _apply_tier2_embed_mode(con, collection, reason: str) -> None:
    """On restart, on add, and on a TOML edit the file wins (invariant 10):
    the Library's runtime override is replaced by the TOML's `embed_mode`
    (or the default when the TOML is silent), and the event log says so —
    an operator who flipped a mode from the UI sees why it changed back and
    where to make it permanent."""
    replaced = db.apply_desired_embed_mode(con, collection)
    if replaced is not None:
        desired = collection.embed_mode_default or collections_mod.DEFAULT_EMBED_MODE
        source = (f"collections/{collection.name}.toml" if collection.embed_mode_default
                  else "the default — the TOML sets none")
        db.log_event(con, "info", "control",
                     f"embed mode {replaced!r} -> {desired!r} on {reason}: the "
                     f"file wins ({source}); the Library's override lasts until "
                     f"the next restart or TOML edit")


def _sync_collections(all_collections, cons, last_discover, pipeline: bool = False):
    """Hot-reload collections each round: a collections/*.toml added or
    removed while the worker runs takes effect within one round — no restart.

    A new collection is boot-validated in isolation; failing validation
    skips it (logged, retried next round once the TOML is fixed) instead of
    crashing the worker. Removal closes the connection and stops ingestion;
    data on disk is untouched, matching the admin UI's removal semantics.
    Existing collections are refreshed by name (profile/acquire toggles
    apply immediately); renaming a collection's data PATHS mid-run is the
    one thing that still needs a restart.
    """
    current = collections_mod.load_all()
    known = {c.name for c in all_collections}
    now_names = {c.name for c in current}
    previous_by_name = {c.name: c for c in all_collections}
    # BEFORE the change detection below, so the discovery it requests is
    # run by the pipeline thread against the NEW object and not the one
    # that thread was started with.
    _publish_collections(current)

    # A config change that alters what the worker should DO with a
    # collection (license acknowledged, acquisition toggled, profile grown)
    # triggers an immediate discover+acquire pass — otherwise an ack
    # recorded via the UI would sit inert until the next interval scan.
    for kept in current:
        prev = previous_by_name.get(kept.name)
        if prev is None or kept.name not in cons:
            continue
        if prev.embed_mode_default != kept.embed_mode_default:
            _apply_tier2_embed_mode(cons[kept.name], kept, "TOML edit")
        if (prev.acquire_enabled, prev.license_ack, prev.profile,
                prev.extras, prev.peer_source) != \
                (kept.acquire_enabled, kept.license_ack, kept.profile,
                 kept.extras, kept.peer_source):
            db.log_event(cons[kept.name], "info", "worker",
                         f"collection {kept.name!r} config changed "
                         f"(acquire/license/profile/extras/peer) — re-discovering")
            if pipeline:
                _request_discovery(kept.name, "config-changed")
            else:
                _discover(cons[kept.name], kept, "config-changed")
            last_discover[kept.name] = time.time()

    for added in [c for c in current if c.name not in known]:
        # A re-added collection must not inherit a set stop event, or its
        # acquire/archive threads return "stopped" instantly and silently
        # forever — `_archive_worker` logs nothing on an empty fetch, so it
        # would fail invisibly.
        _gone.pop(added.name, None)
        try:
            spaces.validate_boot([added])
        except spaces.BootValidationError as e:
            if cons:
                db.log_event(next(iter(cons.values())), "error", "worker",
                             f"new collection {added.name!r} failed boot "
                             f"validation — skipped until fixed: {e}"[:500])
            continue
        cons[added.name] = db.connect(added)
        db.ensure_browse_index(cons[added.name])
        db.ensure_catalog_rowids(cons[added.name])
        _apply_tier2_embed_mode(cons[added.name], added, "add")
        db.log_event(cons[added.name], "info", "worker",
                     f"collection {added.name!r} added — discovering")
        stale = bundle_ledger.sweep_stale(cons[added.name])
        if stale:
            db.log_event(cons[added.name], "warn", "bundle",
                         f"{len(stale)} stale bundle import(s) swept")
        if pipeline:
            _request_discovery(added.name, "added")
        else:
            _discover(cons[added.name], added, "added")
        last_discover[added.name] = time.time()

    for gone in sorted(known - now_names):
        db.log_event(cons[gone], "info", "worker",
                     f"collection {gone!r} removed — ingestion stopped, data kept")
        # Rescue the node's control state BEFORE the connection closes.
        # worker_state lives in whichever collection sorts first, so when
        # the collections ahead of the new owner all leave in one round the
        # donor list `all_collections[1:]` is empty and the node drops to
        # its 'paused' default with no log line. Reading the values out
        # here is the only moment they are still reachable.
        for key in db.NODE_CONTROL_KEYS:
            value = db.get_control(cons[gone], key)
            if value is not None:
                _orphaned_node_control.setdefault(key, value)
        # Signal the byte-moving threads HERE, not at _sync_pipeline_threads:
        # that runs later in the round and not at all in non-pipeline mode or
        # while the node is paused. Waking the transfer manager is required
        # too — a thread parked in the token bucket or on an endpoint slot
        # checks the predicate only after its wait returns, so setting the
        # event without the wake is inert for the whole wait cap.
        _gone_event(gone).set()
        try:
            get_manager().wake_all()
        except Exception:   # noqa: BLE001 — never let telemetry stop a removal
            pass
        for table in (_acquire_threads, _acquire_done, _archive_threads,
                      _archive_log_state, _archive_last_kick, _sizing_names,
                      _license_nagged, _profile_nagged, _pipeline_idle_log):
            table.pop(gone, None)
        cons[gone].close()
        del cons[gone]
        last_discover.pop(gone, None)
        _take_discovery_request(gone)   # never scan a removed collection

    return [c for c in current if c.name in cons]


# Telemetry must not be published by the thread whose work it describes. The
# main loop blocks inside _embed_parallel for the whole batch — a quarter of
# an hour on a CPU encoder — so publishing from there means in-flight work
# becomes visible only once it has stopped being in flight, which is exactly
# backwards. A tiny dedicated thread keeps the Stats and Network pages honest
# while the loop is busy.
STATS_PUBLISH_INTERVAL_S = 10.0
# How often the scheduler's own accounting reaches the worker log. Rarely:
# it is a slow-moving fairness statement, not a rate, and the log read as a
# time series is the first benchmark anyone has (docs/notes/scheduling.md §7).
SHARES_LOG_INTERVAL_S = 600.0
_last_shares_log = 0.0


# Sizing keys hold a manifest PATH, because that is the only identity two
# collections cannot share (CLAUDE.md: key on physical identity, never on a
# name). A path is the wrong thing to PRINT, though: every manifest is
# called `manifest.db`, and the default collection's sits at the data root
# rather than in a directory named after it — so deriving a label from the
# path gave "manifest.db" five times, and then "data". The name is
# registered here beside the key instead: physical identity for the
# lookups, a human name for the log.
_sizing_names: dict[str, str] = {}


def _remember_label(key: str, label: str) -> None:
    _sizing_names[key] = label


def _sizing_label(key: str) -> str:
    known = _sizing_names.get(key)
    if known:
        return known
    stage, sep, path = key.partition(":")
    if not sep:
        stage, path = "", key
    name = Path(path).parent.name or Path(path).name
    return f"{stage} {name}".strip()


def _log_shares(con, live_paths=None) -> None:
    """Publish what each collection actually GOT from each contended
    resource. Without this the fair share is an assertion; with it, a
    scheduling change can be judged on the general picture instead of on
    whichever collection happened to be busy when someone looked."""
    global _last_shares_log
    now = time.time()
    if now - _last_shares_log < SHARES_LOG_INTERVAL_S:
        return
    _last_shares_log = now
    for resource, flows in sched.scheduler().snapshot().items():
        busy = {n: e for n, e in flows.items() if e["turns"]}
        if not busy:
            continue
        parts = [
            f"{n}: {e['share']:.0%} in {e['turns']} turns "
            f"({e['seconds']:.0f}s, {e['units']} units, "
            f"quantum {e['quantum_s']:.1f}s"
            + (f", {e['overruns']} overruns" if e["overruns"] else "")
            + (f", units cost {e['suggest_floor_s']:.1f}s"
               if e["suggest_floor_s"] else "")
            + ")"
            for n, e in sorted(busy.items())
        ]
        db.log_event(con, "info", "sched", f"{resource} — " + "; ".join(parts))
    # ...and what the units themselves now cost, which is what the sizes
    # above are derived from (app/sizing.py). A batch that drifts away from
    # BATCH_TARGET_S is visible here before it is visible as unfairness.
    sizes = sizing.sizer().snapshot()
    if live_paths is not None:
        # Filter on READ. Neither the sizer nor the scheduler prunes on
        # removal, so a retired collection kept printing `unit costs` and
        # `windows` lines with frozen numbers for the life of the process.
        # The keys are `{stage}:{manifest_path}` for rates and the BARE
        # manifest path for windows, so match on the path component.
        def _live(key):
            return any(str(p) == key or key.endswith(":" + str(p))
                       for p in live_paths)
        sizes = {"rates": {k: v for k, v in sizes["rates"].items() if _live(k)},
                 "windows": {k: v for k, v in sizes["windows"].items() if _live(k)}}
    if sizes["rates"]:
        db.log_event(con, "info", "sched", "unit costs — " + "; ".join(
            f"{_sizing_label(k)}: {v * 1000:.1f} ms/doc" if v < 1
            else f"{_sizing_label(k)}: {v:.1f} s/doc"
            for k, v in sorted(sizes["rates"].items())))
    # The adapted window widths. These live only in this process, so
    # `sched_report --queries` CANNOT see them — it times the static
    # default and therefore always reports the "before" number. Without
    # this line there is no way to tell whether the controller is working.
    if sizes["windows"]:
        db.log_event(con, "info", "sched", "windows — " + "; ".join(
            f"{_sizing_label(k)}: {v:,} rowids"
            for k, v in sorted(sizes["windows"].items())))


def _stats_publisher() -> None:
    from .acquire import seeding
    con = None
    owner = None
    seed_cons: dict[str, object] = {}
    while not _stop.wait(STATS_PUBLISH_INTERVAL_S):
        try:
            collections = collections_mod.load_all()
            # Evict BEFORE the empty guard: this thread keeps its own
            # connection map, so a retired collection's write-capable
            # handle (and its -wal/-shm) stayed open until the process
            # exited — on a manifest the node had stopped touching and, on
            # this deployment, was about to delete.
            live = {c.name for c in collections}
            for stale_name in [n for n in seed_cons if n not in live]:
                try:
                    seed_cons.pop(stale_name).close()
                except Exception:   # noqa: BLE001 — one bad handle, not the beat
                    seed_cons.pop(stale_name, None)
            if not collections:
                continue
            control = collections[0]          # node control follows sort order
            if owner != control.name:
                if con is not None:
                    con.close()
                con = db.connect(control)
                owner = control.name
            db.publish_worker_stats(con, stats.recorder().snapshot())
            # SEEDING lives on this thread: it is telemetry-shaped work
            # (start published torrents, gate on the worker state, credit
            # uploaded bytes to the UP bucket, publish the table) and the
            # pipeline threads never touch it — per-collection connections
            # are this thread's own (SQLite is thread-affine).
            for c in collections:
                if c.name not in seed_cons:
                    seed_cons[c.name] = db.connect(c)
            seeding.tick(collections, seed_cons, con, db.worker_state(con))
            db.publish_worker_rates(con, get_manager().rates())
            _log_shares(con, [c.manifest_path for c in all_collections])
        except Exception:   # noqa: BLE001 — telemetry must never kill the worker
            pass
    for c_con in seed_cons.values():
        c_con.close()
    if con is not None:
        con.close()


def run_forever():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    all_collections = collections_mod.load_all()
    # Invariant 1: a space mismatch refuses THAT collection, not the node.
    refused = spaces.refuse_boot(all_collections)
    if refused:
        names = {c.name for c, _ in refused}
        for c, reason in refused:
            print(f"NOT SERVED: {c.name}: {reason}", file=sys.stderr)
        all_collections = [c for c in all_collections if c.name not in names]

    _publish_collections(all_collections)
    cons = {c.name: db.connect(c) for c in all_collections}

    for c in all_collections:
        db.log_event(cons[c.name], "info", "worker", "worker started")
        db.ensure_browse_index(cons[c.name])
        db.ensure_catalog_rowids(cons[c.name])
        _apply_tier2_embed_mode(cons[c.name], c, "restart")
        if c.embed is None:
            db.log_event(cons[c.name], "info", "worker",
                         f"embed recipe comes from env (Tier-1 fallback) — "
                         f"make it explicit desired state with: "
                         f"python -m app.cli embed materialize {c.name}")
        # An import interrupted by a previous kill left a row mid-flight. It
        # needs no compensation — the importer clears embed_done before
        # touching Lance, so _next_book re-queues the document — but the row
        # must not stay 'applying' forever.
        stale = bundle_ledger.sweep_stale(cons[c.name])
        if stale:
            db.log_event(cons[c.name], "warn", "bundle",
                         f"{len(stale)} bundle import(s) interrupted by a previous "
                         f"shutdown; affected documents will be re-embedded")
    sched.scheduler().configure(
        base_quantum_s=config.SCHED_QUANTUM_S,
        floor_cap_s=config.SCHED_FLOOR_CAP_S,
        # the minimum efficient unit is a measurement, not a setting
        learn_floor=True)
    threading.Thread(target=_stats_publisher, name="stats-publisher",
                     daemon=True).start()
    # the vector index maintainer: its own thread, so an hour-long build or
    # a compaction never idles a pipeline thread or the GPU loop
    threading.Thread(target=_index_maintainer, name="index-maintainer",
                     daemon=True).start()
    pipeline = bool(config.WORKER_PIPELINE_THREADS)
    # restart = new documents are considered. With pipeline threads this is
    # QUEUED rather than run here: scanning five collections serially before
    # the loop starts meant nothing processed at all until the slowest scan
    # finished (minutes, at 19M-document collections).
    for c in all_collections:
        if pipeline:
            _request_discovery(c.name, "startup")
        else:
            _discover(cons[c.name], c, "startup")

    last_discover = _last_discover
    last_discover.update({c.name: time.time() for c in all_collections})
    last_idle_log: dict[str, float] = {}
    # zero collections (an empty collections/ directory): the node idles on
    # the fallback control manifest until a TOML appears (hot-reloaded)
    control_con = cons[all_collections[0].name] if all_collections else db.connect()
    # None, not the current name: the first round must run the adoption
    # (this startup may be the one that follows the wipe)
    last_control_name: str | None = None

    while not _stop.is_set():
        try:
            all_collections = _sync_collections(all_collections, cons, last_discover,
                                                 pipeline=pipeline)
            _finish_completed_acquires(cons, all_collections, last_discover,
                                        pipeline=pipeline)
            if not all_collections:
                time.sleep(config.WORKER_IDLE_SLEEP)
                continue
            # re-resolved each round: the previous control collection may
            # have been removed mid-run. Whoever owns it now inherits the
            # node's control state, so pause/bandwidth survive a wiped or
            # removed first collection instead of silently defaulting to
            # 'paused' and stopping every collection.
            control_name = all_collections[0].name
            control_con = cons[control_name]
            if control_name != last_control_name:
                adopted = db.adopt_node_control(
                    control_con,
                    [cons[c.name] for c in all_collections[1:] if c.name in cons],
                    fallback=_orphaned_node_control)
                if adopted:
                    db.log_event(control_con, "info", "worker",
                                 f"node control state now owned by "
                                 f"{control_name!r}; adopted {sorted(adopted)}")
                # A node that has never recorded a worker state defaults to
                # 'paused' (db.worker_state), which stops every collection.
                # That default is correct; being SILENT about it is not —
                # it has twice read as "the node is broken, nothing logs".
                # Say it once, at the moment ownership moves.
                if db.get_control(control_con, db.WORKER_STATE_KEY) is None:
                    db.log_event(
                        control_con, "warn", "worker",
                        f"{control_name!r} now holds the node's control state "
                        f"and has no {db.WORKER_STATE_KEY}; the node is "
                        f"PAUSED by default and will process nothing until "
                        f"one is set (`node resume`, or the System page)")
                last_control_name = control_name
            # Tier-3 "boost bandwidth tonight" reaches the byte-moving process
            # here: the web process writes it to the control table, the worker
            # applies it. Re-read each round so it takes effect within one
            # document and expires on its own.
            get_manager().set_override(db.get_bandwidth_override(control_con))
            # ...and the same channel carries the measurement back the other
            # way: acquisition and seeding move their bytes HERE, so without
            # this the Network page's graph is a flat zero beside a saturated
            # link. Published every round, including while paused (a paused
            # node moving no bytes is a true and useful reading).
            db.publish_worker_rates(control_con, get_manager().rates())
            # Per-collection fetch/embed throughput for the Stats page. The
            # recorder lives in this process because this is where the work
            # happens; publishing is how the web process sees it.
            db.publish_worker_stats(control_con, stats.recorder().snapshot())
            state = db.worker_state(control_con)
            if state in ("paused", "seed-only"):
                # seed-only: downloads and processing stand down exactly
                # like pause; what stays alive — serving peers (the web
                # process, torrent seeding) — does not live in this loop,
                # so the worker-side gating is identical. The distinct
                # state exists so the UI says what the NODE is doing and
                # so a future worker-managed seeder has a state to honor.
                _paused.set()       # pipeline threads idle at the same gate
                time.sleep(2)
                continue
            _paused.clear()
            # acquire-only: discovery and fetching continue (the pipeline
            # threads still drain discovery requests), but rdf/fts stand
            # down there and this loop skips embed/genesis/bundles below
            if state == "acquire-only":
                _acquire_only.set()
            else:
                _acquire_only.clear()
            if pipeline:
                # after the pause gate: a paused worker starts no threads,
                # and running ones idle on _paused
                _sync_pipeline_threads(all_collections)
            any_work = False
            for collection in all_collections:
                if _stop.is_set():
                    break
                con = cons[collection.name]
                if (config.DISCOVER_INTERVAL_MIN > 0 and
                        time.time() - last_discover.get(collection.name, 0.0)
                        > config.DISCOVER_INTERVAL_MIN * 60):
                    if pipeline:
                        _request_discovery(collection.name, "interval")
                        # stamped at REQUEST time so a long scan cannot
                        # queue a second request behind itself
                        last_discover[collection.name] = time.time()
                    else:
                        _discover(con, collection, "interval")
                        last_discover[collection.name] = time.time()
            # The heavy work, after the discovery housekeeping above (which
            # runs for every collection in every mode). acquire-only stands
            # the CPU/GPU stages down and keeps the bytes moving.
            #
            # With pipeline threads on, this is the EMBED loop: rdf/fts run
            # on per-collection threads, and what is scheduled here is embed
            # work plus bundles/genesis. A collection that owns its own
            # embed is excluded — touching it here would put two embed
            # writers on one collection AND let a CPU encoder block the
            # GPU's turn, which is the bug that split exists to fix.
            if state != "acquire-only":
                servable = servable_collections(all_collections, pipeline)
                serve = (_serve_rotation if config.SCHED_FAIR_SHARE
                         else _serve_in_order)
                any_work = serve(cons, servable, last_idle_log, pipeline)
            if not any_work:
                time.sleep(config.WORKER_IDLE_SLEEP)
        except Exception as e:
            if manifest_contended(e):
                # a control-plane write (telemetry publish, worker_state
                # read/write) lost the lock to a long holder on the control
                # collection's manifest — stale telemetry for one round, not
                # an error
                try:
                    db.log_event(control_con, "warn", "worker",
                                 f"round deferred — control manifest busy ({e})")
                except Exception:   # noqa: BLE001 — the logger writes too
                    pass
                time.sleep(2)
                continue
            try:
                db.log_event(control_con, "error", "worker", traceback.format_exc()[:1500])
            except Exception:
                # control_con may point at a just-removed collection — the
                # loop must survive even when its logger is gone
                print(traceback.format_exc()[:1500], flush=True)
            time.sleep(5)
    _join_pipeline_threads()
    parallel.shutdown()             # the parse pool's children go with the worker
    for c in all_collections:
        db.log_event(cons[c.name], "info", "worker", "worker stopped (signal)")

if __name__ == "__main__":
    run_forever()
