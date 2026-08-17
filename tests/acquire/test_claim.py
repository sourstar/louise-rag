"""Advisory acquisition claim — prevents the CLI and the worker from fetching
the same collection concurrently, without ever being able to wedge acquisition
permanently.
"""
import json
import os
import time

import pytest

from app import collections as collections_mod, db
from app.acquire import claim as claim_mod


def _collection(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    return collections_mod.Collection(
        name="test", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


@pytest.fixture
def con(tmp_path):
    return db.connect(_collection(tmp_path))


def test_claim_is_taken_and_released(con):
    assert claim_mod.read_claim(con) is None
    with claim_mod.acquisition_claim(con, "test"):
        held = claim_mod.read_claim(con)
        assert held["pid"] == os.getpid()
        assert held["source_id"] == "test"
    assert not claim_mod.read_claim(con)


def test_claim_is_released_even_when_the_body_raises(con):
    with pytest.raises(RuntimeError, match="boom"):
        with claim_mod.acquisition_claim(con, "test"):
            raise RuntimeError("boom")
    assert not claim_mod.read_claim(con), "a crash must not leave the claim held"


def test_a_live_claim_from_another_process_is_refused(con):
    db.set_control(con, claim_mod.CLAIM_KEY,
                    json.dumps({"pid": os.getpid() + 99_999, "ts": time.time(),
                                 "source_id": "test"}))
    with pytest.raises(claim_mod.AcquisitionBusy, match="already running"):
        with claim_mod.acquisition_claim(con, "test"):
            pass


def test_a_stale_claim_is_taken_over(con):
    """A hard-killed holder must never wedge acquisition forever."""
    db.set_control(con, claim_mod.CLAIM_KEY,
                    json.dumps({"pid": os.getpid() + 99_999,
                                 "ts": time.time() - claim_mod.STALE_AFTER_SECONDS - 1,
                                 "source_id": "test"}))
    with claim_mod.acquisition_claim(con, "test"):
        assert claim_mod.read_claim(con)["pid"] == os.getpid()


def test_reentrant_claim_by_the_same_pid_is_allowed(con):
    """The same process re-entering (e.g. worker retrying) is not a conflict."""
    db.set_control(con, claim_mod.CLAIM_KEY,
                    json.dumps({"pid": os.getpid(), "ts": time.time(), "source_id": "test"}))
    with claim_mod.acquisition_claim(con, "test"):
        assert claim_mod.read_claim(con)["pid"] == os.getpid()


def test_force_overrides_a_live_claim(con):
    db.set_control(con, claim_mod.CLAIM_KEY,
                    json.dumps({"pid": os.getpid() + 99_999, "ts": time.time(),
                                 "source_id": "test"}))
    with claim_mod.acquisition_claim(con, "test", force=True):
        assert claim_mod.read_claim(con)["pid"] == os.getpid()


def test_corrupt_claim_value_is_treated_as_absent(con):
    db.set_control(con, claim_mod.CLAIM_KEY, "not json{{{")
    with claim_mod.acquisition_claim(con, "test"):
        assert claim_mod.read_claim(con)["pid"] == os.getpid()


def test_refresh_keeps_a_long_run_from_going_stale(con, monkeypatch):
    with claim_mod.acquisition_claim(con, "test"):
        original = claim_mod.read_claim(con)["ts"]
        # too soon: no write
        assert claim_mod.refresh_claim(con, "test", time.time()) is not None
        assert claim_mod.read_claim(con)["ts"] == original
        # long enough ago: refreshed
        claim_mod.refresh_claim(con, "test", time.time() - 3600)
        assert claim_mod.read_claim(con)["ts"] >= original


def test_refresh_does_not_steal_another_processes_claim(con):
    other = {"pid": os.getpid() + 99_999, "ts": 12345.0, "source_id": "test"}
    db.set_control(con, claim_mod.CLAIM_KEY, json.dumps(other))
    claim_mod.refresh_claim(con, "test", time.time() - 3600)
    assert claim_mod.read_claim(con) == other
