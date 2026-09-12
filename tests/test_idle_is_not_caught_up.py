"""A switched-off stage is not a finished one.

The worker announced "idle — pipeline is caught up" every five minutes
while 4,577,101 pubmed documents sat unembedded and the node ran at 0.16
load average, because a DISABLED stage selects no row exactly as a
FINISHED stage does. The operator read the machine as stuck — it was —
and the log said the opposite (2026-09-12).

"Did work" is not "made progress", and "selected nothing" is not "has
nothing left".
"""
import pytest

from app import collections as collections_mod, db, worker


def _seed(con, n, *, embedded=0):
    for i in range(n):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,title,"
            "in_scope,rdf_done,fts_done,embed_done,updated_at) "
            "VALUES(?,?,?,?,?,?,1,1,1,?,0)",
            (f"pubmed:{i}", "pubmed", str(i), f"/c/{i}", "en", "t",
             1 if i < embedded else 0))


def test_it_names_the_work_that_will_never_start(isolated_config):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 10, embedded=3)
    db.set_scope(con, {"embed_mode": "off"})

    said = worker._waiting_because_off(con, collection)
    assert "7 document(s)" in said
    assert "embed is OFF" in said
    assert f"config/collections/{collection.name}.toml" in said, \
        "and where to change it, because that is the next question"


def test_a_genuinely_finished_pipeline_still_says_caught_up(isolated_config):
    """The line is not replaced, only corrected where it was wrong."""
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 5, embedded=5)
    db.set_scope(con, {"embed_mode": "off"})
    assert worker._waiting_because_off(con, collection) == "", \
        "nothing waiting means the pipeline really is caught up"


def test_an_enabled_stage_says_nothing(isolated_config):
    """With embedding ON, no row selected means the queue is drained —
    the ordinary, honest idle."""
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 10, embedded=3)
    db.set_scope(con, {"embed_mode": "full"})
    assert worker._waiting_because_off(con, collection) == ""


def test_the_idle_line_never_raises(isolated_config, monkeypatch):
    """It runs inside the round loop. A count that throws must cost the
    MESSAGE, never the round."""
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    monkeypatch.setattr(db, "get_scope",
                        lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker._waiting_because_off(con, collection) == ""
