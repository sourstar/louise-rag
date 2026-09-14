"""A reset is seen at once, whatever refresh was already in flight.

Found 2026-09-14 as an intermittent failure of
test_stats.py::test_a_reset_invalidates_the_cache. `invalidate_counts`
dropped the cached entry, but a refresh started BEFORE the reset survived
it: its guard stopped a fresh refresh from starting, so the next view
waited out the cold wait and fell back to the stored row, and its
pre-reset result could still land in the cache and in that row afterwards.
The operator saw the old count right after their own reset. An
invalidation now bumps a generation: a refresh from an older one neither
blocks a new refresh nor writes what it counted.
"""
import threading

from app import collections as collections_mod, db, web


def _join_refreshes() -> None:
    for t in threading.enumerate():
        if t.name.startswith("counts:"):
            t.join(10)


def test_a_refresh_from_before_an_invalidation_is_discarded(isolated_config, monkeypatch):
    col = collections_mod.default_collection()
    con = db.connect(col)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,error,retries) VALUES('g:9','g','9','/x',1,1,'rdf: boom',9)")
    web.invalidate_counts()
    web._counts_refreshing.clear()

    real = web._document_counts_query
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def stale_first(c):
        calls.append(1)
        counts = real(c)                  # counted BEFORE the reset…
        if len(calls) == 1:
            started.set()
            release.wait(10)              # …and landing AFTER it
        return counts
    monkeypatch.setattr(web, "_document_counts_query", stale_first)

    web._refresh_counts_async(col)
    assert started.wait(10), "the stale refresh is in flight"
    con.execute("UPDATE documents SET error=NULL, retries=0")     # the reset
    web.invalidate_counts(col)

    counts, _ = web._document_counts(col)
    release.set()
    assert counts is not None and counts["blocked"] == 0, \
        "the refresh in flight must not stand in for a fresh one"

    _join_refreshes()
    assert web._document_counts(col)[0]["blocked"] == 0, \
        "the stale result must not overwrite the fresh one in the cache"
    assert web._stored_counts(col)[0]["blocked"] == 0, \
        "…nor in the manifest row a cold process reads"
