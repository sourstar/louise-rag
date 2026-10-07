"""The catalog index (app/catalog.py): title, authors and subjects in
tantivy, ranked top-k, so a search's catalog leg no longer costs what its
matches cost. The SQLite FTS5 catalog it replaces scored every match: on
pubmed's 40M records "cancer" took 57.3 s (2026-09-18)."""
import json

import pytest

from app import catalog, collections as collections_mod, db, fts, retrieval, worker

ROWS = [  # uid n, lang, title, authors, subjects
    (1, "en", "Moby Dick; Or, The Whale", "Melville, Herman", "Whaling -- Fiction"),
    (2, "de", "Moby Dick oder Der Wal", "Melville, Herman", "Walfang -- Roman"),
    (3, "en", "The Whale Road", "Low, Robert", "Vikings"),
    (4, "fr", "Les Misérables", "Hugo, Victor", "Paris -- Fiction"),
    (5, "en", "Aspirin and platelet aggregation", "Smith J", "Aspirin; Blood Platelets"),
]


@pytest.fixture
def col(isolated_config):
    c = collections_mod.default_collection()
    con = db.connect(c)
    for n, lang, title, authors, subjects in ROWS:
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,title,authors,subjects,"
                    "rdf_done,updated_at) VALUES(?,?,?,?,?,?,?,?,1,0)",
                    (f"gutenberg:{n}", "gutenberg", str(n), f"/f/{n}", lang, title, authors, subjects))
    yield c, con
    con.close()


def _ids(hits):
    return [h["book_id"] for h in hits]


def test_nothing_answers_until_the_backfill_is_complete(col):
    c, con = col
    assert catalog.search(con, c, "Moby", 10) is None, "incomplete: the caller falls back"
    out = catalog.backfill(con, c, budget_s=60, window=2)
    assert out["complete"] and out["written"] == 5 and out["windows"] == 3
    assert catalog.backfill(con, c) is None, "complete: nothing to do"
    assert sorted(_ids(catalog.search(con, c, "Moby", 10))) == [1, 2]


def test_every_word_must_match_in_some_field_with_folding_and_languages(col):
    c, con = col
    catalog.backfill(con, c, budget_s=60)
    assert _ids(catalog.search(con, c, "moby whale", 10)) == [1]      # AND, as FTS5
    assert sorted(_ids(catalog.search(con, c, "melville", 10))) == [1, 2]  # authors
    assert _ids(catalog.search(con, c, "vikings", 10)) == [3]              # subjects
    assert _ids(catalog.search(con, c, "miserables", 10)) == [4]           # é folded
    assert _ids(catalog.search(con, c, "Moby", 10, langs=("de",))) == [2]
    assert catalog.search(con, c, "moby unicorn", 10) == []
    hit = catalog.search(con, c, "aspirin platelet", 10)[0]
    assert hit == {"book_id": 5, "title": "Aspirin and platelet aggregation",
                   "authors": "Smith J", "subjects": "Aspirin; Blood Platelets", "lang": "en"}


def test_a_search_reads_only_the_rows_it_returns(col):
    """The cost of the leg: tantivy's top k, then those k rows from the
    manifest — never a join over every match, which is what made the SQLite
    catalog cost 2.6 s of its 4.0 s on 15,078 matches."""
    c, con = col
    for n in range(100, 400):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,title,rdf_done,"
                    "updated_at) VALUES(?,?,?,?,?,?,1,0)",
                    (f"gutenberg:{n}", "gutenberg", str(n), f"/f/{n}", "en", f"Whale story {n}"))
    catalog.backfill(con, c, budget_s=60)
    seen = []
    con.set_trace_callback(seen.append)
    hits = catalog.search(con, c, "whale", 3)
    con.set_trace_callback(None)
    assert len(hits) == 3
    reads = [s for s in seen if "FROM documents" in s]
    assert len(reads) == 1 and reads[0].count(",") <= 3 + 5, reads   # 3 rowids, 6 columns


def test_a_row_written_twice_is_there_once_and_a_gone_row_is_dropped(col):
    c, con = col
    catalog.backfill(con, c, budget_s=60)
    rowid = con.execute("SELECT rowid FROM documents WHERE uid='gutenberg:3'").fetchone()[0]
    catalog.write(c, [(rowid, "The Whale Road", "Low, Robert", "Vikings", "en")] * 2)
    assert _ids(catalog.search(con, c, "vikings", 10)) == [3]
    con.execute("DELETE FROM documents WHERE uid='gutenberg:3'")
    assert catalog.search(con, c, "vikings", 10) == []


def test_the_backfill_resumes_where_it_stopped(col):
    c, con = col
    calls = iter([True, False])
    out = catalog.backfill(con, c, budget_s=60, window=2, may_continue=lambda: next(calls))
    assert (out["windows"], out["cursor"], out["complete"]) == (1, 2, False)
    assert catalog.search(con, c, "Moby", 10) is None
    out = catalog.backfill(con, c, budget_s=60, window=2)
    assert (out["cursor"], out["complete"]) == (5, True)
    assert sorted(_ids(catalog.search(con, c, "Moby", 10))) == [1, 2]


