"""The text-index stage's marks, batched, and its look-ahead (app/lookahead.py).

München, 2026-11-07, wikipedia fts at ~503 documents a second: the pipeline
thread waited on the parse pool 52.4 % of its time and wrote the marks
(37.6 %) and tags (7.0 %) after it, one after the other — one UPDATE per
document, found by uid. Now the marks are one statement by rowid, and the
next batch parses while this one is written.

What must not move, and is pinned here:
- the marks and tags write the same columns and values as before;
- no document is marked before ITS batch's index commit, look-ahead or not;
- a stop between batches marks nothing of the batch parsed ahead, and loses
  nothing (a parse at most);
- a row that changed after its parse is not written from that parse;
- every wait keeps its bound.
And what it costs, as counts: statements per batch, parses per document, the
plans at the sizes sent (scheduling.md §6).
"""
import collections
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app import collections as collections_mod
from app import config, db, discovery, entities, fts, parallel, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


# ---- a synthetic collection, and a parse the test controls -----------------

def _synthetic(tmp_path, n: int, name: str = "s"):
    c = collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / f"corpus_{name}", manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")
    con = db.connect(c)
    con.execute("BEGIN")
    con.executemany(
        "INSERT INTO documents(uid,source,native_id,path,lang,title,in_scope,rdf_done,fts_done,"
        "embed_done,updated_at) VALUES (?,?,?,?,?,?,1,1,0,0,1)",
        [(f"s:{i}", "s", str(i), f"/x/{i}", "en", f"title {i}") for i in range(1, n + 1)])
    con.execute("COMMIT")
    return c, con


def _chunks_of(i: int) -> list[dict]:
    return [{"id": f"s:{i}:{k}", "chapter": "", "text": f"word{i} common text", "tokens": 10 + i + k}
            for k in range(1 + i % 3)]


def _entities_of(i: int) -> list[str]:
    return [f"Q{i % 5}", f"Q{100 + i % 7}"] if i % 2 else []


