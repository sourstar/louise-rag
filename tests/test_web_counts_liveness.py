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


def test_fetched_counter_says_when_a_pass_is_still_running(isolated_config):
    """A fetch pass streams the remote listing lazily, so fetch_files grows
    as it goes and done==total mid-pass — '1414/1414' read as complete while
    33 files were still unlisted (observed live on wikivec). The card must
    say 'fetching…' while the acquisition claim is live, and drop it once
    the claim is stale or released."""
    import json as _json
    from dataclasses import replace
    from app.acquire import claim as acquire_claim

    c = replace(collections_mod.default_collection(), acquire_enabled=True)
    con = db.connect(c)

    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_claim.CLAIM_KEY,
                 _json.dumps({"pid": 999999, "ts": time.time(),
                              "source_id": "gutenberg"})))
    assert web._collection_shelf(c, {})["fetched"]["active"] is True

    con.execute("UPDATE control SET value=? WHERE key=?",
                (_json.dumps({"pid": 999999,
                              "ts": time.time() - acquire_claim.STALE_AFTER_SECONDS - 1,
                              "source_id": "gutenberg"}),
                 acquire_claim.CLAIM_KEY))
    assert web._collection_shelf(c, {})["fetched"]["active"] is False

    # ...and the denominator is the ORIGIN's listing total when the engine
    # has recorded one (LISTING_KEY at pass start), never the streaming
    # fetch_files count that equals `done` at every instant mid-pass
    from app.acquire import engine as acquire_engine
    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_engine.LISTING_KEY,
                 _json.dumps({"files_total": 1447, "bytes_total": 1,
                              "profile": None, "ts": int(time.time())})))
    fetched = web._collection_shelf(c, {})["fetched"]
    assert fetched["remote_total"] == 1447


def test_stats_fetching_row_reads_the_listing_not_the_stream(isolated_config):
    """The Stats page's files/bytes-left sat at zero through every long
    acquisition: fetch_files streams (a row per processed file), so rows
    with fetch_done=0 barely exist mid-pass. Remaining work = the origin's
    recorded listing minus what has actually landed."""
    import json as _json
    from dataclasses import replace
    from app.acquire import engine as acquire_engine

    c = replace(collections_mod.default_collection(), acquire_enabled=True)
    con = db.connect(c)
    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_engine.LISTING_KEY,
                 _json.dumps({"files_total": 10, "bytes_total": 1000,
                              "profile": None, "ts": int(time.time())})))
    for i in range(3):
        con.execute(
            "INSERT INTO fetch_files(source_id,rel_path,fetch_done,bytes_fetched,"
            "remote_size) VALUES('gutenberg',?,1,100,100)", (f"f{i}",))
    web.invalidate_counts()

    row = web._stats_row(c, {})
    assert row["fetch"]["files_pending"] == 7, row["fetch"]
    assert row["fetch"]["bytes_pending"] == 700, row["fetch"]
    # and the embed ETA cell can explain an honest absence
    assert row["embed"]["mode"] in ("off", "lazy", "all")
    assert row["embed"]["via_archive"] is False


def test_embed_eta_is_residual_and_frozen_when_the_encoder_is_idle(isolated_config):
    """The ETA answers "how long would the rest take?" no matter the mode:
    live rate when the encoder runs, the LAST measured rate (marked frozen)
    when it is off/lazy/idle, and 'no rate yet' only when nothing was ever
    measured."""
    import json as _json
    c = collections_mod.default_collection()
    con = db.connect(c)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                "fts_done,embed_done,chunks) VALUES('g:1','g','1','/x',1,1,1,0,3600)")
    web.invalidate_counts()

    # never measured: no number to give, and the cell says so
    row = web._stats_row(c, {})
    assert row["embed"]["eta"] == "—" and row["embed"]["eta_frozen"] is False

    # encoder idle with a REMEMBERED rate: the residual ETA still shows,
    # marked frozen (the live-rate path both computes and stores this
    # record whenever the encoder is measured)
    db.set_control(con, "last_embed_rate", _json.dumps({"cps": 1.0, "ts": 1}))
    web.invalidate_counts()
    row = web._stats_row(c, {})
    assert row["embed"]["eta"] == "60 min", row["embed"]  # 3600 chunks at 1/s
    assert row["embed"]["eta_frozen"] is True


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


