"""One tantivy writer per BATCH of documents, not per document.

`idx.writer(heap_size=64_000_000)` plus `commit()` plus
`wait_merging_threads()` is a fixed cost that does not shrink with the
document. Measured live: ~30s for a 2 kb genome scaffold, the same as for a
23 MB chromosome arm, because the work is the commit and not the content. On
a corpus of small documents that toll dominates everything else.

The safety property is an ORDER, and it is what these tests pin down:
index everything, THEN commit, THEN mark the rows. A crash before the mark
leaves documents unmarked and they re-run over the same
delete-then-insert keyed on book_id (invariant 2). Marking before the commit
is the single ordering that loses data.
"""
import shutil
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db, discovery, fts, worker

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
    con.execute("UPDATE documents SET rdf_done=1")   # everything fts-pending
    con.commit()
    return c, con


def _pending(con):
    return con.execute("SELECT * FROM documents WHERE fts_done=0 LIMIT 1").fetchone()


def test_one_step_indexes_several_documents_in_one_commit(tmp_path, monkeypatch):
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total >= 3

    commits = []
    real = fts.index_books
    monkeypatch.setattr(fts, "index_books",
                        lambda entries, collection=None: commits.append(
                            real(list(entries), collection) or len(commits)))

    assert worker._execute_step(con, c, "fts", _pending(con)) is True

    done = con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0]
    assert done == total, f"one step should have indexed all {total}, got {done}"
    assert len(commits) == 1, "the whole batch must go through a single call"


def test_batch_is_capped_by_document_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 2)
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total > 2

    worker._execute_step(con, c, "fts", _pending(con))
    done = con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0]
    assert done == 2, f"document cap ignored: {done} marked"


def test_batch_is_capped_by_chunk_count(tmp_path, monkeypatch):
    """The memory guard: a batch holds every chunk until the commit, and one
    genome contig alone is ~6,700 chunks."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 1000)
    monkeypatch.setattr(config, "FTS_BATCH_CHUNKS", 1)   # first document only
    c, con = _discovered(tmp_path)

    worker._execute_step(con, c, "fts", _pending(con))
    done = con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0]
    assert done == 1, f"chunk cap ignored: {done} marked"


def test_nothing_is_marked_when_the_commit_fails(tmp_path, monkeypatch):
    """The ordering guarantee. If the tantivy commit raises, not one row may
    claim fts_done — otherwise a crash silently drops documents from the
    index forever."""
    c, con = _discovered(tmp_path)

    def boom(entries, collection=None):
        list(entries)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(fts, "index_books", boom)
    assert worker._execute_step(con, c, "fts", _pending(con)) is True

    done = con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0]
    assert done == 0, "rows were marked before the commit landed"


def test_re_running_a_batch_does_not_duplicate(tmp_path):
    """Crash-between-commit-and-mark re-runs the batch. delete_documents is
    still keyed per book_id, so the second pass replaces rather than adds."""
    c, con = _discovered(tmp_path)
    row = _pending(con)
    worker._execute_step(con, c, "fts", row)
    first = fts.search(fts.group_for(row["lang"]), "the", limit=100, collection=c)

    con.execute("UPDATE documents SET fts_done=0")       # pretend the mark was lost
    con.commit()
    worker._execute_step(con, c, "fts", _pending(con))
    second = fts.search(fts.group_for(row["lang"]), "the", limit=100, collection=c)

    assert len(second) == len(first), \
        f"re-running the batch duplicated rows: {len(first)} -> {len(second)}"


def test_a_peer_that_cannot_load_does_not_blame_the_selected_document(tmp_path,
                                                                       monkeypatch):
    """Only the selected document may fail the round. A broken peer keeps its
    pending state and records its own error when its own turn comes."""
    c, con = _discovered(tmp_path)
    selected = _pending(con)

    real_load = worker._load_chunks

    def flaky(row, collection):
        if row["uid"] != selected["uid"]:
            raise RuntimeError("unreadable peer")
        return real_load(row, collection)

    monkeypatch.setattr(worker, "_load_chunks", flaky)
    assert worker._execute_step(con, c, "fts", selected) is True

    got = con.execute("SELECT fts_done, retries, error FROM documents WHERE uid=?",
                      (selected["uid"],)).fetchone()
    assert got["fts_done"] == 1 and got["retries"] == 0 and got["error"] is None
    others = con.execute(
        "SELECT COUNT(*) FROM documents WHERE uid<>? AND (error IS NOT NULL OR retries>0)",
        (selected["uid"],)).fetchone()[0]
    assert others == 0, "a skipped peer must not be errored by someone else's round"


def test_index_book_still_works_for_single_document_callers(tmp_path):
    c, con = _discovered(tmp_path)
    row = _pending(con)
    chunks = worker._load_chunks(row, c)
    fts.index_book(row["lang"], int(row["native_id"]), chunks, c)
    assert fts.search(fts.group_for(row["lang"]), "the", limit=10, collection=c)
