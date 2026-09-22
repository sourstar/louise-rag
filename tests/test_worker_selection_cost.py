"""Selection-query cost and correctness for the worker's hot path.

Three defects lived here at once, all found on the live node against pubmed
(33.4M documents), and all three cost the same thing: a collection whose
pipeline made no progress at all.

1. The lazy-queue selection had no `error IS NULL` / `retries<MAX` filter,
   unlike the rdf and fts selections beside it. Because the queue is
   consulted BEFORE rdf/fts, a single queued document that could not be
   embedded was re-picked every round forever and starved the collection.
2. The queue cleanup used `uid NOT IN (SELECT uid FROM documents ...)`,
   which materialises every not-yet-embedded uid as a list to test a handful
   of queue rows: 22.3s to delete zero of 17.
3. The rdf/fts selections ended `ORDER BY CAST(native_id AS INTEGER)`. The
   CAST makes every index unusable, so SQLite built a TEMP B-TREE over the
   whole candidate set to return one row: 10.0s, against 0.000s without it.

The plan assertions below are the regression guard for 2 and 3 — behaviour
alone cannot catch a query that is merely slow.
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, config, db, discovery, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _discovered(tmp_path, name="alpha"):
    corpus = tmp_path / f"corpus_{name}"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def _uids(con, n):
    rows = con.execute("SELECT uid FROM documents LIMIT ?", (n,)).fetchall()
    assert len(rows) >= n, "fixture needs more documents"
    return [r["uid"] for r in rows]


def _enqueue(con, uid, priority=100):
    con.execute("INSERT OR REPLACE INTO embed_queue(uid,priority,ts) VALUES(?,?,0)",
                (uid, priority))
    con.commit()


def _plan(con, sql, params):
    return " ".join(str(r[-1]) for r in
                    con.execute("EXPLAIN QUERY PLAN " + sql, params))


# --- 1. an unservable queued document must not starve the collection -------

def test_errored_queued_document_does_not_starve_rdf(tmp_path):
    c, con = _discovered(tmp_path)
    stuck, _other = _uids(con, 2)
    # rdf_done so it reaches the queue selection at all, but permanently
    # unservable: exactly the shape that froze pubmed for hours.
    con.execute("UPDATE documents SET rdf_done=1, error='boom' WHERE uid=?", (stuck,))
    con.commit()
    _enqueue(con, stuck)

    scope = db.get_scope(con)                    # default embed_mode=lazy
    stage, row = worker._next_book(con, scope)
    assert stage == "rdf", (
        f"an unservable queued document must be skipped, not re-picked forever "
        f"(got {stage} {row['uid'] if row is not None else None})")
    assert row["uid"] != stuck


def test_retry_exhausted_queued_document_does_not_starve_rdf(tmp_path):
    c, con = _discovered(tmp_path)
    stuck, _other = _uids(con, 2)
    con.execute("UPDATE documents SET rdf_done=1, retries=? WHERE uid=?",
                (config.MAX_RETRIES, stuck))
    con.commit()
    _enqueue(con, stuck)

    stage, row = worker._next_book(con, db.get_scope(con))
    assert stage == "rdf" and row["uid"] != stuck


def test_healthy_queued_document_is_still_served_first(tmp_path):
    """The filters must not break the feature: a servable queued document
    still outranks rdf work, which is the whole point of the lazy queue."""
    c, con = _discovered(tmp_path)
    ready, _other = _uids(con, 2)
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (ready,))
    con.commit()
    _enqueue(con, ready)

    stage, row = worker._next_book(con, db.get_scope(con))
    assert stage == "embed" and row["uid"] == ready


def test_embed_candidates_applies_the_same_filters(tmp_path):
    """_embed_parallel selects through _embed_candidates, so the CPU-encoder
    path must not resurrect the document the serial path just excluded."""
    c, con = _discovered(tmp_path)
    stuck, ready = _uids(con, 2)
    con.execute("UPDATE documents SET rdf_done=1, error='boom' WHERE uid=?", (stuck,))
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (ready,))
    con.commit()
    _enqueue(con, stuck)
    _enqueue(con, ready)

    got = {r["uid"] for r in worker._embed_candidates(con, db.get_scope(con), 8)}
    assert ready in got
    assert stuck not in got


# --- 2. the cleanup retires what it can no longer serve --------------------

def test_queue_cleanup_retires_unservable_rows_and_keeps_pending_ones(tmp_path):
    c, con = _discovered(tmp_path)
    pending, embedded, errored = _uids(con, 3)
    # still waiting for rdf: legitimately queued, must survive the cleanup
    con.execute("UPDATE documents SET rdf_done=0, embed_done=0 WHERE uid=?", (pending,))
    con.execute("UPDATE documents SET embed_done=1 WHERE uid=?", (embedded,))
    con.execute("UPDATE documents SET error='boom' WHERE uid=?", (errored,))
    con.commit()
    for uid in (pending, embedded, errored):
        _enqueue(con, uid)

    con.execute(worker._QUEUE_CLEANUP, (config.MAX_RETRIES,))
    con.commit()

    left = {r["uid"] for r in con.execute("SELECT uid FROM embed_queue")}
    assert left == {pending}, (
        "cleanup must retire embedded and unservable rows while keeping a "
        f"document that has simply not reached rdf yet (left={left})")


def test_queue_cleanup_survives_a_uid_with_no_document(tmp_path):
    """A queue row whose document was removed by a scope change must be
    retired, not kept forever. NOT IN would return NULL-ish here; NOT EXISTS
    is unambiguous."""
    c, con = _discovered(tmp_path)
    _enqueue(con, "gutenberg:does-not-exist")
    con.execute(worker._QUEUE_CLEANUP, (config.MAX_RETRIES,))
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM embed_queue").fetchone()[0] == 0


# --- 3. plan guards: the reason these were slow, not merely wrong ----------

def test_hot_path_selections_never_sort_the_candidate_set(tmp_path):
    """`ORDER BY CAST(native_id AS INTEGER)` forced a TEMP B-TREE over every
    candidate row to return one. Nothing in the rdf/fts selections may sort."""
    c, con = _discovered(tmp_path)
    for sql, params in ((worker._RDF_PENDING, (config.MAX_RETRIES,)),
                        (worker._FTS_FRESH, (config.MAX_RETRIES,)),
                        (worker._FTS_REINDEX, (config.MAX_RETRIES,
                                               config.FTS_VERSION,
                                               config.ENTITY_VERSION))):
        plan = _plan(con, sql, params)
        assert "TEMP B-TREE" not in plan.upper(), \
            f"selection sorts the candidate set: {plan}"


def test_fresh_fts_selection_uses_the_scope_index(tmp_path):
    """The per-round fts check must be an index prefix on
    (in_scope, rdf_done, fts_done), not a scan filtered afterwards — it now
    runs before rdf on every single round."""
    c, con = _discovered(tmp_path)
    plan = _plan(con, worker._FTS_FRESH, (config.MAX_RETRIES,))
    assert "idx_documents_scope" in plan, plan


def test_queue_cleanup_does_not_materialise_every_uid(tmp_path):
    """`uid NOT IN (SELECT uid FROM documents ...)` plans as a LIST SUBQUERY:
    every not-yet-embedded uid built into a list to test a few queue rows.
    The correlated NOT EXISTS form probes the documents primary key instead."""
    c, con = _discovered(tmp_path)
    plan = _plan(con, worker._QUEUE_CLEANUP, (config.MAX_RETRIES,)).upper()
    assert "LIST SUBQUERY" not in plan, \
        f"cleanup materialises the whole uid set: {plan}"


# --- 4. the queue selection must scan the QUEUE, never the documents -------

def test_queue_eligible_scans_the_queue_not_the_documents(tmp_path):
    """Found live on pubmed at 3.6M rdf-done rows: the planner inverted the
    join — SEARCH documents (in_scope, rdf_done) probing the queue per row
    plus a TEMP B-TREE — costing 4.9 s at the top of EVERY _next_book, and
    growing with rdf progress, to serve a queue that is usually empty. The
    CROSS JOIN pins queue-first: one tiny scan plus one uid probe per row.
    Behaviour alone cannot catch this — the query returns the same rows
    either way."""
    c, con = _discovered(tmp_path)
    uid, _ = _uids(con, 2)
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (uid,))
    con.commit()
    _enqueue(con, uid)

    plan = _plan(con, worker._QUEUE_ELIGIBLE,
                 (config.MAX_RETRIES, config.EMBED_VERSION, 1))
    d_at = max(plan.find("SEARCH documents"), plan.find("SCAN documents"),
               plan.find("SEARCH d"), plan.find("SCAN d"))
    q_at = max(plan.find("SCAN embed_queue"), plan.find("SEARCH embed_queue"),
               plan.find("SCAN q"), plan.find("SEARCH q"))
    assert q_at != -1 and d_at != -1, plan
    assert q_at < d_at, f"documents must be probed FROM the queue, not scanned: {plan}"
    # and the documents side must be a uid probe, never an index-range scan
    assert "SEARCH d USING INDEX sqlite_autoindex_documents_1 (uid=?)" in plan \
        or "uid=?" in plan, plan

    # correctness unchanged: the queued document is still served first
    scope = db.get_scope(con)
    scope["embed_mode"] = "lazy"
    stage, row = worker._next_book(con, scope)
    assert stage == "embed" and row["uid"] == uid


# --- 5. amendment 38: coverage-aware dense backfill --------------------------

def _archive_collection(tmp_path, monkeypatch):
    """A gutenberg-mini collection that DECLARES a remote archive covering
    native id 10001 (shard 0 = ids 0..10001); the fixture's 10002 and 10003 lie beyond."""
    from dataclasses import replace
    from app.bundles import genesis
    c, con = _discovered(tmp_path)
    archive = genesis.GenesisArchive(
        archive_id="test-arc", base_url="http://arc", adapter="medcpt-npy",
        shards=("0",), uid_prefix="gutenberg:", ids_per_shard=10002)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, entity_version=?",
                (config.FTS_VERSION, config.ENTITY_VERSION))
    con.commit()
    worker.reset_backfill_cursors()
    return replace(c, genesis_archive="test-arc"), con, archive


