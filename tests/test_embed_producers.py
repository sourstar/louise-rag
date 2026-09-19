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
               held=None, sink=None):
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
