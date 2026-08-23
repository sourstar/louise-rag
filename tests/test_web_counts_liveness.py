"""The Library's counters must never silently freeze, and must say why
they are empty or waiting.

Observed live ("parsed/indexed/embedded counters are stuck", "pre-embedded
data: 0/37 shards"): three display defects stacked on top of a genuinely
starved pipeline. (1) A cold cache after restart blocked the page for
~25 s per large collection — a minute-plus hang that reads as stuck.
(2) A refresh that failed kept the old value FOREVER, silently
(`except: pass`), and a refresh thread that hung held the in-flight guard
forever, so no refresh ever ran again. (3) pubmed's remote genesis archive
sits deliberately behind the settle gate for the weeks its 33M documents
take to index, and the card said "the worker drains the rest
automatically" — promising activity the gate withholds.
"""
import threading
import time

import pytest

from app import collections as collections_mod, db, web


@pytest.fixture
def col(isolated_config):
    c = collections_mod.default_collection()
    con = db.connect(c)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,fts_done,embed_done) VALUES('g:1','g','1','/x',1,1,1,0)")
    web.invalidate_counts()
    yield c
    web.invalidate_counts()


def _wait_counts(c, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        counts, _ = web._document_counts(c)
        if counts is not None:
            return counts
        time.sleep(0.05)
    raise AssertionError("counts never arrived")


def test_a_cold_small_manifest_still_answers_synchronously(col):
    counts, age = web._document_counts(col)
    assert counts is not None and counts["total"] == 1 and age == 0.0


def test_a_slow_cold_scan_yields_counting_not_a_hang(col, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real = web._document_counts_query

    def slow(c):
        started.set()
        assert release.wait(10)
        return real(c)
    monkeypatch.setattr(web, "_document_counts_query", slow)

    t0 = time.time()
    counts, _ = web._document_counts(col)
    took = time.time() - t0
    assert started.is_set() and counts is None, "must render 'counting', not block"
    assert took < web._COUNTS_COLD_WAIT_S + 2

    shelf_pending = web._collection_shelf(col, {})
    assert shelf_pending["counts_pending"] is True
    assert shelf_pending["total"] == 0

    release.set()
    counts = _wait_counts(col)
    assert counts["total"] == 1
    assert web._collection_shelf(col, {})["counts_pending"] is False


def test_a_failing_refresh_is_not_silent_and_a_hung_guard_expires(col, monkeypatch, capsys):
    _wait_counts(col)
    name = web._counts_key(col)

    # a refresh that fails logs, keeps the value while it is young...
    with web._counts_lock:
        at, counts = web._counts_cache[name]
        web._counts_cache[name] = (at - web._COUNTS_TTL_S - 1, counts)
    monkeypatch.setattr(web, "_document_counts_query",
                        lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    web._refresh_counts_async(col)
    deadline = time.time() + 5
    while web._counts_refreshing and time.time() < deadline:
        time.sleep(0.02)
    assert "counts refresh failed" in capsys.readouterr().out
    assert name in web._counts_cache, "a young value survives a failed refresh"

    # ...but a value past _COUNTS_MAX_AGE_S is dropped rather than frozen
    with web._counts_lock:
        web._counts_cache[name] = (time.time() - web._COUNTS_MAX_AGE_S - 1, counts)
    web._refresh_counts_async(col)
    deadline = time.time() + 5
    while name in web._counts_cache and time.time() < deadline:
        time.sleep(0.02)
    assert name not in web._counts_cache, \
        "an unrefreshable entry must eventually make way for the truth"

    # a hung in-flight guard must not block refreshes forever
    with web._counts_lock:
        web._counts_refreshing[name] = time.time() - web._COUNTS_REFRESH_ABANDON_S - 1
    calls = []
    monkeypatch.setattr(web, "_document_counts_query",
                        lambda c: (calls.append(1), {"total": 1})[1])
    web._refresh_counts_async(col)
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    assert calls, "an abandoned guard must expire and let a new refresh run"


def test_remote_genesis_archive_reports_waiting_until_settled(isolated_config):
    """The pubmed shape: medcpt-pubmed declared, a large rdf/fts backlog.
    The card must say WAITING with the backlog size — not 0/37 shards with
    'drains automatically'."""
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                genesis_archive="medcpt-pubmed")
    con = db.connect(c)

    state = web._genesis_state(con, c, {"in_scope": 100, "fts_done": 40})
    assert state["waiting_docs"] == 60
    assert state["local"] is False and state["shard_total"] == 37

    settled = web._genesis_state(con, c, {"in_scope": 100, "fts_done": 100})
    assert settled["waiting_docs"] == 0

    unknown = web._genesis_state(con, c, None)      # cold counts
    assert unknown["waiting_docs"] is None
