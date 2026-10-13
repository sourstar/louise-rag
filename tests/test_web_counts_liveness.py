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


def _wait_counts(c, timeout=30.0):
    """The counts as the refresh lands them. A test that asserts on rates
    or ETAs must wait for this: `_collection_shelf` renders "counting…" —
    and computes no rate at all — when the refresh misses the page's
    2-second cold wait, which a loaded machine does (a full-suite run
    failed `test_a_bursty_stage_keeps_its_eta_across_a_quiet_window` that
    way, 2026-09-14). Returns as soon as they arrive; the bound is only
    there for a refresh that never does."""
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
    # A refresh fills the cache BEFORE it persists the counts, and holds its
    # guard until it has — so the cache can be ready while the guard is still
    # held, and a refresh asked for meanwhile is skipped, by design. On a
    # loaded node the forced failing refresh below was skipped that way and
    # nothing was printed (node suite, 2026-09-15). Persisting is made slow
    # here so the race is met on every run, and the test waits for the guard.
    real_persist = web._persist_counts
    monkeypatch.setattr(web, "_persist_counts",
                        lambda c, counts: (time.sleep(0.5), real_persist(c, counts)))
    name = web._counts_key(col)
    _wait_counts(col)
    deadline = time.time() + 10
    while name in web._counts_refreshing and time.time() < deadline:
        time.sleep(0.02)
    assert name not in web._counts_refreshing, "the first refresh finished"

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


def test_local_genesis_archive_reports_indexing_pacing(isolated_config):
    """The wikivec shape: all 1,447 shard files fetched, yet the card read
    '958/1447 shards' — shards are read when an INDEXED document's locator
    names them, so the shard counter measures pacing, not a stall. A local
    archive must expose waiting_docs too, so the card can lead with
    documents and say 'import follows indexing'."""
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                genesis_archive="upstash-wikipedia-2024-06")
    con = db.connect(c)

    state = web._genesis_state(con, c, {"in_scope": 1000, "fts_done": 10})
    assert state["local"] is True
    assert state["waiting_docs"] == 990


def test_genesis_badge_speaks_the_archive_fetch_state(isolated_config):
    """Operator color scheme for the archive badge: green when the archive
    data is fetched/done, orange while fetching is in progress, grey when
    fetching is off/idle. A STAND-ALONE pre-embedded collection (wikivec,
    the `embedded` plugin: its card IS the archive) reads the card's own
    fetched state; a remote archive reads the shard download state."""
    from dataclasses import replace
    local = replace(collections_mod.default_collection(), plugin="embedded",
                    genesis_archive="upstash-wikipedia-2024-06")
    con = db.connect(local)

    s = web._genesis_state(con, local, None,
                           fetched={"complete": True, "active": False})
    assert s["fetch_state"] == "done"
    s = web._genesis_state(con, local, None,
                           fetched={"complete": False, "active": True})
    assert s["fetch_state"] == "active"
    s = web._genesis_state(con, local, None,
                           fetched={"complete": False, "active": False})
    assert s["fetch_state"] == "idle"

    remote = replace(collections_mod.default_collection(),
                     genesis_archive="medcpt-pubmed")
    rcon = db.connect(remote)
    assert web._genesis_state(rcon, remote, None)["fetch_state"] == "idle"
    rcon.execute("INSERT INTO genesis_imports(archive,shard,source_url,"
                 "content_digest,state,docs,chunks,detail,updated_at) "
                 "VALUES('medcpt-pubmed','s1','u','d','applying',0,0,'',1)")
    assert web._genesis_state(rcon, remote, None)["fetch_state"] == "active"


def test_remote_genesis_card_shows_indexing_progress_toward_import(isolated_config):
    """'0/37, waits by design' is a fact but not a meter (operator report:
    'you must show a progress'). The card line must carry the number that
    actually moves toward the import — indexing — using the same counts as
    the indexed row, so nothing on the card can disagree."""
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                genesis_archive="medcpt-pubmed")
    con = db.connect(c)

    state = web._genesis_state(con, c, {"in_scope": 200, "fts_done": 50})
    assert state["index_done"] == 50
    assert state["index_total"] == 200
    assert state["index_pct"] == 25.0

    cold = web._genesis_state(con, c, None)
    assert cold["index_pct"] is None, "a cold cache says counting, never 0%"


