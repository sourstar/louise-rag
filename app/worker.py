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
import json
import os
import signal
import sqlite3
# run_forever's boot-refusal branch prints to sys.stderr, and this module
# never imported it: a collection refused at boot (invariant 1: refuse THAT
# collection, not the node) raised NameError and crash-looped the worker.
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import httpx

from . import (catalog, chunking, collections as collections_mod, config, cpu_limit, db, discovery,
                layers as layers_mod, parallel,
                dispatch, embeddings, encoder_timeouts, entities, fts, issues as issues_mod, model_load, model_pins,
                origin_missing,
                node_state, plugin_io, refs, sched, sizing, slice_scope, spaces, stats)
from .dispatch import Level
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
    # ...and anything waiting at an encoder's gate: a request not yet
    # admitted is not a unit in flight, so it defers instead of waiting out
    # its timeout (the gates' Conditions are RLock-based, as the bucket's)
    try:
        dispatch.stop_all()
    except Exception:
        pass

#: `louise node stop` (07 §7.3) reaches the worker through node_state; a
#: stop asked for is exit 0, a signal 130 (§7.7). Polled by its own thread,
#: never by a stage: a round does not grow a read of the node's state.
_stop_requested = threading.Event()
STOP_POLL_S = 2.0


def _watch_stop_request(poll_s: float = STOP_POLL_S, end: threading.Event | None = None) -> None:
    from . import node_state
    while not _stop.wait(poll_s):
        if end is not None and end.is_set():
            return
        if node_state.take_stop_request():
            _stop_requested.set()
            _handle_sigterm(None, None)
            return


#: the running watch and its own end: `run_forever` ends it on the way out
#: (`_ends_stop_watch`, an exception included), so a
#: watch never outlives its run (2026-10-27: one left polling by a test whose
#: `_stop` was cleared before it woke read the default node store for the
#: rest of the suite)
_stop_watch: tuple[threading.Thread, threading.Event] | None = None


def _start_stop_watch() -> threading.Thread:
    """A request left from before this start is not this run's: cleared
    first, so `node stop` on a stopped node does not stop the next start."""
    global _stop_watch
    from . import node_state
    node_state.take_stop_request()
    end = threading.Event()
    t = threading.Thread(target=_watch_stop_request, kwargs={"end": end}, name="stop-watch", daemon=True)
    t.start()
    _stop_watch = (t, end)
    return t


def _end_stop_watch() -> None:
    global _stop_watch
    if _stop_watch is None:
        return
    t, end = _stop_watch
    _stop_watch = None
    end.set()
    t.join(timeout=2 * STOP_POLL_S)


def _ends_stop_watch(fn):
    """The run's watch ends however the run does — a return or an exception."""
    import functools

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            _end_stop_watch()
    return run


def _load_chunks(row, collection):
    plugin = collection.plugin_instance()
    # ONE PARSE (docs/notes/one-parse.md): the fresh record where there is one
    from . import record_source
    chapters = record_source.sections(collection, row, lambda: plugin_io.sections(plugin, row))
    # the document's LAYER decides the chunker (amendment 41); one layer =
    # the plugin's chunker, exactly as before
    return layers_mod.chunks_for_document(collection, row, chapters)

_RDF_BATCH_PEERS = (
    "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=0 AND error IS NULL "
    "AND retries<? AND uid<>? LIMIT ?")

# One event line per batch would be ~86k rows/day at full speed — log spam
# that buries real events. At most one line per collection+stage per 30s;
# the counters on the Library/Stats pages are the real progress signal.
# The line carries the batches it stood for: without that, the log read as a
# time series (CLAUDE.md: the first benchmark) dropped most batches — on
# 2026-10-30 its lines summed to 604 of the 2,000 books a measured fts run
# indexed, and read as a 30 s pacing that was only the log's.
_batch_log_ts: dict[tuple[str, str], float] = {}
#: per collection+stage, since its last line: [batches, documents, chunks, stage seconds]
_batch_since: dict[tuple[str, str], list] = {}


def _batch_log(con, stage: str, collection, msg: str, *, docs: int = 0, chunks: int = 0,
               secs: float = 0.0, emit: bool = True) -> None:
    """`emit=False`: counted into the next line, never a line of its own — a
    one-document batch, which wrote nothing before the counts were carried
    (and a line it wrote went to the stdout of a command whose answer is JSON:
    `hold source list --json` after an in-process round, 2026-10-30)."""
    key = (collection.name, stage)
    now = time.time()
    if docs:
        acc = _batch_since.setdefault(key, [0, 0, 0, 0.0])
        acc[0] += 1
        acc[1] += docs
        acc[2] += chunks
        acc[3] += secs
    if emit and now - _batch_log_ts.get(key, 0.0) >= 30:
        _batch_log_ts[key] = now
        acc = _batch_since.pop(key, None)
        if acc is not None and acc[0] > 1:
            msg += (f"; since the last line: {acc[0]} batches, {acc[1]} documents"
                    + (f", {acc[2]} chunks" if acc[2] else "") + f" in {acc[3]:.1f}s of stage time")
        db.log_event(con, "info", stage, msg)


def _rdf_one(con, row, collection, plugin, now: int, meta: dict | None = None) -> tuple:
    if meta is None:
        meta = plugin_io.metadata(plugin, row)
    con.execute(
        "UPDATE documents SET title=?,authors=?,lang=?,subjects=?,bookshelves=?,"
        "work=?,rdf_done=1,updated_at=? WHERE uid=?",
        (meta["title"], meta["authors"], meta["lang"], meta["subjects"],
         meta["bookshelves"], plugin_io.work_identity(meta), now, row["uid"]))
    if meta.get("validity"):
        # one record per Expression (app/validity.py): this version joins its
        # Work's chain; a neighbour whose end or `shown` moved has its stage
        # marks reset here, in this transaction, so its index rows follow
        from app import validity
        validity.record(con, row["uid"], int(row["native_id"]), meta["validity"])
    # The catalog index's row (app/catalog.py), keyed on the document's
    # rowid and written by the caller before the marks commit. (The SQLite
    # FTS5 catalog it replaced was written here until 2026-09-19.)
    rowid = con.execute("SELECT rowid FROM documents WHERE uid=?", (row["uid"],)).fetchone()[0]
    return (rowid, meta["title"], meta["authors"], meta["subjects"], meta["lang"])


def _batch_key(collection, stage: str) -> str:
    """Sizing is per collection AND per stage, keyed on the manifest PATH:
    two collections can share a name and point at different corpora, and a
    document costs what its own corpus makes it cost."""
    key = f"{stage}:{collection.manifest_path}"
    _remember_label(key, f"{stage} {collection.name}")
    return key


def batch_docs(collection, stage: str, default: int) -> int:
    """How many documents this stage should take, from what they have
    actually cost here (app/sizing.py). The configured count is the STARTING
    point — not the floor: a collection whose documents cost 14-37 s each
    (gutenberg) must be able to take one at a time, which is why `lo` is 1.
    What stops a batch collapsing to the floor is the sizer modelling the
    FIXED cost of a batch instead of charging it to the documents."""
    return sizing.sizer().size(
        _batch_key(collection, stage), target_s=config.BATCH_TARGET_S,
        default=default, lo=1, hi=max(default, 1) * 20,
        ceiling_s=config.BATCH_CEILING_S)


def _waiting_because_off(con, collection) -> str:
    """"4,577,101 documents waiting: embed is off", or "" when the
    pipeline really has finished.

    A DISABLED STAGE AND A FINISHED ONE LOOK IDENTICAL from the selection
    query — both return no row — and the difference is the whole
    question an operator is asking when they look at the log. One is
    "done", the other is "will never start". Cheap: one indexed count,
    and only on the five-minute idle line.
    """
    try:
        if str(db.get_scope(con).get("embed_mode") or "").lower() != "off":
            return ""
        waiting = con.execute(
            "SELECT COUNT(*) FROM documents WHERE in_scope=1 AND fts_done=1 "
            "AND embed_done=0 AND error IS NULL").fetchone()[0]
    except Exception:      # noqa: BLE001 — the idle line must never raise
        return ""
    if not waiting:
        return ""
    return (f"{waiting:,} document(s) indexed and waiting to be embedded; "
            f"embed is OFF for this collection — nothing will start until "
            f"it is set in config/collections/{collection.name}.toml")


def stage_rdf(con, row, collection) -> None:
    """Extract metadata for `row`, and for as many other rdf-pending
    documents as RDF_BATCH_DOCS allows, through ONE transaction — opened
    HERE, after the parse, and committed by the caller (_execute_step).

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
    # THE WRITE LOCK BEGINS HERE, not before the parse (CLAUDE.md: long
    # non-DB work stays outside the transaction). The pool round trip above
    # is the batch's long part — 51.7 s for 2,000 documents, measured on the
    # node 2026-09-23 — and it used to run inside `BEGIN IMMEDIATE`, so
    # every other writer of this manifest (the discovery scan, the event
    # log, an acquire) queued behind a parse. stage_fts has opened its lock
    # late since the genome collection wedged its manifest for 68 minutes
    # the same way; this is that fix on the other stage. Only the writes
    # below need the lock, and they are seconds for a batch.
    if not con.in_transaction:               # direct callers wrap the stage
        con.execute("BEGIN IMMEDIATE")
    entries = [_rdf_one(con, row, collection, plugin, now, meta=first)]
    # the origin's inconsistencies each read met (06 §6.1, amended
    # 2026-10-25), recorded in this same transaction for the codes the read
    # checks — `None` when a plugin's lazy read says nothing about them
    found = [(row["uid"], first["issues"])] if first.get("issues") is not None else []
    n = 1
    for peer, (ok, meta) in zip(peers, metas[1:]):
        if not ok:
            continue                        # a peer never fails the round
        try:
            entries.append(_rdf_one(con, peer, collection, plugin, now, meta=meta))
            n += 1
        except Exception:   # noqa: BLE001
            continue
        if meta.get("issues") is not None:
            found.append((peer["uid"], meta["issues"]))
    if found:
        issues_mod.record_many(con, found, codes=issues_mod.declared(plugin),
                               checked=plugin_io.metadata_checks(plugin), now=now)
    # The catalog index is committed HERE, before the caller commits the
    # marks (invariant 2). A failure is CatalogUnavailable: the transaction
    # rolls back and the batch is deferred, never recorded as an error.
    catalog.write(collection, entries)
    took = time.monotonic() - t0
    sizing.sizer().record(_batch_key(collection, "rdf"), n, took)
    _batch_log(con, "rdf", collection,
               f"extracted {n} documents in one transaction ({took:.1f}s)",
               docs=n, secs=took, emit=n > 1)

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
    if not parallel.prefetch_allowed():          # serial, or under a profile's limit
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
    # a collection whose records are also found by NAME (wikidata, operator
    # 2026-10-24): the same parse gives each record's names (fts.search_names)
    with_names = fts.names_capable(collection)
    # the children read refs.db to tag each document (refs.read_store): its
    # schema is made here, by the worker, before they open it
    refs.get_store()
    results = parallel.fts_for_rows(collection, [row, *peers], names=with_names)
    names_of: dict[str, tuple | None] = {}
    entities_of: dict[str, list[str]] = {}

    def _unpack(r, value):
        names_of[r["uid"]] = value.get("names")
        entities_of[r["uid"]] = value["entities"]
        return value["chunks"]
    ok, first = results[0]
    if not ok:
        parallel.raise_for(first)
    chunks = _unpack(row, first)
    batch = [(row, chunks)]
    n_chunks = len(chunks)
    for peer, (ok, peer_value) in zip(peers, results[1:]):
        if n_chunks >= config.FTS_BATCH_CHUNKS:
            break
        if not ok:
            continue            # a peer never fails the round
        peer_chunks = _unpack(peer, peer_value)
        batch.append((peer, peer_chunks))
        n_chunks += len(peer_chunks)

    # the validity columns of a versioned collection (app/validity.py), read
    # before the lock like every other load; `{}` for every other collection
    from app import validity
    cols = validity.columns_for(con, collection, [int(r["native_id"]) for r, _ in batch])
    fts.index_books(((r["lang"], int(r["native_id"]), ch, cols.get(int(r["native_id"])),
                      names_of.get(r["uid"]))
                     for r, ch in batch), collection)

    # the tantivy commit landed; only the tags and marks remain — SQLite
    # writes, seconds for a batch. THIS is where the write lock begins
    # (unless a caller already opened one — direct callers in tests and
    # legacy paths wrap the whole stage; their commit covers these writes).
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    now = int(time.time())
    # the selected document first and unguarded: its failure IS the round's.
    # Its tags were computed by the child that cut it (parallel kind `fts`).
    entities.write_tags(con, row["uid"], entities_of[row["uid"]])
    _mark_indexed(con, row, chunks, now)
    for peer, peer_chunks in batch[1:]:
        try:
            entities.write_tags(con, peer["uid"], entities_of[peer["uid"]])
            _mark_indexed(con, peer, peer_chunks, now)
        except Exception:   # noqa: BLE001 — indexed but left unmarked, so it
            continue        # simply re-runs; never poisons the selected row
    # A document that clears fts inside a FINAL shard's id range is a
    # document the archive holds a published vector for and can no longer
    # deliver — `final` is decided from documents still UPSTREAM of fts, so
    # one that crosses fts during the pass is counted as "not waiting"
    # while never having been offered the shard. Reopening the shard is a
    # ledger write inside this same transaction, so it lands with the marks
    # that caused it or not at all (invariant 2). See
    # genesis.reopen_finals_for_range — measured 38,324 documents stranded
    # this way on pubmed.
    reopened = bundle_genesis.reopen_finals_for_range(
        con, collection, (r["native_id"] for r, _ in batch))
    if reopened:
        db.log_event(con, "info", "genesis",
                     f"fts crossed the range of final shard(s) "
                     f"{', '.join(reopened)}: reopened for one more pass, so "
                     f"their published vectors are imported rather than recomputed")
    took = time.monotonic() - t0
    sizing.sizer().record(_batch_key(collection, "fts"), len(batch), took)
    _batch_log(con, "fts", collection,
               f"indexed {len(batch)} documents / {n_chunks} chunks in one commit ({took:.1f}s)",
               docs=len(batch), chunks=n_chunks, secs=took, emit=len(batch) > 1)

#: how long a collection's embed stage stays idle before its encoder's pin
#: is restored (app/model_pins.py): the idle boundary of the stage, as long
#: as an idle on-demand embedder stays loaded (IDLE_BEATS settle beats)
PIN_IDLE_S = 120.0
#: the worker's holds on its encoders: holder -> [documents in use, last used]
_pins: dict[str, list] = {}
_pins_lock = threading.Lock()


def _pin_begin(collection, recipe) -> str:
    """THE EMBED STAGE PINS ITS ENCODER from its first batch to its idle
    boundary (operator, 2026-10-23: "model eviction during processing shall
    be forbidden … a slow down is acceptable, breaking a processing mid way
    is not"). Counted in, so a document of several hours is never released
    under itself; `_release_idle_pins` restores it once nothing has used it
    for PIN_IDLE_S. Never raises, never waits beyond model_pins' timeouts."""
    holder = f"{model_pins.WORKER}embed {collection.name}"
    with _pins_lock:
        entry = _pins.setdefault(holder, [0, 0.0])
        entry[0] += 1
    try:
        model_pins.hold(getattr(recipe, "url", None), getattr(recipe, "model", None),
                        holder, collection=collection.name)
    except Exception:   # noqa: BLE001 — a pin is a safeguard, never a gate
        pass
    return holder


def _pin_end(holder: str) -> None:
    with _pins_lock:
        entry = _pins.get(holder)
        if entry is not None:
            entry[0] = max(0, entry[0] - 1)
            entry[1] = time.time()


def _release_idle_pins(now: float | None = None) -> list[str]:
    """The settle beat's: the pins of embed stages idle for PIN_IDLE_S go,
    each model back to its state before. Under the lock, so a stage that
    starts meanwhile pins afresh rather than losing its pin."""
    now = time.time() if now is None else now
    gone: list[str] = []
    with _pins_lock:
        for holder, (n, last) in list(_pins.items()):
            if n == 0 and now - last >= PIN_IDLE_S:
                del _pins[holder]
                model_pins.release(holder)
                gone.append(holder)
    return gone


def _possessed_vectors(con, collection, row, chunks) -> dict:
    """chunk_id -> vector for this document from an accepted bundle held
    here that holds the record as it is now (app/share/possession.py
    `held_vectors`): the embed stage carries them instead of asking the
    encoder. RULE 1 — Perugia, 2026-10-23: since its resume it embedded
    12,921 chunks of gutenberg that the node's predecessor bundles, held
    there as files, already carried, and published them as 145 new bundles.
    {} for a document no held bundle covers, a collection with no Codex,
    or any doubt (the encoder then embeds, as before)."""
    codex = getattr(collection, "codex", None)
    if codex is None or not chunks:
        return {}
    try:
        from app.share import possession
        root = collections_mod.louise_root() / "collections"
        # FIRST whether any accepted bundle holds the record: most records of
        # most collections are held by none, and then nothing is looked up
        held = possession.index(root, collection.name, codex, con)
        if row["uid"] not in held:
            return {}
        keys = row.keys() if hasattr(row, "keys") else ()
        # the record's hash in the current snapshot, as possession judges it:
        # the manifest's column is NULL on a peer that never filled it
        # (Perugia: all 79,659 rows) — then read from the part the holding
        # bundle names, never from the whole collection (pubmed: 40 M hashes
        # per embed thread, the worker OOM-killed at 46 GB, 2026-10-30)
        content_hash = (row["content_hash"] if "content_hash" in keys else None) \
            or possession.record_hash_of(root, collection.name, held, row["uid"])
        if not content_hash:
            return {}
        return possession.held_vectors(root, collection.name, codex, con, row["uid"], content_hash,
                                       [c["id"] for c in chunks])
    except Exception:   # noqa: BLE001 — any doubt embeds
        return {}


def _holds_all(held, book_id: int, chunks) -> bool:
    """The vector table already holds every one of these chunks, text for
    text (`held`: the group's one read, `embeddings.held_chunks`): a reused
    document asks no bundle for anything — and so builds no possession index
    (pubmed's re-embedding by reuse, 2026-10-30)."""
    if held is None or not chunks:
        return False
    have = held.of(book_id)
    if len(have) < len(chunks):
        return False                    # never written, or written in part
    return all((have.get(c.get("id")) or {}).get("text") == c.get("text") for c in chunks)


def _given(con, collection, row, chunks) -> dict:
    """`upsert_book`'s `given=`, only when a held bundle gives something."""
    got = _possessed_vectors(con, collection, row, chunks)
    return {"given": got} if got else {}


def stage_embed(con, row, collection) -> None:
    chunks = _load_chunks(row, collection)
    # lock_con serializes the delete+add pair against a concurrent bundle
    # import in another process (app/bundles/writer_lock.py). Passing it here
    # is what makes the importer's lock mean anything.
    started = time.time()
    counts = embeddings.EmbedCounts()
    stats.recorder().set_in_flight(collection.name, 1)
    pin = _pin_begin(collection, collections_mod.recipe_for(collection))
    try:
        # `stop` bounds SIGTERM to one slice: a 5,335-chunk document is
        # 5.3 h of encoder time, and "finish the current unit" has to mean
        # something shorter than that (invariant 2 — smaller where a
        # document is not possible). What is written stays written.
        from app import validity
        cols = validity.columns_for(con, collection, [int(row["native_id"])]).get(int(row["native_id"]))
        embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks,
                               collection, lock_con=con, stop=_embed_stop,
                               reuse=embeddings.may_reuse(row), counts=counts,
                               columns=cols, **_given(con, collection, row, chunks))
        if cols:
            # a document whose every vector was kept rewrote no row: the two
            # columns follow the version table in place (app/validity.py)
            embeddings.sync_columns(int(row["native_id"]), cols, collection, lock_con=con, refuse=True)
            validity.mark_synced(con, [int(row["native_id"])])
    finally:
        stats.recorder().set_in_flight(collection.name, 0)
        _pin_end(pin)
    # Timed around the encoder call only — not around _load_chunks, and not
    # around the manifest write below — so the figure on the Stats page is
    # the endpoint's throughput and nothing else. Comparing a CPU encoder
    # with a GPU one is the whole point of measuring it. A document whose
    # every vector was kept is not encoder work, and is not recorded.
    _record_embedded(collection, started, counts)
    bundle_ledger.clear_verification(con, row["uid"])   # no longer imported provenance
    con.execute(
        "UPDATE documents SET embed_done=1,embed_version=?,updated_at=? WHERE uid=?",
        (config.EMBED_VERSION, int(time.time()), row["uid"]))
    db.log_event(con, "info", "embed",
                 f"embedded {counts.embedded} chunks, reused {counts.reused}"
                 + (f" ({counts.crossed} from other versions)" if counts.crossed else ""), row["uid"])


def _record_embedded(collection, started: float, counts) -> None:
    """The Stats page's encoder throughput: only what reached the encoder."""
    if counts.embedded:
        stats.recorder().record_embed(
            collection.name, seconds=time.time() - started, docs=1,
            chunks=counts.embedded, chars=counts.chars)

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
# final (the covered ranges change): the documents in that range the archive
# did not cover sit BELOW the cursor and are now the encoder's. Without an
# archive the NEW walk has no cursor — a document that clears fts must be
# embeddable in the same round, and its index prefix ends on the candidates
# themselves. The VERSION walk is windowed either way: its prefix ends at
# embed_done=1 and matches every embedded document (2026-09-22).
_BACKFILL_SQL = (
    "SELECT rowid AS rid, * FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=1 AND embed_done=0 AND error IS NULL AND retries<? AND rowid>? "
    "{covered} ORDER BY rowid LIMIT ?")
#: kept for the plan checks and the report: the unwindowed version selection,
#: which nothing runs any more (`_REEMBED_WINDOWED` is what runs)
_REEMBED_SQL = (
    "SELECT rowid AS rid, * FROM documents WHERE in_scope=1 AND rdf_done=1 "
    "AND fts_done=1 AND embed_done=1 AND embed_version<? AND error IS NULL "
    "AND retries<? AND rowid>? ORDER BY rowid LIMIT ?")
