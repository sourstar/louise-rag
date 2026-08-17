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
import signal
import threading
import time
import traceback
from pathlib import Path

from . import chunking, collections as collections_mod, config, db, discovery, embeddings, entities, fts, plugin_io, refs, spaces
from .acquire import engine as acquire_engine
from .acquire.spec import DiskPreflightError, LicenseAckRequiredError
from .acquire.specs import ACQUIRE_REGISTRY
from .bundles import importer as bundle_importer
from .bundles import ledger as bundle_ledger
from .verify import backend_gate
from .transfer.manager import get_manager

# A threading.Event rather than a bool, because it now has to be *observed* by
# acquisition worker threads parked inside the bandwidth bucket. Before P0.7
# a SIGTERM during a fetch was noticed only after the whole acquisition
# returned, which on a large corpus could be many minutes.
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
    native_id = int(row["native_id"])
    chapters = plugin_io.sections(plugin, row)
    return list(chunking.chunk_book(row["uid"], native_id, chapters))

def stage_rdf(con, row, collection) -> None:
    plugin = collection.plugin_instance()
    meta = plugin_io.metadata(plugin, row)
    con.execute(
        "UPDATE documents SET title=?,authors=?,lang=?,subjects=?,bookshelves=?,"
        "rdf_done=1,updated_at=? WHERE uid=?",
        (meta["title"], meta["authors"], meta["lang"], meta["subjects"],
         meta["bookshelves"], int(time.time()), row["uid"]))
    con.execute("DELETE FROM catalog_fts WHERE uid=?", (row["uid"],))
    con.execute(
        "INSERT INTO catalog_fts(uid,title,authors,subjects,bookshelves) "
        "VALUES(?,?,?,?,?)",
        (row["uid"], meta["title"], meta["authors"], meta["subjects"], meta["bookshelves"]))

def stage_fts(con, row, collection) -> None:
    chunks = _load_chunks(row, collection)
    fts.index_book(row["lang"], int(row["native_id"]), chunks, collection)
    ref_store = refs.get_store()
    entities.tag_document(con, row["uid"], row["title"], [c["text"] for c in chunks],
                           refs.RESOLVER, ref_store)
    con.execute(
        "UPDATE documents SET fts_done=1,fts_version=?,entity_version=?,chunks=?,tokens=?,"
        "updated_at=? WHERE uid=?",
        (config.FTS_VERSION, config.ENTITY_VERSION, len(chunks),
         sum(c["tokens"] for c in chunks), int(time.time()), row["uid"]))

def stage_embed(con, row, collection) -> None:
    chunks = _load_chunks(row, collection)
    # lock_con serializes the delete+add pair against a concurrent bundle
    # import in another process (app/bundles/writer_lock.py). Passing it here
    # is what makes the importer's lock mean anything.
    n = embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks, collection,
                                lock_con=con)
    bundle_ledger.clear_verification(con, row["uid"])   # no longer imported provenance
    con.execute(
        "UPDATE documents SET embed_done=1,embed_version=?,updated_at=? WHERE uid=?",
        (config.EMBED_VERSION, int(time.time()), row["uid"]))
    db.log_event(con, "info", "embed", f"embedded {n} chunks", row["uid"])

def _next_book(con, scope, embed_allowed=True):
    # 1. priority embeds (lazy queue), regardless of backfill mode
    if embed_allowed and scope["embed_mode"] in ("lazy", "all"):
        r = con.execute("""
            SELECT d.* FROM embed_queue q JOIN documents d ON d.uid=q.uid
            WHERE d.in_scope=1 AND d.rdf_done=1
              AND (d.embed_done=0 OR d.embed_version < ?)
            ORDER BY q.priority DESC, q.ts LIMIT 1""",
            (config.EMBED_VERSION,)).fetchone()
        if r:
            return "embed", r
        con.execute("DELETE FROM embed_queue WHERE uid NOT IN "
                    "(SELECT uid FROM documents WHERE embed_done=0)")
    # 2. metadata
    r = con.execute(
        "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=0 AND error IS NULL "
        "AND retries<? ORDER BY CAST(native_id AS INTEGER) LIMIT 1", (config.MAX_RETRIES,)).fetchone()
    if r:
        return "rdf", r
    # 3. lexical index (+ entity tagging, gated by its own version so it can
    # be forced to re-run independently of FTS_VERSION)
    r = con.execute(
        "SELECT * FROM documents WHERE in_scope=1 AND rdf_done=1 AND error IS NULL "
        "AND retries<? AND (fts_done=0 OR fts_version<? OR entity_version<?) "
        "ORDER BY CAST(native_id AS INTEGER) LIMIT 1",
        (config.MAX_RETRIES, config.FTS_VERSION, config.ENTITY_VERSION)).fetchone()
    if r:
        return "fts", r
    # 4. dense backfill only in 'all' mode (non-English first: rarest knowledge)
    if embed_allowed and scope["embed_mode"] == "all":
        r = con.execute(
            "SELECT * FROM documents WHERE in_scope=1 AND fts_done=1 AND error IS NULL "
            "AND retries<? AND (embed_done=0 OR embed_version<?) "
            "ORDER BY (lang='en'), CAST(native_id AS INTEGER) LIMIT 1",
            (config.MAX_RETRIES, config.EMBED_VERSION)).fetchone()
        if r:
            return "embed", r
    return None, None