def _render_library(shelf):
    return web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0,
        plugins=[], plugin_profiles={}, plugin_extras={}, corpus_mounts=[],
        corpus_mounts_dir="", worker_state="running")


def test_library_card_renders_pre_embedded_data_in_one_line(isolated_config):
    """Every collection WITH PRE-EMBEDDINGS — pubmed and MedCPT, wikipedia
    and Upstash (operator, 2026-09-14) — gets the sub-box, and it holds
    ONLY what is specific to the archive (its fetched (vectors) meter and
    the waiting note). The dense row and the gap-fill knob live in the MAIN
    box on every card (operator: pubmed's main box read as truncated
    without them). No repeated space badge, no duplicate indexing numbers.
    Only a stand-alone pre-embedded collection (wikivec, the `embedded`
    plugin: its card IS the archive) stays ONE box; a plain collection
    keeps embedded (dense)."""
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                genesis_archive="medcpt-pubmed")
    con = db.connect(c)
    # amendment 38: two shards landed, one applied with documents still
    # indexing, one final; 12 documents covered so far
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,chunks,bytes,pending,updated_at) "
                "VALUES('medcpt-pubmed','0','final',9,9,0,0,0)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,chunks,bytes,pending,updated_at) "
                "VALUES('medcpt-pubmed','1','applied',3,3,3000000000,7,0)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) "
                "VALUES('medcpt-pubmed','2','fetching',0)")
    shelf = web._collection_shelf(c, {})
    g = shelf["genesis"]
    assert (g["fetched"], g["final"], g["docs"], g["pending"]) == (2, 1, 12, 7)
    assert g["resident_bytes"] == 3000000000 and g["fetch_state"] == "active"
    assert g["imported_vectors"] == 12, "the vectors the archive brought: applied and final shards"
    html = _render_library(shelf)
    # operator, 2026-10-13: pre-embedded data is ONE line on every card —
    # "N pre-embedded vectors by XYZ"; the sub-box and its rows are gone
    assert "12 pre-embedded vectors by medcpt-pubmed" in html, "no credit: the archive's id names it"
    assert html.count(">embedded (dense)</span>") == 1, "the main box's dense row is total coverage"
    assert "fetched (vectors)" not in html and ">pre-embedded (dense)</span>" not in html
    assert "pre-embedded data:" not in html and "genesis retry" not in html
    assert html.count("Embed mode —") == 1, "ONE knob"

    wikipedia = replace(collections_mod.default_collection(), plugin="wikipedia",
                        genesis_archive="upstash-wikipedia-2024-06")
    html_wiki = _render_library(web._collection_shelf(wikipedia, {}))
    assert "0 pre-embedded vectors by upstash-wikipedia-2024-06" in html_wiki
    assert "fetched (vectors)" not in html_wiki

    standalone = replace(collections_mod.default_collection(), plugin="embedded",
                         genesis_archive="upstash-wikipedia-2024-06")
    html_local = _render_library(web._collection_shelf(standalone, {}))
    assert "pre-embedded vectors by" in html_local, "the same one line on every card"
    assert ">pre-embedded (dense)</span>" in html_local,         "the corpus IS the vectors: its one dense row keeps the name"
    assert html_local.count("Embed mode —") == 1

    plain = web._collection_shelf(collections_mod.default_collection(), {})
    html2 = _render_library(plain)
    assert ">embedded (dense)</span>" in html2
    assert "pre-embedded (dense)" not in html2


def test_library_card_shows_the_plugin_display_label(isolated_config):
    """'embedded' is the internal id; every human-facing surface says
    wikivec (operator: keep it consistent, in general)."""
    from dataclasses import replace
    c = replace(collections_mod.default_collection(),
                name="wikivec", plugin="embedded")
    assert web._plugin_label("embedded") == "wikivec"
    shelf = web._collection_shelf(c, {})
    assert shelf["plugin_label"] == "wikivec"
    html = _render_library(shelf)
    assert ">wikivec</span>" in html


