"""The embed producers (docs/notes/scheduling.md §12).

Measured live before them (2026-09-19): the main loop served the encoder
endpoints serially, so pubmed's fruitless turns — 250 s in ten minutes, zero
documents — left gutenberg's GPU idle in 233 of 300 seconds with 78,178
documents waiting. Now one producer per collection feeds its encoder, and
the operator's rule holds within it: two documents submitted, a third
prepared, so a returned document is replaced at once.
"""
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db, discovery, embeddings, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
RECIPE = collections_mod.EmbedRecipe(url="http://gpu-enc", api="openai", model="m",
                                     dtype="f16", ctx=512)


def _collection(tmp_path, name="alpha"):
    corpus = tmp_path / f"corpus_{name}"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1")
    con.commit()
    return c, con


def _feed_once(monkeypatch, con):
    rows = con.execute("SELECT rowid AS rid, * FROM documents").fetchall()
    served = {"done": False}

    def candidates(_con, _scope, _limit, _collection):
        if served["done"]:
            return []
        served["done"] = True
        return rows
    monkeypatch.setattr(worker, "_embed_candidates", candidates)
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: None)
    return rows


def test_two_documents_are_submitted_and_a_third_is_prepared(tmp_path, monkeypatch):
    """The operator's rule, measured on the timeline: at most two documents
    inside the encoder at once; the third's chunks are cut while the first
    two are there; and the moment one returns, the prepared one goes in."""
    c, con = _collection(tmp_path)
    rows = _feed_once(monkeypatch, con)
    assert len(rows) == 3
    events: list[tuple[str, str, float]] = []
    lock = threading.Lock()
    inside = {"n": 0, "max": 0}

    def load(row, collection):
        with lock:
            events.append(("prepared", row["uid"], time.monotonic()))
        return [{"text": "x"}]

    def upsert(book_id, lang, chunks, collection, lock_con=None, stop=None, *,
               held=None, sink=None, reuse=True, counts=None, columns=None):
        with lock:
            inside["n"] += 1
            inside["max"] = max(inside["max"], inside["n"])
            events.append(("in", str(book_id), time.monotonic()))
        time.sleep(0.3)
        with lock:
            inside["n"] -= 1
            events.append(("out", str(book_id), time.monotonic()))
        return 1
    monkeypatch.setattr(worker, "_load_chunks", load)
    monkeypatch.setattr(embeddings, "upsert_book", upsert)

    assert worker._embed_parallel(con, c, RECIPE, slots=2)
    assert inside["max"] == 2, "never more than two submitted"
    prepared = sorted(t for kind, _, t in events if kind == "prepared")
    first_out = min(t for kind, _, t in events if kind == "out")
    assert prepared[2] < first_out, "the third was prepared while two were inside"
    ins = sorted(t for kind, _, t in events if kind == "in")
    assert ins[2] - first_out < 0.1, "the prepared one went in as soon as a slot freed"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 3


def test_a_long_document_stopped_between_slices_is_deferred_not_errored(tmp_path, monkeypatch):
    """SIGTERM between two slices of a long document raises EmbedInterrupted
    (stage_embed's contract, now the producers'). Written slices stay; the
    document is NOT done; and it must never carry an error, which would take
    it out of every selection until a human cleared it."""
    c, con = _collection(tmp_path)
    _feed_once(monkeypatch, con)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])

    def interrupted(*a, **k):
        raise embeddings.EmbedInterrupted("stopped after 64 of 785 chunks")
    monkeypatch.setattr(embeddings, "upsert_book", interrupted)
    worker._embed_parallel(con, c, RECIPE, slots=2)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0


def test_one_collections_slow_stream_never_holds_anothers_encoder(tmp_path, monkeypatch):
    """The defect the producers exist for: one collection's fruitless work
    held the loop that fed every other encoder. Now each is its own thread."""
    slow, _ = _collection(tmp_path, "slow")
    fast, _ = _collection(tmp_path, "fast")
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.01)
    release = threading.Event()
    calls = {"fast": 0, "slow": 0}

    def step(con, collection):
        calls[collection.name] += 1
        if collection.name == "slow":
            release.wait(10)
        return False
    monkeypatch.setattr(worker, "_producer_step", step)
    worker._sync_embed_producers([slow, fast])
    try:
        deadline = time.monotonic() + 5
        while calls["fast"] < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["slow"] == 1 and calls["fast"] >= 5, calls
    finally:
        release.set()
        threads = [t for t, _ in worker._embed_producers.values()]
        worker._sync_embed_producers([])
        for t in threads:
            t.join(5)
    assert not worker._embed_producers


