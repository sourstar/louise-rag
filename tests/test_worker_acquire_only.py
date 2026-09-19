"""The "download/seeding only" node mode.

The two halves of this system cost very differently: moving bytes is
nearly free, while rdf/fts/embed run every core hot — enough to heat the
room the node lives in. Turning embedding off used to mean editing every
collection's embed mode and losing what each one was set to; pausing the
worker stopped the downloads too. acquire-only keeps the bytes moving
and stands everything else down, restoring per-collection settings
untouched when it is turned back off.

Since 2026-09-19 "everything else" includes the SCAN of what was
downloaded (operator: "download/seeding only: download/seeding only"):
tests/test_node_modes.py holds that, and what each of the four modes runs.
"""
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, discovery, worker
from app.api import app

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


@pytest.fixture
def collection(isolated_config, tmp_path):
    corpus = tmp_path / "corpus_a"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = collections_mod.Collection(
        name="alpha", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    discovery.sync_collection(db.connect(c), c)
    return c


def test_state_round_trips_through_the_control_table(collection):
    con = db.connect(collection)
    assert db.worker_state(con) == "paused", "a fresh node starts paused"
    for state in db.WORKER_STATES:
        db.set_worker_state(con, state)
        assert db.worker_state(con) == state
    # an unknown value must never read as "running"
    db.set_control(con, db.WORKER_STATE_KEY, "nonsense")
    assert db.worker_state(con) == "paused"


def test_api_accepts_the_new_state(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        r = client.post("/api/control", json={"state": "acquire-only"})
        assert r.status_code == 200 and r.json()["worker_state"] == "acquire-only"
        bad = client.post("/api/control", json={"state": "warp-speed"})
        assert bad.status_code == 400 and "acquire-only" in bad.json()["detail"]


def test_pipeline_thread_still_discovers_but_does_no_stage_work(collection,
                                                                 monkeypatch):
    """The point of the mode: fetches keep being started — a discovery
    request is what starts one — while parse/index stand down. What lands is
    scanned when everything runs again (tests/test_node_modes.py)."""
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    con = db.connect(collection)
    con.execute("UPDATE documents SET rdf_done=0, fts_done=0")

    discovered = []
    monkeypatch.setattr(worker, "_discover",
                        lambda c, coll, reason, stop=None: discovered.append(reason))

    worker._acquire_only.set()
    worker._request_discovery("alpha", "interval")
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker, args=(collection, gone))
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not discovered:
            time.sleep(0.05)
        assert discovered == ["interval"], "discovery must still run"
        time.sleep(0.5)
        done = con.execute(
            "SELECT COALESCE(SUM(rdf_done),0) FROM documents").fetchone()[0]
        assert done == 0, "rdf/fts must stand down in acquire-only"
    finally:
        worker._acquire_only.clear()
        gone.set()
        thread.join(timeout=10)


def test_leaving_the_mode_resumes_stage_work(collection, monkeypatch):
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    con = db.connect(collection)
    worker._acquire_only.set()
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker, args=(collection, gone))
    thread.start()
    try:
        time.sleep(0.4)
        assert con.execute(
            "SELECT COALESCE(SUM(rdf_done),0) FROM documents").fetchone()[0] == 0
        worker._acquire_only.clear()          # "run everything" again
        deadline = time.time() + 20
        while time.time() < deadline:
            if con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0] == 3:
                break
            time.sleep(0.1)
        assert con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0] == 3
    finally:
        worker._acquire_only.clear()
        gone.set()
        thread.join(timeout=10)


def test_embed_modes_are_untouched_by_the_mode(collection):
    """Switching back must restore exactly what each collection was doing,
    which is the whole reason this is not five embed-mode edits."""
    con = db.connect(collection)
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)

    db.set_worker_state(con, "acquire-only")
    assert db.get_scope(con)["embed_mode"] == "all"
    db.set_worker_state(con, "running")
    assert db.get_scope(con)["embed_mode"] == "all"
