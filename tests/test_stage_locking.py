"""WHERE THE MANIFEST'S WRITE LOCK BEGINS — a cost test for both parsing
stages.

CLAUDE.md: "Long non-DB work (an encoder call, a fetch) stays outside the
transaction entirely." A stage's long part is its PARSE, and the parse does
not touch SQLite: it runs in the pool's children. Holding `BEGIN IMMEDIATE`
across it blocks every other writer of that manifest — the discovery scan
committing a batch a second, the event log, an acquire — for the whole
batch. It has been paid for twice:

  fts  wedged genome-hg38's manifest for 68+ minutes on one chromosome
       ("database is locked" for every other write), fixed 2026-09-11.
  rdf  held it for 51.7 s per 2,000-document batch on wikipedia, measured
       on the node 2026-09-23. Same defect, other stage, found while
       looking for why the CPU would not saturate.

So both stages open the lock THEMSELVES, late, once only SQLite writes
remain, and `worker._NO_OUTER_TRANSACTION` stops `_execute_step` opening
one on their behalf. These tests pin that down from both sides: the
connection is not in a transaction while the parse runs, AND a different
connection can actually commit a write at that moment — which is the
property that matters, and the one an `in_transaction` assertion alone
would not prove.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, parallel, worker

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


def _another_writer_can_commit(collection) -> tuple[bool, str]:
    """What a discovery scan or the event log would experience RIGHT NOW.

    A short busy_timeout on purpose: the point is whether the lock is free,
    not how patient this connection is, and db.connect's own 60 s would turn
    a failure into a minute-long hang.
    """
    other = db.connect(collection)
    try:
        other.execute("PRAGMA busy_timeout=250")
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO events(ts,level,stage,doc_uid,message) VALUES(?,?,?,?,?)",
            (0, "info", "discover", None, "a peer writer got in"))
        other.execute("COMMIT")
        return True, ""
    except sqlite3.OperationalError as e:
        return False, str(e)
    finally:
        other.close()


def _pending(con, where):
    return con.execute(
        f"SELECT * FROM documents WHERE {where} ORDER BY native_id LIMIT 1").fetchone()


@pytest.mark.parametrize("stage, pool_call, ready", [
    ("rdf", "metadata_for_rows", "rdf_done=0"),
    ("fts", "fts_for_rows", "rdf_done=1 AND fts_done=0"),
])
def test_the_parse_runs_outside_the_manifest_write_lock(tmp_path, isolated_config,
                                                        monkeypatch, stage,
                                                        pool_call, ready):
    c, con = _discovered(tmp_path)
    if stage == "fts":
        con.execute("UPDATE documents SET rdf_done=1")
        con.commit()
    row = _pending(con, ready)
    assert row is not None, f"the fixture has no {stage}-pending document"

    seen: dict = {}
    real = getattr(parallel, pool_call)

    def watched(collection, rows, **kw):
        seen["in_transaction"] = con.in_transaction
        seen["peer"], seen["why"] = _another_writer_can_commit(collection)
        return real(collection, rows, **kw)

    monkeypatch.setattr(parallel, pool_call, watched)
    assert worker._execute_step(con, c, stage, row) is True

    assert seen, f"{stage} never reached the parse pool — the test proved nothing"
    assert seen["in_transaction"] is False, \
        f"{stage} held the manifest's write transaction across the parse"
    assert seen["peer"], \
        (f"another writer could not commit while {stage} parsed — the write "
         f"lock is still held across the pool round trip ({seen['why']})")

    # and the stage still did its job, inside a transaction the caller committed
    done = con.execute(
        f"SELECT {stage}_done FROM documents WHERE uid=?", (row["uid"],)).fetchone()[0]
    assert done == 1, f"{stage} did not mark its selected document"
    assert con.in_transaction is False, "the step left a transaction open"


def test_every_stage_named_as_taking_its_own_lock_exists():
    """A typo here fails OPEN: `_execute_step` would go back to opening the
    write lock before the parse for that stage, and nothing else would
    notice — the pipeline would still be correct, just serialised against
    every other writer again, which is exactly the defect that took two
    postmortems to find. A stage NOT in the list is a legitimate choice (its
    whole duration is SQLite); a name in the list that is not a stage is
    always a mistake."""
    assert set(worker._NO_OUTER_TRANSACTION) <= set(worker.STAGES), \
        (f"{set(worker._NO_OUTER_TRANSACTION) - set(worker.STAGES)} is not a "
         f"stage — that stage would silently take the lock across its parse")