class Parse:
    """`parallel.fts_for_rows`, deterministic per row; records who asked for
    what, and can be held (an Event) or failed per document."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.lock = threading.Lock()
        self.calls: list[tuple[str, list[dict]]] = []
        self.fail: set[int] = set()
        self.hold: threading.Event | None = None

    def __call__(self, collection, rows, *, names):
        rows = [dict(r) for r in rows]
        ahead = threading.current_thread().name.startswith("fts-ahead-")
        with self.lock:
            self.calls.append((threading.current_thread().name, rows))
        if ahead and self.hold is not None:
            self.hold.wait(30)
        if ahead and self.delay:
            time.sleep(self.delay)
        out = []
        for r in rows:
            i = int(r["native_id"])
            if i in self.fail:
                out.append((False, {"type": "RuntimeError", "msg": f"cannot parse {i}"}))
            else:
                out.append((True, {"chunks": _chunks_of(i), "names": None, "entities": _entities_of(i)}))
        return out

    def parsed(self) -> collections.Counter:
        with self.lock:
            return collections.Counter(r["uid"] for _t, rows in self.calls for r in rows)


@pytest.fixture
def parse(monkeypatch):
    p = Parse()
    monkeypatch.setattr(parallel, "fts_for_rows", p)
    return p


@pytest.fixture
def overlap(monkeypatch):
    """As on a pipeline thread of a node with a parse pool."""
    monkeypatch.setattr(parallel, "prefetch_allowed", lambda: True)
    monkeypatch.setattr(config, "FTS_OVERLAP", 1)
    worker._ahead_here.on = True
    yield
    worker._ahead_here.on = False
    worker.drop_fts_ahead()


@pytest.fixture
def instrumented(monkeypatch):
    """Every index commit and every mark, in order; a mark of a document whose
    index commit has not landed fails the test where it happens."""
    log = {"committed": set(), "events": [], "marked": set(), "ahead_at_mark": []}
    real_index = fts.index_books
    real_mark = worker._mark_indexed_many

    def index_books(entries, collection=None):
        entries = list(entries)
        real_index(entries, collection)
        log["committed"].update(int(e[1]) for e in entries)
        log["events"].append(("commit", sorted(int(e[1]) for e in entries)))

    def mark(con, marks, now):
        late = [uid for _rid, uid, *_ in marks if int(uid.split(":")[1]) not in log["committed"]]
        assert not late, f"marked before their index commit: {late}"
        log["events"].append(("mark", sorted(int(uid.split(":")[1]) for _r, uid, *_ in marks)))
        log["marked"].update(uid for _r, uid, *_ in marks)
        held = worker._fts_ahead.keys()
        log["ahead_at_mark"].append(bool(held))
        real_mark(con, marks, now)

    monkeypatch.setattr(fts, "index_books", index_books)
    monkeypatch.setattr(worker, "_mark_indexed_many", mark)
    return log


def _round(con, c) -> bool:
    stage, row = worker._next_book(con, db.get_scope(con), embed_allowed=False)
    if row is None:
        return False
    assert stage == "fts"
    worker._execute_step(con, c, stage, row)
    return True


def _drain(con, c, rounds: int = 200) -> int:
    n = 0
    while n < rounds and _round(con, c):
        n += 1
    return n


def _expected(i: int) -> tuple:
    ch = _chunks_of(i)
    return (1, config.FTS_VERSION, config.ENTITY_VERSION, len(ch), sum(x["tokens"] for x in ch))


def _assert_all_indexed_once(con, c, n: int) -> None:
    got = {int(r["native_id"]): (r["fts_done"], r["fts_version"], r["entity_version"],
                                 r["chunks"], r["tokens"])
           for r in con.execute("SELECT * FROM documents")}
    assert got == {i: _expected(i) for i in range(1, n + 1)}
    keys = sorted((r["uid"], r["entity_id"]) for r in con.execute("SELECT * FROM doc_keys"))
    assert keys == sorted((f"s:{i}", e) for i in range(1, n + 1) for e in _entities_of(i))
    # the index holds each document's chunks exactly once (delete-then-insert)
    counts = fts.chunk_counts(c, list(range(1, n + 1)))
    assert counts == {i: len(_chunks_of(i)) for i in range(1, n + 1)}


# ---- 1. the marks: the same rows written, in a constant number of statements --

def _old_writes(con, batch, now):
    """The writes of main 1f83b70, verbatim: per document, its tags then its
    mark, found by uid — the oracle."""
    for r, chunks, ents in batch:
        entities.write_tags(con, r["uid"], ents)
        con.execute(
            "UPDATE documents SET fts_done=1,fts_version=?,entity_version=?,chunks=?,tokens=?,"
            "updated_at=? WHERE uid=?",
            (config.FTS_VERSION, config.ENTITY_VERSION, len(chunks),
             sum(c["tokens"] for c in chunks), now, r["uid"]))


def test_the_batched_marks_and_tags_write_exactly_what_one_document_at_a_time_wrote(tmp_path):
    c, con = _synthetic(tmp_path, 300)
    # tags already there for some documents: the delete half must remove them
    con.execute("BEGIN")
    con.executemany("INSERT INTO doc_keys(uid, entity_id) VALUES (?,?)",
                    [(f"s:{i}", "Qstale") for i in range(1, 301, 4)])
    con.execute("COMMIT")
    con.close()
    shutil.copy(c.manifest_path, tmp_path / "copy.db")
    batch = []
    with sqlite3.connect(c.manifest_path) as probe:
        probe.row_factory = sqlite3.Row
        for r in probe.execute("SELECT rowid AS rid, * FROM documents WHERE rowid % 3 <> 0"):
            i = int(r["native_id"])
            batch.append((dict(r), _chunks_of(i), _entities_of(i)))
    now = 1_790_000_000

    def run(path, writes):
        con = sqlite3.connect(path, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        writes(con)
        con.execute("COMMIT")
        out = (con.execute("SELECT rowid, * FROM documents ORDER BY rowid").fetchall(),
               con.execute("SELECT * FROM doc_keys ORDER BY uid, entity_id").fetchall())
        con.close()
        return out

    before = run(tmp_path / "copy.db", lambda con: _old_writes(con, batch, now))
    after = run(c.manifest_path, lambda con: (
        worker._mark_indexed_many(con, [(r["rid"], r["uid"], len(ch), sum(x["tokens"] for x in ch))
                                        for r, ch, _e in reversed(batch)], now),
        entities.write_tags_many(con, {r["uid"]: e for r, _c, e in batch})))
    assert after == before
    # not vacuous: every document of the batch was marked (updated_at, the
    # last column), the others were not, and the stale tags went
    assert sum(row[-1] == now for row in after[0]) == len(batch) == 200
    assert ("s:3", "Qstale") not in after[1] and ("s:1", "Qstale") not in after[1]


def test_the_mark_reads_the_row_by_its_rowid_and_never_descends_the_uid_index(tmp_path):
    _c, con = _synthetic(tmp_path, 2_000)
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN " + worker._MARK_INDEXED, (1, 1, 2, 30, 0, 7, "s:7")))
    assert "USING INTEGER PRIMARY KEY (rowid=?)" in plan, plan
    assert "sqlite_autoindex_documents_1" not in plan and "idx_documents" not in plan, plan
    # and the tag delete walks doc_keys' own key
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN DELETE FROM doc_keys WHERE uid=?", ("s:7",)))
    assert "sqlite_autoindex_doc_keys_1 (uid=?)" in plan, plan


def test_a_rowid_that_now_holds_another_document_marks_nothing(tmp_path):
    """Rowids are stable — nothing VACUUMs a manifest — but a deleted row's id
    can be taken by the next insert. The mark checks the uid on the row it
    found: the other document is untouched, and ours simply re-runs."""
    _c, con = _synthetic(tmp_path, 3)
    rid = con.execute("SELECT rowid FROM documents WHERE uid='s:3'").fetchone()[0]
    con.execute("DELETE FROM documents WHERE uid='s:3'")
    con.execute("INSERT INTO documents(uid,source,native_id,path,rdf_done) VALUES ('s:9','s','9','/x/9',1)")
    assert con.execute("SELECT rowid FROM documents WHERE uid='s:9'").fetchone()[0] == rid
    worker._mark_indexed_many(con, [(rid, "s:3", 2, 30)], 1)
    assert tuple(con.execute("SELECT fts_done, chunks FROM documents WHERE uid='s:9'").fetchone()) == (0, 0)


_WRITES = ("UPDATE documents SET", "DELETE FROM doc_keys", "INSERT INTO doc_keys")


def _statements_of_a_batch(tmp_path, monkeypatch, n: int, name: str) -> collections.Counter:
    c, con = _synthetic(tmp_path, n, name)
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", n)
    seen: collections.Counter = collections.Counter()

    def counting(method):
        def call(sql, *args):
            for write in _WRITES:
                if sql.lstrip().startswith(write):
                    seen[write] += 1
            return method(sql, *args)
        return call
    con.execute = counting(con.execute)
    con.executemany = counting(con.executemany)
    row = con.execute(worker._FTS_FRESH, (config.MAX_RETRIES,)).fetchone()
    worker._execute_step(con, c, "fts", row)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0] == n
    return seen


def test_a_batch_marks_and_tags_in_a_constant_number_of_statements(tmp_path, monkeypatch, parse):
    """A COST test: the writes of a batch are three statements however many
    documents it holds. Before, each document was two or three (its tags'
    delete and insert, its mark) — 37.6 % + 7.0 % of the pipeline thread."""
    small = _statements_of_a_batch(tmp_path, monkeypatch, 6, "small")
    large = _statements_of_a_batch(tmp_path, monkeypatch, 60, "large")
    assert small == large == dict.fromkeys(_WRITES, 1)


# ---- 2. the look-ahead's queries, at the sizes they are sent ----------------

def test_the_next_batch_is_walked_from_a_rowid_on_the_scope_index_and_never_sorted(tmp_path):
    _c, con = _synthetic(tmp_path, 5_000)
    for limit in (1, 500, 4_000):
        plan = " ".join(r[-1] for r in con.execute(
            "EXPLAIN QUERY PLAN " + worker._FTS_NEXT, (100, config.MAX_RETRIES, limit)))
        assert ("idx_documents_scope (in_scope=? AND rdf_done=? AND fts_done=? AND embed_done=? "
                "AND rowid>?)") in plan, plan
        assert "TEMP B-TREE" not in plan and "SCAN documents" not in plan, plan
    got = con.execute(worker._FTS_NEXT, (100, config.MAX_RETRIES, 3)).fetchall()
    assert [r["uid"] for r in got] == ["s:101", "s:102", "s:103"] and got[0]["rid"] == 101


def test_the_recheck_reads_its_rows_by_rowid_in_parts_of_the_size_checked(tmp_path):
    _c, con = _synthetic(tmp_path, 1_200)
    for n in (1, worker._FTS_RECHECK_PART):
        sql = worker._FTS_RECHECK.format(marks=",".join("?" * n))
        plan = " ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + sql, list(range(1, n + 1))))
        assert "USING INTEGER PRIMARY KEY (rowid=?)" in plan, plan
        assert "LIST SUBQUERY" not in plan and "SCAN documents" not in plan, plan
    sizes = []
    real = con.execute
    con.execute = lambda sql, *a: (sizes.append(sql.count("?")) if "rowid IN" in sql else None) or real(sql, *a)
    got = worker._fts_recheck(con, list(range(1, 1_201)))
    assert len(got) == 1_200 and max(sizes) == worker._FTS_RECHECK_PART and len(sizes) == 3


# ---- 3. the overlap: ordering, cost, stop, change, bound ---------------------

def test_no_document_is_marked_before_its_own_index_commit_under_the_overlap(
        tmp_path, monkeypatch, parse, overlap, instrumented):
    """Ten documents a batch and a chunk cap that cuts most batches short: the
    next batch is handed to the pool before this one's index commit, and each
    batch's marks name only documents whose commit has landed (asserted at
    every mark by `instrumented`)."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 10)
    monkeypatch.setattr(config, "FTS_BATCH_CHUNKS", 15)
    parse.delay = 0.05
    c, con = _synthetic(tmp_path, 97)
    rounds = _drain(con, c)
    assert rounds > 9
    _assert_all_indexed_once(con, c, 97)
    # it overlapped: while batch k's marks were written, batch k+1 was held
    # ahead — parsing, or carried — and every document after the first
    # batch was parsed beside the writes, never by the pipeline thread
    assert sum(instrumented["ahead_at_mark"]) >= rounds - 2
    first, *rest = parse.calls
    assert not first[0].startswith("fts-ahead-") and rest
    assert all(t.startswith("fts-ahead-") for t, _rows in rest)
    # every commit precedes the marks of the same documents, batch by batch
    commits = [set(ids) for kind, ids in instrumented["events"] if kind == "commit"]
    marks = [set(ids) for kind, ids in instrumented["events"] if kind == "mark"]
    assert len(commits) == len(marks) and all(m <= k for m, k in zip(marks, commits))