def test_another_version_is_rebuilt_and_readers_reopen_it(col):
    c, con = col
    catalog.backfill(con, c, budget_s=60)
    assert _ids(catalog.search(con, c, "vikings", 10)) == [3]
    state = catalog.path_for(c) / catalog.STATE
    old = json.loads(state.read_text())
    state.write_text(json.dumps({**old, "version": catalog.CATALOG_VERSION - 1}))
    con.execute("UPDATE documents SET subjects='Norse sagas' WHERE uid='gutenberg:3'")
    assert catalog.search(con, c, "vikings", 10) is None, "another version: not ready"
    catalog.backfill(con, c, budget_s=60)
    assert catalog.read_state(c)["generation"] != old["generation"]
    assert catalog.search(con, c, "vikings", 10) == []
    assert _ids(catalog.search(con, c, "sagas", 10)) == [3]


def test_the_rdf_stage_writes_it_before_the_marks_and_defers_when_it_cannot(
        isolated_config, corpus_dir, monkeypatch):
    c = collections_mod.default_collection()
    con = db.connect(c)
    from app import discovery
    discovery.sync_collection(con, c)
    row = con.execute("SELECT * FROM documents WHERE rdf_done=0 LIMIT 1").fetchone()
    calls = []

    def failing(collection, rows):
        calls.append(len(rows))
        raise catalog.CatalogUnavailable("catalog index: LockBusy")
    monkeypatch.setattr(catalog, "write", failing)
    assert worker._execute_step(con, c, "rdf", row) is False, "a deferral"
    assert calls and calls[0] >= 1
    marked, errors = con.execute("SELECT SUM(rdf_done), COUNT(error) FROM documents").fetchone()
    assert (marked or 0, errors) == (0, 0), "nothing marked, nothing recorded"
    monkeypatch.undo()
    assert worker._execute_step(con, c, "rdf", row) is True
    marked = con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0]
    assert marked >= 1
    catalog.backfill(con, c, budget_s=60)
    title = con.execute("SELECT title FROM documents WHERE uid=?", (row["uid"],)).fetchone()[0]
    word = next(w for w in title.split() if w.isalnum() and len(w) > 3)
    assert int(row["native_id"]) in _ids(catalog.search(con, c, word, 20))


def test_the_catalog_is_not_a_language_group(col):
    c, con = col
    catalog.backfill(con, c, budget_s=60)
    assert (catalog.path_for(c) / "meta.json").is_file()
    assert "_catalog" not in fts.existing_groups(c)


def test_catalog_search_is_empty_until_the_index_is_complete_then_uses_it(col):
    """The SQLite catalog that answered meanwhile is retired (2026-09-19):
    until the backfill completes, the catalog leg is empty — never an error,
    and the search's other legs still answer."""
    c, con = col
    assert retrieval.catalog_search(con, "Moby", collection=c) == []
    catalog.backfill(con, c, budget_s=60)
    assert sorted(_ids(retrieval.catalog_search(con, "Moby", collection=c))) == [1, 2]
    assert _ids(retrieval.catalog_search(con, "Moby", langs=("de",), collection=c)) == [2]


def test_closing_every_writer_waits_for_a_commit_in_flight():
    """The node's suite, 2026-09-20: a pipeline thread of a collection the
    test had removed was still committing when teardown closed every
    writer, and PyO3 raised "Already borrowed". close_all now takes each
    writer's write lock, so the commit finishes first."""
    import threading
    import time as _time

    order = []

    class Writer:
        def wait_merging_threads(self):
            order.append("closed")

    path = "/nowhere/_catalog"
    lock = threading.Lock()
    with catalog._lock:
        catalog._writers[path] = Writer()
        catalog._write_locks[path] = lock
    lock.acquire()                       # a commit in flight
    t = threading.Thread(target=catalog.close_all)
    t.start()
    _time.sleep(0.2)
    assert order == [], "closed from under a commit"
    order.append("committed")
    lock.release()
    t.join(5)
    assert order == ["committed", "closed"]
    with catalog._lock:
        catalog._write_locks.pop(path, None)


def test_a_forgotten_collection_s_writer_is_let_go_and_a_new_one_at_the_same_path_writes(col, monkeypatch):
    """Live, 2026-10-07: `louise-stop` was forgotten and made again under
    its name, and every catalog write failed — the worker still held the
    old writer, open on files the forget had removed."""
    import shutil
    from pathlib import Path
    c, _con = col
    catalog.write(c, [(1, "The Whale Road", "Low, Robert", "Vikings", "en")])
    path = str(catalog.path_for(c))
    assert path in catalog._writers, "the worker keeps its writer between batches"
    shutil.rmtree(Path(path))                       # what forget does to the directory
    assert catalog.release_under(Path(c.fts_dir)) == 1
    assert path not in catalog._writers and path not in catalog._indexes
    assert catalog.write(c, [(2, "A Plain Walk", "Fixture, Basil", "Walks", "en")]) == 1, \
        "the collection made again at that path writes through a writer of its own"


def test_the_worker_s_forget_releases_the_collection_s_catalog(monkeypatch, tmp_path):
    from app import collections as collections_mod, worker
    released = []
    monkeypatch.setattr(catalog, "release_under", lambda d: released.append(d) or 1)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: tmp_path)
    worker.retire_collection("louise-stop", timeout=0.1)
    assert released == [tmp_path / "collections" / "louise-stop"]