def test_backfill_under_all_skips_what_the_archive_can_still_cover(tmp_path, monkeypatch):
    """Policy 2 of the operator's scheduling: under "all" the local encoder
    embeds only the corpus the pre-embedded data does NOT cover. Coverage
    is the declared id range of every shard that is not final; a
    finalised shard releases the documents it could not cover."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    ids = sorted(int(r["native_id"]) for r in con.execute("SELECT native_id FROM documents"))
    covered = [i for i in ids if i <= 10001]
    beyond = [i for i in ids if i > 10001]
    assert covered and beyond, f"fixture must straddle the archive: {ids}"

    scope = dict(db.get_scope(con), embed_mode="all")
    picked = {int(r["native_id"]) for r in worker._backfill_rows(con, c, 100)}
    assert picked == set(beyond), "covered documents are the archive's, not the encoder's"
    stage, row = worker._next_book(con, scope, collection=c)
    assert stage == "embed" and int(row["native_id"]) in beyond
    stage, row = worker._next_embed(con, scope, c)
    assert stage == "embed" and int(row["native_id"]) in beyond
    assert {int(r["native_id"]) for r in worker._embed_candidates(con, scope, 100, c)} == set(beyond)

    # shard 0 goes FINAL: the documents it left unembedded are released
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) "
                "VALUES('test-arc','0','final',0)")
    con.commit()
    from app.bundles import genesis
    genesis._bump_coverage_epoch()
    picked = {int(r["native_id"]) for r in worker._backfill_rows(con, c, 100)}
    assert picked == set(beyond) | set(covered)

    # a REJECTED archive covers nothing: everything is the encoder's
    con.execute("UPDATE genesis_imports SET state='rejected'")
    con.commit()
    genesis._bump_coverage_epoch()
    picked = {int(r["native_id"]) for r in worker._backfill_rows(con, c, 100)}
    assert picked == set(ids)



def test_backfill_cursor_never_rewalks_the_covered_prefix(tmp_path, monkeypatch):
    """pubmed: 8M covered documents sit BEFORE the first uncovered one in
    rowid order. The cursor resumes from the first row returned, so the
    next round starts past the covered prefix; a shard going final (epoch)
    or the TTL resets it; a deferred (still pending) row is re-selected."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    key = (str(c.manifest_path), "new")
    rows = worker._backfill_rows(con, c, 1)
    assert rows, "an uncovered candidate exists"
    first = rows[0]
    epoch, rid, top, found, idle_since = worker._backfill_cursor[key]
    assert rid == int(first["rid"]) - 1, "resume FROM the first returned row"
    assert found and idle_since is None
    # the covered prefix lies below the cursor
    below = con.execute("SELECT COUNT(*) FROM documents WHERE rowid<=? AND CAST(native_id AS INTEGER)<=10001",
                        (rid,)).fetchone()[0]
    assert below == con.execute("SELECT COUNT(*) FROM documents WHERE rowid<? AND CAST(native_id AS INTEGER)<=10001",
                                (int(first["rid"]),)).fetchone()[0]
    # deferred (not embedded): selected again
    assert worker._backfill_rows(con, c, 1)[0]["uid"] == first["uid"]
    # embedded: the cursor moves on without revisiting anything before it
    con.execute("UPDATE documents SET embed_done=1 WHERE uid=?", (first["uid"],))
    con.commit()
    nxt = worker._backfill_rows(con, c, 1)
    assert not nxt or nxt[0]["uid"] != first["uid"]
    # a shard going final resets the cursor (its released documents sit below)
    from app.bundles import genesis
    genesis._bump_coverage_epoch()
    worker._backfill_rows(con, c, 1)
    assert worker._backfill_cursor[key][0] == genesis.coverage_epoch()


