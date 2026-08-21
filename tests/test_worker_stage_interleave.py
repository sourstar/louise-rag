"""rdf and fts interleave per document, as this module's contract always said.

`worker.py` opens with "Stage order per document: rdf -> fts -> embed", but
the selection made that order GLOBAL: because rdf was checked first, every
document in a collection finished rdf before any document reached fts. On a
small corpus the difference is invisible. On a large one it is the whole
behaviour — live, wikivec sat at rdf 172,538 of 8,495,470 with fts exactly 0,
and its genesis archive (vectors that travel with the corpus and need no
encoder at all) could import nothing, because a shard is only applied to
documents with fts_done=1.
"""
import shutil
from pathlib import Path

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


def test_fts_follows_its_own_document_not_the_whole_corpus(tmp_path):
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total >= 3, "fixture needs several documents for this to mean anything"
    scope = db.get_scope(con)

    # First selection: nothing is rdf-done, so there is no fts work yet.
    stage, first = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "rdf"

    # Mark that one document rdf-done, exactly as the rdf stage would.
    con.execute("UPDATE documents SET rdf_done=1 WHERE uid=?", (first["uid"],))
    con.commit()

    # The very next selection must be fts FOR THAT DOCUMENT — not rdf for the
    # next one. This is the assertion the old ordering failed.
    stage, second = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "fts", f"expected fts to follow its own document, got {stage}"
    assert second["uid"] == first["uid"]


def test_fts_progresses_before_rdf_has_finished_the_corpus(tmp_path):
    """The property that matters downstream: fts_done becomes non-zero while
    rdf is still incomplete, so genesis and embed have candidates early."""
    c, con = _discovered(tmp_path)
    scope = db.get_scope(con)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    # Drive selection for a few rounds, simulating each stage's commit.
    for _ in range(4):
        stage, row = worker._next_book(con, scope, embed_allowed=False)
        if row is None:
            break
        col = "rdf_done" if stage == "rdf" else "fts_done"
        con.execute(f"UPDATE documents SET {col}=1 WHERE uid=?", (row["uid"],))
        con.commit()

    rdf_done, fts_done = con.execute(
        "SELECT SUM(rdf_done), SUM(fts_done) FROM documents").fetchone()
    assert fts_done > 0, "fts must start before rdf has walked the whole corpus"
    assert rdf_done < total, "this test is meaningless if rdf already finished"


def test_version_reindex_does_not_starve_new_document_ingestion(tmp_path):
    """A FTS_VERSION bump makes every indexed document a candidate at once.
    Those must not be taken ahead of documents that have never been indexed,
    or a re-index stalls ingestion for its whole duration."""
    c, con = _discovered(tmp_path)
    scope = db.get_scope(con)
    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents LIMIT 2")]
    stale, fresh = uids[0], uids[1]

    # `stale` is fully indexed but at an older fts_version; `fresh` has only
    # just finished rdf and has never been indexed.
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, "
                "entity_version=? WHERE uid=?",
                (config.FTS_VERSION - 1, config.ENTITY_VERSION, stale))
    con.execute("UPDATE documents SET rdf_done=1, fts_done=0 WHERE uid=?", (fresh,))
    con.commit()

    stage, row = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "fts" and row["uid"] == fresh, \
        "never-indexed documents come before version-driven re-indexing"


def test_version_reindex_still_happens_once_caught_up(tmp_path):
    """Deprioritised is not dropped: with no fresh work left, the stale
    document must still be selected for re-indexing."""
    c, con = _discovered(tmp_path)
    scope = db.get_scope(con)
    stale = con.execute("SELECT uid FROM documents LIMIT 1").fetchone()["uid"]
    # everything indexed and current...
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, fts_version=?, "
                "entity_version=?", (config.FTS_VERSION, config.ENTITY_VERSION))
    # ...except one document left behind by a version bump
    con.execute("UPDATE documents SET fts_version=? WHERE uid=?",
                (config.FTS_VERSION - 1, stale))
    con.commit()

    stage, row = worker._next_book(con, scope, embed_allowed=False)
    assert stage == "fts" and row["uid"] == stale
