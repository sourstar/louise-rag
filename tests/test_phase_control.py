"""The per-plugin phase controller, the index rotation, pool admission and
query priority — the second half of docs/notes/scheduling.md §10.

Where the fair-share scheduler decides WHICH COLLECTION gets a resource,
these decide what a collection does with the slot it has, and who is
allowed to interrupt. Each is a cost test: none of them changes what the
pipeline produces, only when.
"""
import shutil
import time
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db, discovery, parallel, sched, worker

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


def _rows(con, n, **cols):
    """n synthetic documents with the given stage flags."""
    base = {"in_scope": 1, "rdf_done": 0, "fts_done": 0, "embed_done": 0}
    base.update(cols)
    start = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for i in range(start, start + n):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
            "fts_done,embed_done,fts_version,entity_version) "
            "VALUES(?,'g',?,?,?,?,?,?,?,?)",
            (f"s:{i}", str(i), f"/s{i}", base["in_scope"], base["rdf_done"],
             base["fts_done"], base["embed_done"], config.FTS_VERSION,
             config.ENTITY_VERSION))
    con.commit()


# ----------------------------------------------- the phase controller (A2/A4)

def test_the_archive_yields_when_the_encoder_would_run_dry(tmp_path, isolated_config):
    """An archive pass holds the collection's thread for up to 300 s, and
    while it does, nothing reaches fts_done — so nothing new becomes
    embeddable and the encoder goes idle. That is A4's burstiness at its
    source: 1,900 embeds/s during a pass, zero during the parse phases."""
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=0")
    _rows(con, 50, rdf_done=1)                      # fts has plenty to do
    con.commit()
    assert worker._archive_yields(con, c) is True   # embed queue is empty


def test_the_archive_keeps_the_thread_when_the_encoder_is_fed(tmp_path, isolated_config):
    """The rule is one-directional. A well-fed encoder means the archive is
    free to hold its slot for the whole budget."""
    c, con = _discovered(tmp_path)
    _rows(con, 10, rdf_done=1)                                  # fts work
    _rows(con, config.PHASE_EMBED_LOW + 5, rdf_done=1, fts_done=1)   # embed queue
    con.commit()
    assert worker._archive_yields(con, c) is False


def test_the_archive_is_not_yielded_to_an_empty_fts_queue(tmp_path, isolated_config):
    """Yielding when fts has nothing to do would starve the archive for no
    gain at all — and genesis is what feeds the encoder in that case."""
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=1")
    con.commit()
    assert worker._archive_yields(con, c) is False


def test_the_rule_can_be_turned_off(tmp_path, isolated_config, monkeypatch):
    c, con = _discovered(tmp_path)
    _rows(con, 50, rdf_done=1)
    monkeypatch.setattr(config, "PHASE_EMBED_LOW", 0)
    assert worker._archive_yields(con, c) is False


def test_the_watermark_queries_are_bounded_and_indexed(tmp_path, isolated_config):
    """Both counts run on a collection's hot path, so neither may walk the
    corpus to answer "are there at least N" — the LIMIT is inside the
    subquery, and the plan must use the scope index (CLAUDE.md: a plan
    check AND a bound; a merely slow query passes every behavioural test)."""
    c, con = _discovered(tmp_path)
    _rows(con, 200, rdf_done=1)
    con.commit()
    for sql in (worker._FTS_BACKLOG, worker._EMBED_BACKLOG):
        plan = " | ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN " + sql, (config.MAX_RETRIES, 10)))
        assert "SCAN documents" not in plan, plan
        assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan
        assert "idx_documents_scope" in plan, plan
        # the LIMIT bounds the count: 10 asked for, 10 counted, not 200
        assert con.execute(sql, (config.MAX_RETRIES, 10)).fetchone()[0] <= 10


# --------------------------------------------- the index maintainer rotation