def test_each_document_is_parsed_once_the_rows_past_the_chunk_cap_are_carried(
        tmp_path, monkeypatch, parse, overlap):
    """A COST test. A batch stops at the chunk cap (wikipedia's: a median of
    20,002 chunks), and the peers parsed past it were thrown away and parsed
    again by the next batch. Now they are carried into it: one parse each.
    And after the first batch the pipeline thread starts no parse itself."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 10)
    monkeypatch.setattr(config, "FTS_BATCH_CHUNKS", 12)
    c, con = _synthetic(tmp_path, 80)
    _drain(con, c)
    _assert_all_indexed_once(con, c, 80)
    assert set(parse.parsed().values()) == {1}, parse.parsed().most_common(3)
    on_thread = [t for t, _rows in parse.calls if not t.startswith("fts-ahead-")]
    assert len(on_thread) == 1, on_thread


def test_without_the_overlap_the_stage_is_what_it_was(tmp_path, monkeypatch, parse, instrumented):
    """Off a pipeline thread (a deposit, a tool, the rollback main loop) — or
    with FTS_OVERLAP=0 — nothing is left parsing behind a batch."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 10)
    monkeypatch.setattr(parallel, "prefetch_allowed", lambda: True)
    c, con = _synthetic(tmp_path, 35)
    _drain(con, c)
    _assert_all_indexed_once(con, c, 35)
    assert not any(t.startswith("fts-ahead-") for t, _ in parse.calls)
    assert worker._fts_ahead.keys() == [] and not any(instrumented["ahead_at_mark"])