def test_backfill_selections_use_the_scope_index_and_never_sort(tmp_path, monkeypatch):
    """The former backfill ended `ORDER BY (lang='en')`: a TEMP B-TREE over
    every candidate, per round, growing with the corpus. rowid order IS
    the scope index's order — with and without the coverage predicate."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    covered = "AND NOT (CAST(native_id AS INTEGER) BETWEEN ? AND ?)"
    for sql, params in (
            (worker._BACKFILL_SQL.format(covered=""), (config.MAX_RETRIES, -1, 1)),
            (worker._BACKFILL_SQL.format(covered=covered), (config.MAX_RETRIES, -1, 0, 19, 1)),
            (worker._REEMBED_SQL, (config.EMBED_VERSION, config.MAX_RETRIES, -1, 1))):
        plan = _plan(con, sql, params)
        assert "TEMP B-TREE" not in plan.upper(), f"backfill sorts the candidate set: {plan}"
        assert "idx_documents_scope" in plan, plan
    # the cursor walk's windows: bounded by the rowid RANGE, never by an
    # index walk that only the table's end can stop
    for sql, params in (
            (worker._BACKFILL_WINDOWED.format(covered=covered),
             (-1, 1000, config.MAX_RETRIES, 0, 19, 1)),
            (worker._REEMBED_WINDOWED,
             (-1, 1000, config.EMBED_VERSION, config.MAX_RETRIES, 1))):
        plan = _plan(con, sql, params)
        assert "TEMP B-TREE" not in plan.upper(), f"window sorts: {plan}"
        assert "INTEGER PRIMARY KEY" in plan.upper(), f"window is not a rowid range: {plan}"
        assert "idx_documents_scope" not in plan, plan


# --- the cursor walk is bounded per call (measured live, 2026-09-19) --------

def _embedded_bulk(con, n, *, first_id=20_000):
    """n documents beyond the archive's range, all already embedded at the
    current version: the shape of pubmed's tail, where a sweep finds
    nothing at all."""
    con.executemany(
        "INSERT INTO documents(uid, source, native_id, path, in_scope, rdf_done, "
        "fts_done, embed_done, embed_version) VALUES(?,?,?,?,1,1,1,1,?)",
        [(f"gutenberg:{i}", "gutenberg", str(i), f"/nowhere/{i}", config.EMBED_VERSION)
         for i in range(first_id, first_id + n)])
    con.execute("UPDATE documents SET embed_done=1, embed_version=?", (config.EMBED_VERSION,))
    con.commit()


def _fixed_windows(monkeypatch, width):
    from app import sizing
    monkeypatch.setattr(sizing.sizer(), "window", lambda key, **kw: width)
    monkeypatch.setattr(sizing.sizer(), "note_window", lambda key, **kw: width)


def _window_queries(con):
    seen: list[str] = []
    con.set_trace_callback(lambda s: seen.append(s) if "rowid>" in s and "rowid<=" in s else None)
    return seen


def test_a_backfill_call_that_finds_nothing_walks_its_budget_not_the_table(tmp_path, monkeypatch):
    """Measured live on pubmed (40M rows): one call that found nothing took
    105-153 s, and every call did, because the whole table was one query.
    A call now walks windows until its budget is spent and stops; the
    sweep carries on from the cursor at the next call."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    _embedded_bulk(con, 5000)
    _fixed_windows(monkeypatch, 1000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 0.0)
    worker.reset_backfill_cursors()
    seen = _window_queries(con)
    assert worker._backfill_rows(con, c, 10) == []
    # zero budget: ONE window per walk (new, then ver), not the table
    assert len(seen) == 2, seen
    new = worker._backfill_cursor[(str(c.manifest_path), "new")]
    assert new[1] == -1 + 1000 and new[4] is None, "one window on, sweep not over"
    seen.clear()
    worker._backfill_rows(con, c, 10)
    assert len(seen) == 2 and worker._backfill_cursor[(str(c.manifest_path), "new")][1] == 1999