# The same two selections over ONE rowid window, for the cursor walk. The
# `+` keeps the planner on the rowid range instead of the scope index: the
# window is what bounds the call, and a scope-index walk from the cursor has
# no end short of the table's (the `_FTS_REINDEX_WINDOWED` rule).
#: THE NEW WALK'S WINDOW READS THE CANDIDATES, not the rows (2026-10-30). All
#: four scope columns are equalities here, so SQLite seeks the scope index on
#: (1,1,1,0) AND the rowid range (its implicit tail): a window costs what it
#: holds of candidates. Forced onto the primary key like the version walk
#: below (whose prefix is every embedded document), each window read all its
#: rows: wikipedia's first sweep after a restart crossed 34 M rowids of its
#: covered, embedded layer 1 at ~33 k rowids/s — ~17 minutes before the
#: encoder saw one of the 18.8 M dump-layer documents behind them (München).
_BACKFILL_WINDOWED = (
    "SELECT rowid AS rid, * FROM documents WHERE rowid>? AND rowid<=? "
    "AND in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=0 "
    "AND error IS NULL AND retries<? {covered} ORDER BY rowid LIMIT ?")
_REEMBED_WINDOWED = (
    "SELECT rowid AS rid, * FROM documents WHERE rowid>? AND rowid<=? "
    "AND +in_scope=1 AND +rdf_done=1 AND +fts_done=1 AND +embed_done=1 "
    "AND embed_version<? AND error IS NULL AND retries<? ORDER BY rowid LIMIT ?")
_BACKFILL_WINDOW = 200_000       # the starting width; sizing.py adjusts it
_BACKFILL_CURSOR_TTL_S = 120
#: …but a VERSION sweep that found nothing waits far longer before the next.
#: A re-embed candidate is a document whose vector is older than the encoder's
#: version, and one can appear in only two ways: a version bump, which is a
#: code change and so a worker restart — and a restart clears these cursors
#: outright — or an import of older vectors, which moves the coverage epoch
#: and clears them too. Polling for it every two minutes is therefore a sweep
#: of the whole corpus that CANNOT find anything: 58 s of CPU per sweep on
#: pubmed's 40M, about a tenth of a core for ever, after the windowing above
#: had already cut it from a whole one (the node, 2026-09-22).
#: Nothing an OPERATOR asks for waits on this: every re-embed they can
#: request — `api.reindex`, a scope change — sets
#: `embed_done=0`, and that is the new walk's business, at its short wait.
_REEMBED_IDLE_TTL_S = 3600


def _idle_ttl(kind: str) -> float:
    """How long a sweep that found NOTHING waits before the next one. The new
    walk keeps the short wait — a document that clears fts below the cursor is
    work waiting on it."""
    return _REEMBED_IDLE_TTL_S if kind == "ver" else _BACKFILL_CURSOR_TTL_S
# (path, kind) -> (epoch, cursor rowid, sweep's top rowid, found in this
# sweep, idle since) — `idle since` is set when a whole sweep found nothing
_backfill_cursor: dict[tuple[str, str], tuple[int, int, int, bool, float | None]] = {}
_backfill_cursor_lock = threading.Lock()


def _covered_ranges(con, collection) -> list[tuple[int, int]]:
    """Merged id ranges of the archive's shards that are not final — what
    the archive can still deliver, so the local encoder skips them. A
    REJECTED archive covers nothing (it is not this function)."""
    if collection is None or not collection.genesis_archive:
        return []
    archive = bundle_genesis.ARCHIVES.get(collection.genesis_archive)
    if archive is None or not archive.ids_per_shard:
        return []
    rows = con.execute("SELECT shard, state, proven_at, docs FROM genesis_imports "
                       "WHERE archive=?", (archive.archive_id,)).fetchall()
    if bundle_genesis.archive_stopped(rows):
        return []           # nothing proven, something rejected: not this function
    if archive.local:
        # A LOCAL archive (wikipedia's layer 1): its shards are FILES and
        # never go final, so its declared range stays the archive's, and
        # what a read cannot cover — a straddler, a rejected file's
        # documents, text that would not load — reaches the encoder by uid
        # (`_backfill_rows`, `genesis.residual_uids`)
        return bundle_genesis.declared_spans(archive)
    # a final shard delivered all it can; a rejected one (under a proven
    # archive) never will — both release their range to the encoder
    done = {r["shard"] for r in rows if r["state"] in ("final", "rejected")}
    return bundle_genesis.declared_spans(archive, exclude=done)


def _kept_window(con, sql: str, start: int, end: int, params: tuple, n: int, keep) -> list:
    """One rowid window of `sql`, paged until `n` rows pass `keep` or the
    window is exhausted. A window whose candidates are ALL left to another
    peer returns nothing, so `_cursor_page` moves past it — never the same
    skipped head every round (`"did work" is not "made progress"`)."""
    out: list = []
    lo = start
    while len(out) < n:
        rows = con.execute(sql, (lo, end, *params, n)).fetchall()
        if not rows:
            break
        out += [r for r in rows if keep(r)]
        lo = int(rows[-1]["rid"])
        if len(rows) < n:
            break
    return out[:n]


#: manifest path -> (cursor rowid, found in this sweep, idle since) — the walk
#: over the candidates when some are another peer's (`_lead_walk`)
_lead_cursor: dict[str, tuple[int, bool, float | None]] = {}
_lead_cursor_lock = threading.Lock()
#: rows read per step of `_lead_walk`
_LEAD_PAGE = 512


def _lead_walk(con, key: str, limit: int, keep, deadline: float) -> list:
    """The new walk when some candidates are another peer's (04 §4.5): ON
    THE SCOPE INDEX, by rowid from a cursor — the plain walk's prefix
    (in_scope, rdf_done, fts_done, embed_done=0) with `rowid>?`, so a sweep
    reads the CANDIDATES, skipped ones included, and never the rest of the
    table (review of 45535ea: a rowid-window walk would read every row per
    sweep — the 2026-09-19 pubmed regression at 30M rows).

    The cursor resumes from the first row kept (a deferred one is selected
    again); a sweep that reaches the end having found work restarts at once
    from the top (documents below the cursor may have become candidates);
    one that found nothing waits `_BACKFILL_CURSOR_TTL_S` from its end. A
    call stops at `deadline` and the sweep carries on at the next."""
    sql = _BACKFILL_SQL.format(covered="")
    with _lead_cursor_lock:
        pos, found, idle = _lead_cursor.get(key, (-1, False, None))
    if idle is not None:
        if time.time() - idle < _BACKFILL_CURSOR_TTL_S:
            return []
        pos, found, idle = -1, False, None
    out: list = []
    restarted = False
    state = (pos, found, None)
    while True:
        rows = con.execute(sql, (config.MAX_RETRIES, pos, max(_LEAD_PAGE, limit))).fetchall()
        for r in rows:
            if keep(r):
                out.append(r)
                if len(out) >= limit:
                    break
        if out:
            state = (int(out[0]["rid"]) - 1, True, None)
            break
        if not rows:                               # the end of the candidates
            if found and not restarted:
                pos, found, restarted = -1, False, True
                continue
            state = (-1, False, time.time())
            break
        pos = int(rows[-1]["rid"])
        if time.monotonic() >= deadline:
            state = (pos, found, None)
            break
    with _lead_cursor_lock:
        _lead_cursor[key] = state
    return out[:limit]


def _backfill_rows(con, collection, limit: int, mode: str | None = None) -> list:
    """Dense backfill candidates under "all" and "p2p": new documents first,
    then version-driven re-embeds — both cursor-paged when an archive is
    declared, both walked from the start otherwise.

    Under "p2p" a shared collection's open parts that ANOTHER live peer leads
    (04 §4.5) are left to it (`share.leading`), in both walks: their documents are not
    candidates here, and the new walk then goes along the scope index from a
    cursor (`_lead_walk`), so a skipped head never blocks it and a sweep
    costs the candidates, never the table. No peer, no lead elsewhere:
    exactly as before."""
    ranges = _covered_ranges(con, collection)
    key = str(collection.manifest_path) if collection is not None else ""
    if collection is not None:
        _remember_label(key, collection.name)
    from app.share import leading
    # the partition is `embed = "p2p"`'s ("all p2p", 07 §7.8); under `all`
    # ("all local") this node embeds every part itself, whoever leads it
    if mode is None:
        mode = db.get_scope(con).get("embed_mode")
    skip = leading.skip_test(collection, con) if collection is not None and mode == "p2p" else None
    keep = None if skip is None else (lambda r: not skip(r))
    if not ranges:
        if keep is None:
            # THE NEW WALK STAYS UNBOUNDED, and must: its index prefix
            # (in_scope, rdf_done, fts_done, embed_done=0) IS the candidate set,
            # so it lands on the first candidate or on nothing, and a document
            # that clears fts has to be embeddable in the same round.
            rows = con.execute(_BACKFILL_SQL.format(covered=""),
                               (config.MAX_RETRIES, -1, limit)).fetchall()
        else:
            rows = _lead_walk(con, key, limit, keep,
                              time.monotonic() + config.BACKFILL_WALK_BUDGET_S)
            if not rows:
                # 04 §4.5 (2026-10-20): nothing left among the parts at this
                # rank — the bounty list has no leader, so the next rank's
                # parts are this peer's at the next round; never idle while
                # open parts remain
                leading.widen(collection)
        # THE VERSION WALK IS WINDOWED HERE TOO. Its prefix ends at
        # embed_done=1, which is EVERY embedded document, and `embed_version`
        # is then tested row by row — so with no archive this read the whole
        # scope index for nothing: 57.8 s per call on pubmed's 40M, again and
        # again, one core of the worker for as long as the collection was set
        # to embed "all" (the node, 2026-09-22). A re-embed is never urgent —
        # the document already has a vector — so it may wait for the sweep,
        # which is exactly what the archive branch below already does.
        if len(rows) < limit:
            ver_sql = _REEMBED_WINDOWED
            rows += _cursor_page(
                con, key, "ver",
                lambda start, end, n: _window_page(
                    con, ver_sql, start, end, (config.EMBED_VERSION, config.MAX_RETRIES), n, keep),
                limit - len(rows),
                time.monotonic() + config.BACKFILL_WALK_BUDGET_S)
        return rows
    spans_sql, params = bundle_genesis.in_spans_sql(ranges)
    new_sql = _BACKFILL_WINDOWED.format(covered="AND NOT " + spans_sql)
    # ONE budget for the whole call, both walks: a call that finds nothing
    # must cost a bounded slice of the encoder's turn, never a sweep
    deadline = time.monotonic() + config.BACKFILL_WALK_BUDGET_S
    coverage = tuple(tuple(r) for r in ranges)
    rows = _cursor_page(
        con, key, "new",
        lambda start, end, n: _window_page(con, new_sql, start, end,
                                           (config.MAX_RETRIES, *params), n, keep),
        limit, deadline, coverage)
    if len(rows) < limit:
        archive = bundle_genesis.ARCHIVES.get(collection.genesis_archive or "")
        if archive is not None and archive.local:
            rows += _residual_rows(con, collection, limit - len(rows),
                                   {r["uid"] for r in rows})
    if len(rows) < limit:
        ver_sql = _REEMBED_WINDOWED
        rows += _cursor_page(
            con, key, "ver",
            lambda start, end, n: _window_page(
                con, ver_sql, start, end, (config.EMBED_VERSION, config.MAX_RETRIES), n, keep),
            limit - len(rows), deadline, coverage)
    if not rows and keep is not None:
        leading.widen(collection)             # as above, the archive-bounded walk
    return rows


def _window_page(con, sql: str, start: int, end: int, params: tuple, n: int, keep) -> list:
    """One rowid window of a windowed walk: as it was with no filter, paged
    through `_kept_window` when some candidates are another peer's."""
    if keep is None:
        return con.execute(sql, (start, end, *params, n)).fetchall()
    return _kept_window(con, sql, start, end, params, n, keep)


#: a LOCAL archive's residuals, by primary key: bounded by the residual set
#: (a straddler per shard boundary, the odd unloadable text), never a walk
_RESIDUAL_SQL = ("SELECT rowid AS rid, * FROM documents WHERE uid IN ({marks}) "
                 "AND in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=0 "
                 "AND error IS NULL AND retries<?")


def _residual_rows(con, collection, limit: int, seen: set) -> list:
    """The documents inside a local archive's declared range that its reads
    could not cover (`genesis.residual_uids`) — the only ones there the
    local encoder may embed (amendment 11)."""
    uids = [u for u in bundle_genesis.residual_uids(collection) if u not in seen]
    out: list = []
    stale: list[str] = []
    for i in range(0, len(uids), 500):
        part = uids[i:i + 500]
        got = con.execute(_RESIDUAL_SQL.format(marks=",".join("?" * len(part))),
                          (*part, config.MAX_RETRIES)).fetchall()
        eligible = {r["uid"] for r in got}
        stale += [u for u in part if u not in eligible]
        out += got
        if len(out) >= limit:
            break
    # a residual no longer eligible is asked for no more
    bundle_genesis.forget_residuals(collection, stale)
    return out[:limit]


def _cursor_page(con, key: str, kind: str, query, limit: int, deadline: float,
                 coverage: tuple = ()) -> list:
    """One bounded step of the backfill walk: rowid windows from the
    cursor, until a window holds candidates, the sweep reaches the rowid it
    started at the top of, or `deadline` passes — whichever is first.

    WHY WINDOWS. Measured live on pubmed (40M rows, 2026-09-19): one call
    that found nothing took 153 s cold and 105 s "warm", every call. The
    walk went to the end of the table in ONE query, and the cursor's age was
    stamped BEFORE that query, so a 105 s walk against a 120 s TTL left a
    cursor already expired when it was stored — every call re-walked all 40M
    rows. Its turns on the encoder rotation cost 250 s in ten minutes for
    zero documents, and the serial main loop idled the other encoders
    through all of it. Now a call costs at most BACKFILL_WALK_BUDGET_S, and
    the sweep carries on at the next call from where this one stopped.

    A sweep that FOUND NOTHING waits _BACKFILL_CURSOR_TTL_S from its END
    before the next one starts, since nothing below it can have become
    eligible faster than that matters. A sweep that found rows restarts at
    once, because documents below the cursor may have become eligible while
    it walked.

    THE SWEEP RESTARTS WHEN THE COVERAGE CHANGES, not when the process-wide
    coverage epoch moves (`coverage`: the archive's covered ranges the walk
    filters by). A shard going final releases documents below the cursor,
    and only that is a reason to start over. The epoch moved at every
    shard of every archive going final or applied, about once a minute while
    wikipedia's layer 1 imported, and each time the sweep began again at
    rowid 0, among 22 M rows its own local archive covers (whose declared
    spans never change) — so it never reached the dump layer behind them:
    18.8 M documents ready for the encoder, the producer asleep (München,
    2026-10-30).
    """
    epoch = coverage
    with _backfill_cursor_lock:
        cur = _backfill_cursor.get((key, kind))

    def fresh():
        top = int(con.execute("SELECT COALESCE(MAX(rowid), -1) FROM documents").fetchone()[0])
        return (epoch, -1, top, False, None)

    if cur is None or cur[0] != epoch:
        cur = fresh()
    elif cur[4] is not None:
        if time.time() - cur[4] < _idle_ttl(kind):
            return []               # the last sweep found nothing: not yet
        cur = fresh()
    _, pos, top, found, _ = cur
    wkey = f"{key}|backfill-{kind}"
    name = _sizing_names.get(key)
    if name:
        _remember_label(wkey, f"backfill ({kind}) {name}")
    rows: list = []
    restarted = False
    while True:
        if pos >= top:
            if rows:
                cur = (epoch, int(rows[-1]["rid"]), top, True, None)
                break
            if found and not restarted:
                # this sweep found work: start the next one now, from the
                # beginning, for what became eligible below the cursor
                _, pos, top, found, _ = fresh()
                restarted = True
                continue
            cur = (epoch, pos, top, found, None if found else time.time())
            break
        width = sizing.sizer().window(wkey, default=_BACKFILL_WINDOW,
                                      lo=_REINDEX_WINDOW_MIN, hi=_REINDEX_WINDOW_MAX)
        end = min(pos + width, top)
        probe = time.monotonic()
        got = query(pos, end, limit - len(rows))
        sizing.sizer().note_window(
            wkey, seconds=time.monotonic() - probe,
            target_s=config.REINDEX_PROBE_TARGET_S, default=_BACKFILL_WINDOW,
            lo=_REINDEX_WINDOW_MIN, hi=_REINDEX_WINDOW_MAX)
        # THE CALL FILLS ITS LIMIT ACROSS WINDOWS, within its budget. It
        # stopped at the first window holding a candidate: where candidates
        # are sparse (wikipedia's archive layer) each call brought ONE
        # document, and the 18.8 M of the dump layer behind them waited —
        # one embed batch of one document every few minutes (München,
        # 2026-10-30). The next call resumes AFTER the last row returned; a
        # document deferred meanwhile comes back with the next sweep.
        rows += got
        if got and len(rows) >= limit:
            cur = (epoch, int(rows[-1]["rid"]), top, True, None)
            break
        pos = end                       # the window gave all it had: fewer than asked
        if time.monotonic() >= deadline and pos < top:
            cur = (epoch, int(rows[-1]["rid"]), top, True, None) if rows else (epoch, pos, top, found, None)
            break
    with _backfill_cursor_lock:
        _backfill_cursor[(key, kind)] = cur
    return rows


def reset_backfill_cursors() -> None:
    with _backfill_cursor_lock:
        _backfill_cursor.clear()
    with _lead_cursor_lock:
        _lead_cursor.clear()


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
    if embed_allowed and scope["embed_mode"] in collections_mod.EMBEDDING_MODES:
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
    # 4. dense backfill only under all / p2p — coverage-aware (amendment 38):
    # what a declared archive can still deliver is not the encoder's
    if embed_allowed and scope["embed_mode"] in collections_mod.BACKFILL_MODES:
        rows = _backfill_rows(con, collection, 1, mode=scope["embed_mode"])
        if rows:
            return "embed", rows[0]
    return None, None


def _next_embed(con, scope, collection=None):
    """Embed-stage selection ONLY — the main loop's share of the pipeline
    when per-collection threads own rdf/fts. Exactly _next_book's parts 1
    (lazy queue) and 4 (backfill under 'all'), so the two selectors can
    never claim the same document: a document is embed-eligible only after
    its fts commit, and a pipeline thread never touches it again after that."""
    if scope["embed_mode"] not in collections_mod.EMBEDDING_MODES:
        return None, None
    # INVARIANT 9 GATES EMBEDDING TOO — it is use of the corpus. _next_book
    # gates its round; this selector, which the main loop uses whenever
    # pipeline threads own rdf/fts, did not, so an unacknowledged yellow or
    # red collection's documents past fts were still embedded (found by the
    # review of `louise node health`, 2026-09-13).
    if collection is not None and not _license_allows(con, collection, "embedding"):
        return None, None
    r = con.execute(_QUEUE_ELIGIBLE,
                    (config.MAX_RETRIES, config.EMBED_VERSION, 1)).fetchone()
    if r:
        return "embed", r
    con.execute(_QUEUE_CLEANUP, (config.MAX_RETRIES,))
    if scope["embed_mode"] in collections_mod.BACKFILL_MODES:
        rows = _backfill_rows(con, collection, 1, mode=scope["embed_mode"])
        if rows:
            return "embed", rows[0]
    return None, None

STAGES = {"rdf": stage_rdf, "fts": stage_fts, "embed": stage_embed}

#: Stages `_execute_step` must NOT open the manifest's write transaction
#: for. Each one's long part is not SQLite — the parse pool, the tantivy
#: commit, the encoder call — and CLAUDE.md is explicit that long non-DB
#: work stays outside the transaction: the RESERVED lock blocks every other
#: writer of that manifest, including the discovery scan and the event log.
#: rdf and fts take `BEGIN IMMEDIATE` themselves, late, once only SQLite
#: writes remain; embed takes none at all and lets `upsert_book` lock the
#: vector store for the Lance write alone. A stage NOT listed here is one
#: whose whole duration is SQLite, and holding the lock for it is right.
_NO_OUTER_TRANSACTION = ("embed", "fts", "rdf")


def _embed_candidates(con, scope, limit: int, collection=None):
    """Up to `limit` embed-stage candidates, queue priority first — the same
    two selections _next_book makes, widened for the parallel path."""
    # invariant 9, as in _next_embed: embedding is use of the corpus
    if collection is not None and not _license_allows(con, collection, "embedding"):
        return []
    # a document whose encoder timed out waits for the endpoint to answer
    # someone else (app/encoder_timeouts.py): passed over, and the read
    # widened by as many, so the rest still fill the call
    place = str(collection.manifest_path) if collection is not None else ""
    marked = encoder_timeouts.waiting(place) if place else 0
    want = limit + marked
    rows = []
    if scope["embed_mode"] in collections_mod.EMBEDDING_MODES:
        rows += con.execute(
            _QUEUE_ELIGIBLE,
            (config.MAX_RETRIES, config.EMBED_VERSION, want)).fetchall()
    if len(rows) < want and scope["embed_mode"] in collections_mod.BACKFILL_MODES:
        seen = {r["uid"] for r in rows}
        rows += [r for r in _backfill_rows(con, collection, want, mode=scope["embed_mode"])
                 if r["uid"] not in seen][: want - len(rows)]
    if marked:
        rows = [r for r in rows if not encoder_timeouts.passed_over(place, r["uid"])]
    return rows[:limit]


#: the embed stage cuts a candidate in the parse pool when the manifest says
#: it has at most this many chunks (`_embed_parallel`'s `prepared`)
EMBED_PREPARE_MAX_CHUNKS = 64