def test_a_producer_stands_down_while_the_node_is_paused(tmp_path, monkeypatch):
    c, _ = _collection(tmp_path)
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.01)
    calls = []
    monkeypatch.setattr(worker, "_producer_step", lambda con, col: calls.append(1) or False)
    worker._paused.set()
    try:
        worker._sync_embed_producers([c])
        time.sleep(0.3)
        assert calls == [], "paused: nothing selected, nothing sent"
    finally:
        worker._paused.clear()
        threads = [t for t, _ in worker._embed_producers.values()]
        worker._sync_embed_producers([])
        for t in threads:
            t.join(5)


linux_only = pytest.mark.skipif(not hasattr(os, "setpriority") or sys.platform != "linux",
                                reason="per-thread nice is a Linux property")


@linux_only
def test_indexing_and_fetching_yield_the_cpu_and_the_feeders_do_not(monkeypatch):
    """Operator, 2026-09-19: the threads feeding the GPU outrank indexing
    and parsing. Background threads lower themselves; a producer does not;
    and the process's main thread is never touched (a nice cannot be raised
    back)."""
    monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 1)
    monkeypatch.setattr(config, "WORKER_EMBED_PRODUCERS", 1)
    main_before = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    seen = {}

    def background():
        worker._background_priority()
        seen["background"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

    def producer():
        seen["producer"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    for fn in (background, producer):
        t = threading.Thread(target=fn)
        t.start()
        t.join(5)
    worker._background_priority()        # on the main thread: a no-op
    assert seen["background"] == min(19, main_before + config.BACKGROUND_NICE)
    assert seen["producer"] == main_before
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == main_before


@linux_only
def test_a_background_thread_started_by_one_does_not_compound(monkeypatch):
    """Live 2026-09-20, the first deploy: nice is INHERITED, and the fetch
    started from a discovery started from a pipeline thread lowered itself
    by another step each time — acquisition at 15, below the parse pool,
    which §10 A1 forbids. The level is a target over the process's base."""
    monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 1)
    monkeypatch.setattr(config, "WORKER_EMBED_PRODUCERS", 1)
    base = os.getpriority(os.PRIO_PROCESS, os.getpid())
    seen = {}

    def inner():
        worker._background_priority()
        seen["inner"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

    def outer():
        worker._background_priority()
        t = threading.Thread(target=inner)
        t.start()
        t.join(5)
    t = threading.Thread(target=outer)
    t.start()
    t.join(5)
    assert seen["inner"] == min(19, base + config.BACKGROUND_NICE)


@linux_only
def test_a_parse_child_sits_at_the_workers_base_plus_ten_whoever_started_it():
    """A child process inherits its starter's nice; started from a lowered
    pipeline thread, a relative nice(10) put it at 15, below the index
    maintainer — whose rule is that it yields to parsing."""
    from app import parallel
    base = os.getpriority(os.PRIO_PROCESS, os.getpid())
    seen = {}

    def starter():
        worker._lower_own_priority(config.BACKGROUND_NICE)   # a pipeline thread
        parallel._child_init({}, base)                        # what the child runs first
        seen["child"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    t = threading.Thread(target=starter)
    t.start()
    t.join(5)
    assert seen["child"] == min(19, base + 10)


def test_a_document_cut_to_nothing_is_marked_without_a_slot(tmp_path, monkeypatch):
    """wikipedia's dump layer, 2026-10-30 (München): 15.76 M of the 18.8 M
    documents left to embed are redirects the current chunker cuts to
    nothing — ~85 % of every stream from rowid 35 M on, each taking a slot
    and 12 ms of cutting to end in `mark_done`. They are marked done with
    their group, in one transaction, never cut and never sent; a document
    that still holds vectors, one cut by an older chunker, and one with
    chunks take the normal path."""
    c, con = _collection(tmp_path)
    rows = con.execute("SELECT rowid AS rid, native_id, uid FROM documents ORDER BY rowid").fetchall()
    assert len(rows) == 3
    empty, holding, stale = rows
    con.execute("UPDATE documents SET chunks=0, tokens=0, fts_version=?", (config.FTS_VERSION,))
    con.execute("UPDATE documents SET fts_version=? WHERE uid=?", (config.FTS_VERSION - 1, stale["uid"]))
    con.commit()
    _feed_once(monkeypatch, con)
    held = embeddings.HeldRead({int(holding["native_id"]): {"c0": {"text": "x", "vector": None}}}, 0, None)
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: held)
    loaded, sent = [], []
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: loaded.append(row["uid"]) or [])
    monkeypatch.setattr(embeddings, "upsert_book",
                        lambda book_id, *a, **k: sent.append(str(book_id)) or 0)
    assert worker._embed_parallel(con, c, RECIPE, slots=2)
    assert empty["uid"] not in loaded, "never cut"
    assert sorted(loaded) == sorted([holding["uid"], stale["uid"]]), "the others: the normal path"
    done = {u for (u,) in con.execute("SELECT uid FROM documents WHERE embed_done=1")}
    assert done == {r["uid"] for r in rows}, "all three end done"


def test_a_refill_whose_group_was_all_cut_to_nothing_takes_the_next(tmp_path, monkeypatch):
    """The node, 2026-11-01: wikipedia's embed producer raised `pop from empty
    list` every few seconds. A refill fetched a group whose every document the
    current chunker cut to nothing (its redirects); `settle_empty` marked them
    all done and left nothing to queue, and the refill popped anyway. It takes
    the next group instead, and every document ends done."""
    c, con = _collection(tmp_path)
    first = con.execute("SELECT uid FROM documents ORDER BY rowid LIMIT 1").fetchone()["uid"]
    con.execute("UPDATE documents SET chunks=0, tokens=0, fts_version=? WHERE uid != ?",
                (config.FTS_VERSION, first))
    con.commit()
    rows = con.execute("SELECT rowid AS rid, * FROM documents ORDER BY rowid").fetchall()
    assert len(rows) == 3
    groups = [[rows[0]], rows[1:]]                 # one to embed; then a group all cut to nothing
    monkeypatch.setattr(worker, "_embed_candidates", lambda *a: groups.pop(0) if groups else [])
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: embeddings.HeldRead({}, 0, None))
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [])
    monkeypatch.setattr(embeddings, "upsert_book", lambda book_id, *a, **k: 0)
    assert worker._embed_parallel(con, c, RECIPE, slots=2)
    done = {u for (u,) in con.execute("SELECT uid FROM documents WHERE embed_done=1")}
    assert done == {r["uid"] for r in rows}, "the one embedded and the two cut to nothing"


