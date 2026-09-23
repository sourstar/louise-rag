"""The per-plugin phase controller, the index rotation, pool admission and
query priority — the second half of docs/notes/scheduling.md §10.

Where the fair-share scheduler decides WHICH COLLECTION gets a resource,
these decide what a collection does with the slot it has, and who is
allowed to interrupt. Each is a cost test: none of them changes what the
pipeline produces, only when.
"""
import shutil
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

def test_the_only_collection_with_work_takes_the_whole_pool():
    """The live case, and the reason the old rule was wrong. Admission used
    to divide the children by how many collections had TOUCHED the pool in
    the last 30 s. On the node gutenberg only embeds — the embed path chunks
    in-process on its own thread and never enters the pool — so wikipedia is
    the only collection parsing and indexing, and it was still sized for half
    the children: six of twelve cores with nothing to run on them."""
    parallel.forget_users()
    assert parallel.slots("wikipedia", 22) == 22


def test_a_collection_gives_up_only_the_slots_another_is_really_holding():
    parallel.forget_users()
    parallel._want("gutenberg", 1)
    parallel._occupy("gutenberg", 4)
    assert parallel.slots("wikipedia", 22) == 18
    assert parallel.slots("gutenberg", 22) == 22, \
        "a collection must not be charged for what it is holding itself"


def test_a_collection_arriving_at_a_full_pool_still_gets_its_share():
    """Work-conserving is not first-come-takes-everything. The starvation the
    old share existed to prevent is real: a collection whose documents cost
    30 s each would hold every child for its whole batch, and the one waiting
    behind it holds BEGIN IMMEDIATE on its manifest the while. So the old
    fair share survives as the FLOOR — the surplus queues, and the incumbent
    stops refilling past its own share, so the two converge within a wave."""
    parallel.forget_users()
    parallel._want("wikipedia", 1)
    parallel._occupy("wikipedia", 22)              # it started alone
    assert parallel.slots("pubmed", 22) == 11

    # and it converges by the newcomer TAKING them, not by the incumbent
    # anticipating: until pubmed holds something there is nothing to give up,
    # and once it does the incumbent stops refilling past its own share.
    parallel._want("pubmed", 1)
    parallel._occupy("pubmed", 11)
    assert parallel.slots("wikipedia", 22) == 11


def test_a_collection_that_finished_its_batch_holds_nothing():
    """The old rule counted presence for 30 s after the fact, so a collection
    that had STOPPED using the pool went on shrinking everyone else's share.
    A slot is released as its result is taken, and compute() releases whatever
    is left on the way out — including the timeout and broken-pool paths,
    where a whole window is still in flight."""
    parallel.forget_users()
    parallel._want("gone", 1)
    parallel._occupy("gone", 8)
    parallel._occupy("gone", -8)
    parallel._want("gone", -1)
    assert parallel.held() == {} and parallel.wanting() == {}
    assert parallel.slots("here", 22) == 22


# ------------------------------------------------------- query priority (A5)

def _node_store():
    """The worker makes the node store at boot; a claim never does."""
    from app import dispatch, node_state
    node_state.connect().close()
    dispatch.reset()


def test_a_query_claim_is_visible_to_the_worker(tmp_path, isolated_config):
    """The reservation has to cross a PROCESS boundary: the query is
    answered by the web process and the backfill runs in the worker, so
    neither one's in-process scheduler can see the other's demand. And it
    is released when the last interactive call ends — not a TTL later,
    which would idle the encoder for nothing after every query."""
    from app import dispatch
    c, con = _discovered(tmp_path)
    _node_store()
    assert worker.query_waiting(con) is False
    with dispatch.interactive(dispatch.Level.QUERY):
        worker.reset_query_priority_memo()
        assert worker.query_waiting(con) is True
    worker.reset_query_priority_memo()
    assert worker.query_waiting(con) is False


def test_a_claim_left_by_a_dead_web_process_expires(tmp_path, isolated_config):
    """A web process that dies mid-call cannot clear its claim; its
    refresher stops, and the claim lapses at its TTL. The failure direction
    is deliberate: losing a claim costs a request one chunk's wait, keeping
    one forever would stop corpus embedding for good."""
    import time as _time

    from app import dispatch
    c, con = _discovered(tmp_path)
    _node_store()
    dispatch._write_claim(_time.time() + 60)
    worker.reset_query_priority_memo()
    assert worker.query_waiting(con) is True
    dispatch._write_claim(_time.time() - 1)
    worker.reset_query_priority_memo()
    assert worker.query_waiting(con) is False


def test_the_claim_is_polled_not_read_per_request(tmp_path, isolated_config, monkeypatch):
    """A CORPUS request waiting at a gate asks every poll, and at 175
    embeds/s an unmemoised read would be a query on the node store per
    chunk."""
    from app import dispatch
    _node_store()
    opened = []
    real = dispatch._node_con
    monkeypatch.setattr(dispatch, "_node_con", lambda: opened.append(1) or real())
    monkeypatch.setattr(config, "GATE_POLL_S", 60.0)
    for _ in range(50):
        dispatch.interactive_active()
    assert len(opened) == 1, "the answer is memoised for GATE_POLL_S"


def test_a_search_claims_before_the_encoder_is_used(tmp_path, isolated_config):
    """The claim must be made by the QUERY path itself — a reservation
    nobody makes is not a reservation."""
    import inspect

    from app import retrieval
    lines = [ln.split("#")[0] for ln in
             inspect.getsource(retrieval._search_one_collection).splitlines()]
    claim = next(i for i, ln in enumerate(lines)
                 if "dispatch.interactive(dispatch.Level.QUERY)" in ln)
    use = next(i for i, ln in enumerate(lines) if "lists.append(embeddings.search" in ln)
    assert claim < use, "claimed before the encoder is used, not after"
