"""rdf processes a batch of documents inside one transaction.

Profiled live on pubmed (33M rows): 95% of a warm rdf step was SQLite — the
UPDATE against the wide documents table plus the catalog_fts FTS5
delete/insert, committed once per document — against 0.8 ms of parsing. One
transaction per batch writes each dirty page to the WAL once per batch and
lets FTS5 buffer its index writes, the same economics as fts batching.

The safety property is inherited rather than invented: _execute_step already
wraps the stage in BEGIN..COMMIT, so the batch is all-or-nothing. A crash
mid-batch marks nothing and the re-run repeats delete-then-insert keyed on
uid (invariant 2).
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, config, db, discovery, plugin_io, worker

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


def _pending(con):
    return con.execute(
        "SELECT * FROM documents WHERE rdf_done=0 AND error IS NULL LIMIT 1").fetchone()


def test_one_step_extracts_several_documents(tmp_path):
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total >= 3

    assert worker._execute_step(con, c, "rdf", _pending(con)) is True

    done = con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=1").fetchone()[0]
    assert done == total, f"one step should have extracted all {total}, got {done}"
    catalog = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]
    assert catalog == total, "every batched document must reach the catalog"


def test_batch_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RDF_BATCH_DOCS", 2)
    c, con = _discovered(tmp_path)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total > 2

    worker._execute_step(con, c, "rdf", _pending(con))
    done = con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=1").fetchone()[0]
    assert done == 2, f"cap ignored: {done} extracted"


def test_a_failing_peer_does_not_fail_the_round_or_get_errored(tmp_path, monkeypatch):
    c, con = _discovered(tmp_path)
    selected = _pending(con)
    real = plugin_io.metadata

    def flaky(plugin, row):
        if row["uid"] != selected["uid"]:
            raise RuntimeError("unreadable peer")
        return real(plugin, row)

    monkeypatch.setattr(plugin_io, "metadata", flaky)
    assert worker._execute_step(con, c, "rdf", selected) is True

    got = con.execute("SELECT rdf_done, error FROM documents WHERE uid=?",
                      (selected["uid"],)).fetchone()
    assert got["rdf_done"] == 1 and got["error"] is None
    blamed = con.execute(
        "SELECT COUNT(*) FROM documents WHERE uid<>? AND (error IS NOT NULL OR retries>0)",
        (selected["uid"],)).fetchone()[0]
    assert blamed == 0, "a skipped peer must not be errored by someone else's round"
    # and the peers are still pending, to be retried on their own turns
    left = con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=0").fetchone()[0]
    assert left > 0


def test_batch_is_all_or_nothing(tmp_path, monkeypatch):
    """The transaction owns the batch: a failure AFTER peers were processed
    must roll back every one of them, not leave a half-marked batch."""
    c, con = _discovered(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("post-batch failure")

    monkeypatch.setattr(worker, "_batch_log", boom)   # runs after all peers
    assert worker._execute_step(con, c, "rdf", _pending(con)) is True

    done = con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=1").fetchone()[0]
    assert done == 0, f"rollback must undo the whole batch, {done} rows survived"
    catalog = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]
    assert catalog == 0


def test_rerunning_a_batch_does_not_duplicate_catalog_rows(tmp_path):
    c, con = _discovered(tmp_path)
    worker._execute_step(con, c, "rdf", _pending(con))
    first = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]

    con.execute("UPDATE documents SET rdf_done=0")       # pretend the mark was lost
    con.commit()
    worker._execute_step(con, c, "rdf", _pending(con))
    second = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]
    assert second == first, f"re-run duplicated catalog rows: {first} -> {second}"