STAGES = {"rdf": stage_rdf, "fts": stage_fts, "embed": stage_embed}

def _acquire(con, collection, reason: str) -> None:
    """Runs the acquisition engine (app/acquire/) before each discover pass,
    when a collection has opted in. Off by default (Collection.acquire_enabled
    defaults False) so an existing hand-populated deployment sees zero
    behavior change and no surprise outbound network access on restart."""
    if not collection.acquire_enabled:
        return
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        return
    profile = collection.profile or "demo"
    try:
        result = acquire_engine.run_acquisition(con, collection, spec, profile, stop=_stop)
        db.log_event(con, "info", "acquire", f"corpus fetch ({reason}): {result}")
    except (LicenseAckRequiredError, DiskPreflightError) as e:
        db.log_event(con, "error", "acquire", str(e))

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


def _discover(con, collection, reason: str) -> None:
    _acquire(con, collection, reason)
    db.log_event(con, "info", "discover", f"corpus scan ({reason})")
    stats = discovery.sync_collection(con, collection)
    if stats.get("added") or stats.get("changed"):
        db.log_event(con, "info", "discover",
                     f"picked up {stats.get('added',0)} new, "
                     f"{stats.get('changed',0)} changed books")

def _run_one_step(con, collection, last_idle_log: dict) -> bool:
    """Runs at most one document's worth of work for this collection.
    Returns True if work was done (so the caller can skip the idle sleep)."""
    # Imported vectors are cheaper than computed ones, so drain the inbox
    # before looking for local work: a bundle may satisfy the very document
    # the embed stage was about to pick up.
    if _drain_bundle_inbox(con, collection):
        return True
    scope = db.get_scope(con)
    stage, row = _next_book(con, scope)
    if stage == "embed" and not backend_gate.embed_allowed(con, collection):
        # The endpoint failed its behavioral baseline (or is down). Embedding
        # pauses — the document is untouched, never errored — while rdf/fts
        # work continues. embed_allowed() logged the transition and caches the
        # verdict, so this costs nothing per round until the TTL expires.
        stage, row = _next_book(con, scope, embed_allowed=False)
    if row is None:
        now = time.time()
        if now - last_idle_log.get(collection.name, 0.0) > 300:
            db.log_event(con, "info", "worker", "idle — pipeline is caught up")
            last_idle_log[collection.name] = now
        return False
    t0 = time.time()
    con.execute("BEGIN")
    try:
        STAGES[stage](con, row, collection)
        if stage == "embed":
            con.execute("DELETE FROM embed_queue WHERE uid=?", (row["uid"],))
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        con.execute(
            "UPDATE documents SET retries=retries+1, error=?, updated_at=? WHERE uid=?",
            (f"{stage}: {e}"[:500], int(time.time()), row["uid"]))
        db.log_event(con, "error", stage, f"{type(e).__name__}: {e}", row["uid"])
        return True   # an attempted (if failed) step still counts as "did work" this round
    dt = round(time.time() - t0, 2)
    if stage != "rdf" or int(row["native_id"]) % 500 == 0:
        db.log_event(con, "info", stage, f"done in {dt}s", row["uid"])
    return True