def test_stage_rates_prove_motion_on_slow_stages(isolated_config):
    """pubmed's parsed bar moves 0.045%/h at a healthy 18k docs/h — it READ
    as stuck (operator report) while the events table showed steady motion.
    The card now carries a measured +N/h beside parsed/indexed, computed
    across page loads and persisted; a zero delta shows NOTHING (a stale
    rate beside a genuinely stalled stage would lie); a shrinking count
    (reset) re-baselines."""
    import json as _json
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(100):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,0,0,0)", (f"g:{i}", str(i), f"/x{i}"))
    for i in range(100, 300):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,0,0,0,0)", (f"g:{i}", str(i), f"/x{i}"))
    # memory-first: seed the memo the way a prior page load would have
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    # an earlier sample saw 10 parsed, 600 s ago -> (100-10)/600*3600 = 540/h
    db.set_control(con, "stage_rate",
                   _json.dumps({"rdf": 10, "fts": 0, "embed": 0, "fetch": 0, "rdf_ph": 540, "rdf_n": 1,
                                "ts": int(time.time()) - 600}))
    web.invalidate_counts()
    _wait_counts(c)

    shelf = web._collection_shelf(c, {})
    assert shelf["stage_rates"]["rdf"] == 540
    assert shelf["stage_rates"]["fts"] is None, \
        "fts did not advance (0 -> 0): no rate, never a stale one"
    # the card shows a compact ETA, dot-grouped, never a bare rate:
    # 200 remaining at 540/h = ~22 min
    assert shelf["stage_etas"]["rdf"] == "~22min"
    assert shelf["stage_etas"]["fts"] == web.ETA_UNKNOWN, \
        "work remains, no trustworthy rate: a dash, and the reason in the tooltip"
    assert "no rate yet" in shelf["stage_eta_why"]["fts"]
    html = _render_library(shelf)
    assert "(~22min)" in html
    # An unknown ETA shows a DASH, and the reason is in its tooltip
    # (operator, 2026-09-12). It used to show "TBD" and a tooltip reading
    # literally "+/h" — a plus and a per-hour around nothing.
    assert "(-)" in html and "measuring — no rate yet" in html
    assert "TBD" not in html and "+/h" not in html

    # A COUNT THAT SHRANK RE-BASELINES ITS OWN STAGE, NOT ALL FOUR. This
    # used to be one `any(...)` over every field, so a single dip — a
    # reset, a re-mint, a fetch total revised down — discarded the rate
    # and sample count of the other three stages too, and every one of
    # them fell back to a placeholder for the next two windows.
    web._stage_rate_memo[str(c.manifest_path)] = {
        "rdf": 99999, "fts": 0, "embed": 0, "fetch": 0,
        "rdf_ph": 540, "rdf_n": 2, "embed_ph": 1200, "embed_n": 2,
        "ts": int(time.time()) - 600}
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {})
    rec = web._stage_rate_memo[str(c.manifest_path)]
    assert rec["rdf"] == 100 and rec["rdf_ph"] is None and rec["rdf_n"] == 0, \
        "the stage whose count went backwards starts over"
    assert rec["embed_ph"] == 1200 and rec["embed_n"] == 2, \
        "and the stages that did not are left alone"
    assert shelf["stage_rates"]["rdf"] is None

    # a pre-upgrade record (rdf/fts only) in the CONTROL table (a restart's
    # warm start) re-baselines instead of minting a bogus rate from a
    # missing field
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    db.set_control(con, "stage_rate",
                   _json.dumps({"rdf": 10, "fts": 0, "ts": int(time.time()) - 600}))
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {})
    assert all(v is None for v in shelf["stage_rates"].values())