@pytest.mark.parametrize("why", ["switch", "stop", "mode", "profile"])
def test_nothing_is_left_parsing_when_the_node_would_not_parse_it(
        tmp_path, monkeypatch, parse, overlap, why):
    """What stops for SIGTERM stops for the mode (pitfall 16): after a stop,
    under a mode that holds parsing, or a node-profile limit below the pool,
    no next batch is handed to the pool."""
    if why == "switch":
        monkeypatch.setattr(config, "FTS_OVERLAP", 0)
    elif why == "stop":
        monkeypatch.setattr(worker, "_stop", threading.Event())
        worker._stop.set()
    elif why == "mode":
        monkeypatch.setattr(worker, "_acquire_only", threading.Event())
        worker._acquire_only.set()
    else:
        monkeypatch.setattr(parallel, "prefetch_allowed", lambda: False)
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    c, con = _synthetic(tmp_path, 20)
    assert _round(con, c)
    assert worker._fts_ahead.keys() == []
    assert not any(t.startswith("fts-ahead-") for t, _ in parse.calls)


def test_a_stop_between_batches_marks_nothing_of_the_batch_parsed_ahead(
        tmp_path, monkeypatch, parse, overlap, instrumented):
    """Batch 1 committed and marked; batch 2 parsing in the pool when the
    pipeline thread leaves (its `finally`). Nothing of batch 2 is in the index
    or marked, and the next start indexes it as if nothing happened."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    parse.hold = threading.Event()
    c, con = _synthetic(tmp_path, 12)
    assert _round(con, c)
    held = worker._fts_ahead.keys()
    assert held == [str(c.manifest_path)]
    worker.drop_fts_ahead(c)                       # the pipeline thread's exit
    parse.hold.set()                               # its parse answers, to nobody
    time.sleep(0.2)
    marked = {r["native_id"] for r in con.execute("SELECT native_id FROM documents WHERE fts_done=1")}
    assert marked == {"1", "2", "3", "4", "5"}
    assert instrumented["committed"] == {1, 2, 3, 4, 5}
    assert set(fts.chunk_counts(c, list(range(1, 13)))) == {1, 2, 3, 4, 5}
    # the next start: no look-ahead, so the stage selects and parses as before
    parse.hold = None
    _drain(con, c)
    _assert_all_indexed_once(con, c, 12)


def test_the_pipeline_thread_stops_with_every_commit_marked_and_nothing_ahead_kept(
        tmp_path, monkeypatch, parse, overlap, instrumented):
    """The real thread, stopped mid-run (the collection's removal: the same
    exit as SIGTERM, between rounds): what was committed is marked, what was
    marked was committed, and the batch parsed ahead is let go of."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    parse.delay = 0.05
    c, con = _synthetic(tmp_path, 60)
    gone = threading.Event()
    real_mark = worker._mark_indexed_many

    def mark_then_stop(con_, marks, now):
        real_mark(con_, marks, now)
        if len(instrumented["marked"]) >= 15:
            gone.set()
    monkeypatch.setattr(worker, "_mark_indexed_many", mark_then_stop)
    worker._ahead_here.on = False                 # only the pipeline thread's own flag counts
    thread = threading.Thread(target=worker._pipeline_worker, args=(c, gone))
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    marked = {int(r["native_id"]) for r in con.execute("SELECT native_id FROM documents WHERE fts_done=1")}
    assert marked and marked == instrumented["committed"]
    assert len(marked) < 60
    assert worker._fts_ahead.keys() == []
    assert set(fts.chunk_counts(c, list(range(1, 61)))) == marked


