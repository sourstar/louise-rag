"""Discovery is a resumable, batched, off-thread stage.

Observed live (wikivec, 2026-08-23): every worker restart re-ran an
18-minute startup scan on the collection's pipeline thread — no rdf, fts or
genesis for its duration — and an interrupted scan lost all of its progress
because every plugin saved its cursor once, at the very end. Restarts are
frequent. Three properties are pinned here:

1. BATCHED. sync_collection commits every DISCOVER_BATCH_REFS refs in one
   transaction (it used to autocommit per row), and tells the plugin after
   each commit (DiscoverState.committed, on_commit).
2. RESUMABLE. A plugin stages a container into its cursor when it has
   yielded it, and cursor.Progress promotes it only once its last ref is
   committed — so an interrupted scan (SIGTERM, collection removed) resumes
   from exactly the containers whose rows are durable, never skipping one
   whose rows died with the transaction, never re-enumerating one that is
   done. The embedded plugin's boundary straddler — yielded only when the
   NEXT shard starts — makes this subtle, and it is where the losses would
   hide: a straddler must survive the interruption with both segments, and
   a rescanned shard whose predecessor is skipped must recover the carry
   from that predecessor's tail instead of minting a one-segment document.
3. OFF-THREAD. The worker runs a scan on its own thread beside the pipeline
   thread, one per collection at most, and the pipeline keeps working.
"""
import json
import threading
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import collections as collections_mod, db, discovery, worker
from app.plugins import cursor
from app.plugins.base import DiscoverState, DocRef
from app.plugins.embedded.source import EmbeddedSource, split_locator

DIM = 4


def _write_shard(path: Path, specs, rows_per_group: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [np.ones(DIM, dtype=np.float32).tolist() for _ in specs],
    })
    pq.write_table(table, path, row_group_size=rows_per_group)