def test_fetched_counter_counts_extras_apart(isolated_config):
    """spec.extras policy: extra-format files (local-only, never p2p) are a
    SEPARATE '(+ e/E)' pair on the card — they must never inflate the
    Louise done/total, and the extras denominator reads the listing's
    extra_files_total (the ledger streams, like the Louise one)."""
    import json as _json
    from dataclasses import replace
    from app.acquire import engine as acquire_engine

    c = replace(collections_mod.default_collection(), acquire_enabled=True,
                extras=("epub",))
    con = db.connect(c)
    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_engine.LISTING_KEY,
                 _json.dumps({"files_total": 4, "bytes_total": 400,
                              "extra_files_total": 9, "extra_bytes_total": 900,
                              "profile": None, "ts": int(time.time())})))
    for i in range(2):
        con.execute(
            "INSERT INTO fetch_files(source_id,rel_path,fetch_done,bytes_fetched,"
            "remote_size,extra) VALUES('gutenberg',?,1,100,100,0)", (f"{i}/pg{i}.txt",))
    con.execute(
        "INSERT INTO fetch_files(source_id,rel_path,fetch_done,bytes_fetched,"
        "remote_size,extra) VALUES('gutenberg','0/pg0.epub',1,50,50,1)")
    web.invalidate_counts()

    fetched = web._collection_shelf(c, {})["fetched"]
    assert fetched["done"] == 2, "the epub must not inflate the Louise count"
    assert fetched["remote_total"] == 4
    assert fetched["has_extras"] is True
    assert fetched["extra_done"] == 1
    assert fetched["extra_total"] == 9, "denominator = the listing, not the stream"
    # a collection with no extras configured and no extra rows stays clean
    plain = replace(c, extras=())
    con.execute("DELETE FROM fetch_files WHERE extra=1")
    web.invalidate_counts()
    assert web._collection_shelf(plain, {})["fetched"]["has_extras"] is False


def test_fetch_label_state_complete_vs_waiting(isolated_config):
    """The stage label is the state: 'fetching…' while the claim is live,
    'done' only when every listed file (Louise AND opted-in extras) has
    landed with no pass running, 'waiting' otherwise. `complete` must be
    judged against the recorded listing, never the streaming ledger."""
    import json as _json
    from dataclasses import replace
    from app.acquire import engine as acquire_engine

    c = replace(collections_mod.default_collection(), acquire_enabled=True,
                extras=("epub",))
    con = db.connect(c)
    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_engine.LISTING_KEY,
                 _json.dumps({"files_total": 2, "bytes_total": 200,
                              "extra_files_total": 1, "extra_bytes_total": 50,
                              "profile": None, "ts": int(time.time())})))
    for i in range(2):
        con.execute(
            "INSERT INTO fetch_files(source_id,rel_path,fetch_done,bytes_fetched,"
            "remote_size,extra) VALUES('gutenberg',?,1,100,100,0)", (f"{i}/pg{i}.txt",))
    web.invalidate_counts()

    # Louise files all landed, but the opted-in extra has not: waiting
    f = web._collection_shelf(c, {})["fetched"]
    assert f["active"] is False and f["complete"] is False

    con.execute(
        "INSERT INTO fetch_files(source_id,rel_path,fetch_done,bytes_fetched,"
        "remote_size,extra) VALUES('gutenberg','0/pg0.epub',1,50,50,1)")
    web.invalidate_counts()
    f = web._collection_shelf(c, {})["fetched"]
    assert f["complete"] is True, f

    # a live claim always reads as fetching, never done — mid-pass
    # done==total at every instant
    from app.acquire import claim as acquire_claim
    con.execute("INSERT INTO control(key,value) VALUES(?,?)",
                (acquire_claim.CLAIM_KEY,
                 _json.dumps({"pid": 999999, "ts": time.time(),
                              "source_id": "gutenberg"})))
    f = web._collection_shelf(c, {})["fetched"]
    assert f["active"] is True and f["complete"] is False


def test_embed_eta_from_availability_rate_for_imported_collections(isolated_config):
    """A genesis collection's encoder never runs, but its embeddings still
    become available at a measurable rate (import, paced by parsing and
    indexing — a non-trivial cost). The ETA must price THAT rate instead of
    shrugging 'with indexing (imported)': d(embed_done)/dt sampled across
    page loads, doc-based, covering the whole remaining pipeline (so it is
    an estimate, never a floor)."""
    import json as _json
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                genesis_archive="medcpt-pubmed")
    con = db.connect(c)
    # 50 already available, 10 indexed and waiting, 5 not indexed yet
    for i in range(50):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,1,1,10)", (f"g:{i}", str(i), f"/x{i}"))
    for i in range(50, 60):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,1,0,10)", (f"g:{i}", str(i), f"/x{i}"))
    for i in range(60, 65):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,0,0,0)", (f"g:{i}", str(i), f"/x{i}"))
    # an earlier sample saw 0 available, 100 s ago -> 0.5 docs/s since
    db.set_control(con, "embed_avail_rate",
                   _json.dumps({"done": 0, "ts": int(time.time()) - 100}))
    web.invalidate_counts()

    row = web._stats_row(c, {})
    e = row["embed"]
    assert e["eta_source"] == "avail", e
    assert e["eta_frozen"] is False
    assert e["eta_is_floor"] is False, "the availability rate covers unindexed docs"
    # 15 docs remain (10 ready + 5 unindexed) at 0.5 docs/s = 30 s -> "0 min"
    assert e["eta"] != "—"

    # a stage reset that SHRINKS embed_done starts a fresh baseline
    # rather than recording a negative rate
    con.execute("UPDATE documents SET embed_done=0 WHERE uid='g:0'")
    db.set_control(con, "embed_avail_rate",
                   _json.dumps({"done": 999, "ts": int(time.time()) - 100}))
    web.invalidate_counts()
    row = web._stats_row(c, {})
    rec = _json.loads(db.get_control(con, "embed_avail_rate"))
    assert rec["done"] == 49 and "dps" not in rec