def _sync_collections(all_collections, cons, last_discover):
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

    # A config change that alters what the worker should DO with a
    # collection (license acknowledged, acquisition toggled, profile grown)
    # triggers an immediate discover+acquire pass — otherwise an ack
    # recorded via the UI would sit inert until the next interval scan.
    for kept in current:
        prev = previous_by_name.get(kept.name)
        if prev is None or kept.name not in cons:
            continue
        if (prev.acquire_enabled, prev.license_ack, prev.profile) != \
                (kept.acquire_enabled, kept.license_ack, kept.profile):
            db.log_event(cons[kept.name], "info", "worker",
                         f"collection {kept.name!r} config changed "
                         f"(acquire/license/profile) — re-discovering")
            _discover(cons[kept.name], kept, "config-changed")
            last_discover[kept.name] = time.time()

    for added in [c for c in current if c.name not in known]:
        try:
            spaces.validate_boot([added])
        except spaces.BootValidationError as e:
            if cons:
                db.log_event(next(iter(cons.values())), "error", "worker",
                             f"new collection {added.name!r} failed boot "
                             f"validation — skipped until fixed: {e}"[:500])
            continue
        cons[added.name] = db.connect(added)
        db.log_event(cons[added.name], "info", "worker",
                     f"collection {added.name!r} added — discovering")
        stale = bundle_ledger.sweep_stale(cons[added.name])
        if stale:
            db.log_event(cons[added.name], "warn", "bundle",
                         f"{len(stale)} stale bundle import(s) swept")
        _discover(cons[added.name], added, "added")
        last_discover[added.name] = time.time()

    for gone in sorted(known - now_names):
        db.log_event(cons[gone], "info", "worker",
                     f"collection {gone!r} removed — ingestion stopped, data kept")
        cons[gone].close()
        del cons[gone]
        last_discover.pop(gone, None)

    return [c for c in current if c.name in cons]


def run_forever():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    all_collections = collections_mod.load_all()
    spaces.validate_boot(all_collections)

    cons = {c.name: db.connect(c) for c in all_collections}

    for c in all_collections:
        db.log_event(cons[c.name], "info", "worker", "worker started")
        # An import interrupted by a previous kill left a row mid-flight. It
        # needs no compensation — the importer clears embed_done before
        # touching Lance, so _next_book re-queues the document — but the row
        # must not stay 'applying' forever.
        stale = bundle_ledger.sweep_stale(cons[c.name])
        if stale:
            db.log_event(cons[c.name], "warn", "bundle",
                         f"{len(stale)} bundle import(s) interrupted by a previous "
                         f"shutdown; affected documents will be re-embedded")
    for c in all_collections:
        _discover(cons[c.name], c, "startup")   # restart = new documents are considered

    last_discover = {c.name: time.time() for c in all_collections}
    last_idle_log: dict[str, float] = {}
    control_con = cons[all_collections[0].name]

    while not _stop.is_set():
        try:
            all_collections = _sync_collections(all_collections, cons, last_discover)
            if not all_collections:
                time.sleep(config.WORKER_IDLE_SLEEP)
                continue
            # re-resolved each round: the previous control collection may
            # have been removed mid-run
            control_con = cons[all_collections[0].name]
            # Tier-3 "boost bandwidth tonight" reaches the byte-moving process
            # here: the web process writes it to the control table, the worker
            # applies it. Re-read each round so it takes effect within one
            # document and expires on its own.
            get_manager().set_override(db.get_bandwidth_override(control_con))
            if db.worker_state(control_con) != "running":
                time.sleep(2)
                continue
            any_work = False
            for collection in all_collections:
                if _stop.is_set():
                    break
                con = cons[collection.name]
                if (config.DISCOVER_INTERVAL_MIN > 0 and
                        time.time() - last_discover[collection.name] > config.DISCOVER_INTERVAL_MIN * 60):
                    _discover(con, collection, "interval")
                    last_discover[collection.name] = time.time()
                # Time-budgeted round: each collection works until the budget
                # elapses, not exactly one document. One-doc rounds starved
                # cheap stages behind expensive neighbors (observed live: a
                # 0.1s pubmed rdf step per 30-90s gutenberg embed = 10 days
                # for 30k articles). An expensive single step still exceeds
                # the budget after one iteration, so fairness is unchanged
                # where it mattered; the stop event is honored per step.
                round_deadline = time.time() + config.WORKER_ROUND_BUDGET_S
                while not _stop.is_set():
                    if not _run_one_step(con, collection, last_idle_log):
                        break
                    any_work = True
                    if time.time() >= round_deadline:
                        break
            if not any_work:
                time.sleep(config.WORKER_IDLE_SLEEP)
        except Exception:
            try:
                db.log_event(control_con, "error", "worker", traceback.format_exc()[:1500])
            except Exception:
                # control_con may point at a just-removed collection — the
                # loop must survive even when its logger is gone
                print(traceback.format_exc()[:1500], flush=True)
            time.sleep(5)
    for c in all_collections:
        db.log_event(cons[c.name], "info", "worker", "worker stopped (signal)")

if __name__ == "__main__":
    run_forever()