def _embed_parallel(con, collection, recipe, *, slots: int | None = None,
                    keep_going=None) -> bool:
    """CPU-encoder collections (recipe.concurrency > 1) embed several WHOLE
    DOCUMENTS concurrently. Per-doc request concurrency cannot help a corpus
    whose documents are single chunks (PubMed abstracts); the unit of
    parallelism must be the document. Each thread derives chunks and writes
    vectors on its own manifest connection (the Lance writer lock is a real
    SQLite lock, cross-connection); manifest row updates and logging happen
    back on this thread, which owns `con`.

    WITH `slots` — the embed producers, every encoder, GPU included
    (docs/notes/scheduling.md §12). The operator's rule: at most `slots`
    documents SUBMITTED to the encoder's gate at once, and one more being
    PREPARED meanwhile (its chunks cut, its held rows read), so that the
    moment a document comes back its slot is taken by one that is ready.
    The gate admits the server's capacity at a time — one request for a GPU
    llama-server, which is what the old document-serial path guaranteed:
    the server never sees two sequences at once, the failure mode the batch
    gate guards. Without `slots` (the rollback), `recipe.concurrency`
    documents run at once, as before.

    `keep_going()` is asked before each refill: False stops taking new
    documents (a pause), and what is in flight finishes and is committed.

    ONE READ AND ONE COMMIT PER BATCH, not per document (2026-09-15).
    Measured per pubmed abstract: reading what it already held 503 ms, its
    commit 37 ms, the encoder 32 ms — ~2 documents a second with the GPU
    idle ~90 % of the time. And each one-document commit left a fragment
    that every later read scans (+0.5 ms apiece; 6,566 of them between two
    hourly compactions). So the held rows of EMBED_BATCH_DOCS candidates are
    read at once (`embeddings.held_chunks`); a worker still embeds one
    document, but leaves a one-slice document's rows in `sink`; and this
    thread commits them together (`embeddings.write_pending`), marking them
    done inside the same manifest transaction (invariant 2) — at
    EMBED_BATCH_DOCS documents, after EMBED_COMMIT_S, and when the call
    ends, SIGTERM included. A longer document still writes its own slices.
    """
    scope = db.get_scope(con)
    window = recipe.concurrency if slots is None else slots + 1
    submitted = None if slots is None else threading.Semaphore(slots)
    group = max(window * 2, config.EMBED_BATCH_DOCS)
    rows = _embed_candidates(con, scope, group, collection)
    if not rows:
        return False
    sink = embeddings.PendingWrites()
    # chunks embedded vs kept, for the call's log line (RULE 1's numbers)
    tally = embeddings.EmbedCounts()
    tally_lock = threading.Lock()

    # connections LENT to the documents in turn, one per document in flight,
    # opened once per call: `db.connect` runs the manifest's ALTERs and its
    # schema script, and per document that was paid ~75.9 M times on wikidata
    lent: list = []
    lent_lock = threading.Lock()

    def borrow():
        with lent_lock:
            if lent:
                return lent.pop()
        return db.connect(collection, shared=True)

    def give_back(c) -> None:
        with lent_lock:
            lent.append(c)

    def prepared(group_rows) -> dict:
        """Each SHORT candidate's chunks, cut by the parse pool's children for
        the whole group at once — the threads below cut them one by one under
        the GIL, which bound short documents: pubmed ran 31 documents/s while
        the encoder answered in 5–36 ms (STATUS.md). A long document is cut
        by its own thread as before (the encoder is its cost, and a group of
        books would sit in memory), and so is one the pool could not cut,
        whose failure stays the document's own."""
        short = [r for r in group_rows if 0 < (r["chunks"] or 0) <= EMBED_PREPARE_MAX_CHUNKS]
        if not short:
            return {}
        try:
            got = parallel.chunks_for_rows(collection, short)
        except Exception:   # noqa: BLE001 — each thread cuts its own, as before
            return {}
        return {r["uid"]: v for r, (ok, v) in zip(short, got) if ok}

    def work(row, held, cut=None):
        if _stop.is_set():
            return (row, None, "stopped", False)
        counts = None
        try:
            # a pool thread starts with an empty context: its requests
            # declare themselves here, or they would reach the gate at the
            # process default
            with dispatch.working_as(Level.CORPUS, collection.name):
                chunks = cut if cut is not None else _load_chunks(row, collection)   # PREPARED
                tcon = borrow()
                try:
                    if submitted is not None:
                        # SUBMITTED: wait for one of the plugin's slots
                        while not submitted.acquire(timeout=1.0):
                            if _stop.is_set():
                                return (row, None, "stopped", False)
                    started = time.time()
                    counts = embeddings.EmbedCounts()
                    # the validity columns of a versioned collection, as
                    # stage_embed passes them: this path did not, so the first
                    # write created Normattiva's table WITHOUT them and every
                    # later row was stripped to fit it (2026-10-13)
                    from app import validity
                    nid = int(row["native_id"])
                    cols = validity.columns_for(tcon, collection, [nid]).get(nid)
                    try:
                        # `stop` bounds SIGTERM to one slice of a long
                        # document (stage_embed's reason: gutenberg 4656 is
                        # 5,335 chunks); the rest resumes from what is written
                        reuse = embeddings.may_reuse(row)
                        n = embeddings.upsert_book(nid, row["lang"],
                                                    chunks, collection, lock_con=tcon,
                                                    stop=_embed_stop, held=held, sink=sink,
                                                    reuse=reuse,
                                                    counts=counts, columns=cols,
                                                    **({} if reuse and _holds_all(held, nid, chunks)
                                                       else _given(tcon, collection, row, chunks)))
                        if cols and not counts.embedded and counts.reused:
                            # every vector kept, no row rewritten: the columns
                            # follow the version table in place (app/validity.py)
                            embeddings.sync_columns(nid, cols, collection, lock_con=tcon, refuse=True)
                    finally:
                        if submitted is not None:
                            submitted.release()
                finally:
                    give_back(tcon)
            # Measured HERE as well as in stage_embed, because every CPU
            # encoder in the deployment takes this path and only the GPU one
            # takes the other. Recording in stage_embed alone left the Stats
            # page permanently blank for exactly the encoders it exists to
            # compare. The recorder is lock-guarded, so several document
            # threads reporting at once is fine.
            _record_embedded(collection, started, counts)
            with tally_lock:
                tally.add(counts)
            if counts.embedded:
                # the endpoint answered: evidence for a timed-out neighbour
                encoder_timeouts.answered(endpoint, place, row["uid"])
                answered_here.append(True)
            else:
                encoder_timeouts.forget(place, row["uid"])
            return (row, n, None, sink.holds(int(row["native_id"])))
        except embeddings.EmbedInterrupted:
            # SIGTERM between two slices of a long document: what is written
            # stays, the document is NOT done, and resumes from it — never
            # an error, which would take it out of every selection
            return (row, None, DEFERRED, False)
        except Exception as e:  # noqa: BLE001 — isolated per document
            timeout = endpoint_timed_out(e)
            if timeout is not None:
                if counts is not None and counts.embedded:
                    # this attempt embedded part of it: not stuck, resumed later
                    encoder_timeouts.forget(place, row["uid"])
                n = encoder_timeouts.timed_out(endpoint, place, row["uid"])
                if n >= encoder_timeouts.BLAME_AFTER:
                    return (row, None, _timeout_blame(timeout, n), False)
                timed_out.append(timeout)       # the hold is made on this thread's caller
                return (row, None, DEFERRED, False)
            if transient_failure(e):
                if endpoint_misconfigured(e):
                    wrong_endpoint.append(e)    # the pause is made on this thread's caller
                return (row, None, DEFERRED, False)
            return (row, None, f"{type(e).__name__}: {e}"[:400], False)

    def read_held(group_rows):
        """What these documents already hold, in ONE read. None makes each
        read its own, as before: slower, never wrong."""
        try:
            return embeddings.held_chunks(collection, [int(r["native_id"]) for r in group_rows])
        except Exception as e:  # noqa: BLE001
            _batch_log(con, "embed", collection,
                       f"one read for {len(group_rows)} documents failed "
                       f"({type(e).__name__}: {e}); each reads its own")
            return None

    # A document only commits when ALL of its chunks are done, so a large one
    # is a long silence — a yeast chromosome is ~200 chunks and DNABERT-2 on
    # CPU answers in ~5s each, which is a quarter of an hour with nothing in
    # the log. Announcing the batch is the difference between "slow" and
    # "stuck", which an operator cannot otherwise tell apart.
    pending_chunks = sum(r["chunks"] or 0 for r in rows)
    db.log_event(con, "info", "embed",
                 f"embedding {len(rows)} document(s)"
                 + (f" / {pending_chunks} chunks" if pending_chunks else "")
                 + (f" with {recipe.concurrency} workers" if slots is None
                    else f", {slots} submitted and 1 prepared at a time"))

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
    first_held = read_held(rows)
    first_cut = prepared(rows)
    queue: list = [(r, first_held, first_cut.get(r["uid"])) for r in rows]
    awaiting: dict[int, str] = {}   # book_id -> uid: embedded, its batch not yet committed
    waiting_since = 0.0
    done = deferred = 0
    wrong_endpoint: list[BaseException] = []   # 404/401/…: the collection's embedding pauses
    timed_out: list[BaseException] = []        # read timeouts: the endpoint held as busy
    answered_here: list[bool] = []             # a document the encoder answered
    endpoint = encoder_timeouts.endpoint_of(recipe)
    place = str(collection.manifest_path)

    from app import validity as validity_mod
    versioned = validity_mod.declared(collection) is not None

    def mark_done(uids) -> None:
        # the rows just written carry the version table's columns: synced
        # (app/validity.py), as stage_embed marks them
        from app import embed_marks
        embed_marks.mark_embedded(con, collection, uids, synced=versioned)

    def record_error(uid, err) -> None:
        missing = origin_missing.path_of(err)
        con.execute(
            "UPDATE documents SET retries=retries+1, error=?, updated_at=? "
            "WHERE uid=?", (f"embed: {origin_missing.message(missing) if missing else err}"[:500],
                            int(time.time()), uid))
        if missing:
            _say_origin_missing(con, collection, "embed", missing)
        db.log_event(con, "error", "embed", err, uid)

    def commit(row, n, err, buffered) -> None:
        nonlocal done, deferred, waiting_since
        if err == "stopped":
            return
        if err == DEFERRED:
            # the encoder was not answering: leave the row exactly as it was
            deferred += 1
            return
        if err is not None:
            record_error(row["uid"], err)
        elif buffered:
            # embedded and not yet written: its batch's commit marks it
            if not awaiting:
                waiting_since = time.time()
            awaiting[int(row["native_id"])] = row["uid"]
        else:
            mark_done([row["uid"]])
            done += 1

    def flush() -> None:
        """ONE Lance commit for every document embedded since the last,
        marked done inside the same manifest transaction."""
        nonlocal done, deferred
        if not awaiting:
            return
        batch = dict(awaiting)
        awaiting.clear()
        docs = sink.take(list(batch))
        uids = [batch[d.book_id] for d in docs]
        try:
            embeddings.write_pending(docs, collection, lock_con=con,
                                     on_written=lambda: mark_done(uids))
            done += len(uids)
            return
        except Exception as e:  # noqa: BLE001
            if transient_failure(e):
                deferred += len(uids)       # nothing marked: embedded again later
                return
        # not the moment's fault: one document at a time, so that only the
        # one at fault is recorded as such
        for doc in docs:
            uid = batch[doc.book_id]
            try:
                embeddings.write_pending([doc], collection, lock_con=con,
                                         on_written=lambda u=uid: mark_done([u]))
                done += 1
            except Exception as e:  # noqa: BLE001
                if transient_failure(e):
                    deferred += 1
                else:
                    record_error(uid, f"{type(e).__name__}: {e}"[:400])

    def settle_empty(group_rows, held) -> list:
        """The candidates the CURRENT chunker cut to nothing (`chunks` = 0,
        `tokens` = 0, written by this `FTS_VERSION`) and that hold no vector:
        marked done here, in one transaction, without a slot, a cut or an
        encoder call — what their trip through a stream ended in anyway
        (`upsert_book` writes nothing, `mark_done`). wikipedia's dump layer:
        15.76 M of the 18.8 M remaining are redirects, ~85 % of every
        stream from rowid 35 M on, 12 ms of cutting each (München,
        2026-10-30). A document that still holds vectors takes the normal
        path, whose delete removes them. Returns the rest."""
        nonlocal done
        if held is None:
            return list(group_rows)
        empty = [r for r in group_rows
                 if not (r["chunks"] or 0) and not (r["tokens"] or 0)
                 and r["fts_version"] == config.FTS_VERSION and not held.of(r["native_id"])]
        if not empty:
            return list(group_rows)
        uids = [r["uid"] for r in empty]
        own = not con.in_transaction
        try:
            if own:
                con.execute("BEGIN IMMEDIATE")
            mark_done(uids)
            if own:
                con.execute("COMMIT")
        except Exception as e:  # noqa: BLE001 — the manifest held: they take the normal path
            if own and con.in_transaction:
                con.execute("ROLLBACK")
            if not transient_failure(e):
                raise
            return list(group_rows)
        done += len(uids)
        gone = set(uids)
        return [r for r in group_rows if r["uid"] not in gone]

    def refill(pool) -> None:
        nonlocal queue
        while len(in_flight) < window and not _stop.is_set() \
                and time.time() < deadline and (keep_going is None or keep_going()):
            if not queue:
                # candidates committed by THIS thread are already out of the
                # selection; the ones in flight or awaiting their batch's
                # commit are not, hence `seen`
                fresh = [r for r in _embed_candidates(con, scope, group, collection)
                         if r["uid"] not in seen]
                if not fresh:
                    return
                seen.update(r["uid"] for r in fresh)
                held = read_held(fresh)
                fresh = settle_empty(fresh, held)
                cut = prepared(fresh)
                queue = [(r, held, cut.get(r["uid"])) for r in fresh]
            row, held, cut = queue.pop(0)
            in_flight[pool.submit(work, row, held, cut)] = row["uid"]
        stats.recorder().set_in_flight(collection.name, len(in_flight))

    # the first group's empty documents, as every refill's (`settle_empty`)
    left = {r["uid"] for r in settle_empty([q[0] for q in queue], first_held)}
    queue = [q for q in queue if q[0]["uid"] in left]
    pin = _pin_begin(collection, recipe)
    try:
        with ThreadPoolExecutor(max_workers=window) as pool:
            refill(pool)
            while in_flight:
                finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in finished:
                    in_flight.pop(fut)
                    commit(*fut.result())
                if len(awaiting) >= group or (
                        awaiting and time.time() - waiting_since >= config.EMBED_COMMIT_S):
                    flush()
                # a dead encoder must not be walked document by document:
                # stop refilling as soon as a deferral shows up
                if deferred == 0:
                    refill(pool)
                stats.recorder().set_in_flight(collection.name, len(in_flight))
        flush()         # what was embedded is written, SIGTERM included
    finally:
        stats.recorder().set_in_flight(collection.name, 0)
        _pin_end(pin)
        with lent_lock:
            for c in lent:
                c.close()
            lent.clear()
    if wrong_endpoint:
        _pause_embedding(con, collection, wrong_endpoint[-1])
    elif done:
        _endpoint_answered(con, collection)
    if timed_out and not answered_here:
        # timed out and answered nobody: a hung backend is sent nothing more
        # for a while (an answer to anyone else releases it)
        _hold_busy(con, collection, endpoint, timed_out[-1])
    if done:
        db.log_event(con, "info", "embed",
                     f"embedded {done} document(s) at concurrency "
                     f"{recipe.concurrency if slots is None else slots}: "
                     f"{tally.embedded} chunks embedded, {tally.reused} reused")
    if deferred:
        db.log_event(con, "warn", "embed",
                     f"{deferred} document(s) deferred — encoder unreachable, busy, "
                     f"timed out or the wrong endpoint, "
                     f"the store held by another writer, an answer from a run not "
                     f"yet validated (03 §3.3), or the node's mode no longer running "
                     f"this; "
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
#: collections whose corpus fetch the node's PAUSE stopped or refused — not
#: SIGTERM, not retirement. The main loop re-arms them on the first round
#: the node is no longer paused (`_rearm_paused_aborts`). Guarded by
#: _acquire_lock.
_paused_aborts: set[str] = set()
_acquire_lock = threading.Lock()


def _background_priority() -> None:
    """Acquisition, archive fetches and discovery yield the CPU to the
    encoder feeders like indexing does (docs/notes/scheduling.md §12) —
    measured 2026-09-19, gutenberg's rsync listing held the GIL parsing
    dates while its encoder sat idle. Threads they spawn inherit it.

    Never the MAIN thread: nice cannot be raised back without
    CAP_SYS_NICE, and a caller that ran one of these inline there (a test,
    a CLI) would leave the whole process's main thread lowered for good."""
    if threading.current_thread() is threading.main_thread():
        return
    if producers_enabled(bool(config.WORKER_PIPELINE_THREADS)):
        _lower_own_priority(config.BACKGROUND_NICE)


def _acquire_worker(collection, spec, profile: str, reason: str) -> None:
    _background_priority()
    con = db.connect(collection)
    fetched_any = False
    stop: _AnyStop | None = None
    try:
        # `_paused` IS PART OF THE STOP, and for ten hours it was not.
        #
        # The main loop sets `_paused` for both `paused` and `seed-only`,
        # and both promise no downloads — docs/07 defines `node pause` as
        # "no work", and seed-only is "downloads and processing stand down
        # exactly like pause". But this stop was SIGTERM-or-removed only,
        # so a fetch already in flight never looked at the state again.
        # A gutenberg fetch is one rsync over the whole mirror, hours long.
        #
        # Measured 2026-09-13: seed-only was set at 22:45 and cleared at
        # 08:50 the next morning, and 204,486 files (160.3 GB) were fetched
        # inside that window — in every one of its ten hours, not a burst
        # at an edge. The operator asked why the node was still
        # downloading while it said "seeding only". It was, the whole time.
        #
        # Stopping here reuses the SIGTERM path end to end, which is why it
        # is safe: rsync's leader is SIGTERMed within `_POLL_SECONDS` and
        # ends itself and its receiver in under a second (its group is
        # SIGKILLed only past a bound), `--partial` leaves a truthful `.part`, the row is
        # recorded by `_upsert_aborted` — no error, no retry burned — and
        # the next run resumes by Range. `acquire-only` does not set
        # `_paused`, so it keeps fetching, which is that mode's whole point.
        stop = _AnyStop(_stop, _gone_event(collection.name), _paused)
        result = acquire_engine.run_acquisition(
            con, collection, spec, profile, stop=stop)
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
    except httpx.TransportError as e:
        # THE ORIGIN DID NOT ANSWER — a timeout, a reset, a refused
        # connection: the origin's state, not the collection's (CLAUDE.md:
        # deferral is not failure). What landed is kept and checkpointed; the
        # next discovery starts the fetch again, which is the backoff — a
        # retry every round would hammer an origin that is down. Logged as an
        # ERROR until 2026-10-13 (normattiva: one ReadTimeout).
        db.log_event(con, "warn", "acquire",
                     f"corpus fetch ({reason}) deferred — the origin did not answer "
                     f"({type(e).__name__}: {e}); the next discovery retries it"[:400])
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
            # A FETCH THE PAUSE STOPPED MUST BE RESTARTED BY THE RESUME.
            #
            # Marked HERE, in the same critical section that reports the
            # fetch done — never earlier. The second review reproduced why:
            # marked in the try body, the name was visible while this thread
            # still had a closing log write to make (busy_timeout 60 s); a
            # resume inside that window consumed the re-arm, `_acquire` saw
            # this thread still alive and returned silently, and then this
            # `finally` reported done with the name no longer pending — so
            # the next round queued `acquire=False` and the 360-minute gap
            # came straight back. Now "done" and "stopped by the pause" are
            # published together or not at all, and `_rearm_paused_aborts`
            # also waits for the thread to be gone.
            #
            # From `stop.fired`, which records WHICH event stopped it — not
            # from `_paused` now, since the operator may already have
            # resumed while rsync's terminate grace ran.
            if stop is not None and stop.fired is _paused and not _stop.is_set():
                _paused_aborts.add(collection.name)


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
    if _paused.is_set():
        # Don't START a fetch the stop would abort in its first second —
        # mirrors `_acquire_archive`. But REMEMBER it. The first version
        # returned here recording nothing, on the belief that "the next
        # discovery after resume calls here again". It does not: that
        # discovery is DISCOVER_INTERVAL_MIN away (360 min). Checked after
        # the spec and profile gates, so only a collection that WOULD fetch
        # is re-armed; one that turns out to need nothing costs a single
        # no-op fetch, which requests no rescan and so cannot loop.
        with _acquire_lock:
            _paused_aborts.add(collection.name)
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
    _background_priority()
    con = db.connect(collection)
    try:
        state = _archive_log_state.setdefault(collection.name, {})
        # The same omission as the corpus fetch above, and the same fix: a
        # genesis archive shard is a multi-GB download (NCBI's shard 8 was
        # 3.05 GB), and `_acquire_archive` checked `_paused` only before
        # STARTING one — so a shard already downloading ignored a pause.
        result = bundle_genesis_fetch.run_fetcher(
            con, collection,
            stop=_AnyStop(_stop, _gone_event(collection.name), _paused),
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
_vlight_last: dict[str, float] = {}     # lance dir -> last light-compaction check


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
        return _light_compact(con, collection, now)
    if not _maintenance_allowed():
        return False        # not stamped: due again as soon as the pause lifts
    _vindex_last[key] = now
    _vlight_last[key] = now  # the pass runs the light compaction first
    try:
        out = vector_index.ensure(collection, con, may_continue=_maintenance_allowed)
    except Exception as e:   # noqa: BLE001 — an index is an optimisation, never a crash
        db.log_event(con, "warn", "index",
                     f"vector index maintenance deferred: {type(e).__name__}: {e}"[:300])
        return False
    return bool(out)


def _light_compact(con, collection, now: float) -> bool:
    """Between the index's passes, the light compaction on its own cadence
    (`vector_index.light`, every VECTOR_INDEX_LIGHT_CHECK_S): the small
    fragments come back at ~65 a minute on wikipedia and every read and
    write pays for each. True when it compacted."""
    from . import vector_index
    key = str(collection.lance_dir)
    if now - _vlight_last.get(key, 0.0) < config.VECTOR_INDEX_LIGHT_CHECK_S:
        return False
    if not _maintenance_allowed():
        return False
    _vlight_last[key] = now
    try:
        out = vector_index.light(collection, con)
    except Exception as e:   # noqa: BLE001 — an optimisation, never a crash
        db.log_event(con, "warn", "index",
                     f"light compaction deferred: {type(e).__name__}: {e}"[:300])
        return False
    return bool(out and out.get("compacted"))


def _maintain_catalog(con, collection) -> bool:
    """The catalog index's backfill (app/catalog.py): on the maintainer's
    turn and at its priority, one rowid window at a time for at most
    CATALOG_BACKFILL_BUDGET_S. Nothing to do once the collection's catalog
    is complete — the rdf stage keeps it current from then on. Returns True
    when it covered anything."""
    if not _maintenance_allowed():
        return False
    try:
        out = catalog.backfill(con, collection, may_continue=_maintenance_allowed)
    except Exception as e:   # noqa: BLE001 — an index is an optimisation, never a crash
        db.log_event(con, "warn", "index",
                     f"catalog index backfill deferred: {type(e).__name__}: {e}"[:300])
        return False
    if not out or not out["windows"]:
        return False
    if out["complete"]:
        db.log_event(con, "info", "index",
                     f"catalog index complete ({out['cursor']:,} rowids); "
                     f"catalog searches use it from now on")
    else:
        db.log_event(con, "info", "index",
                     f"catalog index: {out['written']:,} documents in {out['windows']} "
                     f"window(s) ({out['secs']}s), rowid {out['cursor']:,} of {out['top']:,}")
    return True


def _vector_writes_allowed(collection, con=None) -> bool:
    """Whether this collection's vector writers may run.

    NOT BELOW THE DISK FLOOR (operator, 2026-10-18; app/disk_guard.py): a
    vector store whose filesystem is short of space takes no more rows from
    any writer — a deferral, said in the event log, never an error on a
    document.

    For a BUILD or a REFRESH they always may, and that is deliberate
    (2026-08-28). This used to answer "not while the index maintainer is
    working", and with a 41-minute pass reached every few minutes of work
    the collection stood down for most of its life. Lance is MVCC — 210
    concurrent delete+add writes measured during `optimize()`, zero errors
    — and a rare commit conflict surfaces as an exception that
    `vector_store_contended` turns into a deferral: the document is
    untouched and retried, never errored.

    A COMPACTION is the one exception (2026-09-18). It rewrites fragments,
    so a writer committing underneath takes its commit away rather than
    merely racing it: three of wikipedia's passes died that way in two
    days, 40–51 minutes of work each, against an archive import committing
    about twice a second. It is also rare by construction — only past
    `VECTOR_INDEX_COMPACT_FRAGMENTS` small fragments — so waiting for one
    is affordable where waiting for every refresh was not.
    """
    from . import disk_guard, vector_index
    if vector_index.rewriting(collection):
        return False
    short = disk_guard.vectors_short(collection)
    if short:
        disk_guard.signal(con, collection, short)
        return False
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

# The maintainer's CPU yields to the pipeline's, as the parse pool's does
# (nice 10). Measured 2026-09-18: a wikipedia index refresh ran on
# pylance's runtime threads at 12.2 of the node's 12 cores, at the SAME
# priority as the pipeline. The pipeline was not starved that time — its
# threads wanted about one core and got it, and pubmed's rate did not move
# once this was in — but a background job had been competing with it as an
# equal, and any stage that wanted more would have lost. Nice is per
# THREAD on Linux and inherited by the threads a thread spawns, so the
# maintainer lowers itself before its first Lance call and pylance's
# runtime, which that call creates, starts at the same level. lancedb's own runtime (the embed writes) is created
# at boot by refuse_boot on the main thread and stays at 0 — which is also
# why a first index BUILD, run there by lancedb, is not covered.
INDEX_MAINTAINER_NICE = 10


def _lower_own_priority(offset: int) -> int | None:
    """Put the CALLING thread at the process's base nice + `offset`, never
    lower than that; the new value, or None where the platform does not
    allow it (then it runs unniced).

    A TARGET, not a step. Nice is inherited by the threads a thread starts,
    so a relative step compounds: live 2026-09-20, the fetch started from a
    discovery started from a (lowered) pipeline thread sat at nice 15 —
    below the parse pool, which scheduling.md §10 A1 forbids ("fetching:
    never starve it of CPU"). The base is the main thread's (its tid is the
    pid)."""
    try:
        tid = threading.get_native_id()
        target = min(19, os.getpriority(os.PRIO_PROCESS, os.getpid()) + offset)
        if os.getpriority(os.PRIO_PROCESS, tid) < target:
            os.setpriority(os.PRIO_PROCESS, tid, target)
        return os.getpriority(os.PRIO_PROCESS, tid)
    except (AttributeError, OSError):
        return None


def _maintenance_allowed() -> bool:
    """Index maintenance is CPU work like any stage's, and a pause stops
    it — asked before EACH collection and between the steps of
    `vector_index.ensure`, not once per cycle. Live 2026-09-15: asked only
    at the top of the cycle, a node the operator had paused ran pubmed's
    optimize for 485 s and went straight on to the next collection's —
    twelve Lance threads, ~9 cores. A Lance call already running cannot be
    interrupted; it finishes, and nothing new starts after it."""
    # INDEX, by the one table of what a mode runs (app/node_state.py): it
    # stands down in "download/seeding only" too, which it did not before
    # 2026-09-19 — twelve Lance threads are not downloading or seeding
    return (not _stop.is_set()
            and node_state.runs(node_state.worker_state(), node_state.INDEX))


_ask_indexed: dict[str, tuple] = {}


def _fill_content_hashes(con, collection) -> int:
    """09 §9.5's `content_hash` onto the manifest's rows that have none,
    from the current snapshot's records — once per snapshot (the caller's).
    Only `hold records build` wrote the column, so a peer that FETCHED its
    SHARD never had it: NULL on all 79,659 of Perugia's gutenberg rows
    (2026-10-23), and the freshness check, verify-on-use and the embed
    stage's possession lookup each read it. Never overwrites a value: a
    records build writes the column as its shards close, ahead of the
    snapshot that will carry them."""
    from app.share import possession
    root = collections_mod.louise_root() / "collections"
    # the rows first: a manifest with no row missing its hash reads nothing
    if con.execute("SELECT 1 FROM documents WHERE content_hash IS NULL LIMIT 1").fetchone() is None:
        return 0
    snap = possession.current_snapshot(root, collection.name)
    if snap is None:
        return 0
    # PART BY PART: one part's (uid, hash) list at a time, one transaction
    # each. The whole snapshot's hashes in one dict, with a list of every
    # NULL uid beside it, is 40 M entries on pubmed and 75.9 M on wikidata
    # (the same dict four embed threads built at once took the worker to
    # 46 GB and the OOM killer, 2026-10-30)
    filled = 0
    for s_ in snap.shards:
        pairs = [(h, u) for u, h in possession.part_records(root, collection.name, snap, s_) if h]
        if not pairs:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            before = con.total_changes
            con.executemany("UPDATE documents SET content_hash=? WHERE uid=? AND content_hash IS NULL", pairs)
            filled += con.total_changes - before
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    if not filled:
        return 0
    left = con.execute("SELECT COUNT(*) FROM documents WHERE content_hash IS NULL").fetchone()[0]
    db.log_event(con, "info", "ask", f"content_hash filled on {filled} document(s) from the "
                                     f"current snapshot's records ({left} not in it)")
    return filled


def _maintain_ask_indexes(con, collection) -> bool:
    """05 §5.7's facet values and §5.5's edges, derived and local, rebuilt
    when the collection's current snapshot changes (app/ask/facets.py,
    app/ask/traverse.py). One pass per snapshot: the facets' pass over the
    manifest's columns and the edges' over the relation shards held."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return False
    from .ask import facets as ask_facets, traverse as ask_traverse
    from .hold import collection as col
    from .share import seeder
    root = collections_mod.louise_root() / "collections"
    shard_tree = col.shard_dir_of(root, collection.name)
    snap = seeder.current_snapshot(shard_tree)
    if snap is None:
        return False
    key = (snap.snapshot_id, snap.hash)
    if _ask_indexed.get(collection.name) == key:
        return False
    index_dir = root / collection.name / "index"
    if ask_facets.read_marker(index_dir) == key:
        # indexed whole by an earlier process for this very snapshot: a
        # restart does not recount (the marker is written below only after a
        # pass that raised nothing)
        _ask_indexed[collection.name] = key
        return False
    t0 = time.monotonic()
    did = False
    failed = False
    try:
        did = _fill_content_hashes(con, collection) or did
    except Exception as e:   # noqa: BLE001 — one fill, not the maintainer
        failed = True
        db.log_event(con, "warn", "ask", f"content hashes: {type(e).__name__}: {e}"[:300])
    try:
        got = ask_traverse.index_edges(con, shard_tree, snap)
        did = did or bool(got["shards"])
    except Exception as e:   # noqa: BLE001 — one index, not the maintainer
        failed = True
        db.log_event(con, "warn", "ask", f"edges: {type(e).__name__}: {e}"[:300])
    try:
        # bounded whatever the collection's size (app/ask/facets.py): the
        # 2026-10-07 pass over pubmed reached 34 GB and the kernel killed the
        # worker; a field too wide to count says so, and stops with the node
        built = ask_facets.build(con, codex, index_dir, snapshot_id=snap.snapshot_id,
                                 stop=_stop.is_set, shard_dir=shard_tree, snapshot=snap,
                                 log=lambda m: db.log_event(con, "warn", "ask", m[:400]))
        did = did or bool(built)
        try:
            # app/validity.py: the vector table's validity columns of rows the
            # embed stage did not rewrite (embed off, an import-only copy)
            from app import validity
            synced = validity.sync_vectors(con, collection, lock_con=con)
            did = did or bool(synced)
        except Exception as e:   # noqa: BLE001 — one index, not the maintainer
            failed = True
            db.log_event(con, "warn", "ask", f"validity columns: {type(e).__name__}: {e}"[:300])
        if built:
            db.log_event(con, "info", "ask", f"facets counted for snapshot {snap.snapshot_id}: {', '.join(built)} "
                                             f"({time.monotonic() - t0:.0f}s)")
    except Exception as e:   # noqa: BLE001
        failed = True
        db.log_event(con, "warn", "ask", f"facets: {type(e).__name__}: {e}"[:300])
    _ask_indexed[collection.name] = key
    if not failed and not _stop.is_set():
        try:
            ask_facets.write_marker(index_dir, snap.snapshot_id, snap.hash)
        except OSError as e:
            db.log_event(con, "warn", "ask", f"snapshot marker not written: {e}"[:300])
    return did


def _refresh_anchor(con, collection) -> bool:
    """03 §3.7: a certified collection's anchor document fetched every 24
    hours with jitter (app/hold/anchor.py `due`), from the maintainer's
    turn, in a mode that fetches. Unreachable changes nothing but the
    note. Whether a fetch was made."""
    codex = getattr(collection, "codex", None)
    if codex is None or getattr(codex, "publication", "open") != "certified":
        return False
    if not node_state.runs(node_state.worker_state(), node_state.FETCH):
        return False
    from .hold import anchor as anchor_mod
    root = collections_mod.louise_root() / "collections"
    now = time.time()
    try:
        if not anchor_mod.due(root, collection.name, codex, now=now):
            return False
        before = anchor_mod.status(root, collection.name, codex, now=now)["state"]
        st = anchor_mod.refresh(root, collection.name, codex, now=now, stop=_stop,
                                log=lambda m: db.log_event(con, "warn", "anchor", m[:300]))
    except Exception as e:   # noqa: BLE001 — one fetch, not the maintainer
        db.log_event(con, "warn", "anchor", f"{type(e).__name__}: {e}"[:300])
        return False
    if st["state"] != before:
        db.log_event(con, "error" if st["state"] == anchor_mod.MISMATCH else "info", "anchor",
                     f"anchor {st['state']}: {st['why']}")
    return True


T4_BATCH = 32          # cited chunks re-embedded per maintainer turn: a few seconds of encoder


def _verify_cited(con, collection) -> bool:
    """03 §3.5 T4, 05 §5.3: the imported chunks searches cited, re-embedded
    once with this peer's encoder and marked — one bounded pass per
    maintainer turn, asynchronous to every search. The encoder's work, so
    only in a mode that embeds (app/node_state.py). A reproduced miss is a
    proof of forgery: written, published, and the key burned
    (`_forgery_found`). Whether anything was checked."""
    if not node_state.runs(node_state.worker_state(), node_state.EMBED):
        return False
    from .bundles import verify_on_use
    try:
        if not verify_on_use.queued(con, 1):
            return False
        out = verify_on_use.drain(con, collection, limit=T4_BATCH,
                                  on_forgery=lambda f: _forgery_found(con, collection, f))
    except Exception as e:   # noqa: BLE001 — one pass, not the maintainer
        db.log_event(con, "warn", "verify", f"T4: {type(e).__name__}: {e}"[:300])
        return False
    if out.get("unavailable"):
        return False                      # the encoder did not answer: deferral, not failure
    if out["checked"]:
        db.log_event(con, "info", "verify",
                     f"T4: {out['checked']} cited chunks re-embedded — {out['verified']} verified, "
                     f"{out['failed']} failed, {out.get('inconclusive', 0)} inconclusive; "
                     f"{out['queued']} still queued")
    return bool(out["checked"])


def _forgery_found(con, collection, f) -> None:
    """A T4 miss that reproduced (03 §3.5): the proof written at 09 §9.8's
    path and published when this peer can sign and knows its image, and
    the producer's key burned in the trust file either way."""
    from contextlib import closing
    from .hold import collection as col, equivalence, forgery, trust_file
    from .share import attest, seeder
    from .bundles import verify_on_use
    root = collections_mod.louise_root()
    # the ledger keeps the bundle header's bare hex; the proof, the burn and
    # the trust level speak 09 §9.2's `ed25519:<hex>` — one form, or a burn
    # would never shut the key out of counting
    producer = verify_on_use.key_text(f.producer) or "unknown"
    aid = None
    why = ""
    codex = getattr(collection, "codex", None)
    if codex is None:
        why = "a collection without a Codex attests nothing"
    else:
        try:
            from . import identity
            from .bundles import keys as node_keys
            snap = seeder.current_snapshot(col.shard_dir_of(root / "collections", collection.name))
            image = equivalence.running_image()
            key = node_keys.load_signing_key()
            own = identity.own()
            if snap is None:
                why = "no current snapshot"
            elif image is None:
                why = "this process does not know its image (LOUISE_PLATFORM_IMAGE)"
            else:
                row = con.execute("SELECT content_hash FROM documents WHERE uid=?", (f.uid,)).fetchone()
                now = time.time()
                aid, _ = forgery.prove(root / "collections", collection.name, codex, snap, key,
                                       platform_image=image, now=now, chunk_id=f.chunk_id,
                                       producer=producer, bundle=f.bundle_id, text=f.text,
                                       claimed=f.claimed, recomputed=f.recomputed,
                                       content_hash=row["content_hash"] if row else None)
                with closing(node_state.connect()) as node_con:
                    attest.publish(root / "collections", collection.name, codex, [aid], key=key,
                                   own_key=own, con=node_con, now=now)
        except Exception as e:   # noqa: BLE001 — the burn below stands whether or not the proof travels
            why = f"{type(e).__name__}: {e}"[:200]
    trust_file.burn(producer, aid, root=root)
    db.log_event(con, "error", "verify",
                 f"T4: chunk {f.chunk_id} re-embedded twice at cosine {f.cosine:.4f}: forgery by {producer}; "
                 f"key burned" + (f", proof {aid}" if aid else f", not attested ({why})"))


def _index_maintainer() -> None:
    """One low-priority thread for every collection's vector index. Its
    own connections (SQLite is thread-affine); stands down while the node
    is paused (`_maintenance_allowed`); one collection at a time, so two
    hour-long builds never run together. Low priority in fact, not only in
    name: see INDEX_MAINTAINER_NICE."""
    _lower_own_priority(INDEX_MAINTAINER_NICE)
    cons: dict[str, object] = {}
    while not _stop.wait(INDEX_MAINTAINER_INTERVAL_S):
        try:
            # A PAUSED COLLECTION IS ONE THE NODE IS NOT RUNNING (07 §7.7):
            # its derived indexes wait with it. 2026-10-18: pubmed-v8, paused
            # for its carry, got facets written into its index/ and the carry
            # refused it as not empty
            paused = node_state.paused_collections()
            collections = [c for c in collections_mod.load_all() if c.name not in paused]
            if not collections:
                continue
            if not _maintenance_allowed():
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
                if not _maintenance_allowed():
                    break                   # paused mid-cycle: nothing after this starts
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
                did = _maintain_catalog(cons[key], c) or did
                did = _maintain_ask_indexes(cons[key], c) or did
                did = _verify_cited(cons[key], c) or did
                did = _refresh_anchor(cons[key], c) or did
                did = _heal_origin_missing(cons[key], c) or did
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
    if not _vector_writes_allowed(collection, con):
        return False            # the index maintainer holds the table, or the disk is short
    if not _license_allows(con, collection, "archive import"):
        return False            # vectors are USE of the corpus they describe
    _acquire_archive(con, collection, "round")
    # AFTER the fetch, before the apply: what a copy in quarantine keeps doing
    # is "the fetch its operator asked for" — it is what makes it testable —
    # and what waits is the import (03 §3.6)
    if not _quarantine_allows(con, collection, "archive import"):
        return False
    # THE MODE IS ONE OF A PASS'S STOPS (operator, 2026-09-19: "pause: nothing
    # is running"). A pass asks its stop at every row and was only ever given
    # SIGTERM or its collection's removal, so one in flight when the node was
    # paused ran on to the end of its budget — up to 300 s. It ends at its
    # next row now: what is flushed is committed, the rest is partial and
    # resumes, exactly as under SIGTERM.
    stop = _AnyStop(stop, _parse_held) if stop is not None else _AnyStop(_parse_held)
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
        paused_off = set(_paused_aborts)
    # A fetch the PAUSE stopped is not a finished fetch. Its post-fetch
    # rescan would be `acquire=False` — and `_request_discovery` is
    # first-reason-wins, so no later acquire=True could replace it — and it
    # would stamp the discovery clock. The fetch would then not come back for
    # DISCOVER_INTERVAL_MIN. Left to `_rearm_paused_aborts` instead, whose
    # acquire=True discovery picks up what landed AND restarts the fetch.
    for name in paused_off:
        done.pop(name, None)
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
            # invariant 9, "not discovered": the fetch that raised this flag
            # passed the gate when it STARTED; the terms can change before
            # it ends (audit, 2026-09-13)
            if not _license_allows(cons[collection.name], collection, "corpus scan"):
                continue
            if _parse_held.is_set():
                # download/seeding only: what landed is scanned when
                # everything runs again (app/node_state.py: MODE_RUNS)
                _owe_scan(collection.name)
                continue
            db.log_event(cons[collection.name], "info", "discover",
                         "background fetch finished — scanning")
            stats = discovery.sync_collection(cons[collection.name], collection)
            if stats.get("added") or stats.get("changed"):
                db.log_event(cons[collection.name], "info", "discover",
                             f"picked up {stats.get('added', 0)} new, "
                             f"{stats.get('changed', 0)} changed books")
            last_discover[collection.name] = time.time()


def _rearm_paused_aborts(all_collections, last_discover, pipeline: bool,
                         cons=None) -> list[str]:
    """Restart the corpus fetches the node's pause stopped or refused.
    Returns the names re-armed.

    Called on EVERY round the node is not paused, not only on the
    transition: a fetch can finish unwinding after the resume and land its
    name here a round late, and a transition-only hook would miss it for
    good.

    A NAME WHOSE OLD FETCH THREAD IS STILL ALIVE STAYS PENDING for the next
    round. Re-arming it now would reach `_acquire`, which refuses to start
    a second fetch beside a live one and returns silently — the race the
    second review reproduced, which lost the restart entirely.

    Only collections still live are re-armed — one retired during the pause
    is dropped here, not kept forever. A stale queued request is taken first
    because discovery requests are first-reason-wins: an `acquire=False`
    left in the queue would silently beat the `acquire=True` this needs.

    Without pipeline threads there are no requests, so this runs `_discover`
    itself. The first version only cleared the interval stamp and left the
    interval check to do it — and with DISCOVER_INTERVAL_MIN=0 ("startup
    only") that check never runs, so the fetch was lost until a restart.
    """
    with _acquire_lock:
        if not _paused_aborts:
            # THE IDLE ROUND COSTS NOTHING. This runs every round the node
            # is not paused and almost always has nothing to do; it must not
            # walk the collection list to find that out (CLAUDE.md: a
            # per-round cost scales with the work available, never with the
            # size of a list). Third review, 2026-09-13.
            return []
        pending = set(_paused_aborts)
        live = {c.name: c for c in all_collections}
        still_running = set()
        for name in pending:
            thread = _acquire_threads.get(name)
            if thread is not None and thread.is_alive():
                still_running.add(name)
        _paused_aborts.clear()
        _paused_aborts.update(still_running)
        rearmed = sorted((pending - still_running) & live.keys())
        # Forget "done" for what this re-arms, under the SAME lock. Third
        # review, 2026-09-13: `_finish_completed_acquires` and this function
        # read the mark at different moments of one round, so a fetch whose
        # `finally` landed between them was re-armed here AND left
        # `_acquire_done` set — and the next round's `_finish` treated it as
        # a normal completion: an acquire=False rescan in pipeline mode, a
        # second inline full scan otherwise (1,083 s at wikivec scale).
        # Nothing was lost — the acquire=True request queued first wins —
        # but the work was paid twice. The discovery armed here scans
        # whatever the stopped fetch landed, so the flag has nothing left to
        # ask for.
        for name in rearmed:
            _acquire_done.pop(name, None)
    failed: list[str] = []
    for name in rearmed:
        _take_discovery_request(name)
        # EACH NAME ON ITS OWN. Third review, 2026-09-13, reproduced: every
        # pending name was cleared above, then `_discover` ran for each with
        # no guard — so one scan raising (a corpus-mount OSError, a non-lock
        # sqlite error, a busy `apply_scope`) left this function and aborted
        # the round, and every name after it was already cleared, never
        # discovered, its stamp still fresh: back in 360 minutes, or never
        # with DISCOVER_INTERVAL_MIN=0. A regression against the version
        # before, which only popped stamps and so retried by itself. A name
        # that fails is kept for the next round instead.
        try:
            if pipeline:
                _request_discovery(name, "resume after pause", acquire=True)
                last_discover[name] = time.time()
            elif cons is not None and name in cons:
                _discover(cons[name], live[name], "resume after pause")
                last_discover[name] = time.time()
            else:
                last_discover.pop(name, None)
        except Exception as e:   # noqa: BLE001 — one collection's scan, not the round
            failed.append(name)
            try:
                db.log_event(cons[name], "warn", "acquire",
                             f"resume after pause: discovery failed, retrying next "
                             f"round: {type(e).__name__}: {e}"[:400])
            except Exception:   # noqa: BLE001 — the log is a courtesy
                pass
    if failed:
        with _acquire_lock:
            _paused_aborts.update(failed)
    return [n for n in rearmed if n not in failed]

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
    # INVARIANT 9, "not imported": a bundle is vectors OF the corpus, use
    # like any other. Every other way data arrives asked the gate; this one
    # did not, and a bundle landed in an unacknowledged collection
    # (verification pass, 2026-09-13, reproduced). Left in the inbox, so it
    # imports once the terms are acknowledged.
    if not _license_allows(con, collection, "bundle import"):
        return 0
    if not _quarantine_allows(con, collection, "bundle import"):
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
    # A SCAN IS PARSING, and "download/seeding only" parses nothing
    # (operator, 2026-09-19; app/node_state.py: MODE_RUNS). It is owed, not
    # dropped: what landed meanwhile is scanned when everything runs again.
    if _parse_held.is_set():
        _owe_scan(collection.name)
        return
    db.log_event(con, "info", "discover", f"corpus scan ({reason})")
    held = _AnyStop(stop, _parse_held) if stop is not None else _AnyStop(_parse_held)
    stats = discovery.sync_collection(con, collection, stop=held)
    if not held.is_set():
        # WHAT THE NODE HOLDS IS WHAT IT PROCESSES (app/slice_scope.py): a
        # collection whose one origin holds every slice is narrowed to the
        # slices its file names, before its stages select anything
        out = slice_scope.apply(con, collection, stop=held,
                                log=lambda m, _con=con: db.log_event(_con, "info", "discover", m))
        if out.get("deferred"):
            db.log_event(con, "info", "discover", f"slice scope waits: {out['deferred'][:400]}")
    if held.fired is _parse_held:
        # the mode changed under a running scan: it ended at its next ref,
        # its progress committed and its cursor that far — and the rest is owed
        _owe_scan(collection.name)
        db.log_event(con, "info", "discover",
                     "corpus scan stopped: the node's mode runs no parsing now; "
                     "it goes on from here when everything runs again")
    if stats.get("added") or stats.get("changed"):
        db.log_event(con, "info", "discover",
                     f"picked up {stats.get('added', 0)} new, "
                     f"{stats.get('changed', 0)} changed books")


_license_nagged: dict[str, float] = {}
#: last "collection is not served" reason per collection, so a broken TOML
#: is reported once and not every five seconds — see _sync_collections
_bad_toml_nagged: dict[str, str] = {}

#: last "no profile declared" warning per collection — see _acquire
_profile_nagged: dict[str, float] = {}

#: (collection, what) -> when it was last said: a waiting thread says why
#: once an hour, not every idle beat
_said: dict[tuple[str, str], float] = {}


def _say_once(con, name: str, what: str, message: str, every_s: float = 3600.0) -> None:
    now = time.time()
    if now - _said.get((name, what), 0.0) > every_s:
        _said[(name, what)] = now
        db.log_event(con, "info", "worker", message)


#: the collection's manifest PATH -> (when it was last said, what was said).
#: Keyed on the path, never the name (CLAUDE.md): two collections can share a
#: name across roots, and one's silence must not swallow the other's reason.
_quarantine_nagged: dict[str, tuple[float, str]] = {}


def _quarantine_allows(con, collection, use: str) -> bool:
    """03 §3.6: for a copy in quarantine nothing is embedded, contributed,
    imported or certified — what goes on is what makes it testable: its
    fetch, its tests, its gate. ONE derivation (`app/copy_state.py`), the
    same the search and `hold collection show` read.

    WHILE THE NODE ONLY REPORTS (`platform.toml`: `[node] quarantine`, the
    first of step 14's two deploys) this allows everything and says what it
    WOULD withhold, so the operator sees the day enforcement would bite
    before it does. Logged when the state changes, and hourly. Never raises:
    a state that cannot be derived is reported and withholds nothing here —
    the licence gate has already failed closed on its own."""
    from . import copy_state
    try:
        state = copy_state.of(collection)
    except Exception as e:   # noqa: BLE001
        state, why = None, f"its state cannot be derived ({type(e).__name__}: {e})"
    else:
        if state.in_use:
            _quarantine_nagged.pop(str(collection.manifest_path), None)
            return True
        why = f"{state.state}: " + "; ".join(state.reasons)
    enforcing = copy_state.enforced()
    now = time.time()
    place = str(collection.manifest_path)
    last = _quarantine_nagged.get(place)
    if last is None or last[1] != why or now - last[0] > 3600:
        _quarantine_nagged[place] = (now, why)
        db.log_event(con, "warn", "control",
                     (f"{use} withheld — {why}" if enforcing else
                      f"REPORT ONLY, nothing is withheld — this copy is not in use yet "
                      f"and {use} would wait — {why}")[:900])
    return not enforcing


def _embedding_allowed(con, collection) -> bool:
    """THE GATE FIRST, QUARANTINE SECOND. The gate is one of the tests that
    clear a quarantine (03 §3.6, condition 3), so it must run for a copy
    that is in one; asking in the other order would leave an untested run
    untested for ever."""
    if _integrity_halts(con, collection):
        return False
    if _endpoint_pause_holds(con, collection):
        return False        # a wrong endpoint (404, 401, …): asked again on a bound
    if _endpoint_busy_holds(collection):
        return False        # it timed out answering nobody: asked again on a bound
    if not backend_gate.embed_allowed(con, collection, stop=_stop.is_set):
        return False
    return _quarantine_allows(con, collection, "embedding")


#: collection path -> the violations last said, so the halt is logged on change
_integrity_said: dict[str, tuple] = {}


def _integrity_halts(con, collection) -> bool:
    """03 §3.8: a certified manifest-only copy holding a shard no signed
    snapshot lists stops embedding for the whole collection — vectors for
    chunks outside the snapshot would poison everyone's coverage map — until
    the shard is gone or replaced (app/hold/integrity.py)."""
    from .hold import integrity
    codex = getattr(collection, "codex", None)
    if not integrity.applies(codex):
        return False
    shard_tree = Path(collection.corpus_dir) if getattr(collection, "source", "origin") == "shard" else \
        collections_mod.louise_root() / "collections" / collection.name / "shard"
    found = integrity.violations(shard_tree.parent.parent, shard_tree.parent.name, codex)
    key = str(collection.manifest_path)
    if _integrity_said.get(key, ()) != found:          # first sight of a clean copy: nothing to say
        _integrity_said[key] = found
        if found:
            db.log_event(con, "error", "integrity",
                         f"INTEGRITY VIOLATED: {len(found)} record shard(s) no certifier-signed snapshot "
                         f"lists ({found[0]}…): embedding stops, no bundle is published, search goes on "
                         f"over the certified rest (03 §3.8); `share fetch` replaces them"[:500])
        else:
            db.log_event(con, "info", "integrity", "integrity restored: every record shard is signed")
    return bool(found)


def _settle_the_run(con, collection) -> None:
    """A version-3 copy whose encoder's run is not yet tested is gated AT
    ONCE (03 §3.3), whether or not it has anything to embed: a collection
    that arrived whole from its peers has no embed work to reach the gate
    through, and would wait in quarantine for ever. Costs a dictionary
    lookup a round — `embed_allowed` caches its verdict."""
    from . import copy_state, run_check
    if not run_check.applies(collection):
        return
    try:
        gate = copy_state.of(collection).test("gate")
    except Exception:   # noqa: BLE001
        return
    if gate is not None and gate.status == copy_state.OPEN:
        backend_gate.embed_allowed(con, collection, stop=_stop.is_set)


def _license_allows(con, collection, use: str) -> bool:
    """Invariant 9 / amendment 36, for EVERY way data arrives: a yellow-
    or red-class collection without a valid acknowledgement of its terms
    (the origin's, or the pack's byte-exact copy) is not scanned and its
    archive is not applied. Logged once an hour per collection."""
    from . import licensing
    try:
        if licensing.state_for(collection) != licensing.MISSING:
            return True
        why = (f"license_class={licensing.license_class_for(collection)} needs an "
               f"acknowledgement of the terms")
    except Exception as e:   # noqa: BLE001 — FAIL CLOSED, as search and health do
        # this raised into the round, while search withheld the collection
        # and health called it fine: three answers to one fault
        # (verification pass, 2026-09-13)
        why = f"its license state cannot be judged ({type(e).__name__}: {e})"
    now = time.time()
    if now - _license_nagged.get(collection.name, 0.0) > 3600:
        _license_nagged[collection.name] = now
        db.log_event(con, "warn", "control",
                     f"{use} withheld: {why} — a hand-populated or packed folder "
                     f"is not a bypass (invariant 9)")
    return False


def _discover(con, collection, reason: str, stop=None) -> None:
    """Start what is to be fetched, then scan what is there. The two halves
    answer to different activities of the node's mode (app/node_state.py):
    the fetch runs wherever FETCH does, the scan only where PARSE does —
    `_scan_only` holds that gate, so there is one."""
    _acquire(con, collection, reason)
    _acquire_archive(con, collection, reason)
    _scan_only(con, collection, reason, stop=stop)


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
    on SIGTERM and when its collection is removed mid-run, and a DOWNLOAD
    must also stop when the node is paused or seed-only (`_paused`)."""
    def __init__(self, *events: threading.Event) -> None:
        self._events = events
        #: the event that made is_set() return True, once one has — so a
        #: caller can tell WHY its work stopped after the fact. A pause
        #: lifted while the fetch was still unwinding (rsync's 10 s
        #: terminate grace) must still read as "the pause stopped this", or
        #: nothing re-arms it (`_paused_aborts`). Written from several
        #: threads; every writer stores an event that was genuinely set at
        #: the time, so the race is benign.
        self.fired: threading.Event | None = None

    def is_set(self) -> bool:
        for e in self._events:
            if e.is_set():
                self.fired = e
                return True
        return False


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
    _background_priority()
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


#: HTTP answers that mean "not now", never "not this document": a server
#: still loading its weights (llama.cpp answers 503 "Loading model" until
#: they are in — 41 pubmed documents were blocked by it across two app
#: restarts on 2026-09-15), one overloaded (429), or a proxy that lost it
#: (502/504). A 500 is NOT here: it can be the document's own failure.
_ENDPOINT_BUSY_STATUS = frozenset({429, 502, 503, 504})


def _http_status(exc: BaseException | None) -> int | None:
    """The HTTP status behind an exception, however deep: the embed client
    wraps the server's answer in a RuntimeError (`raise … from e`)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code
        exc = exc.__cause__ or exc.__context__
    return None


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
    from .bundles.writer_lock import VectorWriteBusy
    # The vector write lock IS the manifest's lock (`BEGIN IMMEDIATE`),
    # timed out. Its class was recognised nowhere, so on 2026-09-15 it was
    # written as an error onto four pubmed documents at a deploy's restart.
    return isinstance(exc, VectorWriteBusy) or (
        isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower())


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
    # a connection dropped mid-answer, or an answer that is not HTTP: the
    # endpoint's state (amended 2026-10-09). A bare READ TIMEOUT stays out —
    # an oversized document can genuinely cause one; the embed client's own
    # (`EndpointTimedOut`, a ConnectionError below) is a deferral, and only
    # `encoder_timeouts`' evidence ever puts one on a document.
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.PoolTimeout)):
        return True
    # the embed client's own verdict on an HTTP answer (embeddings.py):
    # EndpointBusy and EndpointMisconfigured are ConnectionErrors, below
    if isinstance(exc, ConnectionError):      # ConnectionRefusedError et al
        return True
    if _http_status(exc) in _ENDPOINT_BUSY_STATUS:
        return True
    return isinstance(exc, OSError) and exc.errno in _ENDPOINT_DOWN_ERRNOS


