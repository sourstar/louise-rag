"""A finished fetch must not start another fetch.

Observed live: after discovery moved onto the pipeline threads, the
post-fetch scan went through _discover — which STARTS a fetch — and that
fetch's completion requested another scan. One cycle every ~5 seconds,
forever, and it starved rdf/fts/embed completely (fts sat at the same
count for 7 minutes while the worker burned CPU doing nothing).
"""
import threading
import time

import pytest

from app import collections as collections_mod, config, db, worker


@pytest.fixture(autouse=True)
def clean_state():
    worker._acquire_done.clear()
    worker._discover_requests.clear()
    yield
    worker._acquire_done.clear()
    worker._discover_requests.clear()


def _collection(tmp_path):
    return collections_mod.Collection(
        name="c", plugin="gutenberg", profile="demo", embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance",
        acquire_enabled=True)


def test_a_no_op_fetch_requests_no_scan(isolated_config, tmp_path):
    """Nothing landed, so there is nothing to rescan — and asking for one
    is what kept the loop alive."""
    worker._acquire_done["c"] = False
    worker._finish_completed_acquires({}, [], {}, pipeline=True)
    assert worker._take_discovery_request("c") is None


def test_a_fetch_that_landed_files_requests_a_SCAN_not_a_fetch(isolated_config,
                                                              tmp_path):
    worker._acquire_done["c"] = True
    worker._finish_completed_acquires({}, [_collection(tmp_path)], {}, pipeline=True)
    request = worker._take_discovery_request("c")
    assert request is not None
    reason, acquire = request
    assert "fetch finished" in reason
    assert acquire is False, "the post-fetch scan must NOT start a fetch"


def test_the_pipeline_thread_honours_the_no_acquire_flag(isolated_config,
                                                          tmp_path, monkeypatch):
    """The loop closed here: a scan request that calls _discover acquires,
    and acquiring schedules another scan."""
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    collection = _collection(tmp_path)
    db.connect(collection)
    acquired, scanned = [], []
    # the scan now runs on its own thread (worker._start_scan) and is
    # stop-aware — the stand-ins take the same signature
    monkeypatch.setattr(worker, "_discover",
                        lambda con, c, reason, stop=None: acquired.append(reason))
    monkeypatch.setattr(worker, "_scan_only",
                        lambda con, c, reason, stop=None: scanned.append(reason))

    worker._request_discovery("c", "background fetch finished", acquire=False)
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker,
                              args=(collection, gone))
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not (scanned or acquired):
            time.sleep(0.05)
        assert scanned == ["background fetch finished"]
        assert acquired == [], "a post-fetch scan must never re-acquire"
    finally:
        gone.set()
        thread.join(timeout=10)


def test_startup_and_interval_still_acquire(isolated_config):
    """The flag is narrow: the scans whose JOB is to start fetching still do."""
    for reason in ("startup", "interval", "config-changed", "added"):
        worker._request_discovery("c", reason)
        assert worker._take_discovery_request("c") == (reason, True)


def test_a_fetch_that_finished_after_removal_requests_nothing(isolated_config,
                                                              tmp_path):
    """A background fetch can outlive its collection's retirement. Re-arming
    a discovery request for a name the node no longer holds was not inert:
    the retired collection's pipeline thread keeps polling until
    `_sync_pipeline_threads` runs later in the round, so the request could be
    taken and a scan started against a collection just taken out of service.
    """
    worker._acquire_done["c"] = True
    worker._finish_completed_acquires({}, [], {}, pipeline=True)
    assert worker._take_discovery_request("c") is None


def test_acquisition_with_no_profile_refuses_instead_of_fetching_demo(
        isolated_config, tmp_path, monkeypatch):
    """`collection.profile or "demo"` used to stand in _acquire.

    A collection with acquisition ON and no profile is a configuration
    error, and answering it by fetching the DEMO slice is the wrong kind of
    answer: the operator asked for a corpus and silently got a sample —
    with the Library's "fetched N of M" counting against the sample's
    denominator, so it even looked complete. Observed live 2026-09-11,
    gutenberg's `profile = "full"` lost in a TOML rewrite.
    """
    from dataclasses import replace as _replace
    from app import db

    c = _replace(_collection(tmp_path), profile=None)
    con = db.connect(c)
    started = []
    monkeypatch.setattr(worker.threading, "Thread",
                        lambda *a, **k: started.append(k.get("args")) or _Dummy())

    worker._acquire(con, c, "test")
    assert started == [], "no fetch may start without a declared profile"
    rows = list(con.execute(
        "SELECT message FROM events WHERE stage='acquire' AND level='warn'"))
    assert rows and "no profile" in rows[-1]["message"]


class _Dummy:
    def is_alive(self): return False
    def start(self): pass