def test_a_deferral_between_the_commit_and_the_marks_redoes_the_batch_idempotently(
        tmp_path, monkeypatch, parse, overlap):
    """The manifest busy when the marks are written (a crash there is the same
    state): the batch is in the index and unmarked. The look-ahead started
    behind it no longer starts at the lowest pending row, so it is abandoned;
    the batch is indexed again over its own delete-then-insert."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 6)
    c, con = _synthetic(tmp_path, 30)
    real = worker._mark_indexed_many
    calls = {"n": 0}

    def busy_once(con_, marks, now):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        real(con_, marks, now)
    monkeypatch.setattr(worker, "_mark_indexed_many", busy_once)

    def marked():
        return {int(r[0]) for r in con.execute("SELECT native_id FROM documents WHERE fts_done=1")}
    assert _round(con, c) and marked() == set(range(1, 7))
    assert _round(con, c) and marked() == set(range(1, 7))       # 7-12 indexed, deferred
    # the very next round is the deferred batch's — not the look-ahead behind
    # it, which would leave it waiting for the end of the pending set
    assert _round(con, c) and marked() == set(range(1, 13))
    _drain(con, c)
    _assert_all_indexed_once(con, c, 30)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL OR retries>0").fetchone()[0] == 0


def test_a_row_that_changed_after_its_parse_is_parsed_again_not_written_from_it(
        tmp_path, monkeypatch, parse, overlap):
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    c, con = _synthetic(tmp_path, 15)
    assert _round(con, c)                                         # 1-5; 6-10 parsing ahead
    con.execute("UPDATE documents SET path='/y/8', updated_at=2 WHERE uid='s:8'")   # a rescan
    con.execute("UPDATE documents SET error='gone', retries=1 WHERE uid='s:9'")    # left the set
    assert _round(con, c)
    done = {r["native_id"] for r in con.execute("SELECT native_id FROM documents WHERE fts_done=1")}
    assert done == {str(i) for i in (1, 2, 3, 4, 5, 6, 7, 10)}
    _drain(con, c)
    paths = [r["path"] for _t, rows in parse.calls for r in rows if r["uid"] == "s:8"]
    assert paths == ["/x/8", "/y/8"], "parsed again from its new state"
    assert parse.parsed()["s:9"] == 1, "an errored row is not parsed again"
    assert con.execute("SELECT fts_done FROM documents WHERE uid='s:9'").fetchone()[0] == 0


def test_a_failed_peer_is_parsed_again_on_its_own_turn_and_errs_alone(
        tmp_path, monkeypatch, parse, overlap):
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    parse.fail = {3}
    c, con = _synthetic(tmp_path, 12)
    _drain(con, c)
    assert parse.parsed()["s:3"] == 2
    got = con.execute("SELECT fts_done, retries, error FROM documents WHERE uid='s:3'").fetchone()
    assert got["fts_done"] == 0 and got["retries"] == 1 and "cannot parse 3" in got["error"]
    assert con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0] == 11


def test_the_wait_for_a_batch_parsed_ahead_is_bounded(tmp_path, monkeypatch, parse, overlap):
    """A look-ahead whose parse never answers (a pool gone quiet): the next
    round waits PARSE_TIMEOUT_S for it, abandons it, and parses the batch
    itself — `parallel.compute`, with its own watchdog and fall-back."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 5)
    monkeypatch.setattr(config, "PARSE_TIMEOUT_S", 0.3)
    parse.hold = threading.Event()
    c, con = _synthetic(tmp_path, 10)
    try:
        assert _round(con, c)
        t0 = time.monotonic()
        assert _round(con, c)
        assert time.monotonic() - t0 < 5
        assert con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0] == 10
    finally:
        parse.hold.set()