def endpoint_misconfigured(exc: BaseException | None) -> embeddings.EndpointMisconfigured | None:
    """The embed client's "this endpoint is the wrong one, or refuses us"
    (404, 405, 401, 403, …) however deep it is wrapped; None otherwise. A
    deferral like any other — and one that waiting does not heal, so the
    collection's embedding PAUSES with it as the reason (`_pause_embedding`)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, embeddings.EndpointMisconfigured):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


#: How often a collection paused for a wrong endpoint asks it again: one
#: attempt with real work, then paused again if it still refuses. Bounded,
#: never a hot loop; a change to the collection's [encoder] retries at once.
ENDPOINT_RETRY_S = 300.0
#: collection place (its manifest's path, never its name) ->
#: (the recipe the pause is about, the reason, the next attempt at)
_endpoint_paused: dict[str, tuple[object, str, float]] = {}
_endpoint_paused_lock = threading.Lock()
#: who wrote the embed_gate state: only a pause of ours is lifted by us
_ENDPOINT_BY = "endpoint"


def _recipe_or_none(collection):
    try:
        return collections_mod.recipe_for(collection)
    except Exception:   # noqa: BLE001 — no recipe: nothing to compare
        return None


def _set_gate_state(con, allowed: bool, reason: str, *, only_ours: bool = False) -> None:
    """The surface the backend gate and the run check write their pauses to
    (`backend_gate.GATE_STATE_KEY`): the Library card and `hold collection
    show` read it. Never breaks the caller."""
    try:
        if only_ours:
            current = backend_gate.gate_state(con) or {}
            if current.get("by") != _ENDPOINT_BY:
                return
        db.set_control(con, backend_gate.GATE_STATE_KEY, json.dumps(
            {"allowed": allowed, "reason": reason[:400], "ts": int(time.time()),
             "by": _ENDPOINT_BY}))
    except Exception:   # noqa: BLE001 — a status write must never break the stage
        pass


def _pause_embedding(con, collection, exc: BaseException) -> None:
    """The endpoint refused as a wrong one would: embedding for this
    collection pauses, with the reason in its log and on the gate's surface,
    until its [encoder] changes or the endpoint answers again. No document
    is touched. Logged on a change of reason only, so a retry every
    ENDPOINT_RETRY_S that meets the same refusal says nothing new."""
    reason = str(endpoint_misconfigured(exc) or exc)[:400]
    place = str(collection.manifest_path)
    with _endpoint_paused_lock:
        before = _endpoint_paused.get(place)
        _endpoint_paused[place] = (_recipe_or_none(collection), reason,
                                   time.time() + ENDPOINT_RETRY_S)
    if before is None or before[1] != reason:
        db.log_event(con, "error", "embed",
                     (f"[{collection.name}] embedding PAUSED: {reason} — asked again "
                      f"every {int(ENDPOINT_RETRY_S)} s, and at once when the "
                      f"collection's [encoder] changes")[:900])
        _set_gate_state(con, False, reason)


def _endpoint_answered(con, collection) -> None:
    """An embed succeeded: a pause for a wrong endpoint, if any, is over."""
    place = str(collection.manifest_path)
    if place not in _endpoint_paused:          # the common case: one lookup
        return
    with _endpoint_paused_lock:
        if _endpoint_paused.pop(place, None) is None:
            return
    db.log_event(con, "info", "embed",
                 f"[{collection.name}] embedding enabled: the endpoint answers again")
    _set_gate_state(con, True, "the endpoint answers again", only_ours=True)


def _endpoint_pause_holds(con, collection) -> bool:
    """Is this collection's embedding paused for a wrong endpoint? A change
    to its [encoder] lifts the pause at once (and has the gate look at the
    new endpoint afresh); otherwise one attempt is let through every
    ENDPOINT_RETRY_S, and a refusal pauses it again."""
    place = str(collection.manifest_path)
    entry = _endpoint_paused.get(place)
    if entry is None:
        return False
    recipe, _, retry_at = entry
    now_recipe = _recipe_or_none(collection)
    if now_recipe != recipe:
        with _endpoint_paused_lock:
            _endpoint_paused.pop(place, None)
        from . import run_check
        run_check.forget(collection)
        backend_gate._cache.pop(collection.name, None)
        db.log_event(con, "info", "embed",
                     f"[{collection.name}] embedding enabled: the collection's [encoder] "
                     f"changed; the endpoint is asked again")
        _set_gate_state(con, True, "the collection's [encoder] changed", only_ours=True)
        return False
    now = time.time()
    if now < retry_at:
        return True
    with _endpoint_paused_lock:
        _endpoint_paused[place] = (recipe, entry[1], now + ENDPOINT_RETRY_S)
    return False        # one attempt; `_pause_embedding` again if it refuses


def endpoint_timed_out(exc: BaseException | None) -> embeddings.EndpointTimedOut | None:
    """The embed client's "the encoder took the request and did not answer
    in time" however deep it is wrapped; None otherwise. A deferral (it is
    an EndpointBusy) — and the one failure that COULD be a document's own,
    so it is put on a document only by `encoder_timeouts`' evidence: the
    same document timing out again and again while the endpoint answers
    others. A bare httpx.ReadTimeout from elsewhere is not this."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, embeddings.EndpointTimedOut):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def _timeout_blame(exc: BaseException, n: int) -> str:
    """The error put on a document that kept timing out alone."""
    return (f"EndpointTimedOut: timed out {n} times while the endpoint answered "
            f"other documents in between — this document's own: {exc}")[:400]