def test_the_index_maintainer_serves_in_fair_share_order(tmp_path, isolated_config,
                                                          monkeypatch):
    """One collection at a time is deliberate (two hour-long builds must
    never run together), which is exactly why the ORDER matters: whoever is
    served last waits for everyone ahead. It uses the same rotation as the
    encoder, so a collection that did work goes to the tail and one that
    was not due drops out instead of holding a place."""
    served = []
    s = sched.Scheduler(base_quantum_s=1.0)
    monkeypatch.setattr(sched, "_scheduler", s)

    for name in ("a", "b", "c"):
        s.demand(sched.RES_INDEX, name)
    for _ in range(6):
        turn = s.acquire(sched.RES_INDEX)
        served.append(turn.collection)
        did = turn.collection != "b"        # b is never due
        turn.spent(0.5, units=1 if did else 0)
        turn.release(idle=not did)
    assert served.count("b") == 1           # probed once, then out
    # and the rest share what is left evenly (5 turns between two flows)
    assert abs(served.count("a") - served.count("c")) <= 1


# ------------------------------------------------------- parse-pool admission

def test_one_collection_alone_still_gets_every_child(monkeypatch):
    monkeypatch.setattr(config, "PARSE_PROCESSES", 8)
    parallel.forget_users()
    assert parallel.active_users("solo") == 1


def test_children_are_shared_when_several_collections_are_busy(monkeypatch):
    """The executor is FIFO and a submission is ~2 tasks per child, so
    whichever collection submits first takes every child — and one whose
    documents cost 30 s each holds them for the whole batch."""
    parallel.forget_users()
    for name in ("pubmed", "gutenberg", "wikivec"):
        parallel.active_users(name)
    assert parallel.active_users("pubmed") == 3


def test_a_collection_that_stopped_using_the_pool_stops_counting(monkeypatch):
    """Otherwise a finished collection would permanently shrink everyone
    else's share — the share must follow real demand, like the rotation."""
    monkeypatch.setattr(parallel, "ACTIVE_WINDOW_S", 0.05)
    parallel.forget_users()
    parallel.active_users("gone")
    parallel.active_users("here")
    time.sleep(0.1)
    assert parallel.active_users("here") == 1


# ------------------------------------------------------- query priority (A5)

def test_a_query_claim_is_visible_to_the_worker(tmp_path, isolated_config):
    """The reservation has to cross a PROCESS boundary: the query is
    answered by the web process and the backfill runs in the worker, so
    neither one's in-process scheduler can see the other's demand."""
    c, con = _discovered(tmp_path)
    assert worker.query_waiting(con) is False
    db.claim_query_priority(con, c.name)
    worker.reset_query_priority_memo()
    assert worker.query_waiting(con) is True


def test_a_query_claim_expires(tmp_path, isolated_config, monkeypatch):
    """A stale claim would idle the encoder for nothing. The failure
    direction is deliberate: losing a claim costs a query one document's
    wait, keeping one too long costs everybody."""
    c, con = _discovered(tmp_path)
    db.claim_query_priority(con, c.name)
    assert db.query_priority_claimed(con) is True
    assert db.query_priority_claimed(con, max_age_s=-1.0) is False


def test_the_claim_is_polled_not_read_per_document(tmp_path, isolated_config):
    """At 175 embeds/s an unmemoised read would put a SELECT on the control
    table for every document embedded."""
    c, con = _discovered(tmp_path)
    worker.reset_query_priority_memo()
    worker.query_waiting(con)
    reads = []
    con.set_trace_callback(reads.append)
    for _ in range(50):
        worker.query_waiting(con)
    con.set_trace_callback(None)
    assert reads == [], "the answer is memoised between documents"


def test_a_search_claims_the_encoder_before_using_it(tmp_path, isolated_config,
                                                     monkeypatch):
    """The claim must be written by the QUERY path itself — a reservation
    nobody makes is not a reservation."""
    import inspect

    from app import retrieval
    lines = [ln.split("#")[0] for ln in
             inspect.getsource(retrieval._search_one_collection).splitlines()]
    claim = next(i for i, ln in enumerate(lines) if "claim_query_priority" in ln)
    use = next(i for i, ln in enumerate(lines) if "lists.append(embeddings.search" in ln)
    assert claim < use, "claimed before the encoder is used, not after"
