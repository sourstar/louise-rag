"""The Library card after a restart: stale-and-labelled, never zero.

`_counts_cache` lives in the web PROCESS. Pubmed's count scan takes 55.4 s
on the reference node, so for the 55 seconds after every restart its card
read `total 0, in_scope 0, rdf_done 0` — the collection looked EMPTY, which
is indistinguishable from a stuck pipeline and was reported as one
(operator, 2026-09-12). Six restarts in one afternoon made that the normal
view.

THE COUNTS ARE THE ONE THING THE PAGE EXISTS TO SHOW, so the rule is: a
cold process says what was true a few minutes ago, with the age beside it.
"""
import json
import threading
import time

import pytest

from app import collections as collections_mod, db, web


@pytest.fixture(autouse=True)
def _cold(monkeypatch):
    """Every test starts with an empty in-process cache — the state a
    restarted container is in."""
    web._counts_cache.clear()
    web._counts_refreshing.clear()
    web._counts_scan_secs.clear()
    yield
    # `_slow_scan` starts threads that sleep five seconds and then WRITE.
    # Left running, they hold the manifest's write lock while the next
    # test is connecting, and it fails at `PRAGMA journal_mode=WAL` — a
    # failure that reads like a bug in the code under test and is not one.
    _settle()
    web._counts_cache.clear()
    web._counts_refreshing.clear()
    web._counts_scan_secs.clear()


def _settle(timeout=30.0):
    """Wait for the refresh threads by NAME (`counts:<collection>`).

    `_slow_scan` starts threads that sleep and then WRITE. Left running,
    they hold the manifest's write lock while the next test is
    connecting, and it fails at `PRAGMA journal_mode=WAL` — a failure
    that reads like a bug in the code under test and is not one. It only
    showed up in the FULL suite, which is what made it look flaky.
    """
    for thread in threading.enumerate():
        if thread.name.startswith("counts:"):
            thread.join(timeout=timeout)


def _slow_scan(monkeypatch, counts, seconds=5.0):
    """A scan too slow to finish inside the cold wait — pubmed's is 55 s
    against a 2 s wait."""
    def scan(collection):
        time.sleep(seconds)
        return counts
    monkeypatch.setattr(web, "_document_counts_query", scan)


def test_a_cold_process_shows_the_stored_counts_not_zeros(isolated_config, monkeypatch):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    stored = {"total": 39994988, "in_scope": 39994988, "rdf_done": 39994988,
              "fts_done": 39994988, "embed_done": 35417887}
    db.set_control(con, web._COUNTS_CONTROL_KEY,
                   json.dumps({"at": time.time() - 300, "counts": stored}))
    _slow_scan(monkeypatch, dict(stored, embed_done=35417999))

    counts, age = web._document_counts(collection)
    assert counts is not None, "a cold process must not report an empty collection"
    assert counts["total"] == 39994988
    assert 250 < age < 400, "and must say how old the number is"


def test_the_age_is_what_makes_it_honest(isolated_config, monkeypatch):
    """Stale is fine; stale-and-presented-as-live is not. The card shows
    `counts_age`, so the operator can tell a five-minute-old number from a
    current one."""
    collection = collections_mod.default_collection()
    db.set_control(db.connect(collection), web._COUNTS_CONTROL_KEY,
                   json.dumps({"at": time.time() - 1234, "counts": {"total": 7}}))
    _slow_scan(monkeypatch, {"total": 7})
    _, age = web._document_counts(collection)
    assert age > 1000


def test_a_successful_scan_persists_for_the_next_process(isolated_config, monkeypatch):
    collection = collections_mod.default_collection()
    monkeypatch.setattr(web, "_document_counts_query",
                        lambda c: {"total": 42, "in_scope": 42})
    web._refresh_counts_async(collection)
    # JOIN, don't poll. Polling opened a connection per attempt — a
    # hundred of them — and `db.connect` runs `PRAGMA journal_mode=WAL`,
    # which needs the write lock, so the poll raced the very write it was
    # waiting for and failed as "database is locked".
    _settle()
    raw = db.get_control(db.connect(collection), web._COUNTS_CONTROL_KEY)
    assert raw, "the refresh thread never persisted its counts"
    got = json.loads(raw)
    assert got["counts"]["total"] == 42
    assert got["at"] > 0


def test_no_stored_counts_still_means_counting_not_zero(isolated_config, monkeypatch):
    """With nothing stored there is nothing honest to show, and the card
    renders "counting…" — `None`, never a fabricated zero."""
    collection = collections_mod.default_collection()
    _slow_scan(monkeypatch, {"total": 1})
    counts, age = web._document_counts(collection)
    assert counts is None and age == 0.0


def test_a_malformed_stored_row_is_ignored_not_fatal(isolated_config, monkeypatch):
    collection = collections_mod.default_collection()
    db.set_control(db.connect(collection), web._COUNTS_CONTROL_KEY, "not json{")
    _slow_scan(monkeypatch, {"total": 1})
    counts, _ = web._document_counts(collection)
    assert counts is None, "unreadable is no answer, and never a broken page"


def test_a_warm_cache_still_beats_the_stored_row(isolated_config, monkeypatch):
    """The stored row is the COLD path only: a live process must serve its
    own fresh numbers, not a minutes-old snapshot."""
    collection = collections_mod.default_collection()
    db.set_control(db.connect(collection), web._COUNTS_CONTROL_KEY,
                   json.dumps({"at": time.time() - 900, "counts": {"total": 1}}))
    monkeypatch.setattr(web, "_document_counts_query", lambda c: {"total": 999})
    web._document_counts(collection)           # warms it
    _settle()
    counts, age = web._document_counts(collection)
    assert counts["total"] == 999 and age < 60