def _hold_busy(con, collection, endpoint, exc: BaseException) -> None:
    """The encoder timed out and answered no document: hold it as busy —
    nothing sent to it for `encoder_timeouts.hold`'s bound, doubling while it
    stays silent, released by its first answer. No document is touched."""
    seconds = encoder_timeouts.hold(endpoint)
    db.log_event(con, "warn", "embed",
                 (f"[{collection.name}] the encoder timed out and answered no "
                  f"document ({exc}); held as busy for {int(seconds)} s — no "
                  f"document is blamed for a hung endpoint")[:900])


def _endpoint_busy_holds(collection) -> bool:
    """Is this collection's encoder held as busy after a timeout?"""
    if not encoder_timeouts.any_held():
        return False                            # the common case: no lookup
    return encoder_timeouts.held(
        encoder_timeouts.endpoint_of(_recipe_or_none(collection))) > 0


#: (collection, stage) -> when its platform-missing deferral was last said
_platform_said: dict[tuple[str, str], float] = {}
PLATFORM_SAY_EVERY_S = 600.0


def platform_missing(exc: BaseException) -> bool:
    """Is a file or package the platform must carry missing (app/chunking.py
    `PlatformMissing`), anywhere in the exception's chain? By class, and by
    name for one that crossed the parse pool's pickling."""
    from app.chunking import PlatformMissing
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, PlatformMissing) or any(c.__name__ in ("PlatformMissing", "TokenizerMissing")
                                                 for c in type(e).__mro__):
            return True
        e = e.__cause__ or e.__context__
    return False


def _say_origin_missing(con, collection, stage: str, path: str) -> None:
    """Once per missing file per collection (app/origin_missing.py): the
    documents that read it are set aside with an error naming it, and the
    maintainer clears them by itself once the file is back."""
    if origin_missing.note(collection, path):
        db.log_event(con, "warn", stage,
                     f"origin missing: {path} — the documents that read it are set aside (an error "
                     f"naming this file) and cleared by themselves once it is back, checked every "
                     f"{origin_missing.HEAL_EVERY_S / 60:.0f} min; a rename or a mount that is gone?")


def _heal_origin_missing(con, collection) -> bool:
    """The maintainer's turn: the errors of files that are back, cleared."""
    if not origin_missing.due(collection):
        return False
    try:
        healed = origin_missing.heal(con, collection)
    except Exception as e:   # noqa: BLE001 — a manifest busy: the next turn
        db.log_event(con, "warn", "worker", f"origin-missing heal deferred: {type(e).__name__}: {e}"[:300])
        return False
    for path, n in healed.items():
        db.log_event(con, "info", "worker", f"origin back: {path} — {n:,} document(s) cleared of its error")
    return bool(healed)


def _say_platform_missing(con, collection, stage: str, exc: BaseException) -> None:
    """Once per PLATFORM_SAY_EVERY_S per collection and stage: every document
    of the stage defers for the same reason, and one line says it."""
    key = (str(getattr(collection, "manifest_path", "") or getattr(collection, "name", "")), stage)
    now = time.monotonic()
    if now - _platform_said.get(key, -PLATFORM_SAY_EVERY_S) < PLATFORM_SAY_EVERY_S:
        return
    _platform_said[key] = now
    db.log_event(con, "warn", stage,
                 f"{stage} deferred — the platform is missing what it needs ({exc}); no document "
                 f"is marked: the deploy that brings it resumes them all")


def transient_failure(exc: BaseException) -> bool:
    """The moment's fault, not the document's: the encoder away or busy,
    the manifest or the vector store held by another writer. ONE test for
    both embed paths — the parallel one (every encoder at concurrency > 1,
    pubmed's among them) asked only `endpoint_unavailable`, so a 503 or a
    vector-lock timeout there was written onto the document as an error,
    which takes it out of every selection until a human clears it.

    AND THE ENCODER'S RUN (03 §3.3). An answer that named a run this peer
    has not validated — or named none — wrote nothing, and that is the
    endpoint's state, never the document's: the document is untouched and
    stays selectable, while `backend_gate.embed_allowed` gates the new run
    at once or pauses the collection's embedding and says why."""
    return (endpoint_unavailable(exc) or manifest_contended(exc)
            or vector_store_contended(exc) or run_not_validated(exc) or platform_missing(exc)
            # the vector table lacks the validity columns the collection
            # declares: the TABLE's state, repaired once by `hold vectors
            # validity-columns`; every document defers until then (2026-10-13)
            or isinstance(exc, embeddings.ValidityColumnsMissing))


