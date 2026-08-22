"""A locked manifest is a deferral, not a broken document.

Recording an error is not neutral: every stage selection requires
`error IS NULL`, so an errored document drops out of the pipeline even with
retries left and only returns when a human presses "clear errors & retry".
`endpoint_unavailable` already drew this distinction for an absent encoder,
and its docstring cites the acquire path, which has always logged "a manifest
busy past its timeout" as a deferral. The document stages never did.

It went unnoticed while every stage held the write lock for microseconds. It
surfaced the moment fts began committing a whole batch while gutenberg was on
embed_mode=all — an embed runs inside _execute_step's transaction and can take
minutes against a 60s busy_timeout, so the fts batch behind it lost the lock
and errored six perfectly good documents in the first minutes, still accruing.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, worker

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


def test_predicate_matches_a_lock_and_nothing_else():
    assert worker.manifest_contended(sqlite3.OperationalError("database is locked"))
    assert worker.manifest_contended(sqlite3.OperationalError("database table is locked"))
    assert not worker.manifest_contended(sqlite3.OperationalError("no such table: x"))
    assert not worker.manifest_contended(ValueError("locked"))   # wrong type
    assert not worker.manifest_contended(RuntimeError("boom"))


def test_a_locked_manifest_leaves_the_document_completely_untouched(tmp_path,
                                                                     monkeypatch):
    c, con = _discovered(tmp_path)
    row = con.execute("SELECT * FROM documents LIMIT 1").fetchone()

    def locked(con_, row_, collection_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(worker.STAGES, "rdf", locked)
    assert worker._execute_step(con, c, "rdf", row) is False, \
        "a contended manifest must DEFER the round, not consume it"

    got = con.execute("SELECT rdf_done, retries, error FROM documents WHERE uid=?",
                      (row["uid"],)).fetchone()
    assert got["retries"] == 0, "a lock timeout must not burn a retry"
    assert got["error"] is None, "a lock timeout must not error the document"
    assert got["rdf_done"] == 0


def test_the_document_is_still_selectable_afterwards(tmp_path, monkeypatch):
    """The point of not recording an error: the next round picks it up again
    with no human intervention."""
    c, con = _discovered(tmp_path)
    scope = db.get_scope(con)
    stage, row = worker._next_book(con, scope, embed_allowed=False)

    monkeypatch.setitem(worker.STAGES, stage,
                        lambda *a: (_ for _ in ()).throw(
                            sqlite3.OperationalError("database is locked")))
    worker._execute_step(con, c, stage, row)

    stage2, row2 = worker._next_book(con, scope, embed_allowed=False)
    assert row2 is not None and row2["uid"] == row["uid"], \
        "the deferred document must come straight back, not be dropped"


def test_a_genuine_error_is_still_recorded(tmp_path, monkeypatch):
    """Deferral must not swallow real failures."""
    c, con = _discovered(tmp_path)
    row = con.execute("SELECT * FROM documents LIMIT 1").fetchone()

    monkeypatch.setitem(worker.STAGES, "rdf",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("truly broken")))
    assert worker._execute_step(con, c, "rdf", row) is True

    got = con.execute("SELECT retries, error FROM documents WHERE uid=?",
                      (row["uid"],)).fetchone()
    assert got["retries"] == 1 and "truly broken" in got["error"]