# ---------------------------------------------------------------- the stream renewed
# gpu-saturation-plan.md, step 1 lever 1: at EMBED_STREAM_S a producer's
# stream drained to zero and waited for the next one's selection, held read
# and cut. Now it is renewed in place while nothing a new stream re-reads
# has changed.

def _endless(monkeypatch, con, n_groups: int):
    """`n_groups` groups of the fixture's documents, under fresh uids each
    time, then nothing: a selection that outlasts several deadlines."""
    base = con.execute("SELECT rowid AS rid, * FROM documents").fetchall()
    served = {"n": 0}

    def candidates(_con, _scope, _limit, _collection):
        if served["n"] >= n_groups:
            return []
        served["n"] += 1
        return base if served["n"] == 1 else [
            {**dict(r), "uid": f"{r['uid']}#{served['n']}"} for r in base]
    monkeypatch.setattr(worker, "_embed_candidates", candidates)
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])
    return served


def test_a_renewed_stream_never_drains_between_periods(tmp_path, monkeypatch):
    c, con = _collection(tmp_path)
    _endless(monkeypatch, con, n_groups=4)
    monkeypatch.setattr(config, "EMBED_STREAM_S", 0.0)       # every refill is past the deadline
    lock = threading.Lock()
    inside = {"n": 0, "zero_after_start": 0, "started": False}

    def upsert(*a, **k):
        with lock:
            inside["n"] += 1
            inside["started"] = True
        time.sleep(0.05)
        with lock:
            inside["n"] -= 1
        return 1
    monkeypatch.setattr(embeddings, "upsert_book", upsert)
    renewals = []
    monkeypatch.setattr(worker, "_endpoint_answered", lambda *a: None)
    worker._embed_parallel(con, c, RECIPE, slots=2, renew=lambda: renewals.append(1) or True)
    assert renewals, "the deadline asked to renew"
    logged = [m for (m,) in con.execute(
        "SELECT message FROM events WHERE stage='embed' AND message LIKE 'embedded %'")]
    total = sum(int(m.split()[1]) for m in logged)
    assert total == 12, f"the period lines add up to every document, once: {logged}"
    assert any(m.endswith("the stream goes on") for m in logged)


def test_without_renewal_the_stream_drains_at_its_deadline_as_before(tmp_path, monkeypatch):
    c, con = _collection(tmp_path)
    served = _endless(monkeypatch, con, n_groups=4)
    monkeypatch.setattr(config, "EMBED_STREAM_S", 0.0)
    monkeypatch.setattr(embeddings, "upsert_book", lambda *a, **k: 1)
    worker._embed_parallel(con, c, RECIPE, slots=2)
    assert served["n"] == 1, "past the deadline: no second group taken"
    worker._embed_parallel(con, c, RECIPE, slots=2, renew=lambda: False)
    assert served["n"] == 2, "renew said no: drained after one group again"