def run_not_validated(exc: BaseException) -> bool:
    from .hold import embedder as hold_embedder, runs as hold_runs
    return isinstance(exc, (hold_runs.RunUnvalidated, hold_runs.RunRefused,
                            hold_embedder.GateDeferred))


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
    SQLite writes only — seconds for a batch — and contenders wait
    busy_timeout instead of failing. Both stages open it THEMSELVES, late:
    their long part is not SQLite (the parse pool, the tantivy commit) and
    holding the manifest's write lock across it blocks every other writer,
    which wedged genome-hg38 for 68 minutes through fts and cost rdf 51.7 s
    a batch on wikipedia. `_NO_OUTER_TRANSACTION` is what keeps this step
    from opening one on their behalf. The embed step opens NO outer
    transaction at all: its long part is the
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
    if stage not in _NO_OUTER_TRANSACTION:
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
                         f"{stage} interrupted — the node is stopping, or its mode "
                         f"no longer runs this ({e})")
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
        if isinstance(e, catalog.CatalogUnavailable):
            # the catalog index could not take the batch's entries: the
            # marks rolled back with them, nothing is wrong with the documents
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — {e}; the documents are untouched "
                         f"and will be retried")
            return False
        if vector_store_contended(e):
            # the vector store's optimistic concurrency: another writer or
            # the index maintainer committed first. Same rule as above —
            # a deferral, never an error on the document.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — vector store busy ({e}); "
                         f"the document is untouched and will be retried")
            return False
        if run_not_validated(e):
            # 03 §3.3: the answer named a run this peer has not validated,
            # or none. Nothing was written and nothing is wrong with the
            # document; the next `embed_allowed` gates the run or pauses the
            # collection's embedding with the reason.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — {e}; the document is untouched "
                         f"and will be retried")
            return False
        timeout = endpoint_timed_out(e) if stage == "embed" else None
        if timeout is not None:
            # The encoder did not answer in time: the endpoint's, by default
            # (2026-10-09: a hung LocalAI backend errored three gutenberg
            # documents). Deferred and held as busy — unless THIS document
            # keeps timing out while the endpoint answers others.
            endpoint = encoder_timeouts.endpoint_of(_recipe_or_none(collection))
            n = encoder_timeouts.timed_out(endpoint, str(collection.manifest_path),
                                           row["uid"])
            if n < encoder_timeouts.BLAME_AFTER:
                _hold_busy(con, collection, endpoint, timeout)
                db.log_event(con, "warn", stage,
                             f"{stage} deferred — {timeout}", row["uid"])
                return False
            blame = _timeout_blame(timeout, n)
            con.execute(
                "UPDATE documents SET retries=retries+1, error=?, updated_at=? WHERE uid=?",
                (f"{stage}: {blame}"[:500], int(time.time()), row["uid"]))
            db.log_event(con, "error", stage, blame, row["uid"])
            return True
        if endpoint_misconfigured(e):
            # The endpoint is the wrong one or refuses us (404, 401, …): the
            # same deferral, and the collection's embedding pauses with the
            # reason until its [encoder] changes or the endpoint answers
            # (2026-10-09: 4,295 gutenberg documents errored for a doubled
            # `/v1/embeddings` in the url).
            _pause_embedding(con, collection, e)
            return False
        if endpoint_unavailable(e):
            # Nothing is wrong with this document, so nothing is written to
            # it: no error, no retry increment. The backend gate owns the
            # endpoint's health; this stage simply yields the round.
            db.log_event(con, "warn", stage,
                         f"{stage} deferred — encoder unreachable ({e}); "
                         f"the document is untouched and will be retried")
            return False
        if platform_missing(e):
            # a file or package the PLATFORM must carry is missing (a
            # tokenizer the image was to fetch): the same for every
            # document, the fault of none — deferred, said once a while
            _say_platform_missing(con, collection, stage, e)
            return False
        if isinstance(e, plugin_base.UnindexableDocument):
            # a CORPUS fact surfaced mid-stage (the mirror ships no text for
            # this entry): recorded UNPREFIXED, like discovery writes it, so
            # it lands in the Library's 'not indexable' bucket — not as a
            # stage-prefixed error that reads as a blocked pipeline and that
            # "clear errors & retry" would bounce forever. A document indexed
            # before the origin said so leaves every index FIRST (pg673,
            # 2026-10-22: the error alone left its markup in the lexical leg)
            from . import unindexable
            try:
                unindexable.withdraw(con, collection, row["uid"])
            except Exception as w:  # noqa: BLE001 — an index busy: deferred, nothing recorded
                db.log_event(con, "warn", stage,
                             f"{stage} deferred — not indexable, but its index entries could "
                             f"not be withdrawn yet ({type(w).__name__}: {w})", row["uid"])
                return False
            con.execute(
                "UPDATE documents SET error=?, updated_at=? WHERE uid=?",
                (str(e)[:500], int(time.time()), row["uid"]))
            db.log_event(con, "info", stage,
                         f"not indexable: {e}", row["uid"])
            return True
        missing = origin_missing.path_of(e)
        con.execute(
            "UPDATE documents SET retries=retries+1, error=?, updated_at=? WHERE uid=?",
            (f"{stage}: {origin_missing.message(missing) if missing else e}"[:500],
             int(time.time()), row["uid"]))
        if missing:
            _say_origin_missing(con, collection, stage, missing)
        db.log_event(con, "error", stage, f"{type(e).__name__}: {e}", row["uid"])
        return True
    if stage == "embed":
        _endpoint_answered(con, collection)
        if encoder_timeouts.watching():         # evidence matters only beside a timeout
            encoder_timeouts.answered(
                encoder_timeouts.endpoint_of(_recipe_or_none(collection)),
                str(collection.manifest_path), row["uid"])
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
    _settle_the_run(con, collection)     # an untested run is gated at once (03 §3.3)
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
    # the node profile pauses the parse-pool work (an archive pass, rdf, fts)
    # at this unit boundary; embedding goes on (app/cpu_limit.py)
    cpu_paused = _cpu_paused(con, collection)
    if collection.genesis_archive and not cpu_paused \
            and not (pipeline_active and _remote_archive(collection)):
        # same phase rule as the pipeline thread (_archive_yields): a pass
        # that would starve the encoder waits for one fts batch first
        if not _archive_yields(con, collection) and \
                _drain_remote_archive(con, collection, _stop):
            return True
    scope = db.get_scope(con)
    if pipeline_active or cpu_paused:
        stage, row = _next_embed(con, scope, collection)
        if stage == "embed" and not _embedding_allowed(con, collection):
            return False        # rdf/fts continue on the pipeline thread, or the profile's pause ends
    else:
        stage, row = _next_book(con, scope, collection=collection)
        if stage == "embed" and not _embedding_allowed(con, collection):
            # The endpoint failed its behavioral baseline (or is down).
            # Embedding pauses — the document is untouched, never errored —
            # while rdf/fts work continues. embed_allowed() logged the
            # transition and caches the verdict, so this costs nothing per
            # round until the TTL expires.
            stage, row = _next_book(con, scope, embed_allowed=False, collection=collection)
    if stage == "embed" and row is not None and encoder_timeouts.passed_over(
            str(collection.manifest_path), row["uid"]):
        # it timed out and the endpoint has answered nobody since: another
        # document is asked first (app/encoder_timeouts.py), or none
        rows = _embed_candidates(con, scope, 1, collection)
        if rows:
            row = rows[0]
        elif pipeline_active or cpu_paused:
            return False
        else:
            stage, row = _next_book(con, scope, embed_allowed=False, collection=collection)
    if stage == "embed" and not _vector_writes_allowed(collection, con):
        return False            # index rewrite in progress, or the disk short: deferred, not an error
    if stage == "embed":
        recipe = collections_mod.recipe_for(collection)
        if recipe.concurrency > 1:
            return _embed_parallel(con, collection, recipe)
    if row is None:
        now = time.time()
        if not pipeline_active and not cpu_paused and \
                now - last_idle_log.get(collection.name, 0.0) > 300:
            # "CAUGHT UP" IS A CLAIM, AND IT WAS FALSE. A stage that is
            # switched OFF selects no row, exactly like a stage with
            # nothing left to do — so the worker announced a finished
            # pipeline every five minutes while 4,577,101 pubmed
            # documents sat unembedded and the node ran at 0.16 load.
            # The operator read the machine as stuck, which it was; the
            # log said the opposite (2026-09-12).
            waiting = _waiting_because_off(con, collection)
            db.log_event(con, "info", "worker",
                         f"idle — {waiting}" if waiting
                         else "idle — pipeline is caught up")
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
# what each per-collection thread was started for (`_identity`), by thread
# name: a name that now holds another collection gets a new thread
_thread_identity: dict[str, str] = {}
_paused = threading.Event()          # set = the mode fetches nothing: paused, seed-only
# set = "download/seeding only": the bytes keep moving and NOTHING reads
# them — no scan, no rdf, no fts, no index maintenance, no embedding
# (operator, 2026-09-19; app/node_state.py: MODE_RUNS). What lands is
# scanned when everything runs again (`_scan_owed`). This mode used to go
# on scanning, which is parsing under a button that says download. Moving
# bytes is nearly free; the other stages run every core hot, which matters
# when the node lives in a room someone sits in.
_acquire_only = threading.Event()


class _ParseHeld:
    """`is_set()` while the node's mode runs no parsing. A STOP for a scan,
    beside SIGTERM and the collection's removal: a scan already running when
    the mode changes ends at its next ref, its progress committed and its
    cursor exactly that far — it is resumable by construction — where a pause
    used to wait for it to finish (1,083 s on wikivec)."""

    def is_set(self) -> bool:
        return _paused.is_set() or _acquire_only.is_set()


_parse_held = _ParseHeld()
#: EMBEDDING STOPS WITH THE MODE TOO, at the same unit SIGTERM already has: a
#: slice (invariant 2; EMBED_SLICE_CHUNKS). Seen live the evening this landed
#: (2026-09-19): in "download/seeding only" the producers took no new document,
#: and the two in flight went on — RAGE served 470 embedding requests in the
#: next 100 s, and a large book is thousands of chunks (5.3 h for the largest).
#: "Pause: nothing is running" cannot wait hours. `upsert_book` raises
#: EmbedInterrupted between slices; what is written stays, the document is
#: deferred untouched, and it resumes from there when everything runs again.
_embed_stop = _AnyStop(_stop, _parse_held)
#: THE NODE PROFILE'S CORES (app/cpu_limit.py; operator, 2026-10-27): one
#: watch per collection, keyed on its manifest's path, so each collection's
#: log says each change of the profile once ("profile quiet: paused until
#: 08:00", "profile day: 8 cores")
_cpu_watches: dict[str, cpu_limit.Watch] = {}


def _cpu_paused(con, collection) -> bool:
    """Whether the node's profile pauses this collection's parse-pool work
    now (cpu_cores = 0): rdf, fts and the archive pass. Asked at the unit
    boundary — the unit already running finishes and commits — and cheap
    (a cached read, app/cpu_limit.py). It never pauses embedding, bundles or
    scans: a profile limits background CPU, and a collection's own `embed`
    runs in every profile (decision 3, docs/notes/node-profiles-design.md).
    The N-core limit itself is applied by the pool (`parallel.compute`)."""
    watch = _cpu_watches.setdefault(str(collection.manifest_path), cpu_limit.Watch())
    lim, line = watch.see()
    if line is not None:
        db.log_event(con, "warn" if lim.error else "info", "worker", line)
    return lim.cores == 0


#: collections whose corpus may have changed, or whose scan was cut short,
#: while the mode ran no parsing: scanned when everything runs again
_scan_owed: set[str] = set()
_scan_owed_lock = threading.Lock()


def _owe_scan(name: str) -> None:
    with _scan_owed_lock:
        _scan_owed.add(name)


def _take_owed_scans() -> set[str]:
    with _scan_owed_lock:
        owed = set(_scan_owed)
        _scan_owed.clear()
    return owed

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
    if fresh.manifest_path != collection.manifest_path or _identity(fresh) != _identity(collection):
        return collection       # another collection under the name: this thread is stopped
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


def query_waiting(con) -> bool:
    """Is a person waiting on a model right now (A5)? The node's
    interactive claim (app/dispatch.py), memoised there. The rollback
    rotation (`_serve_turn`) reads it between documents; the producers do
    not need to, because their requests stand aside at the gate, per
    chunk. `con` is kept for the rollback's call shape."""
    return dispatch.interactive_active()


def reset_query_priority_memo() -> None:
    dispatch._claim_seen.clear()


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
    producers = producers_enabled(True)
    # indexing and parsing yield the CPU to the threads feeding the encoders
    # (operator, 2026-09-19; docs/notes/scheduling.md §12) — only with
    # producers: in the rollback this thread may embed
    _background_priority()
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
                if not slice_scope.ready(con, collection):
                    # the slices held are not applied to in_scope yet (the
                    # scan thread does it): nothing outside them is processed
                    _say_once(con, collection.name, "slice-scope",
                              "waiting for the slice scope: only the slices the "
                              "collection file holds are processed (app/slice_scope.py)")
                    time.sleep(config.WORKER_IDLE_SLEEP)
                    continue
                if not producers and owns_own_embed(collection):
                    # The full pipeline, embed and genesis included: this
                    # collection's encoder is its own container, so nothing
                    # here competes with the GPU or with another collection.
                    if not _run_one_step(con, collection, _pipeline_idle_log,
                                          pipeline_active=False):
                        time.sleep(config.WORKER_IDLE_SLEEP)
                    continue
                # With producers, imported vectors arrive HERE: a bundle
                # import is CPU/IO work (reads, T3's sixteen, Lance writes),
                # and on the thread that feeds an encoder it would idle it.
                if producers and _drain_bundle_inbox(con, collection):
                    continue
                # THE NODE PROFILE (app/cpu_limit.py): cpu_cores = 0 pauses
                # the parse-pool work below at this unit boundary — the last
                # unit committed, nothing begun — in a bounded sleep that
                # re-checks the stop. Bundles above go on: p2p embedding
                # runs in every profile (decision 3)
                if _cpu_paused(con, collection):
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
        if name not in live or _thread_identity.get(thread.name) != _identity(live[name]):
            gone.set()                      # removed, or the name holds another collection
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
            _thread_identity[thread.name] = _identity(collection)
            thread.start()


# ---- embed producers (docs/notes/scheduling.md §12) ------------------------
# One thread per collection feeds that collection's encoder gate, replacing
# the main loop's encoder rotation. Measured before (2026-09-19): the loop
# served the endpoints serially, so pubmed's fruitless turns (250 s in ten
# minutes, 0 documents) left gutenberg's GPU idle in 233 of 300 seconds with
# 78,178 documents waiting. A producer only feeds: selection, chunks, the
# held-rows read, the commit. Bundles and archives run on the pipeline
# thread. It stays at nice 0 while indexing and parsing sit below it.
_embed_producers: dict[str, tuple[threading.Thread, threading.Event]] = {}


def producers_enabled(pipeline: bool) -> bool:
    return bool(pipeline and config.WORKER_EMBED_PRODUCERS)


def _producer_step(con, collection) -> bool:
    """One stream of this collection's embedding, or False when it has
    none to do now — switched off, unlicensed, its encoder failing its
    baseline, or its vector table under maintenance: each a deferral."""
    scope = db.get_scope(con)
    if scope["embed_mode"] not in collections_mod.EMBEDDING_MODES:
        return False
    if not _license_allows(con, collection, "embedding"):
        return False
    if not _vector_writes_allowed(collection, con):
        return False
    if not _embedding_allowed(con, collection):
        return False
    recipe = collections_mod.recipe_for(collection)
    slots = max(config.EMBED_PLUGIN_SLOTS, recipe.concurrency)
    with dispatch.working_as(Level.CORPUS, collection.name):
        return _embed_parallel(
            con, collection, recipe, slots=slots,
            keep_going=lambda: not (_paused.is_set() or _acquire_only.is_set()))


def _embed_producer(collection, gone: threading.Event) -> None:
    con = db.connect(collection)
    db.log_event(con, "info", "worker",
                 f"embed producer started for {collection.name!r}")
    try:
        while not _stop.is_set() and not gone.is_set():
            if _paused.is_set() or _acquire_only.is_set():
                time.sleep(2)
                continue
            collection = _live_collection(collection)
            try:
                if not _producer_step(con, collection):
                    time.sleep(config.WORKER_IDLE_SLEEP)
            except Exception as e:   # noqa: BLE001 — a producer must outlive any stream
                if manifest_contended(e):
                    db.log_event(con, "warn", "embed",
                                 f"stream deferred — manifest busy ({e})")
                    time.sleep(2)
                    continue
                try:
                    db.log_event(con, "error", "embed",
                                 f"embed producer error:\n{traceback.format_exc()[:1200]}")
                except Exception:
                    print(traceback.format_exc()[:1200], flush=True)
                time.sleep(5)
        db.log_event(con, "info", "worker",
                     f"embed producer stopped for {collection.name!r}")
    finally:
        con.close()


def _sync_embed_producers(collections) -> None:
    """Reconcile producers with the collections that may embed: start one
    per new collection, stop the producer of a removed one (it finishes
    what it has in flight and commits it — invariant 2)."""
    live = {c.name: c for c in collections}
    for name in list(_embed_producers):
        thread, gone = _embed_producers[name]
        if name not in live or _thread_identity.get(thread.name) != _identity(live[name]):
            gone.set()                      # removed, or the name holds another collection
            del _embed_producers[name]
        elif not thread.is_alive():
            del _embed_producers[name]      # crashed: restarted below
    for name, collection in live.items():
        if name not in _embed_producers:
            gone = threading.Event()
            thread = threading.Thread(target=_embed_producer, args=(collection, gone),
                                      name=f"embed-{name}", daemon=True)
            _embed_producers[name] = (thread, gone)
            _thread_identity[thread.name] = _identity(collection)
            thread.start()


def _join_pipeline_threads(timeout: float = 90.0) -> None:
    """Shutdown: each thread finishes its current document and exits (the
    stop event is checked between documents). The deadline is shared, and
    stragglers are daemon threads — a hung step cannot block SIGTERM's
    exit-0 contract past the container's grace period."""
    deadline = time.time() + timeout
    for thread, _ in list(_pipeline_threads.values()):
        thread.join(timeout=max(0.0, deadline - time.time()))
    # the embed producers: what is in flight finishes (a long document stops
    # at its next slice) and is committed before the process exits
    for thread, _ in list(_embed_producers.values()):
        thread.join(timeout=max(0.0, deadline - time.time()))
    # scans stop at their next ref and commit the batch so far; give them
    # the same deadline so that commit lands before the process exits
    with _scan_lock:
        scans = list(_scan_threads.values())
    for thread in scans:
        thread.join(timeout=max(0.0, deadline - time.time()))
    # Corpus and archive fetches too. They were never joined: a SIGTERM
    # abandoned them as daemon threads mid-pass — a listing unstoppable
    # anyway, a claim row left behind for the next boot to wait out. Their
    # stops include `_stop` and every phase of a pass now honours it, the
    # listing included, so each ends at its unit (a truthful `.part`, the
    # claim released) inside this same deadline.
    with _acquire_lock:
        fetches = list(_acquire_threads.values())
    fetches += list(_archive_threads.values())
    for thread in fetches:
        thread.join(timeout=max(0.0, deadline - time.time()))


def _effective_embed_mode(collection, con=None) -> str:
    """The mode the worker acts on: the runtime scope where it can be read,
    else the file's, else the default."""
    if con is not None:
        try:
            mode = db.get_scope(con).get("embed_mode")
            if mode:
                return str(mode)
        except Exception:   # noqa: BLE001 — the file's word then
            pass
    return collection.embed_mode_default or collections_mod.DEFAULT_EMBED_MODE


def _release_embedder(collection, con=None, others=None, reason: str = "") -> bool:
    """AN EMBEDDER IS HELD ONLY FOR A COLLECTION THAT EMBEDS EVERYTHING
    (operator, 2026-10-14). The worker's start-up run check loads every
    collection's encoder to settle its run for search, and LocalAI keeps a
    model until evicted: gutenberg's 4B embedder (embed on-demand) held
    9.9 GB of the card all night, the card crossed LocalAI's eviction
    threshold, and its reclaimer evicted the chat model five seconds into
    every load — each Ask answer ended in a 500. So when this collection's
    mode is not "all", its embedder is unloaded, unless another collection
    whose mode IS "all" uses the same model on the same runner. It loads
    again on the next request (a search, an on-demand embed). True when
    unloaded; never raises."""
    if _effective_embed_mode(collection, con) in collections_mod.BACKFILL_MODES:
        return False
    try:
        recipe = collections_mod.recipe_for(collection)
    except Exception:   # noqa: BLE001 — no encoder declared: nothing held
        return False
    if not recipe or not getattr(recipe, "model", None):
        return False
    server = model_load.server_of(recipe.url)
    if others is None:
        with _live_lock:
            others = list(_live_collections.values())
    for other in others:
        if other.name == collection.name:
            continue
        try:
            o = collections_mod.recipe_for(other)
        except Exception:   # noqa: BLE001
            continue
        if o and model_load.server_of(o.url) == server and o.model == recipe.model \
                and _effective_embed_mode(other) in collections_mod.BACKFILL_MODES:
            return False
    done = model_load.unload(recipe.url, recipe.model)
    if done and con is not None:
        db.log_event(con, "info", "control",
                     f"embedder {recipe.model} unloaded{(' on ' + reason) if reason else ''}: "
                     f"embed is {_effective_embed_mode(collection, con)!r}, not 'all'; it "
                     f"loads again on the next request")
    return done


#: Louise's embedders on the model runner are VeGa's (docs/notes/vega.md):
#: `vega-<model>`. Chat, reranking and translation are not, and are never swept.
EMBEDDER_PREFIX = "vega-"
#: settle beats (SETTLE_BEAT_S each) an on-demand embedder must have answered
#: no request for before it is unloaded: two beats, two minutes
IDLE_BEATS = 2