# ---- 4. the real pool, end to end -----------------------------------------

def test_through_the_real_pool_the_overlap_indexes_what_the_serial_stage_does(
        tmp_path, monkeypatch, isolated_config):
    """gutenberg books through spawned children, a look-ahead in flight while
    each batch is written: the same marks, tags and index as the serial stage
    one batch at a time."""
    from tests.conftest import gutenberg_books
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 4)

    def build(name):
        corpus = gutenberg_books(tmp_path / f"corpus_{name}", 14)
        c = collections_mod.Collection(
            name=name, plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus,
            manifest_path=tmp_path / name / "manifest.db", fts_dir=tmp_path / name / "fts",
            lance_dir=tmp_path / name / "lancedb")
        con = db.connect(c)
        discovery.sync_collection(con, c)
        con.execute("UPDATE documents SET rdf_done=1")
        return c, con

    def state(con, c):
        rows = [tuple(r) for r in con.execute(
            "SELECT uid, fts_done, fts_version, entity_version, chunks, tokens FROM documents ORDER BY uid")]
        keys = [tuple(r) for r in con.execute("SELECT * FROM doc_keys ORDER BY uid, entity_id")]
        ids = [int(r["native_id"]) for r in con.execute("SELECT native_id FROM documents")]
        return rows, keys, fts.chunk_counts(c, ids)

    serial_c, serial_con = build("serial")
    _drain(serial_con, serial_c)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    parallel.shutdown()
    try:
        c, con = build("pooled")
        worker._ahead_here.on = True
        ahead_batches = []
        real_take = worker._fts_take_ahead

        def take(con_, row, collection):
            got = real_take(con_, row, collection)
            ahead_batches.append(got is not None)
            return got
        monkeypatch.setattr(worker, "_fts_take_ahead", take)
        _drain(con, c)
    finally:
        worker._ahead_here.on = False
        worker.drop_fts_ahead()
        parallel.shutdown()
    assert sum(ahead_batches) >= 2, ahead_batches
    assert state(con, c) == state(serial_con, serial_c)
    assert all(r[1] == 1 for r in state(con, c)[0])