def test_a_deferral_ends_the_stream_whatever_renew_says(tmp_path, monkeypatch):
    c, con = _collection(tmp_path)
    served = _endless(monkeypatch, con, n_groups=4)
    monkeypatch.setattr(config, "EMBED_STREAM_S", 0.0)

    def down(*a, **k):
        raise embeddings.EmbedInterrupted("stopped")
    monkeypatch.setattr(embeddings, "upsert_book", down)
    worker._embed_parallel(con, c, RECIPE, slots=2, renew=lambda: True)
    assert served["n"] == 1, "deferred: no next group, the stream drains"


def test_a_document_done_and_queued_again_is_taken_again_in_the_same_stream(
        tmp_path, monkeypatch):
    """`seen` keeps the documents in flight or awaiting their commit out of
    the next selection; one that is done is out of it already, and is
    dropped. A renewed stream lives for hours: a document re-queued meanwhile
    (its text changed) must be taken again, and `seen` must not grow with
    every uid the stream ever took."""
    c, con = _collection(tmp_path)
    base = con.execute("SELECT rowid AS rid, * FROM documents").fetchall()
    calls = {"n": 0, "again": None}

    def candidates(_con, _scope, _limit, _collection):
        calls["n"] += 1
        if calls["n"] == 1:
            return base
        if calls["again"] is None:
            done = _con.execute("SELECT uid FROM documents WHERE embed_done=1 LIMIT 1").fetchone()
            if done is None:
                return []
            calls["again"] = done[0]
            _con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (done[0],))
            return [r for r in base if r["uid"] == done[0]]
        return []
    monkeypatch.setattr(worker, "_embed_candidates", candidates)
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])
    taken = []

    def upsert(book_id, *a, **k):
        taken.append(book_id)
        time.sleep(0.02)
        return 1
    monkeypatch.setattr(embeddings, "upsert_book", upsert)
    worker._embed_parallel(con, c, RECIPE, slots=1)
    again = next(int(r["native_id"]) for r in base if r["uid"] == calls["again"])
    assert taken.count(again) == 2, taken
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 3


def test_the_producers_renewal_stops_for_a_changed_file_a_removed_collection_or_a_switch_off(
        tmp_path, monkeypatch):
    c, con = _collection(tmp_path)
    got = {}

    def run(con_, coll, recipe, *, slots, keep_going, renew):
        got["renew"] = renew
        return False
    monkeypatch.setattr(worker, "_embed_parallel", run)
    monkeypatch.setattr(worker, "_may_embed", lambda con_, coll: got.get("may", True))
    monkeypatch.setattr(collections_mod, "recipe_for", lambda coll: RECIPE)
    me = threading.current_thread()
    gone = threading.Event()
    monkeypatch.setitem(worker._embed_producers, c.name, (me, gone))
    try:
        worker._producer_step(con, c)
        renew = got["renew"]
        assert renew() is True, "nothing changed: the stream goes on"
        got["may"] = False
        assert renew() is False, "switched off, unlicensed, under maintenance or failing"
        got["may"] = True
        monkeypatch.setattr(worker, "_live_collection",
                            lambda coll: replace(coll, title="renamed"))
        assert renew() is False, "its file changed: the next stream reads it"
        monkeypatch.setattr(worker, "_live_collection", lambda coll: coll)
        gone.set()
        assert renew() is False, "removed: this producer stops"
        gone.clear()
        worker._embed_producers[c.name] = (threading.Thread(target=lambda: None), gone)
        assert renew() is False, "another thread is its producer now"
    finally:
        worker._embed_producers.pop(c.name, None)


def test_an_unchanged_collection_read_again_is_equal_so_its_stream_can_be_renewed(
        isolated_config, data_dir, config_dir):
    """The renewal compares the live collection with the one its stream
    started from. A field that differed between two reads of the same file
    would refuse every renewal, and the lever would silently do nothing."""
    from tests.conftest import codex_collection
    codex_collection("steady", louise_root=data_dir, config_dir=config_dir)
    first = collections_mod.by_name("steady")
    again = collections_mod.by_name("steady")
    assert first is not again and first == again
    assert collections_mod.recipe_for(first) == collections_mod.recipe_for(again)


def test_a_renewal_refused_once_is_not_asked_again_while_the_stream_drains(
        tmp_path, monkeypatch):
    c, con = _collection(tmp_path)
    _endless(monkeypatch, con, n_groups=4)
    monkeypatch.setattr(config, "EMBED_STREAM_S", 0.0)
    monkeypatch.setattr(embeddings, "upsert_book", lambda *a, **k: (time.sleep(0.02), 1)[1])
    asked = []
    worker._embed_parallel(con, c, RECIPE, slots=2, renew=lambda: asked.append(1) and False)
    assert len(asked) == 1