def _sweep_unheld_embedders(collections, con=None) -> list[str]:
    """NO HELD COLLECTION, NO EMBEDDER ON THE CARD (operator, 2026-10-14:
    deposit-granite was removed and vega-granite stayed loaded). On every
    runner a held collection's encoder lives on, each loaded `vega-` model
    that no held collection names — as its document model or its query
    model — is unloaded. Returns what went; never raises."""
    servers: set[str] = set()
    named: set[str] = set()
    for c in collections:
        try:
            r = collections_mod.recipe_for(c)
        except Exception:   # noqa: BLE001 — no encoder declared
            continue
        if not r:
            continue
        for url in (r.url, getattr(r, "query_url", None)):
            if url:
                servers.add(model_load.server_of(url))
        for m in (r.model, getattr(r, "query_model", None)):
            if m:
                named.add(m)
    # the models some collection on "all" names: those stay, idle or not
    kept: set[str] = set()
    for c in collections:
        if _effective_embed_mode(c) not in collections_mod.BACKFILL_MODES:
            continue
        try:
            r = collections_mod.recipe_for(c)
        except Exception:   # noqa: BLE001
            continue
        if r:
            kept |= {m for m in (r.model, getattr(r, "query_model", None)) if m}
    # WHO IS USING A MODEL is read from the node's store, not from LocalAI's
    # CPU figure (Perugia, 2026-10-19): `/system`'s cpu_percent is the VeGa
    # wrapper's process, 0.07–2 % whatever the encoder does — so a busy
    # encoder read idle (seven shutdowns under a running contribute) and an
    # idle one, its HIP runtime spinning 2.8 cores, never did. Two signals
    # replace it: the LEASE a process outside the worker holds while it
    # embeds (node_state.lease_encoder, renewed per batch), and the LAST
    # REQUEST any process made of the model (embeddings._touch, written by
    # the client itself on every answer, rate-limited).
    now = time.time()
    try:
        leased = node_state.leased_encoders(now=now)
        last_used = node_state.encoder_last_used()
    except Exception:   # noqa: BLE001 — the store unreadable: nothing known, as before
        leased, last_used = set(), {}
    idle_after_s = IDLE_BEATS * SETTLE_BEAT_S
    gone: list[str] = []
    idle: list[str] = []
    for server in sorted(servers):
        loaded = model_load.activity(server)
        if loaded is None:
            continue
        for m in sorted(loaded):
            if not m.startswith(EMBEDDER_PREFIX) or m in kept or (server, m) in leased:
                continue
            if m not in named:
                if model_load.unload(server, m):
                    gone.append(m)
                continue
            # named only by collections that do not embed everything: a search
            # or an on-demand embed loaded it (a gutenberg or wikipedia query
            # encoder); it goes once nobody has asked it anything for IDLE_BEATS
            # beats — a model nobody recorded a request for is idle now
            if now - last_used.get((server, m), 0.0) >= idle_after_s and model_load.unload(server, m):
                idle.append(m)
    if con is not None and (gone or idle):
        if gone:
            db.log_event(con, "info", "control",
                         f"embedder(s) {', '.join(gone)} unloaded: no held collection names them")
        if idle:
            db.log_event(con, "info", "control",
                         f"embedder(s) {', '.join(idle)} unloaded: idle, and no collection "
                         f"that names them embeds everything")
    return gone + idle


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
    # A MALFORMED FILE REFUSES ITS OWN COLLECTION, NEVER THE NODE. This is
    # invariant 1's rule ("the node keeps running its others") applied to
    # the other way a collection can be unrunnable. `load_all` RAISES on a
    # malformed file on purpose — a typo must not silently become the
    # default — and it is right to raise; what was wrong was this loop
    # letting the raise reach the round, where one collection's bad TOML
    # stopped every collection, pubmed's archive import included.
    #
    # Observed live 2026-09-12, while re-minting gutenberg: its file named
    # a Codex whose SHARD had been moved aside for the re-mint, and the
    # round loop logged a traceback every five seconds and served nothing.
    try:
        current = collections_mod.load_all()
    except Exception as e:                              # noqa: BLE001
        current, bad = collections_mod.load_all(skip_unreadable=True)
        for name, reason in bad:
            if _bad_toml_nagged.get(name) != reason:
                _bad_toml_nagged[name] = reason
                con = cons.get(name) or (next(iter(cons.values())) if cons else None)
                if con is not None:
                    db.log_event(con, "error", "worker",
                                 f"collection {name!r} is not served: {reason}"[:500])
        if not current and not bad:
            raise e
    else:
        _bad_toml_nagged.clear()
    known = {c.name for c in all_collections}
    now_names = {c.name for c in current}
    previous_by_name = {c.name: c for c in all_collections}
    # A NAME THAT NOW HOLDS ANOTHER COLLECTION leaves and joins again, its
    # handles closed first. 2026-10-27: `forget wikidata` then `rename
    # wikidata-p2 wikidata` between two rounds; the name never went missing,
    # so the worker kept seven connections and the lock of the deleted
    # manifest, wrote the old rows into the new collection's catalog index
    # and never discovered the new one (caches key on identity, not a name).
    current_by_name = {c.name: c for c in current}
    replaced = {n for n in known & now_names
                if _identity(previous_by_name[n]) != _identity(current_by_name[n])}
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
        if prev is None or kept.name not in cons or kept.name in replaced:
            continue
        if prev.embed_mode_default != kept.embed_mode_default:
            _apply_tier2_embed_mode(cons[kept.name], kept, "TOML edit")
            # switched to off or on-demand: its embedder leaves the card
            _release_embedder(kept, cons[kept.name], current, "the switch")
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

    for name in sorted(replaced):
        _leave(name, cons, current, previous_by_name, last_discover, "replaced by another collection")
    known -= replaced

    for added in [c for c in current if c.name not in known]:
        # A re-added collection must not inherit a set stop event, or its
        # acquire/archive threads return "stopped" instantly and silently
        # forever — `_archive_worker` logs nothing on an empty fetch, so it
        # would fail invisibly.
        _gone.pop(added.name, None)
        # ANOTHER WRITER HOLDS IT: not opened, not even its manifest, until it
        # lets go (07 §7.7). 2026-10-13: `dev carry` held normattiva-1861 and
        # had removed its empty manifest to put the carried one in place; this
        # loop opened the collection two seconds later, `db.connect` created a
        # fresh empty manifest, the carry took it for its own earlier copy,
        # and 318,349 documents' parse, index and embed marks were lost. Not
        # added this round; the next round asks again.
        from app.hold import lock as lock_mod
        held_by = lock_mod.holder(Path(added.manifest_path).parent)
        if held_by is not None:
            if _lock_nagged.get(added.name) != str(held_by) and cons:
                _lock_nagged[added.name] = str(held_by)
                db.log_event(next(iter(cons.values())), "info", "worker",
                             f"new collection {added.name!r} not opened this round: "
                             f"{held_by.get('what') or 'another writer'} holds it")
            continue
        try:
            spaces.validate_boot([added])
        except (spaces.BootValidationError, collections_mod.NoEncoder) as e:
            if cons:
                db.log_event(next(iter(cons.values())), "error", "worker",
                             f"new collection {added.name!r} failed boot "
                             f"validation — skipped until fixed: {e}"[:500])
            continue
        cons[added.name] = db.connect(added)
        db.ensure_browse_index(cons[added.name])
        db.drop_catalog_fts(cons[added.name])
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
        _leave(gone, cons, current, previous_by_name, last_discover,
               "removed — ingestion stopped, data kept")

    return [c for c in current if c.name in cons]


def _identity(c) -> str:
    """What a name holds: its Codex's collection_id, else its manifest's path."""
    codex = getattr(c, "codex", None)
    return getattr(codex, "collection_id", None) or str(Path(c.manifest_path))


def _leave(gone: str, cons, current, previous_by_name, last_discover, why: str) -> None:
    """A collection the worker stops holding: its threads signalled, its
    handles (manifest, text index, catalog, vectors) closed and forgotten."""
    db.log_event(cons[gone], "info", "worker", f"collection {gone!r} {why}")
    # its embedders leave the card when no held collection names them
    _sweep_unheld_embedders(current, cons[gone])
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
    # its index and vector handles, keyed on paths a re-created collection
    # of the same name reuses (normattiva, 2026-10-13: 244 fts errors)
    left = previous_by_name.get(gone)
    if left is not None:
        from . import catalog as catalog_mod, embeddings as embeddings_mod, fts as fts_mod
        fts_mod.forget(left.fts_dir)
        catalog_mod.forget(left)
        embeddings_mod.forget(left)
    cons[gone].close()
    del cons[gone]
    last_discover.pop(gone, None)
    _take_discovery_request(gone)   # never scan a removed collection


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
        # CUMULATIVE since the worker start, not a round's figures: München
        # read 36 turns / 1 unit / 1,067 s as "1,067 s a round" (2026-10-28)
        db.log_event(con, "info", "sched", f"{resource} (since start) — " + "; ".join(parts))
    # THE PARSE POOL'S ADMISSION — the time average of tasks IN FLIGHT,
    # capped at the pool size. Everything else here measures how long
    # something took; this says whether the pool was ALLOWED work, and its
    # absence is why admission handed wikipedia 3 of 11 children for a day
    # while every other instrument reported "slow" (2026-09-23,
    # docs/notes/scheduling.md §11). A number well below `processes()` while
    # a collection has parse work queued is the defect it exists to show.
    #
    # It is NOT how many children were running, and the wording says so on
    # purpose: `compute` submits a whole batch at once, so the slots stay in
    # flight through the straggler tail while the early-finishing children
    # idle. Measured the night this was written, the two read 73-81 % and
    # 34 % over comparable windows. For running children, sample state R at
    # 1 Hz — tools/sched_measure.sh.
    pool = parallel.occupancy()
    if pool["elapsed_s"] > 0:
        db.log_event(con, "info", "sched",
                     f"parse pool — {pool['slots_in_flight']:.1f} of "
                     f"{pool['processes']} slots in flight "
                     f"({pool['utilisation']:.0%}) over {pool['elapsed_s']:.0f}s")
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
    # The encoder gates (docs/notes/scheduling.md §12), cumulative since
    # start: per level, requests admitted, the time they waited to be, and
    # the time they held the server. A corpus level that waits while
    # nothing interactive runs, or an encoder whose served time does not
    # grow while documents wait, is the defect this exists to show.
    for name, g in dispatch.snapshot().items():
        if g["levels"]:
            db.log_event(con, "info", "sched", f"gate {name} — " + "; ".join(
                f"{lvl}: {s['admitted']} admitted, waited {s['waited_s']:.0f}s, "
                f"served {s['served_s']:.0f}s" for lvl, s in g["levels"].items()))


#: set while run_forever's loop runs. The stats beat acts on the node state
#: only then: a beat with no main loop behind it has nobody to clear what it
#: sets (`_note_pause_from_state` never clears).
_main_loop_live = threading.Event()


def _control_state_for_beat(node_con) -> str | None:
    """The node state the stats beat may act on — from state/node.sqlite,
    where the main loop reads it too (app/node_state.py) — or None when
    there is no main loop to agree with.

    ONE SOURCE, NOT TWO. The beat once read `load_all()[0]` while the main
    loop read its FILTERED `all_collections[0]`. When they differed, the
    beat's collection had no worker_state, which reads 'paused', so the
    beat set `_paused` every 10 s while the main loop cleared it and
    re-armed the fetch every round: a fetch stopped and restarted per beat,
    each with a full listing (the second review, 2026-09-13). The node
    store removed the choice of collection altogether.

    None without a main loop is not only defensive: `test_stats.py` runs a
    real `_stats_publisher` with no main loop against a fresh config, whose
    state defaults to 'paused'. The beat set `_paused`, nothing cleared it,
    and five pipeline-thread tests later in the suite idled until timeout.
    """
    if not _main_loop_live.is_set():
        return None
    try:
        return node_state.worker_state(node_con)
    except Exception:   # noqa: BLE001 — telemetry must never decide by raising
        return None


#: the mode the main loop last acted on, for `_say_mode`
_mode_seen: list[str | None] = [None]


def _say_mode(state: str) -> None:
    """ONE LINE when the mode the worker acts on changes, and at its first
    read (2026-10-23: the node set seed-only at 12:25 and the worker's log
    had no line saying whether the worker ever read it — the question an
    operator who needs the GPU asks first)."""
    if _mode_seen[0] == state:
        return
    before, _mode_seen[0] = _mode_seen[0], state
    runs = sorted(node_state.MODE_RUNS.get(state, frozenset()))
    said = "read" if before is None else f"{before} -> {state}"
    _node_line("control")(f"node mode {said}: runs {', '.join(runs) if runs else 'nothing'} "
                          f"(read from {node_state.path()})")


def _note_pause_from_state(state: str) -> None:
    """Set `_paused` if the node's state says so, and NEVER clear it — see
    the comment at the call in `_stats_publisher`. Stopping late is the
    bug; resuming a round late is harmless, so this can only ever make the
    stop prompter, never make the node do work it was told not to."""
    if not node_state.runs(state, node_state.FETCH):     # paused, seed-only
        _paused.set()


#: the manifests whose run this process has settled for search (keyed by path)
_settled_for_search: set[str] = set()
SETTLE_BEAT_S = 60.0


def holds_vectors(collection) -> bool:
    lance = getattr(collection, "lance_dir", None)
    return bool(lance) and (Path(lance) / "chunks.lance").is_dir()


def _node_line(stage: str):
    """A logger for a node-level thread with no manifest of its own: the
    line on stdout, naming the collection in its message."""
    def log(m: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} [node info:{stage}] {m[:500]}", flush=True)
    return log


#: how often the publisher looks for finished led parts (config.PUBLISH_BEAT_S)
PUBLISH_BEAT_S = config.PUBLISH_BEAT_S
#: the publisher's last pass per collection, for whoever asks; written by the
#: publisher thread, read by node show / the Library — a summary a pass, not telemetry per unit
_publish_state: dict[str, dict] = {}
#: the control row `publish:<collection>` in node.sqlite carries the same summary
PUBLISH_KEY = "publish:"
#: what the publisher said once per collection and will not repeat until it changes
_publish_said: dict[str, str] = {}


class _Until:
    """A stop that is also a deadline: `once` and `export` ask `is_set()`
    between units, so a pass yields at its budget and the next beat resumes
    (CLAUDE.md: a pipeline thread never waits without a bound).

    AND THE MODE (2026-10-23): `still()` says whether the work may go on —
    the node's mode still embeds, the collection is not paused. The beat
    asked it once, at its start, so a pass begun before the operator's
    seed-only went on embedding into the card they had asked for (München:
    the publisher pinned and embedded with pubmed's encoder at 13:03, the
    node seed-only since 12:25). Asked at most every STILL_S."""

    STILL_S = 5.0

    def __init__(self, deadline: float, stop: threading.Event,
                 still: Callable[[], bool] | None = None):
        self.deadline, self.stop, self.still = deadline, stop, still
        self._asked, self._ok = 0.0, True

    def is_set(self) -> bool:
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            return True
        if self.still is not None and time.monotonic() - self._asked >= self.STILL_S:
            self._asked = time.monotonic()
            try:
                self._ok = bool(self.still())
            except Exception:   # noqa: BLE001 — unreadable: runs nothing, as everywhere
                self._ok = False
        return not self._ok


def _publish_pass(collection, codex, con, *, stop, log) -> dict:
    """One publisher pass over `collection` (app/share/contribute.py `once`,
    `build=False`): the open parts whose vectors this node holds — the
    embed stage's output under `embed = "p2p"`, and anything held before —
    bundled, through this node's own ladder, recorded in the coverage map
    and, where this node serves the collection, announced. No GPU beyond
    each bundle's sixteen challenge rows (09 §9.7)."""
    from contextlib import closing
    from . import encoder_use, identity
    from .bundles import keys as node_keys
    from .share import contribute, coverage as cov, peers
    root = collections_mod.louise_root() / "collections"
    key = node_keys.load_signing_key()
    own = identity.own()
    recipe = collections_mod.recipe_for(collection)
    # the encoder is pinned while the pass embeds and restored when it ends
    # (app/model_pins.py); a `worker ` holder, so a killed worker's is dropped at boot
    holder = f"{model_pins.WORKER}publisher {collection.name}"
    embed, _before_write = encoder_use.callables(codex, recipe, root=root, name=collection.name, stop=stop,
                                                 holder=holder, log=log)
    now = time.time()
    # 04 §4.2: announced only where it is served — the file shares and the mode seeds
    serving = bool(getattr(collection, "share", False)) and \
        node_state.runs(node_state.worker_state(), node_state.SEED)
    with closing(peers.connect()) as node_con:
        live = cov.live_peers(codex, node_con, now=now, own_key=own,
                              own_benchmarks=cov.own_benchmarks(root, collection.name, codex, own))
        try:
            return contribute.once(
                root, collection.name, codex, key=key, own_key=own, embed=embed,
                vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=collection),
                held_books=lambda ids: embeddings.books_with_vectors(ids, collection=collection),
                write=None, live=live, con=con, node_con=node_con, now=now, stop=stop, log=log,
                batch=config.EMBED_SLICE_CHUNKS, serving=serving, collection=collection, build=False)
        finally:
            encoder_use.release(holder)


def _publisher_beat(budget_s: float | None = None) -> dict[str, dict]:
    """One beat of the publisher: every SHARED public collection this worker
    runs (holds the lock of), where the mode embeds, gets a pass bounded by
    `budget_s`. Shared, not "p2p": `share` says what the node makes
    available — every embedding it holds, as bundles, whatever `embed`
    says (07 §7.8; a node whose embed is off still bundles what an archive
    or an earlier run gave it) — and `p2p` only adds the build, which is the
    embed stage's. So the parts a night under p2p finished at 05:58 leave
    at 06:00 under on-demand, not at the next night's flip (the node's
    choice, 2026-10-19). Returns the passes made, by collection. Never
    raises; a refusal or a deferral is said once per collection."""
    from contextlib import closing
    from .hold import embedder as emb
    from .share import contribute, counting
    budget_s = config.PUBLISH_BUDGET_S if budget_s is None else budget_s
    done: dict[str, dict] = {}
    try:
        mode = node_state.worker_state()
        if not node_state.runs(mode, node_state.EMBED):
            return done
        collections = collections_mod.load_all()
        paused = node_state.paused_collections()
    except Exception:   # noqa: BLE001 — the next beat
        return done
    for c in collections:
        if _stop.is_set():
            break
        # the worker runs it (its lock is held here) and the operator has not paused it
        if c.name in paused or c.name not in _locks:
            continue
        codex = getattr(c, "codex", None)
        if codex is None or getattr(codex, "privacy", "public") != "public":
            continue
        if not getattr(c, "share", False):
            continue                         # 04 §4.2: nothing leaves a node that does not serve it
        con = None
        try:
            con = db.connect(c)

            def say(m: str, _con=con) -> None:
                db.log_event(_con, "info", "share", m[:500])
            def still(_name=c.name) -> bool:
                return (node_state.runs(node_state.worker_state(), node_state.EMBED)
                        and _name not in node_state.paused_collections())
            out = _publish_pass(c, codex, con, stop=_Until(time.monotonic() + budget_s, _stop, still),
                                log=say)
            _publish_state[c.name] = {
                "at": time.time(), "snapshot": out.get("snapshot_id"), "peers": out.get("peers"),
                "probed": out.get("probed", 0), "bundled": len(out.get("held", [])),
                "awaiting": len(out.get("awaiting", [])), "left_to_fetch": len(out.get("left_to_fetch", [])),
                "accepted": len(out.get("accepted", [])), "refused": len(out.get("refused", [])),
                "announced": out.get("events", 0), "interrupted": bool(out.get("interrupted"))}
            # the last pass, for `node show` in another process (app/node_show.py):
            # a summary a pass, in the node's store
            try:
                with closing(node_state.connect()) as ncon:
                    db.set_control(ncon, f"{PUBLISH_KEY}{c.name}", json.dumps(_publish_state[c.name], sort_keys=True))
            except Exception:   # noqa: BLE001 — telemetry
                pass
            _publish_said.pop(c.name, None)
            done[c.name] = out
            if out.get("bundles") or out.get("refused"):
                say(f"[{c.name}] publish: {len(out['held'])} part(s) bundled from vectors held here, "
                    f"{len(out['accepted'])} accepted, {out['events']} announced, {len(out['refused'])} refused; "
                    f"{len(out['awaiting'])} led part(s) await the embed stage, "
                    f"{len(out['left_to_fetch'])} left to the fetch"
                    + (" — interrupted at the budget, resumes next beat" if out.get("interrupted") else ""))
        except (contribute.ContributeRefused, emb.GateDeferred, counting.CoverageUnknown) as e:
            if _publish_said.get(c.name) != str(e) and con is not None:
                _publish_said[c.name] = str(e)
                db.log_event(con, "info", "share", f"[{c.name}] publish: not this beat — {e}"[:500])
        except Exception as e:   # noqa: BLE001 — one collection, not the thread
            why = f"{type(e).__name__}: {e}"
            if _publish_said.get(c.name) != why:
                _publish_said[c.name] = why
                _node_line("publish")(f"{c.name}: pass failed: {why}")
        finally:
            if con is not None:
                con.close()
    return done


def _publisher() -> None:
    """07 §7.8 "all p2p": "embeds the parts rendezvous gives this node, with
    the cascade, and publishes as it goes". The embed stage does the first
    (`_backfill_rows` under "p2p", app/share/leading.py); this thread does
    the second, for every SHARED collection whatever its embed mode: every
    PUBLISH_BEAT_S, where the mode embeds, the open parts whose vectors
    this node holds are bundled and announced (`_publish_pass`), one
    collection at a time, each pass bounded by PUBLISH_BUDGET_S and
    resumable. Its own thread: a pass reads parts and
    hashes bundles for minutes, and would idle a pipeline thread or the
    GPU loop (scheduling.md §1). Until 2026-10-19 publishing was the
    `share contribute` command's alone, which needs the collection's
    writer lock — the lock this worker holds for as long as it runs the
    collection — so a node could publish only with the collection paused."""
    while not _stop.wait(PUBLISH_BEAT_S):
        try:
            _publisher_beat()
        except Exception:   # noqa: BLE001 — never the thread
            pass


def _settle_runs_for_search() -> None:
    """03 §3.3: a kept statement is re-checked once per process before the
    vector leg relies on it, and the re-check ran only inside the embed
    path. A collection that holds vectors and does not embed here — every
    peer that imports bundles and spends nothing on embedding — never had
    it, and its vector leg was withheld for the life of the process (the
    node, after taking in the second peer's bundles, 2026-10-07). This
    thread settles the run of every version-3 collection holding vectors
    once, through the same `embed_allowed` the embed path calls (cached,
    locked, logged; never raises), and retries a deferral at its next beat.
    Its own thread: a re-check embeds sixteen texts, and a new run the
    whole set."""
    from . import run_check
    while not _stop.wait(SETTLE_BEAT_S):
        try:
            collections = collections_mod.load_all()
        except Exception:   # noqa: BLE001 — the next beat
            continue
        # the pins of embed stages past their idle boundary, back to their
        # state before; what dead holders left; a failed restore, retried
        # (app/model_pins.py)
        try:
            _release_idle_pins()
            with _pins_lock:
                live = set(_pins)
            model_pins.reconcile(live=live, own_idle_s=PIN_IDLE_S)
        except Exception:   # noqa: BLE001 — never the thread
            pass
        # and each beat, the embedders no held collection names (a removal
        # made while the worker was down, a model loaded by hand)
        try:
            gone = _sweep_unheld_embedders(collections)
            if gone:
                _node_line("control")(f"embedder(s) {', '.join(gone)} unloaded: idle or "
                                      f"named by no held collection, and no collection "
                                      f"that names them embeds everything")
        except Exception:   # noqa: BLE001 — never the thread
            pass
        # A SETTLE EMBEDS (sixteen texts, a whole set for a new run), so it is
        # the EMBED activity of the node's mode, as the embed stage is
        # (app/node_state.py): seed-only or paused, the encoder is not loaded
        # for it (operator, 2026-10-23: the node suspended because the
        # operator needs the GPU). Nor for a collection the node has paused.
        # The vector leg of an unsettled collection waits; search answers
        # from its other legs meanwhile.
        try:
            embeds = node_state.runs(node_state.worker_state(), node_state.EMBED)
            paused = node_state.paused_collections()
        except Exception:   # noqa: BLE001 — unreadable: runs nothing, as everywhere
            embeds, paused = False, set()
        if not embeds:
            continue
        for c in collections:
            if _stop.is_set():
                return
            key = str(getattr(c, "manifest_path", c.name))
            if c.name in paused or key in _settled_for_search \
                    or not run_check.applies(c) or not holds_vectors(c):
                continue
            con = None
            try:
                con = db.connect(c)
                if run_check.embed_allowed(con, c, stop=_stop.is_set):
                    _settled_for_search.add(key)
                    # the settle loaded the embedder; a collection that does
                    # not embed everything does not keep it on the card
                    _release_embedder(c, con, reason="the start-up run check")
            except Exception:   # noqa: BLE001 — one collection, not the thread
                pass
            finally:
                if con is not None:
                    con.close()


def _gossip_beat(collections, con) -> None:
    """04 §4.2: this node's digest once an hour to its neighbours and the
    peer list — and at once to one not yet sent to; events received
    forwarded once (9c); every byte drawn from the UP bucket first, and a
    stop ends the round. `con` is None when the node holds nothing: the
    line goes to stdout alone (there is no event table to keep it in)."""
    def log(m: str) -> None:
        if con is not None:
            db.log_event(con, "info", "share", m[:500])
        else:
            print(f"{time.strftime('%H:%M:%S')} [node info:share] {m[:500]}", flush=True)
    try:
        from app.share import gossip as share_gossip
        share_gossip.beat(collections, stop=_stop, log=log)
    except Exception:   # noqa: BLE001 — telemetry must never kill the worker
        pass


