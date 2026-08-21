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