def _collection(tmp_path, name="wikivec"):
    return collections_mod.Collection(
        name=name, plugin="embedded", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")


def _docs(con):
    return {r["uid"]: r["path"] for r in con.execute("SELECT uid, path FROM documents")}


def _cursor(c):
    p = c.manifest_path.parent / "embedded_scan_cursor.json"
    return json.loads(p.read_text()) if p.exists() else {}


class _StopAfter:
    """A stop signal that trips once is_set() has been asked `n` times —
    deterministic interruption mid-scan."""
    def __init__(self, n: int) -> None:
        self.n = n
        self.asked = 0

    def is_set(self) -> bool:
        self.asked += 1
        return self.asked > self.n


@pytest.fixture
def corpus(tmp_path):
    """Four shards, one language. Article 12 straddles 001/002; article 30
    straddles 002/003. 003 is the tail."""
    root = tmp_path / "corpus"
    _write_shard(root / "data/tr/000.parquet", [("10", 0, "A", "a0"), ("11", 0, "B", "b0")])
    _write_shard(root / "data/tr/001.parquet", [("12", 0, "C", "c0"), ("12", 1, "C", "c1")])
    _write_shard(root / "data/tr/002.parquet", [("12", 2, "C", "c2"), ("20", 0, "D", "d0"),
                                                 ("30", 0, "E", "e0")])
    _write_shard(root / "data/tr/003.parquet", [("30", 1, "E", "e1"), ("31", 0, "F", "f0")])
    return root


EXPECTED = {  # uid suffix -> number of locator segments
    "10": 1, "11": 1, "12": 2, "20": 1, "30": 2, "31": 1,
}


def _uid(article):
    return f"embedded:{10 * 10_000_000_000 + int(article)}"


# --- 1. batched -------------------------------------------------------------

class _FakeSource:
    source_id = "fake"
    chunking_profile = "book"

    def __init__(self, n):
        self.n = n
        self.state = None
        self.commits: list[int] = []

    def discover(self, state):
        self.state = state
        state.on_commit.append(lambda: self.commits.append(state.committed))
        for i in range(1, self.n + 1):
            yield DocRef(source_id="fake", native_id=str(i), locator=f"/x/{i}",
                         mtime=1, size=1)

    def fetch(self, ref):
        raise AssertionError("nothing changed; fetch must not be called")


def test_scan_commits_in_batches_and_signals_the_plugin(tmp_path, monkeypatch):
    fake = _FakeSource(4500)
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", fake)
    c = collections_mod.Collection(
        name="fk", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "fk" / "manifest.db",
        fts_dir=tmp_path / "fk" / "fts", lance_dir=tmp_path / "fk" / "lancedb")
    con = db.connect(c)
    begins = []
    con.set_trace_callback(lambda s: begins.append(s) if s.strip().upper().startswith("BEGIN") else None)

    stats = discovery.sync_collection(con, c)
    assert stats["added"] == 4500
    assert len(begins) == 3, f"4500 refs at {discovery.DISCOVER_BATCH_REFS}/batch = 3 transactions, got {len(begins)}"
    assert fake.commits == [2000, 4000, 4500], fake.commits
    assert fake.state.received == 4500


# --- 2. resumable -----------------------------------------------------------

def test_an_interrupted_scan_resumes_without_losing_or_duplicating(corpus, tmp_path):
    c = _collection(tmp_path)
    con = db.connect(c)

    # interrupt after 3 refs (10, 11, 12 — the straddler is the third)
    first = discovery.sync_collection(con, c, stop=_StopAfter(3))
    assert first.get("interrupted") is True
    done_uids = set(_docs(con))
    assert len(done_uids) == 3
    saved = _cursor(c)
    # whatever the cursor remembers, every remembered shard's documents are
    # all in the manifest — that is the invariant, and 000 at least
    assert "data/tr/000.parquet" in saved
    assert "data/tr/003.parquet" not in saved

    # resume: the rest arrives, nothing is lost, nothing duplicated
    second = discovery.sync_collection(con, c)
    assert second.get("interrupted") is None
    docs = _docs(con)
    assert set(docs) == {_uid(a) for a in EXPECTED}, docs
    for a, segs in EXPECTED.items():
        assert len(split_locator(docs[_uid(a)])) == segs, (a, docs[_uid(a)])
    assert second["seen"] < len(EXPECTED), \
        "the resumed scan must skip the shards it had already committed"
    assert set(_cursor(c)) == {f"data/tr/00{i}.parquet" for i in range(4)}

    # and a third scan is a pure stat pass
    third = discovery.sync_collection(con, c)
    assert third["seen"] == 0


@pytest.mark.parametrize("stop_after", [1, 2, 4, 5, 6])
def test_every_interruption_point_resumes_cleanly(corpus, tmp_path, stop_after):
    c = _collection(tmp_path)
    con = db.connect(c)
    discovery.sync_collection(con, c, stop=_StopAfter(stop_after))
    discovery.sync_collection(con, c)
    docs = _docs(con)
    assert set(docs) == {_uid(a) for a in EXPECTED}
    for a, segs in EXPECTED.items():
        assert len(split_locator(docs[_uid(a)])) == segs, (stop_after, a, docs[_uid(a)])
    assert discovery.sync_collection(con, c)["seen"] == 0


def test_a_straddler_keeps_both_segments_when_its_first_shard_is_skipped(corpus, tmp_path):
    """The rescanned-second-shard case: 001 remembered, 002 not (an
    acquisition landing 002 mid-scan, or a resume). Without recovering the
    carry from 001's tail, article 12 would be 'relocated' to a one-segment
    locator — silently losing its first rows."""
    c = _collection(tmp_path)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    docs = _docs(con)
    assert len(split_locator(docs[_uid("12")])) == 2

    saved = _cursor(c)
    del saved["data/tr/002.parquet"]                 # forget 002 only
    (c.manifest_path.parent / "embedded_scan_cursor.json").write_text(json.dumps(saved))
    stats = discovery.sync_collection(con, c)
    assert stats.get("relocated", 0) == 0 and stats["changed"] == 0, stats
    after = _docs(con)
    assert after[_uid("12")] == docs[_uid("12")], "the straddler lost a segment"
    assert after[_uid("30")] == docs[_uid("30")]


def test_a_continuation_shard_arriving_later_is_a_change_not_a_relocation(tmp_path):
    """000 scanned alone: its last article is a one-segment document. Then
    001 arrives continuing it: the document gains a segment, and because its
    bytes changed it is re-processed (flags reset), never silently swapped."""
    root = tmp_path / "corpus"
    _write_shard(root / "data/tr/000.parquet", [("10", 0, "A", "a0"), ("12", 0, "C", "c0")])
    c = _collection(tmp_path)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    assert len(split_locator(_docs(con)[_uid("12")])) == 1
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1")

    _write_shard(root / "data/tr/001.parquet", [("12", 1, "C", "c1"), ("13", 0, "D", "d0")])
    stats = discovery.sync_collection(con, c)
    assert stats["changed"] == 1 and stats["added"] == 1, stats
    row = con.execute("SELECT path, rdf_done, fts_done FROM documents WHERE uid=?",
                      (_uid("12"),)).fetchone()
    assert len(split_locator(row["path"])) == 2
    assert (row["rdf_done"], row["fts_done"]) == (0, 0)


def test_tail_carry_crosses_row_groups(tmp_path):
    """The carry may start before the previous shard's last row group."""
    root = tmp_path / "corpus"
    _write_shard(root / "data/tr/000.parquet",
                 [("10", 0, "A", "a0")] + [("12", s, "C", f"c{s}") for s in range(5)],
                 rows_per_group=2)
    _write_shard(root / "data/tr/001.parquet", [("12", 5, "C", "c5"), ("13", 0, "D", "d0")])
    c = _collection(tmp_path)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    full = _docs(con)[_uid("12")]
    saved = _cursor(c)
    del saved["data/tr/001.parquet"]
    (c.manifest_path.parent / "embedded_scan_cursor.json").write_text(json.dumps(saved))
    discovery.sync_collection(con, c)
    assert _docs(con)[_uid("12")] == full
    segs = split_locator(full)
    assert segs[0][1:] == (1, 6) and segs[1][1:] == (0, 1), segs


def test_progress_promotes_only_what_is_committed(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    g = tmp_path / "b.bin"
    g.write_bytes(b"y")
    state = DiscoverState(corpus_root=tmp_path, state_dir=tmp_path)
    p = cursor.Progress(state, "t")
    state.received = 5
    p.stage(f)                      # complete at 5
    state.received = 9
    p.stage(g)                      # complete at 9
    state.committed = 6
    p.flush()
    saved = cursor.load(tmp_path, "t")
    assert "a.bin" in saved and "b.bin" not in saved
    state.committed = 9
    p.flush()
    assert set(cursor.load(tmp_path, "t")) == {"a.bin", "b.bin"}


def test_bare_discover_without_an_orchestrator_still_saves_its_cursor(corpus, tmp_path):
    """list(plugin.discover(state)) — tests and tools do this — must keep
    working: nothing advances `committed`, so everything is promoted at
    the plugin's final flush."""
    state = DiscoverState(corpus_root=corpus, state_dir=tmp_path)
    assert len(list(EmbeddedSource().discover(state))) == len(EXPECTED)
    assert len(json.loads((tmp_path / "embedded_scan_cursor.json").read_text())) == 4
    assert list(EmbeddedSource().discover(state)) == []


# --- 3. off-thread ----------------------------------------------------------

def test_the_scan_runs_beside_the_pipeline_thread(corpus, tmp_path, monkeypatch):
    c = _collection(tmp_path)
    db.connect(c).close()
    gone = threading.Event()
    started = threading.Event()
    release = threading.Event()
    real = discovery.sync_collection

    def slow_scan(con, collection, *, stop=None):
        started.set()
        assert release.wait(5), "test harness"
        return real(con, collection, stop=stop)
    monkeypatch.setattr(discovery, "sync_collection", slow_scan)
    monkeypatch.setattr(worker, "_acquire", lambda *a, **k: None)

    assert worker._start_scan(c, "test", False, gone) is True
    assert started.wait(5)
    assert worker.scan_running(c.name) is True, "the caller is not blocked: the scan is on its own thread"
    assert worker._start_scan(c, "test", False, gone) is False, "one scan per collection at a time"
    release.set()
    t = worker._scan_threads.get(c.name)
    t.join(10)
    assert worker.scan_running(c.name) is False
    assert c.name in worker._last_discover
    assert set(_docs(db.connect(c))) == {_uid(a) for a in EXPECTED}


def test_a_stop_request_ends_the_scan_with_progress_committed(corpus, tmp_path, monkeypatch):
    c = _collection(tmp_path)
    db.connect(c).close()
    gone = threading.Event()
    monkeypatch.setattr(worker, "_acquire", lambda *a, **k: None)
    # make every ref slow enough that `gone` lands mid-scan
    real_discover = EmbeddedSource.discover

    def slow_discover(self, state):
        for ref in real_discover(self, state):
            time.sleep(0.05)
            yield ref
    monkeypatch.setattr(EmbeddedSource, "discover", slow_discover)
    assert worker._start_scan(c, "test", False, gone)
    time.sleep(0.12)
    gone.set()
    worker._scan_threads[c.name].join(10)
    con = db.connect(c)
    partial = _docs(con)
    assert 0 < len(partial) < len(EXPECTED), partial
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='discover'")]
    assert any("interrupted" in m for m in events), events
    # resume completes it
    monkeypatch.setattr(EmbeddedSource, "discover", real_discover)
    discovery.sync_collection(con, c)
    assert set(_docs(con)) == {_uid(a) for a in EXPECTED}