def test_page_render_never_blocks_on_a_locked_manifest(isolated_config):
    """Observed live: Library and Stats 'look busy' — the rate-sample
    writes added to page-render code sat in the 60 s busy timeout while
    the worker held its write lock, then raised 'database is locked' into
    the page. A page is a READER: under contention it must render now,
    skip the sample, and try again next load."""
    import sqlite3 as _sq
    c = collections_mod.default_collection()
    con = db.connect(c)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,fts_done,embed_done,chunks) "
                "VALUES('g:1','g','1','/x',1,1,1,0,10)")
    con.commit()
    web.invalidate_counts()
    _wait_counts(c)

    blocker = _sq.connect(str(c.manifest_path))
    try:
        blocker.execute("BEGIN IMMEDIATE")   # the worker's write lock
        t0 = time.time()
        shelf = web._collection_shelf(c, {})
        took = time.time() - t0
        assert shelf["name"] == c.name
        assert took < 5, f"page render blocked {took:.1f}s on a locked manifest"
    finally:
        blocker.rollback()
        blocker.close()


def test_stage_rates_survive_a_manifest_too_busy_to_write(isolated_config):
    """Observed live after the throughput fix: pubmed's manifest takes
    back-to-back pipeline transactions, so the 200 ms sample write lost
    the lock race for over an hour and the baseline never landed — no
    ETA on exactly the collection that most needed one. Samples live in
    process memory first; the control write is persistence, not a
    dependency."""
    import json as _json
    import sqlite3 as _sq
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(100):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,0,0,0)", (f"g:{i}", str(i), f"/x{i}"))
    for i in range(100, 300):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,0,0,0,0)", (f"g:{i}", str(i), f"/x{i}"))
    con.commit()
    web.invalidate_counts()
    _wait_counts(c)
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    # seed only MEMORY (the busy-manifest case: control never accepts it)
    web._stage_rate_memo[str(c.manifest_path)] = {
        "rdf": 10, "fts": 0, "embed": 0, "fetch": 0, "rdf_ph": 540, "rdf_n": 1,
        "ts": int(time.time()) - 600}

    blocker = _sq.connect(str(c.manifest_path))
    try:
        blocker.execute("BEGIN IMMEDIATE")     # a busy pipeline, permanently
        shelf = web._collection_shelf(c, {})
    finally:
        blocker.rollback()
        blocker.close()
    assert shelf["stage_rates"]["rdf"] == 540, \
        "the rate must come from memory even when the manifest refuses writes"
    assert shelf["stage_etas"]["rdf"] == "~22min"
    assert web._stage_rate_memo[str(c.manifest_path)]["rdf"] == 100


