"""rdf and fts interleave a BATCH at a time, never a whole corpus at a time.

`worker.py` opens with "Stage order per document: rdf -> fts -> embed", but
the selection made that order GLOBAL: because rdf was checked first, every
document in a collection finished rdf before any document reached fts. On a
small corpus the difference is invisible. On a large one it is the whole
behaviour — live, wikivec sat at rdf 172,538 of 8,495,470 with fts exactly 0,
and its genesis archive (vectors that travel with the corpus and need no
encoder at all) could import nothing, because a shard is only applied to
documents with fts_done=1.

fts commits one tantivy writer per BATCH (see stage_fts and test_fts_batching),
so rdf is allowed to run a batch ahead before fts takes a turn. The property
under test is the bound: fts lags rdf by at most FTS_BATCH_DOCS documents,
not by the size of the corpus.
"""
import shutil
from pathlib import Path

import pytest

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


@pytest.fixture
def small_batch(monkeypatch):
    """Two documents to a batch, so the fixture corpus can show the bound."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 2)
    return 2


def test_fts_takes_its_turn_after_one_batch_not_after_the_corpus(tmp_path, small_batch):
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total > small_batch, "fixture must be larger than one batch"
    scope = db.get_scope(con)

    # Nothing rdf-done: no fts work exists, so rdf runs.
    stage, first = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "rdf"
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (first["uid"],))
    con.commit()

    # One document pending is short of a batch — rdf keeps the turn.
    stage, second = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "rdf", "fts must not pay a whole writer for one document"
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (second["uid"],))
    con.commit()

    # A batch is ready, and rdf has NOT finished the corpus.
    stage, _row = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "fts", "a full batch must take the turn from rdf"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=0"
                       ).fetchone()[0] > 0


def test_the_tail_still_indexes_when_it_is_short_of_a_batch(tmp_path, monkeypatch):
    """rdf exhausted with a partial batch left must still drain, or the last
    documents of every corpus would never be indexed."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 1000)   # never a full batch
    c, con = _discovered(tmp_path)
    con.execute("UPDATE documents SET rdf_done=1")        # rdf fully done
    con.commit()

    stage, row = worker._next_book(con, db.get_scope(con), embed_allowed=False)
    assert stage == "fts" and row is not None


def test_fts_progresses_before_rdf_has_finished_the_corpus(tmp_path, small_batch):
    """The property everything downstream depends on: fts_done becomes
    non-zero while rdf is still incomplete, so genesis and embed have
    candidates early instead of after the whole corpus."""
    c, con = _discovered(tmp_path)
    scope = db.get_scope(con)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    for _ in range(6):
        stage, row = worker._next_book(con, scope, embed_allowed=False)
        if row is None:
            break
        if stage == "rdf":
            con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (row["uid"],))
        else:
            # stand in for stage_fts marking the whole batch
            con.execute("UPDATE documents SET fts_done=1 WHERE rdf_done=1")
        con.commit()
        rdf_done, fts_done = con.execute(
            "SELECT SUM(rdf_done), SUM(fts_done) FROM documents").fetchone()
        if fts_done:
            assert rdf_done < total, "fts started only after rdf walked everything"
            return
    pytest.fail("fts never took a turn")


def test_version_reindex_does_not_starve_new_document_ingestion(tmp_path, monkeypatch):
    """A FTS_VERSION bump makes every indexed document a candidate at once.
    Those must not be taken ahead of documents that have never been indexed,
    or a re-index stalls ingestion for its whole duration. Batch size 1 here
    isolates the priority question from the batching one."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 1)
    c, con = _discovered(tmp_path)
    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents LIMIT 2")]
    stale, fresh = uids[0], uids[1]

    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, "
                "entity_version=? WHERE uid=?",
                (config.FTS_VERSION - 1, config.ENTITY_VERSION, stale))
    con.execute("UPDATE documents SET rdf_done=1, fts_done=0 WHERE uid=?", (fresh,))
    con.commit()

    stage, row = worker._next_book(con, db.get_scope(con), embed_allowed=False)
    assert stage == "fts" and row["uid"] == fresh, \
        "never-indexed documents come before version-driven re-indexing"


def test_version_reindex_still_happens_once_caught_up(tmp_path, monkeypatch):
    """Deprioritised is not dropped: with no fresh work left, the stale
    document must still be selected for re-indexing."""
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 1)
    c, con = _discovered(tmp_path)
    stale = con.execute("SELECT uid FROM documents LIMIT 1").fetchone()["uid"]
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, "
                "entity_version=?", (config.FTS_VERSION, config.ENTITY_VERSION))
    con.execute("UPDATE documents SET fts_version=? WHERE uid=?",
                (config.FTS_VERSION - 1, stale))
    con.commit()

    stage, row = worker._next_book(con, db.get_scope(con), embed_allowed=False)
    assert stage == "fts" and row["uid"] == stale