def _stats_publisher() -> None:
    from .acquire import seeding
    con = None
    owner = None
    node_con = None     # state/node.sqlite — this thread's own (SQLite is thread-affine)
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
                # A NODE THAT HOLDS NOTHING STILL SPEAKS: its digest is how a
                # peer learns it exists and where to send its own (04 §4.2).
                # The second peer, fresh, stayed silent here (2026-10-07):
                # nobody heard of it, so nobody's digest named it a swarm.
                _gossip_beat([], None)
                continue
            control = collections[0]          # node control follows sort order
            if owner != control.name:
                if con is not None:
                    con.close()
                con = db.connect(control)
                owner = control.name
            db.publish_worker_stats(con, stats.recorder().snapshot())
            # pinned now, left pinned, failed calls (app/model_pins.py)
            db.publish_model_pins(con, model_pins.counters())
            # SEEDING lives on this thread: it is telemetry-shaped work
            # (start published torrents, gate on the worker state, credit
            # uploaded bytes to the UP bucket, publish the table) and the
            # pipeline threads never touch it — per-collection connections
            # are this thread's own (SQLite is thread-affine).
            for c in collections:
                if c.name not in seed_cons:
                    seed_cons[c.name] = db.connect(c)
            if node_con is None:
                node_con = node_state.connect()
            # A PAUSE IS SEEN WITHIN ONE BEAT, not one main-loop round. The
            # main loop sets `_paused` once per round, and nothing inside a
            # round checks it — an embed stream runs 120 s, a bundle inbox
            # import is unbounded, a local genesis pass holds ~300 s — so a
            # fetch kept downloading for minutes after the operator clicked
            # seed-only (at the incident's 4.4 MB/s, ~1.3 GB per 5 minutes).
            #
            # SET ONLY, NEVER CLEAR. Stopping late is the bug; resuming a
            # round late is harmless. So the main loop stays the sole owner
            # of the resume path — clearing `_paused` and re-arming the
            # fetches a pause aborted — and this thread only makes the stop
            # prompt. It can never make the node do work it was told not to.
            #
            # From state/node.sqlite, where the main loop reads it too
            # (`_control_state_for_beat`) — and so does the seeder's gate
            # below, which used to read `load_all()[0]`'s manifest and so
            # obeyed the Library while the main loop, reading its filtered
            # first collection, did not.
            beat_state = _control_state_for_beat(node_con)
            if beat_state is not None:
                _note_pause_from_state(beat_state)
            try:
                # 04 §4.3: the members of every shared collection's current
                # snapshot, published to the seeder below and announced (9b)
                from app.acquire import transport_torrent as _tt
                from app.share import gossip as share_gossip, seeder as share_seeder
                share_seeder.sync(collections, seed_cons, con,
                                  node_state.worker_state(node_con),
                                  session=_tt.get_session() if _tt._session is not None else None,
                                  port=share_gossip.own_port(),     # [node] address's, as peers reach it
                                  peer_port=share_gossip.peer_port(),   # the swarm's, for its key
                                  log=lambda m: db.log_event(con, "info", "share", m[:500]))
            except Exception:   # noqa: BLE001 — telemetry must never kill the worker
                pass
            _gossip_beat(collections, con)
            seeding.tick(collections, seed_cons, con,
                         node_state.worker_state(node_con))
            db.publish_worker_rates(con, get_manager().rates())
            # `collections`, not `all_collections`: that name is not bound
            # here, so this raised NameError every beat, the except below
            # swallowed it, and the fair-share lines never reached the log
            _log_shares(con, [c.manifest_path for c in collections])
            # an idle parse pool gives its memory back (app/parallel.py
            # release_idle; München 2026-10-28: 10.8 GiB held through a pause)
            released = parallel.release_idle(config.PARSE_IDLE_RELEASE_S)
            if released:
                db.log_event(con, "info", "sched",
                             f"parse pool released: {released} children idle "
                             f"{config.PARSE_IDLE_RELEASE_S:.0f} s; the next parse starts a fresh pool")
        except Exception:   # noqa: BLE001 — telemetry must never kill the worker
            pass
    for c_con in seed_cons.values():
        c_con.close()
    if con is not None:
        con.close()
    if node_con is not None:
        node_con.close()


def _move_node_control(node_con, loaded) -> None:
    """Boot: carry the node's control state out of the collection manifests
    into state/node.sqlite (app/node_state.py). A no-op once moved.

    The manifests are offered in the order the WEB wrote to — `loaded`
    unfiltered, boot refusals included, because the web never knew about
    them — so what moves is the operator's last click. With no collection
    at all the web wrote to the fallback manifest, offered alone. Each is
    opened on its own, and one that cannot be opened is never the boot's
    end — but it is KEPT in the order as a gap (`None`), because its place
    is what tells `adopt_legacy` that a later sibling's copy must not win
    (fail closed)."""
    legacy: list = []
    for c in (loaded or [None]):
        try:
            legacy.append(db.connect(c))
        except Exception as e:   # noqa: BLE001 — see the docstring
            print(f"node control: cannot open {getattr(c, 'name', 'the default manifest')} "
                  f"to move from ({type(e).__name__}: {e})", flush=True)
            legacy.append(None)
    try:
        move = node_state.adopt_legacy(node_con, legacy)
        log = next((con for con in legacy if con is not None), None)

        def _say(level: str, message: str) -> None:
            if log is not None:
                db.log_event(log, level, "worker", message)
            else:
                print(f"[{level}:worker] {message}", flush=True)

        if move.adopted:
            _say("info", "node control moved to state/node.sqlite (07 §7.8): "
                 + ", ".join(f"{k}={v!r}" if k == db.WORKER_STATE_KEY else k
                             for k, v in sorted(move.adopted.items())))
        if move.blocked:
            _say("warn", f"node control NOT moved for {', '.join(move.blocked)}: a "
                 f"manifest ahead of every copy could not be read, and it may hold "
                 f"the operator's last setting. Nothing was deleted; the next boot "
                 f"tries again, and a click in the Library settles it now")
        # A node that has never recorded a state defaults to 'paused'
        # (db.worker_state), which stops every collection. The default is
        # right; being SILENT about it is not — it has twice read as "the
        # node is broken, nothing logs".
        if db.get_control(node_con, db.WORKER_STATE_KEY) is None:
            _say("warn", f"the node has no {db.WORKER_STATE_KEY} in "
                 f"state/node.sqlite, so it is PAUSED by default and will "
                 f"process nothing until one is set (the Library's mode buttons)")
    finally:
        for con in legacy:
            if con is not None:     # a gap: never opened
                con.close()


#: how often the job runner looks for a queued job while idle
JOB_POLL_S = 2.0
#: how long a stopping worker waits for the job in hand: a create's encoder
#: call cannot be cut, and one not done by then is queued again at the next
#: start (app/jobs.py) — the pipeline joins keep the rest of the grace period
JOB_STOP_WAIT_S = 20.0
#: api_commands.KILL_AFTER_S and a margin: the command thread requeues its
#: job after the child has gone, which this waits for.
COMMAND_STOP_WAIT_S = 75.0
#: how often finished jobs are swept
JOB_SWEEP_S = 600.0


def _job_runner() -> None:
    """The work the web process queues because it holds no private key
    (app/jobs.py; operator, 2026-09-29): creating a collection and
    exporting a bundle. Its own thread, not the round loop: a create embeds
    the calibration set, which takes minutes. Deferral is not failure — a
    busy store is waited out; only the job's own refusal fails it."""
    from . import jobs
    con = jobs.connect()
    swept = 0.0
    try:
        while not _stop.is_set():
            try:
                if time.time() - swept > JOB_SWEEP_S:
                    jobs.sweep(con)
                    swept = time.time()
                done = jobs.run_next(con, node_state.worker_state(con),
                                     log=lambda m: print(m, flush=True))
            except sqlite3.Error:
                _stop.wait(JOB_POLL_S)
                continue
            except Exception:                           # noqa: BLE001
                # the thread must outlive what it runs, or no job runs again
                print("[error:jobs] node: " + traceback.format_exc()[:1500], flush=True)
                _stop.wait(JOB_POLL_S)
                continue
            if done is None or done.state == jobs.QUEUED:
                _stop.wait(JOB_POLL_S)
    finally:
        con.close()


def _register_stack_dump() -> bool:
    """SIGUSR1 prints every thread's stack to stderr and the worker goes on
    — the same instrument `louise` (the CLI) carries: a worker at 98 % CPU
    with the GPU idle could not say where it was, and a SIGUSR1 would have
    KILLED it (Perugia, 2026-10-20; no py-spy in the image). `chain=False`:
    chained, the default action ran after the dump and terminated the
    process (exit 138; Perugia, 2026-10-19). Never a reason not to start."""
    if not hasattr(signal, "SIGUSR1"):
        return False
    try:
        import faulthandler
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        return True
    except (ValueError, OSError, AttributeError, RuntimeError):
        return False


@_ends_stop_watch
def run_forever():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    # a hangup too: rsync leads its own session (transport_rsync), so a
    # worker run from a terminal that goes away would otherwise die by the
    # default SIGHUP and leave rsync writing with nobody to stop it
    signal.signal(signal.SIGHUP, _handle_sigterm)
    _register_stack_dump()
    _start_stop_watch()
    from . import jobs, run_check
    all_collections = collections_mod.load_all()
    # the record of which kept statements THIS start has re-checked, which
    # the web process reads before it relies on one (run_check); emptied
    # now, before any stage runs — and after the first read of the node's
    # files, so a start that fails there writes nothing
    run_check.publish_rechecks()
    # Before anything reads it: the node state leaves the manifests for
    # state/node.sqlite, from the UNFILTERED list the web wrote to.
    node_con = node_state.connect()
    _move_node_control(node_con, all_collections)
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
        db.drop_catalog_fts(cons[c.name])
        _apply_tier2_embed_mode(cons[c.name], c, "restart")
        if c.embed is None:
            db.log_event(cons[c.name], "info", "worker",
                         f"no [encoder] in collections/{c.name}.toml — name "
                         f"one there; `louise hold config show {c.name}` prints "
                         f"what the node resolves")
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
    # This process's model requests are background work unless they say
    # otherwise: a sample proof or a check stands aside for a person too
    # (docs/notes/scheduling.md §12).
    dispatch.set_default_level(Level.CORPUS)
    threading.Thread(target=_stats_publisher, name="stats-publisher",
                     daemon=True).start()
    # the vector index maintainer: its own thread, so an hour-long build or
    # a compaction never idles a pipeline thread or the GPU loop
    threading.Thread(target=_index_maintainer, name="index-maintainer",
                     daemon=True).start()
    # the work the web queues because it holds no private key (app/jobs.py);
    # what a stopped worker left running is queued again first
    requeued = jobs.requeue_running(node_con)
    if requeued:
        print(f"[info:jobs] node: job(s) {requeued} interrupted by the last stop "
              f"are queued again", flush=True)
    # deposit writes queued by the LocalAGI routes (app/jobs.py DEPOSIT): their
    # own thread, so an agent's memory never waits behind a create
    from . import jobs as jobs_mod
    threading.Thread(target=jobs_mod.serve_deposits, args=(_stop,),
                     kwargs={"log": lambda m: print(m, flush=True)},
                     name="deposits", daemon=True).start()
    # any command POSTed to its route (07 §7.4, app/api_commands.py): one at
    # a time, on a thread of its own — a records build must not hold a create
    command_thread = threading.Thread(target=jobs_mod.serve_commands, args=(_stop,),
                                      kwargs={"log": lambda m: print(m, flush=True)},
                                      name="commands", daemon=True)
    command_thread.start()
    # a deposit collection's URL sources (06 §6.7): their own thread, since a
    # sitemap is many pages and a clone takes minutes
    from .hold import sources as sources_mod
    threading.Thread(target=sources_mod.serve, args=(_stop,),
                     kwargs={"log": lambda m: print(m, flush=True)},
                     name="sources", daemon=True).start()
    # the members a collection's file asks for and the SHARD lacks, by
    # torrent from the peers that serve them (04 §4.3; app/share/transport.py):
    # its own thread, since a metadata wait is bounded by minutes
    # the pins a killed worker left are restored before anything embeds, and
    # what was found is said (app/model_pins.py; bounded by its timeouts)
    model_pins.set_worker()
    try:
        model_pins.reconcile(boot=True)
    except Exception:   # noqa: BLE001 — a safeguard, never a boot blocker
        pass
    # the runs of collections that hold vectors and do not embed here,
    # settled for search (03 §3.3; `_settle_runs_for_search`)
    threading.Thread(target=_settle_runs_for_search, name="settle-runs", daemon=True).start()
    # 07 §7.8 "all p2p", the publishing half: the open parts whose vectors
    # this node holds, bundled and announced as the embed stage finishes
    # them (`_publisher`); its own thread, a pass is minutes of IO and hashing
    threading.Thread(target=_publisher, name="publisher", daemon=True).start()
    # 03 §3.2: the plan, once per cadence per collection, in a child process,
    # one at a time (app/scheduled_checks.py)
    from . import scheduled_checks
    threading.Thread(target=scheduled_checks.loop, args=(_stop, _node_line("check")),
                     name="scheduled-checks", daemon=True).start()
    from .share import transport as share_transport
    threading.Thread(target=share_transport.loop, args=(_stop,),
                     kwargs={"log": lambda m: print(f"{time.strftime('%H:%M:%S')} [node info:share] {m}", flush=True),
                             # record shards promoted: a plain scan, at once (9f)
                             "on_promoted": lambda name: _request_discovery(name, "members promoted",
                                                                            acquire=False)},
                     name="share-fetch", daemon=True).start()
    job_thread = threading.Thread(target=_job_runner, name="jobs", daemon=True)
    job_thread.start()
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
    # the fallback control manifest until a TOML appears (hot-reloaded).
    # That manifest carries only telemetry and this loop's event log now —
    # the node's STATE is in state/node.sqlite (`node_con`).
    control_con = cons[all_collections[0].name] if all_collections else db.connect()
    _main_loop_live.set()

    while not _stop.is_set():
        try:
            all_collections = _sync_collections(all_collections, cons, last_discover,
                                                 pipeline=pipeline)
            # 07 §7.7: the lock of every collection the node runs; one the operator
            # paused or another writer holds is not run this round
            active = _hold_locks(all_collections,
                                 node_state.paused_collections(node_con), cons)
            _finish_completed_acquires(cons, all_collections, last_discover,
                                        pipeline=pipeline)
            if not all_collections:
                time.sleep(config.WORKER_IDLE_SLEEP)
                continue
            # re-resolved each round: the previous first collection may have
            # been removed mid-run. Telemetry and the log only — nothing a
            # new first collection has to inherit.
            control_con = cons[all_collections[0].name]
            # Tier-3 "boost bandwidth tonight" reaches the byte-moving process
            # here: the web process writes it to state/node.sqlite, the worker
            # applies it. Re-read each round so it takes effect within one
            # document and expires on its own.
            get_manager().set_override(node_state.bandwidth_override(node_con))
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
            state = node_state.worker_state(node_con)
            _say_mode(state)
            # WHAT A MODE RUNS is one table (app/node_state.py: MODE_RUNS;
            # operator, 2026-09-19), read here and by every other gate.
            if not node_state.runs(state, node_state.FETCH):
                # paused, seed-only: nothing is fetched, parsed or indexed.
                # What seed-only keeps alive — the seeder, on the stats
                # beat — does not live in this loop, so the gating here is
                # the same for both.
                _paused.set()       # pipeline threads idle at the same gate
                time.sleep(2)
                continue
            _paused.clear()
            # Re-arm the fetches a pause stopped, BEFORE the interval check
            # below — without pipeline threads, that check is what runs them.
            _rearm_paused_aborts(active, last_discover, pipeline, cons=cons)
            # download/seeding only: fetching continues — the pipeline
            # threads still drain discovery requests, which is what starts a
            # fetch — and NOTHING reads what lands: the scan is owed
            # (`_scan_only`), rdf/fts stand down on their threads, and this
            # loop skips embed/genesis/bundles below
            if node_state.runs(state, node_state.PARSE):
                _acquire_only.clear()
                # what landed, or was cut short, while nothing was parsed
                owed = _take_owed_scans()
                for collection in active:
                    if collection.name not in owed:
                        continue
                    if pipeline:
                        _request_discovery(collection.name,
                                           "landed while the node parsed nothing",
                                           acquire=False)
                    else:
                        _scan_only(cons[collection.name], collection,
                                   "landed while the node parsed nothing")
                        last_discover[collection.name] = time.time()
            else:
                _acquire_only.set()
            if pipeline:
                # after the pause gate: a paused worker starts no threads,
                # and running ones idle on _paused
                _sync_pipeline_threads(active)
            any_work = False
            for collection in active:
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
            if producers_enabled(pipeline):
                # every collection's encoder is fed by its own producer
                # (docs/notes/scheduling.md §12); this loop only keeps them
                # matched to the collection set. They stand down on their
                # own in acquire-only and while paused.
                _sync_embed_producers(active)
                time.sleep(config.WORKER_IDLE_SLEEP)
                continue
            if node_state.runs(state, node_state.INDEX):
                servable = servable_collections(active, pipeline)
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
    _main_loop_live.clear()         # the beat stands down with the loop
    job_thread.join(timeout=JOB_STOP_WAIT_S)
    if job_thread.is_alive():
        print("[info:jobs] node: the job in hand did not finish before the stop; "
              "it is queued again at the next start", flush=True)
    # a command in hand was sent SIGTERM by its thread; it finishes its unit
    # or is killed (api_commands.KILL_AFTER_S), and is queued again either way
    command_thread.join(timeout=COMMAND_STOP_WAIT_S)
    _join_pipeline_threads()
    _release_locks()                     # the node holds a lock only while it runs
    parallel.shutdown()             # the parse pool's children go with the worker
    for c in all_collections:
        db.log_event(cons[c.name], "info", "worker", "worker stopped (signal)")
    node_con.close()


def _warn_if_pid1() -> None:
    """PID 1 must reap orphans, and Python does not. rsync forks a
    receiver; when rsync exits first (a SIGKILL, some error exits) the
    receiver is re-parented to PID 1 and stays a zombie — observed live
    2026-09-13, 25 in three hours, all under this process. Reaping them
    here would race subprocess for its own children's exit status, and a
    stolen status reads as exit 0: a failed rsync's staging file moved into
    place as complete. So: say it, and let the container's init reap."""
    if os.getpid() == 1:
        print("WARNING: the worker is PID 1 with no init — rsync's orphaned "
              "children will never be reaped. Set `init: true` on the service "
              "(deploy/truenas-app.yaml).", file=sys.stderr, flush=True)


def main() -> int:
    """`python -m app.worker`. docs/07 §7.7: the node "stops every
    collection at its unit and exits 130 on a signal" — invariant 2 says
    the same — and it exited 0. `_stop` is set by the signal handler, and
    by `node stop`'s request, which exits 0 (`_stop_requested`)."""
    _warn_if_pid1()
    # the public key the web process recognises must be this key's half
    # (app/identity.py): a node whose two processes disagree about who it is
    # refuses to start, with the reason
    from . import identity
    try:
        identity.check_at_start()
    except identity.IdentityError as e:
        print(f"REFUSED TO START: {e}", file=sys.stderr, flush=True)
        return 2
    run_forever()
    return exit_code()


def exit_code() -> int:
    """0 for `node stop` or a run that ended by itself, 130 for a signal (07 §7.7)."""
    if _stop_requested.is_set():
        return 0
    return 130 if _stop.is_set() else 0



# ---- forget (07 §7.3) -----------------------------------------------------
def retire_collection(name: str, timeout: float = 60.0) -> None:
    """`hold collection forget`, run by this worker's job thread (app/jobs.py,
    the DEPOSIT kind's op "forget"): the collection's threads told to stop
    and waited for, bounded — a pipeline thread finishes its document first
    (invariant 2) — so nothing of this process writes under a directory
    about to go. The held file is already gone, so no round starts them
    again. Nothing to do for a collection this worker never ran."""
    deadline = time.time() + timeout
    threads: list[threading.Thread] = []
    pipe = _pipeline_threads.pop(name, None)
    if pipe is not None:
        pipe[1].set()
        threads.append(pipe[0])
    producer = _embed_producers.pop(name, None)
    if producer is not None:
        producer[1].set()
        threads.append(producer[0])
    _gone_event(name).set()             # scans and fetches stop at their next unit
    with _scan_lock:
        scan = _scan_threads.get(name)
    with _acquire_lock:
        fetch = _acquire_threads.get(name)
    threads += [t for t in (scan, fetch) if t is not None]
    for thread in threads:
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.time()))
    # its catalog writer too, held by path: the directory goes, and a
    # collection made again under this name would inherit a writer on
    # deleted files (`catalog.release_under`)
    try:
        from . import catalog
        catalog.release_under(collections_mod.louise_root() / "collections" / name)
    except Exception:   # noqa: BLE001 — the forget goes on; the next start holds nothing
        pass
    _gone.pop(name, None)
    lock = _locks.pop(name, None)       # the node holds it only while it runs it
    if lock is not None:
        lock.release()


# ---- the one writer per collection (07 §7.7) -------------------------------
#: the write lock of every collection this worker runs, by name
_locks: dict[str, object] = {}
_lock_nagged: dict[str, str] = {}


def _hold_locks(collections, paused: set[str], cons=None) -> list:
    """Each round: the lock of every collection the node runs, taken once
    and held for as long as it runs it (07 §7.7); released — after the
    collection's threads have finished their unit — for one the operator
    paused (`node pause <collection>`) or one no longer held. A collection
    another writer holds (a `hold` command) is left to it: not run this
    round, said once. Returns the collections this round runs."""
    from app.hold import lock as lock_mod
    live = {c.name: c for c in collections}
    for name in list(_locks):
        if name not in live or name in paused:
            retire_collection(name)          # the end of its current unit first
            lock = _locks.pop(name, None)
            if lock is not None:
                lock.release()
    for name, c in live.items():
        if name in paused or name in _locks:
            continue
        try:
            _locks[name] = lock_mod.take(Path(c.manifest_path).parent, name,
                                         "the node (worker)")
            _lock_nagged.pop(name, None)
        except lock_mod.Held as e:
            if _lock_nagged.get(name) != str(e) and cons and name in cons:
                _lock_nagged[name] = str(e)
                db.log_event(cons[name], "info", "worker",
                             f"collection {name!r} not run this round: {e}"[:500])
    return [c for c in collections if c.name in _locks]


def _release_locks() -> None:
    for name in list(_locks):
        _locks.pop(name).release()

if __name__ == "__main__":
    from . import process_exit
    process_exit.run(main)