def test_an_empty_sweep_waits_its_ttl_from_its_end(tmp_path, monkeypatch):
    """The defect: the cursor's age was stamped BEFORE the walk, so a walk
    longer than the TTL stored a cursor already expired. The idle wait now
    runs from the moment the sweep ENDED, and costs no query at all."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    _embedded_bulk(con, 5000)
    _fixed_windows(monkeypatch, 1000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 60.0)
    worker.reset_backfill_cursors()
    key = (str(c.manifest_path), "new")
    assert worker._backfill_rows(con, c, 10) == []
    epoch, pos, top, found, idle_since = worker._backfill_cursor[key]
    assert pos >= top and not found and idle_since is not None, "a whole empty sweep"
    seen = _window_queries(con)
    assert worker._backfill_rows(con, c, 10) == []
    assert seen == [], "inside the TTL an empty sweep is not repeated"
    # the TTL counted from the sweep's end has passed: a new sweep starts
    worker._backfill_cursor[key] = (epoch, pos, top, found,
                                    idle_since - worker._BACKFILL_CURSOR_TTL_S - 1)
    worker._backfill_rows(con, c, 10)
    assert seen, "after the TTL the walk starts again"


def test_a_sweep_that_found_work_restarts_for_what_became_eligible_below(tmp_path, monkeypatch):
    """Documents BELOW the cursor can become eligible while it walks (fts
    finishing on earlier rowids). A sweep that found work therefore starts
    the next one at once, from the beginning, instead of idling."""
    c, con, archive = _archive_collection(tmp_path, monkeypatch)
    _embedded_bulk(con, 3000)
    _fixed_windows(monkeypatch, 1000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 60.0)
    worker.reset_backfill_cursors()
    late, early = "gutenberg:22500", "gutenberg:20100"
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (late,))
    con.commit()
    assert [r["uid"] for r in worker._backfill_rows(con, c, 1)] == [late]
    con.execute("UPDATE documents SET embed_done=1 WHERE uid=?", (late,))
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (early,))
    con.commit()
    assert [r["uid"] for r in worker._backfill_rows(con, c, 1)] == [early]


def test_the_version_walk_is_windowed_with_no_archive_too(tmp_path, monkeypatch):
    """The node, 2026-09-22: pubmed, 39,994,988 documents, every one embedded
    at the current version, no archive declared — so this branch, which the
    bounded walk of 2026-09-19 did not cover. One call read the whole scope
    index to find nothing in 57.8 s, and the producer thread called it again
    at once: 46 % of the worker's CPU samples, a core of twelve, for as long
    as the collection was set to embed "all".

    The index prefix of the version selection ends at `embed_done=1` — every
    embedded document — and `embed_version` is tested row by row, so the plan
    reads as a SEARCH while SQLite walks the corpus. That is why a plan check
    alone never caught it, and why this test counts windows instead.
    """
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, entity_version=?",
                (config.FTS_VERSION, config.ENTITY_VERSION))
    con.commit()
    _embedded_bulk(con, 5000)
    assert worker._covered_ranges(con, c) == [], "no archive: the branch under test"
    _fixed_windows(monkeypatch, 1000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 0.0)
    worker.reset_backfill_cursors()

    seen = _window_queries(con)
    assert worker._backfill_rows(con, c, 10) == []
    assert len(seen) == 1, f"zero budget: ONE window of the version walk, not the table: {seen}"
    cur = worker._backfill_cursor[(str(c.manifest_path), "ver")]
    assert cur[1] == -1 + 1000 and cur[4] is None, "one window on, sweep not over"
    seen.clear()
    worker._backfill_rows(con, c, 10)
    assert len(seen) == 1 and worker._backfill_cursor[(str(c.manifest_path), "ver")][1] == 1999, \
        "the next call carries the sweep on from the cursor"


def test_with_no_archive_a_new_document_is_still_embeddable_in_the_same_round(
        tmp_path, monkeypatch):
    """What the unbounded walk was FOR, and what windowing the version walk
    must not cost: with no archive there is no cursor on the new walk, so a
    document that clears fts is picked up at once however far down the table
    it sits — no sweep to wait for."""
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, entity_version=?",
                (config.FTS_VERSION, config.ENTITY_VERSION))
    con.commit()
    _embedded_bulk(con, 5000)
    _fixed_windows(monkeypatch, 1000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 0.0)
    worker.reset_backfill_cursors()
    assert worker._backfill_rows(con, c, 10) == []

    late = "gutenberg:24900"                    # the far end of the table
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (late,))
    con.commit()
    assert [r["uid"] for r in worker._backfill_rows(con, c, 1)] == [late], \
        "a new document waits for no sweep, even with the version walk out of budget"


def test_a_version_sweep_that_found_nothing_waits_far_longer_than_a_new_one(
        tmp_path, monkeypatch):
    """Windowing alone still re-read the corpus every two minutes: 58 s of CPU
    per sweep on pubmed's 40M for a candidate that cannot exist. A re-embed
    appears only from a version bump — a code change, so a restart, which
    clears these cursors — or an import of older vectors, which moves the
    coverage epoch and clears them too. The NEW walk keeps its short wait: a
    document clearing fts below the cursor is work waiting on it."""
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, entity_version=?",
                (config.FTS_VERSION, config.ENTITY_VERSION))
    con.commit()
    _embedded_bulk(con, 2000)
    _fixed_windows(monkeypatch, 10_000)
    monkeypatch.setattr(config, "BACKFILL_WALK_BUDGET_S", 60.0)
    worker.reset_backfill_cursors()
    key = (str(c.manifest_path), "ver")

    assert worker._backfill_rows(con, c, 10) == []
    epoch, pos, top, found, idle_since = worker._backfill_cursor[key]
    assert pos >= top and not found and idle_since is not None, "a whole empty sweep"

    seen = _window_queries(con)
    worker._backfill_cursor[key] = (epoch, pos, top, found,
                                    idle_since - worker._BACKFILL_CURSOR_TTL_S - 1)
    assert worker._backfill_rows(con, c, 10) == []
    assert seen == [], "the NEW walk's two minutes are not enough to sweep the corpus again"

    worker._backfill_cursor[key] = (epoch, pos, top, found,
                                    idle_since - worker._REEMBED_IDLE_TTL_S - 1)
    assert worker._backfill_rows(con, c, 10) == []
    assert seen, "after its own wait the version sweep does run again"
    assert worker._idle_ttl("ver") > worker._idle_ttl("new") == worker._BACKFILL_CURSOR_TTL_S


def test_the_version_selection_that_reads_the_corpus_is_no_longer_run(tmp_path, monkeypatch):
    """The one that cost 57.8 s. Kept as a constant for the plan checks and
    the scheduling report; if it comes back into the walk, this fails."""
    import inspect
    source = inspect.getsource(worker._backfill_rows)
    assert "_REEMBED_SQL" not in source, "the selection that reads the corpus is not run"
    assert source.count("_REEMBED_WINDOWED") == 2, "the version walk is windowed in BOTH branches"
    # three pages in all: the version walk in each branch, and the new walk in
    # the archive branch — which is the one the archive's spans bound
    assert source.count("_cursor_page") == 3


def test_the_prefetch_probe_walks_forward_from_the_current_document(tmp_path):
    """Regression (2026-08-28, measured live): the prefetch lookahead used
    `ORDER BY rowid LIMIT 1 OFFSET k` over the whole pending set, so SQLite
    started at rowid 1 and re-filtered the 28.6M documents already done to
    reach the frontier — three scans per call, seen as 10.5s rdf batches
    (the engineering rule broken is the first: a per-round cost that scales
    with the corpus). Anchored on the current rowid it is k steps from
    where the stage is."""
    c, con = _discovered(tmp_path)
    for i in range(5000):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                    "fts_done,embed_done) VALUES(?,'g',?,?,1,?,0,0)",
                    (f"p:{i}", str(i), f"/x{i}", 1 if i < 4000 else 0))
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + worker._RDF_AHEAD, (0, 3, 10)))
    assert "SCAN documents" not in plan, f"a full scan of the corpus per probe: {plan}"
    assert "USE TEMP B-TREE" not in plan, f"a sort of the whole pending set per probe: {plan}"
    anchor = con.execute("SELECT rowid FROM documents WHERE uid='p:4000'").fetchone()[0]
    got = con.execute(worker._RDF_AHEAD, (anchor, config.MAX_RETRIES, 100)).fetchone()
    assert got is not None and got["uid"] == "p:4101", dict(got)
    # nothing behind the anchor is ever examined, and a probe past the end
    # of the pending set simply finds nothing
    assert con.execute(worker._RDF_AHEAD,
                       (anchor, config.MAX_RETRIES, 100_000)).fetchone() is None


def test_prefetch_ahead_costs_nothing_when_there_is_nothing_to_prepare(tmp_path, monkeypatch):
    """It probes ONLY when there is a background to prepare in AND a plugin
    with something to prepare: four of the five plugins have no derived
    cache and a serial node has no pool, so paying three lookahead queries
    per stage for them would be pure cost on the hot path."""
    from app import parallel
    c, con = _discovered(tmp_path)          # gutenberg: no `prepare` hook
    row = con.execute("SELECT * FROM documents LIMIT 1").fetchone()
    queries = []
    con.set_trace_callback(queries.append)
    worker._prefetch_last.clear()
    worker._prefetch_ahead(con, c, "rdf", row)
    assert queries == [], "serial node: not one query"
    monkeypatch.setattr(parallel, "pool", lambda: object())      # a pool exists...
    worker._prefetch_last.clear()
    worker._prefetch_ahead(con, c, "rdf", row)
    con.set_trace_callback(None)
    assert queries == [], "...but this plugin has nothing to prepare"


def test_prefetch_ahead_is_anchored_throttled_and_never_fatal(isolated_config, tmp_path, monkeypatch):
    """With a pool and a plugin that HAS a derived cache (pubmed), it
    submits the documents ahead of the CURRENT one, at most once per
    interval, and a failure inside it never reaches the round."""
    from dataclasses import replace
    from app import licensing, parallel
    d = tmp_path / "pm"
    c = collections_mod.Collection(
        name="pm", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=Path(__file__).parent / "fixtures" / "pubmed-mini",
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    c = replace(c, license_ack=licensing.make_ack(c))
    con = db.connect(c)
    for i in range(300):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                    "fts_done,embed_done) VALUES(?,'pubmed',?,?,1,0,0,0)",
                    (f"pubmed:{i}", str(i), f"/x/c.xml::{i}"))
    row = con.execute("SELECT * FROM documents WHERE uid='pubmed:0'").fetchone()
    seen = []
    monkeypatch.setattr(parallel, "pool", lambda: object())
    monkeypatch.setattr(parallel, "prefetch", lambda coll, rows: seen.append([r["uid"] for r in rows]))
    monkeypatch.setattr(worker, "_PREFETCH_OFFSETS", (10, 50))
    worker._prefetch_last.clear()
    worker._prefetch_ahead(con, c, "rdf", row)
    assert seen == [["pubmed:11", "pubmed:51"]], seen   # the anchor itself is excluded
    worker._prefetch_ahead(con, c, "rdf", row)
    assert len(seen) == 1, "throttled per collection+stage"
    # a broken probe is silent: the stage goes on
    worker._prefetch_last.clear()
    monkeypatch.setattr(parallel, "prefetch",
                        lambda coll, rows: (_ for _ in ()).throw(RuntimeError("pool gone")))
    worker._prefetch_ahead(con, c, "rdf", row)
    con.close()


def test_the_reindex_walk_is_windowed_and_then_free(tmp_path, monkeypatch):
    """Regression (2026-08-28, measured on the live node): the version-driven
    re-index selection is the one query no index can answer, and it ran on
    EVERY round of a caught-up collection — 16.7s on wikivec, 18.3s on
    wikipedia, 127.9s on pubmed, each round, to conclude there was nothing
    to do. It now walks rowid WINDOWS from a per-collection cursor and,
    once the walk ends, costs nothing until a version bump."""
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=1, "
                "fts_version=?, entity_version=?", (config.FTS_VERSION, config.ENTITY_VERSION))
    for i in range(1000):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                    "fts_done,embed_done,fts_version,entity_version) "
                    "VALUES(?,'g',?,?,1,1,1,0,?,?)",
                    (f"p:{i}", str(i), f"/x{i}", config.FTS_VERSION, config.ENTITY_VERSION))
    # pin the width: this test is about the WALK (one window per round,
    # terminating, then free). That the width itself adapts to what a probe
    # costs is app/sizing.py's job and is tested separately.
    monkeypatch.setattr(worker, "_REINDEX_WINDOW", 100)
    monkeypatch.setattr(worker, "_REINDEX_WINDOW_MIN", 100)
    monkeypatch.setattr(worker, "_REINDEX_WINDOW_MAX", 100)
    worker.reset_reindex_cursors()
    top = con.execute("SELECT MAX(rowid) FROM documents").fetchone()[0]

    # nothing to re-index: the walk advances a window per call and stops
    calls = 0
    while worker._next_reindex(con, c) is None and not worker._reindex_cursor[
            str(c.manifest_path)][4]:
        calls += 1
        assert calls < 100, "the walk must terminate"
    state = worker._reindex_cursor[str(c.manifest_path)]
    assert state[4] is True and state[2] >= top
    assert calls >= top // 100, "one window per round, not the whole corpus"
    # done: no query at all now
    queries = []
    con.set_trace_callback(queries.append)
    assert worker._next_reindex(con, c) is None
    con.set_trace_callback(None)
    assert queries == [], "a caught-up collection pays nothing per round"

    # a version bump makes work again, and it is found
    monkeypatch.setattr(config, "FTS_VERSION", config.FTS_VERSION + 1)
    found = None
    for _ in range(100):
        found = worker._next_reindex(con, c)
        if found is not None:
            break
    assert found is not None, "the bump reopens the walk"
    # the batch's peers come from the same window, not the whole corpus
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + worker._FTS_REINDEX_PEERS,
        (3, 0, 100, config.FTS_VERSION, config.ENTITY_VERSION, "x", 10)))
    assert "TEMP B-TREE" not in plan and "SCAN documents" not in plan, plan
    peers = worker._fts_peers(con, c, found, 50)
    assert peers and all(p["uid"] != found["uid"] for p in peers)


def test_fresh_fts_peers_use_the_index_prefix(tmp_path):
    """The common case must not pay for the re-index OR: 0.4 ms instead of
    19.3 s on a 22M-row collection (measured)."""
    c, con = _discovered(tmp_path)
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + worker._FTS_FRESH_PEERS, (3, "x", 10)))
    assert "idx_documents_scope (in_scope=? AND rdf_done=? AND fts_done=?)" in plan, plan
    assert "TEMP B-TREE" not in plan and "SCAN documents" not in plan, plan
    row = con.execute("SELECT * FROM documents WHERE fts_done=0 LIMIT 1").fetchone()
    if row is not None:
        peers = worker._fts_peers(con, c, row, 10)
        assert all(p["uid"] != row["uid"] and not p["fts_done"] for p in peers)


def test_the_reindex_window_is_a_rowid_range_not_an_index_walk(tmp_path):
    """Regression within the regression (2026-08-28): windowing the walk was
    not enough — with the scope columns indexable SQLite could not use
    `rowid>?` as a range (embed_done sits between the constrained prefix
    and the index's rowid tail), so every window still re-walked the whole
    (in_scope, rdf_done, fts_done) range: 5.4s per window on pubmed,
    measured. The `+` prefixes force the integer primary key."""
    c, con = _discovered(tmp_path)
    for sql, params in (
            (worker._FTS_REINDEX_WINDOWED, (0, 100, config.MAX_RETRIES,
                                            config.FTS_VERSION, config.ENTITY_VERSION)),
            (worker._FTS_REINDEX_PEERS, (0, 100, config.MAX_RETRIES, config.FTS_VERSION,
                                         config.ENTITY_VERSION, "x", 10))):
        plan = _plan(con, sql, params)
        assert "USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)" in plan, plan
        assert "idx_documents_scope" not in plan, plan
        assert "TEMP B-TREE" not in plan.upper(), plan
