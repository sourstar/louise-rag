"""Per-collection pipeline threads: rdf/fts run on one dedicated thread per
collection (never two writers on one tantivy index) while the main loop
keeps embed — the GPU-serial-across-collections doctrine needs one owner.
The stage split is enforced by selection: the pipeline selector never
returns embed work, the main loop's _next_embed never returns rdf/fts, and
a document is embed-eligible only after its fts commit, so the two sides
can never claim the same document.
"""
import shutil
import threading
import time
from pathlib import Path

from app import collections as collections_mod, config, db, discovery, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _collection(tmp_path, name, corpus_dir):
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")


def _discovered(tmp_path, name):
    corpus = tmp_path / f"corpus_{name}"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = _collection(tmp_path, name, corpus)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def test_next_embed_never_selects_rdf_or_fts(tmp_path):
    c, con = _discovered(tmp_path, "alpha")
    scope = db.get_scope(con)          # default embed_mode=lazy, empty queue
    assert worker._next_embed(con, scope) == (None, None), \
        "rdf/fts-pending documents must be invisible to the embed selector"
    # and the pipeline selector sees exactly that pending rdf work
    stage, row = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "rdf" and row is not None


def test_main_loop_step_leaves_rdf_fts_alone_when_pipeline_active(tmp_path):
    c, con = _discovered(tmp_path, "alpha")
    did = worker._run_one_step(con, c, {}, pipeline_active=True)
    assert did is False
    assert con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0] in (0, None)


def test_pipeline_worker_drains_rdf_and_fts_then_idles(tmp_path, monkeypatch):
    c, con = _discovered(tmp_path, "alpha")
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker, args=(c, gone))
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            row = con.execute(
                "SELECT COUNT(*) n, SUM(rdf_done) r, SUM(fts_done) f "
                "FROM documents").fetchone()
            if row["n"] == 3 and row["r"] == 3 and row["f"] == 3:
                break
            time.sleep(0.1)
        assert (row["r"], row["f"]) == (3, 3), "pipeline thread must finish rdf+fts"
        assert con.execute(
            "SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0, \
            "a pipeline thread must NEVER embed"
    finally:
        gone.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_two_collections_progress_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    a, con_a = _discovered(tmp_path, "alpha")
    b, con_b = _discovered(tmp_path, "beta")
    stops = []
    threads = []
    for c in (a, b):
        gone = threading.Event()
        t = threading.Thread(target=worker._pipeline_worker, args=(c, gone))
        stops.append(gone)
        threads.append(t)
        t.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            done_a = con_a.execute("SELECT SUM(fts_done) FROM documents").fetchone()[0]
            done_b = con_b.execute("SELECT SUM(fts_done) FROM documents").fetchone()[0]
            if done_a == 3 and done_b == 3:
                break
            time.sleep(0.1)
        assert done_a == 3 and done_b == 3
    finally:
        for gone in stops:
            gone.set()
        for t in threads:
            t.join(timeout=10)


def test_paused_event_holds_the_pipeline(tmp_path, monkeypatch):
    c, con = _discovered(tmp_path, "alpha")
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    worker._paused.set()
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker, args=(c, gone))
    thread.start()
    try:
        time.sleep(1.0)
        assert con.execute(
            "SELECT COALESCE(SUM(rdf_done),0) FROM documents").fetchone()[0] == 0, \
            "a paused worker's pipeline threads must not process documents"
        worker._paused.clear()
        deadline = time.time() + 30
        while time.time() < deadline:
            if con.execute("SELECT SUM(fts_done) FROM documents").fetchone()[0] == 3:
                break
            time.sleep(0.1)
        assert con.execute("SELECT SUM(fts_done) FROM documents").fetchone()[0] == 3
    finally:
        worker._paused.clear()
        gone.set()
        thread.join(timeout=10)


def test_sync_pipeline_threads_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 1)
    worker._pipeline_threads.clear()
    a, _ = _discovered(tmp_path, "alpha")
    b, _ = _discovered(tmp_path, "beta")
    try:
        worker._sync_pipeline_threads([a, b])
        assert set(worker._pipeline_threads) == {"alpha", "beta"}
        assert all(t.is_alive() for t, _ in worker._pipeline_threads.values())

        # removal signals the thread and drops the entry
        _, gone_b = worker._pipeline_threads["beta"]
        worker._sync_pipeline_threads([a])
        assert set(worker._pipeline_threads) == {"alpha"}
        assert gone_b.is_set()

        # disabled -> no threads are ever started
        monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 0)
        worker._sync_pipeline_threads([a, b])
        assert set(worker._pipeline_threads) == {"alpha"}
    finally:
        for _, gone in worker._pipeline_threads.values():
            gone.set()
        worker._join_pipeline_threads(timeout=10)
        worker._pipeline_threads.clear()