def test_rdf_ahead_of_fts_is_shown_as_waiting_not_tbd(isolated_config):
    """Operator report: pubmed's parsed (rdf) ETA read "TBD" for hours. It
    was not stalled — rdf led fts by 4.2M documents, and the worker gives
    the thread to fts while a full batch is ready (_next_book), so rdf
    waits by design. The card says so instead of TBD.

    IN WORDS since 2026-09-12. "after fts (607 ahead)" named an internal
    stage, an unstated unit and an arithmetic the reader cannot check
    (operator: "means nothing to the user"), and it was the one count in
    the interface with comma grouping — the separator the `num` filter
    exists to avoid. The lead moves to the tooltip."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(2000):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) VALUES(?,'g',?,?,1,?,0,0,0)",
                    (f"g:{i}", str(i), f"/x{i}", 1 if i < 1000 else 0))
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {}, "running")
    assert shelf["stage_etas"]["rdf"] == "waiting for the index"
    assert "1.000 documents ahead" in shelf["stage_eta_why"]["rdf"]
    assert shelf["stage_etas"]["fts"] == web.ETA_UNKNOWN,         "fts itself has no history yet"
    html = _render_library(shelf)
    assert "waiting for the index" in html
    assert "1.000 documents ahead" in html
    assert "after fts" not in html, "the internal stage name is gone"
    assert "+None" not in html and "+/h" not in html

    # NOT while the worker is stopped: nothing is waiting for indexing
    # then, it is waiting for the operator
    stopped = web._collection_shelf(c, {}, "paused")
    assert stopped["stage_etas"]["rdf"] == web.ETA_UNKNOWN

    # once fts catches up to within a batch, rdf is judged on its own rate again
    con.execute("UPDATE documents SET fts_done=1 WHERE CAST(native_id AS INTEGER) < 950")
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {}, "running")
    assert shelf["stage_etas"]["rdf"] == web.ETA_UNKNOWN


def test_embed_mode_persists_even_when_the_manifest_is_locked(isolated_config, monkeypatch, tmp_path):
    """Operator report: wikipedia switched to off reverted to on-demand on
    refresh. The endpoint wrote the runtime scope FIRST, and on a manifest
    under continuous pipeline writes that raised 'database is locked'
    before the TOML was ever written — the 500 reloaded the page and the
    mode reverted. The TOML (a file, never contended) must be written
    FIRST; the scope write is best-effort immediacy, never a gate."""
    import sqlite3 as _sq
    from fastapi.testclient import TestClient
    from app import config as _config, spaces
    from app.api import app as _app
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(_config, "ADMIN_TOKEN", "sekrit")

    c = collections_mod.default_collection()
    db.connect(c)                                     # create the manifest
    with TestClient(_app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        blocker = _sq.connect(str(c.manifest_path))
        try:
            blocker.execute("BEGIN IMMEDIATE")        # the busy pipeline
            r = client.post(f"/admin/library/{c.name}/embed-mode",
                            json={"mode": "off"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["persisted"] is True
            assert body["applied_now"] is False, \
                "the scope write must yield to the lock, not 500"
        finally:
            blocker.rollback()
            blocker.close()
    # the DESIRED STATE survived: a fresh load shows the new mode
    loaded = collections_mod.by_name(c.name)
    assert loaded.embed_mode_default == "off"
    # and with the manifest free, the same call applies immediately too
    with TestClient(_app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        r = client.post(f"/admin/library/{c.name}/embed-mode",
                        json={"mode": "lazy"})
        assert r.status_code == 200 and r.json()["applied_now"] is True
    assert collections_mod.by_name(c.name).embed_mode_default == "lazy"


def test_stage_label_colors_are_consistent(isolated_config):
    """Operator's four-state scheme, pinned per state: green = complete,
    turquoise = running/scheduled, orange = on-demand only, grey =
    off/paused — and NEUTRAL while counts are still being computed (a
    cold-cache card colored its labels from all-zero counts)."""
    import re
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(4):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,?,0,1)", (f"g:{i}", str(i), f"/x{i}",
                                                    1 if i < 2 else 0))
    web.invalidate_counts()
    _wait_counts(c)

    def color_of(html, label):
        m = re.search(r'style="color:([^"]*)"[^>]*>' + re.escape(label) + "<", html)
        if m:
            return m.group(1)
        m = re.search(r"<span[^>]*>" + re.escape(label) + "<", html)
        return "neutral" if m else None

    shelf = web._collection_shelf(c, {})
    html = _render_library(shelf)                      # worker running
    assert color_of(html, "parsed (rdf)") == "var(--ok)"          # 4/4
    assert color_of(html, "indexed (lexical)") == "var(--brass)"  # 2/4, running
    assert color_of(html, "embedded (dense)") == "var(--amber)"   # mode lazy

    scope = db.get_scope(con); scope["embed_mode"] = "off"; db.set_scope(con, scope)
    web.invalidate_counts(); _wait_counts(c)
    html = _render_library(web._collection_shelf(c, {}))
    assert color_of(html, "embedded (dense)") == "var(--muted)"   # mode off

    # paused node: nothing runs — grey everywhere that is not complete
    html_p = web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library",
        shelves=[web._collection_shelf(c, {})], counts_age=0, plugins=[],
        plugin_profiles={}, plugin_extras={}, corpus_mounts=[],
        corpus_mounts_dir="", worker_state="paused")
    assert color_of(html_p, "indexed (lexical)") == "var(--muted)"
    assert color_of(html_p, "parsed (rdf)") == "var(--ok)", \
        "complete stays green even while paused"

    # counts pending: neutral, never colored from zeros
    pending = dict(web._collection_shelf(c, {}))
    pending.update(counts_pending=True, in_scope=0, rdf_done=0, fts_done=0,
                   embed_done=0)
    html_c = _render_library(pending)
    assert color_of(html_c, "parsed (rdf)") == "neutral"
    assert color_of(html_c, "indexed (lexical)") == "neutral"
    assert color_of(html_c, "embedded (dense)") == "inherit"


def test_seed_only_and_state_gates(isolated_config):
    """The two seeding modes exist, validate, and gate like their design
    says: seed-only idles the pipeline exactly like pause (what stays
    alive lives outside the worker loop)."""
    assert "seed-only" in db.WORKER_STATES
    assert "acquire-only" in db.WORKER_STATES
    con = db.connect()
    db.set_worker_state(con, "seed-only")
    assert db.worker_state(con) == "seed-only"
    html = web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[], counts_age=0, plugins=[],
        plugin_profiles={}, plugin_extras={}, corpus_mounts=[],
        corpus_mounts_dir="", worker_state="seed-only")
    assert "seeding only" in html and "download/seeding only" in html
    assert "rescan" in html


def test_a_bursty_stage_keeps_its_eta_across_a_quiet_window(isolated_config):
    """Louise2's gutenberg embeds a 1,000-chunk book every ~6 minutes: most
    5-minute windows see NO commit. A quiet window is not evidence of a
    zero rate — the window stretches until the stage moves again — and
    only a stall of _STALL_S (30 min) drops the ETA to a dash."""
    import json as _json
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(100):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    "VALUES(?,'g',?,?,1,1,1,?,0)", (f"g:{i}", str(i), f"/x{i}", 1 if i < 20 else 0))
    key = str(c.manifest_path)
    now = int(time.time())
    # two windows of evidence at 60/h; the latest sample saw NO progress
    # 6 minutes ago, but progress last showed 12 minutes ago (embed_pc=20)
    web._stage_rate_memo[key] = {
        "rdf": 100, "fts": 100, "embed": 20, "fetch": 0, "ts": now - 360,
        "embed_ph": 60, "embed_n": 2, "embed_pts": now - 720, "embed_pc": 20}
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {})
    assert shelf["stage_rates"]["embed"] == 60 and shelf["stage_etas"]["embed"] == "~80min", \
        "a quiet window keeps the smoothed rate (80 left at 60/h)"
    rec = web._stage_rate_memo[key]
    assert rec["embed_n"] == 2 and rec["embed_pts"] == now - 720, \
        "no progress: the window keeps stretching from the last progress"

    # progress arrives: measured over the STRETCHED span (12 min, 4 docs
    # -> 20/h raw), smoothed into the EMA, never over the last 5 minutes
    con.execute("UPDATE documents SET embed_done=1 WHERE uid IN ('g:20','g:21','g:22','g:23')")
    web._stage_rate_memo[key] = {**rec, "ts": now - 360}
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {})
    assert shelf["stage_rates"]["embed"] == round(0.3 * 20 + 0.7 * 60)
    assert web._stage_rate_memo[key]["embed_pc"] == 24

    # a real stall (no progress for 30 min) is TBD, not a frozen number
    web._stage_rate_memo[key] = {
        "rdf": 100, "fts": 100, "embed": 24, "fetch": 0, "ts": now - 360,
        "embed_ph": 48, "embed_n": 3, "embed_pts": now - 1801, "embed_pc": 24}
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {})
    assert shelf["stage_rates"]["embed"] is None
    assert shelf["stage_etas"]["embed"] == web.ETA_UNKNOWN
    assert "30 minutes" in shelf["stage_eta_why"]["embed"], \
        "a stall says which of the four reasons it is"


# ---- the instrument must not cost what it measures -------------------------

def test_a_cheap_collection_keeps_the_two_minute_ttl():
    from app import web
    web._counts_scan_secs.clear()
    assert web._counts_ttl_for("/x/small.db") == web._COUNTS_TTL_S
    web._counts_scan_secs["/x/small.db"] = 0.4          # a small manifest
    assert web._counts_ttl_for("/x/small.db") == web._COUNTS_TTL_S


def test_an_expensive_collection_earns_a_longer_ttl():
    """Measured on the live node 2026-09-11: pubmed's counts scan is a full
    pass over 40M rows and took 56 s, against a 120 s TTL — so nearly half
    of every interval was spent counting, on the same disk and the same
    SQLite page cache the archive import was using. A constant TTL does not
    grow with the corpus and the scan does, which is the shape CLAUDE.md
    rules out: a per-round cost must scale with the work available, never
    with the size of the corpus."""
    from app import web
    web._counts_scan_secs.clear()
    web._counts_scan_secs["/x/pubmed.db"] = 56.0
    ttl = web._counts_ttl_for("/x/pubmed.db")
    assert ttl == 560.0
    assert 56.0 / ttl <= web._COUNTS_MAX_DUTY
    web._counts_scan_secs.clear()


def test_a_finished_stage_prints_nothing_at_all(isolated_config):
    """"print nothing when the operation is completed, it is nicer to see"
    (operator, 2026-09-12).

    Half of it already worked and half was UNREACHABLE. `remaining` was
    computed from raw `in_scope` for all three document stages, while the
    template derived `indexable = in_scope - unindexable` for parsed and
    indexed and used raw `in_scope` for the dense row. So on any
    collection with an unindexable document the dense row's target was
    documents no stage will ever finish, and its slot could never go
    quiet. Live: gutenberg, 79,320 in scope, 33 unindexable.

    One target per stage, computed once, is the fix — "a bar that cannot
    reach its end is not a progress bar" was already the note above the
    parsed row.
    """
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(10):
        # eight finishable documents and two the source cannot give us
        bad = i >= 8
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks,error) "
                    "VALUES(?,'g',?,?,1,?,?,?,1,?)",
                    (f"g:{i}", str(i), f"/x{i}",
                     0 if bad else 1, 0 if bad else 1, 0 if bad else 1,
                     "no txt file" if bad else None))
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {}, "running")

    assert shelf["unindexable"] == 2
    assert shelf["stage_targets"]["rdf"] == 8, "the two are out of the target"
    assert shelf["stage_targets"]["embed"] == 8, \
        "the dense row counts to the same place as the others"
    for stage in ("rdf", "fts", "embed"):
        assert shelf["stage_etas"][stage] is None, \
            f"{stage} is done: nothing at all, not even a dash"

    html = _render_library(shelf)
    assert "(-)" not in html and "TBD" not in html
    assert "8 / 8" in html, "and it reads as complete"


def test_only_blocked_work_left_prints_nothing_either(isolated_config):
    """A blocked document waits for a human, so while the only thing left
    is blocked the stage is as finished as it can be on its own — and a
    dash that never resolves is the noise this change removes. Unlike
    `unindexable` the blocked ones stay IN the target: retrying can fix
    them, so the bar legitimately stops short and the badge says why."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    for i in range(10):
        stuck = i >= 8
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks,error) "
                    "VALUES(?,'g',?,?,1,?,?,?,1,?)",
                    (f"g:{i}", str(i), f"/x{i}",
                     0 if stuck else 1, 0 if stuck else 1, 0 if stuck else 1,
                     "fts: tantivy exploded" if stuck else None))
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    web.invalidate_counts()
    _wait_counts(c)
    shelf = web._collection_shelf(c, {}, "running")

    assert shelf["blocked"] == 2
    assert shelf["stage_targets"]["fts"] == 10, \
        "blocked documents stay in the target — retrying can fix them"
    assert shelf["stage_etas"]["fts"] is None, \
        "but there is no work the stage can take, so no placeholder"
    html = _render_library(shelf)
    assert "(-)" not in html


def test_a_card_says_how_old_its_counts_are_and_how_often_they_move(isolated_config):
    """pubmed's card moved in ~25-minute jumps (its count is recounted at
    most 10 % of the time, and one count takes minutes) under a page note
    that said "cached 2 min": the operator read it as stuck (2026-10-13)."""
    c = collections_mod.load_all()[0]
    _wait_counts(c)
    shelf = dict(web._collection_shelf(c, {}))
    assert shelf["counts_every"] >= web._COUNTS_TTL_S
    shelf.update(counts_age=24 * 60.0, counts_every=25 * 60.0)
    html = _render_library(shelf)
    assert "counted 24 min ago, every 25 min" in html
    assert "cached 2 min" not in html
    fresh = dict(shelf, counts_age=30.0)
    assert "counted" not in _render_library(fresh).split("embedded (dense)")[1][:400]
