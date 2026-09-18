"""Measured throughput, and the honesty rules around it.

The page exists to answer one question — is a GPU version of this encoder
worth running — so the numbers have to be comparable across collections and
have to distinguish "not observed" from "stopped". Both are easy to get
wrong in ways that look fine.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, spaces, stats, web
from app.api import app


@pytest.fixture(autouse=True)
def fresh_recorder():
    stats.recorder().reset()
    yield
    stats.recorder().reset()


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


# ---------------- the recorder ----------------

def test_nothing_measured_reports_nothing_not_zero():
    """A zero and an absence look identical on a dashboard and mean opposite
    things: one says the encoder stalled, the other says nobody looked."""
    assert stats.recorder().snapshot() == {}
    assert stats.rates(None) == {}
    assert stats.rates({}) == {}


def test_a_single_sample_is_not_enough_to_publish():
    r = stats.recorder()
    r.record_embed("g", seconds=1.0, docs=1, chunks=10, chars=1000)
    assert r.snapshot() == {}, "one document's luck is not a rate"
    for _ in range(stats.MIN_EVENTS):
        r.record_embed("g", seconds=1.0, docs=1, chunks=10, chars=1000)
    assert "g" in r.snapshot()


def test_encoder_throughput_is_work_over_time_inside_the_call():
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("g", seconds=2.0, docs=1, chunks=5, chars=10_000)
    rates = stats.rates(r.snapshot()["g"])["embed"]

    assert rates["chars_per_s"] == pytest.approx(40_000 / 8.0)   # 5000
    assert rates["chunks_per_s"] == pytest.approx(20 / 8.0)
    assert rates["ms_per_chunk"] == pytest.approx(8000.0 / 20)
    assert rates["docs"] == 4 and rates["chunks"] == 20


def test_duty_separates_a_slow_encoder_from_a_contended_one():
    """The distinction the hardware decision turns on: an encoder at 20% duty
    is idle four fifths of the time, and buying a faster one changes nothing
    until whatever it is queueing behind gets faster."""
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("busy", seconds=0.5, docs=1, chunks=1, chars=100)
        time.sleep(0.01)
    entry = r.snapshot()["busy"]
    # busy time is real work; span is elapsed wall-clock across the samples
    assert entry["embed"]["busy_seconds"] == pytest.approx(2.0)
    assert entry["embed"]["span_seconds"] < entry["embed"]["busy_seconds"], \
        "this fixture reports more work than elapsed time — duty caps at 1 in reality"

    assert stats.rates(entry)["embed"]["duty"] is not None


def test_characters_not_chunks_is_the_cross_collection_unit():
    """Two encoders doing identical work, one with half the context window.
    Chunks/second ranks the small-context one twice as fast; chars/second
    correctly calls them equal."""
    r = stats.recorder()
    for _ in range(4):                       # big ctx: 1 chunk of 8000 chars
        r.record_embed("big", seconds=1.0, docs=1, chunks=1, chars=8000)
        r.record_embed("small", seconds=1.0, docs=1, chunks=2, chars=8000)
    snap = r.snapshot()
    big = stats.rates(snap["big"])["embed"]
    small = stats.rates(snap["small"])["embed"]

    assert big["chars_per_s"] == pytest.approx(small["chars_per_s"])
    assert small["chunks_per_s"] == pytest.approx(big["chunks_per_s"] * 2), \
        "chunks/s really does disagree — which is why it is not the headline"


def test_adopted_files_never_enter_the_fetch_rate():
    """A verify-only pass moves no bytes. Counting it would report an
    infinite download speed and poison every ETA on the page."""
    r = stats.recorder()
    for _ in range(4):
        r.record_fetch("g", seconds=0.0, bytes_moved=0)
    assert r.snapshot() == {}


def test_old_samples_fall_out_of_the_window(monkeypatch):
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("g", seconds=1.0, docs=1, chunks=1, chars=100)
    assert "g" in r.snapshot()

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + stats.MAX_AGE_S + 60)
    assert r.snapshot() == {}, "a two-hour-old rate is not a current rate"


# ---------------- ETA ----------------

@pytest.mark.parametrize("remaining,rate", [
    (100, None),     # never measured
    (100, 0),        # measured as stopped
    (0, 5),          # nothing left
    (-5, 5),         # nonsense input
])
def test_eta_is_none_when_it_cannot_be_known(remaining, rate):
    assert stats.eta_seconds(remaining, rate) is None


def test_eta_divides_remaining_by_rate():
    assert stats.eta_seconds(1000, 10) == pytest.approx(100.0)


def test_etas_are_coarse_and_absent_when_unknown():
    """An ETA from a rolling sample is accurate to a factor, not a minute —
    and an unknowable one is NOTHING on the card, never a placeholder
    (the Stats page is gone; the Library bars carry these)."""
    assert web._human_eta(None) is None
    assert web._human_eta(0) is None
    assert web._human_eta(45) == "~45s"
    assert web._human_eta(600) == "~10min"
    assert web._human_eta(3600 * 5) == "~5h"
    assert web._human_eta(3600 * 24 * 10) == "~10d"
    assert web._human_eta(3600 * 24 * 900).endswith("y")


def test_number_grouping_uses_dots():
    """"+377,217/h" was read as 377.217/h (comma = decimal separator to
    half the world) and computed into an 11-year ETA from a 4-day one."""
    assert web.TEMPLATES.env.filters["num"](4020578) == "4.020.578"
    assert web.TEMPLATES.env.filters["num"](None) == ""


def test_the_stats_page_is_gone(client):
    assert client.get("/admin/stats").status_code == 404
    assert 'href="/admin/stats"' not in client.get("/admin/library").text


def test_stale_measurements_are_dropped(client, isolated_config):
    import json
    con = db.connect()
    old = time.time() - (db.WORKER_STATS_MAX_AGE_S + 60)
    con.execute("INSERT OR REPLACE INTO control(key,value) VALUES(?,?)",
                (db.WORKER_STATS_KEY,
                 json.dumps({"at": old, "payload": {"gutenberg": {}}})))
    payload, age = db.read_worker_stats(con)
    assert payload is None
    assert age > db.WORKER_STATS_MAX_AGE_S


# ---------------- "0 chunks left" must not mean two different things ------

def test_unindexed_documents_are_reported_as_uncounted_not_as_zero(
        client, isolated_config):
    """Observed live: genome-dm6 showed "0 chunks left" with 1,051 contigs
    still to index. `documents.chunks` is written by the fts stage, so a
    document that has not reached it contributes an UNKNOWN amount of work,
    not zero — and zero reads as "nothing to do"."""
    from app import collections as collections_mod
    col = collections_mod.default_collection()
    con = db.connect(col)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,fts_done,embed_done,chunks) VALUES"
                "('g:1','g','1','/x',1,1,0,0,0)")          # awaiting fts
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,fts_done,embed_done,chunks) VALUES"
                "('g:2','g','2','/x',1,1,1,0,40)")         # counted: 40 chunks
    web.invalidate_counts()

    counts, _ = web._document_counts(col)
    assert counts["chunks_pending"] == 40
    assert counts["docs_unindexed"] == 1


def test_blocked_documents_are_named_rather_than_silently_dropped(
        client, isolated_config):
    """genome-sacCer3 showed 0 chunks left with 3,459 chunks known: every
    document carried an error, and embed_ready requires error IS NULL."""
    from app import collections as collections_mod
    col = collections_mod.default_collection()
    con = db.connect(col)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,fts_done,embed_done,chunks,error) VALUES"
                "('g:3','g','3','/x',1,1,1,0,99,'embed: FileNotFoundError')")
    web.invalidate_counts()

    counts, _ = web._document_counts(col)
    assert counts["chunks_pending"] == 0, "blocked work is not pending work"
    assert counts["blocked"] == 1, "but it must still be visible"



# ---------------- the counts cache ----------------

def test_counts_are_cached_per_manifest_not_per_name(isolated_config, tmp_path):
    """Two Collections can share a name and point at different files — the
    same trap fts.get_index() documents. A name-keyed cache would serve one
    collection's numbers for another."""
    from app import collections as collections_mod
    a = collections_mod.Collection(
        name="same", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "a.db",
        fts_dir=tmp_path / "fa", lance_dir=tmp_path / "la")
    b = collections_mod.Collection(
        name="same", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "b.db",
        fts_dir=tmp_path / "fb", lance_dir=tmp_path / "lb")
    assert web._counts_key(a) != web._counts_key(b)

    db.connect(a).execute("INSERT INTO documents(uid,source,native_id,path,"
                          "in_scope) VALUES('x','g','1','/p',1)")
    db.connect(b)
    web.invalidate_counts()
    assert web._document_counts(a)[0]["total"] == 1
    assert web._document_counts(b)[0]["total"] == 0, "must not reuse a's entry"


def test_a_cached_value_is_served_without_rescanning(isolated_config, monkeypatch):
    """The whole point: pubmed's scan is 12 seconds, and it must not run on
    every page load."""
    from app import collections as collections_mod
    col = collections_mod.default_collection()
    web.invalidate_counts()

    calls = []
    real = web._document_counts_query
    monkeypatch.setattr(web, "_document_counts_query",
                        lambda c: (calls.append(c.name), real(c))[1])

    web._document_counts(col)
    web._document_counts(col)
    web._document_counts(col)
    assert len(calls) == 1, "cold once, cached thereafter"


def test_a_reset_invalidates_the_cache(client, isolated_config):
    """A reset is exactly when an operator wants to see change; a cached
    count from before their own click reads as a no-op."""
    from app import collections as collections_mod
    col = collections_mod.default_collection()
    con = db.connect(col)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "rdf_done,error,retries) VALUES('g:9','g','9','/x',1,1,'rdf: boom',9)")
    web.invalidate_counts()
    assert web._document_counts(col)[0]["blocked"] == 1

    r = client.post(f"/admin/library/{col.name}/reset", json={"stage": "errors"})
    assert r.status_code == 200 and r.json()["background"], \
        "the full clear runs in the background: on pubmed it reads 40M rows"
    import time
    deadline = time.monotonic() + 10
    while web._document_counts(col)[0]["blocked"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert web._document_counts(col)[0]["blocked"] == 0, "cache must not hide the reset"


def test_the_parallel_embed_path_is_measured_too(isolated_config, tmp_path,
                                                  monkeypatch):
    """Every CPU encoder in the deployment goes through _embed_parallel
    (recipe.concurrency > 1) and only the GPU one goes through stage_embed.
    Recording in stage_embed alone left the Stats page permanently blank for
    exactly the encoders it exists to compare — found live: DNABERT-2 was at
    312% CPU with the page showing "—"."""
    from app import collections as collections_mod, worker

    col = collections_mod.default_collection()
    recipe = collections_mod.EmbedRecipe(
        url="http://x", api="openai", model="m", dtype="f16", ctx=512,
        concurrency=2)
    con = db.connect(col)
    db.set_scope(con, {"embed_mode": "all"})
    # four, not two: the recorder withholds below MIN_EVENTS samples, which
    # is its own honesty rule and not something this test should defeat
    for i in range(4):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    f"VALUES('g:{i}','g','{i}','/x','en',1,1,1,0,3)")

    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, c: [{"text": "x" * 100, "tokens": 25}] * 3)
    monkeypatch.setattr(worker.embeddings, "upsert_book",
                        lambda *a, **k: 3)
    stats.recorder().reset()

    assert worker._embed_parallel(con, col, recipe) is True
    snapshot = stats.recorder().snapshot()
    assert col.name in snapshot, "parallel embeds must be measured"
    assert snapshot[col.name]["embed"]["totals"]["docs"] == 4
    assert snapshot[col.name]["embed"]["totals"]["chunks"] == 12
    assert snapshot[col.name]["embed"]["totals"]["chars"] == 1200


def test_a_parallel_batch_announces_itself(isolated_config, monkeypatch):
    """A document commits only when all its chunks are done, so a 200-chunk
    chromosome is ~16 minutes of silence. "Slow" and "stuck" are otherwise
    indistinguishable from the log."""
    from app import collections as collections_mod, worker

    col = collections_mod.default_collection()
    recipe = collections_mod.EmbedRecipe(
        url="http://x", api="openai", model="m", dtype="f16", ctx=512,
        concurrency=2)
    con = db.connect(col)
    db.set_scope(con, {"embed_mode": "all"})
    con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
                "rdf_done,fts_done,embed_done,chunks) "
                "VALUES('g:0','g','0','/x','en',1,1,1,0,200)")
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, c: [{"text": "x", "tokens": 1}])
    monkeypatch.setattr(worker.embeddings, "upsert_book", lambda *a, **k: 200)

    worker._embed_parallel(con, col, recipe)
    msgs = [r["message"] for r in
            con.execute("SELECT message FROM events WHERE stage='embed'")]
    assert any("200 chunks" in m for m in msgs), msgs


def test_documents_inside_the_encoder_are_not_reported_as_waiting(
        client, isolated_config):
    """Observed live: genome-sacCer3's card said "17 documents awaiting the
    encoder" while DNABERT-2 was at 312% CPU working on four of them. A
    document commits only when all its chunks are done, so on a slow encoder
    several are in flight for many minutes — and calling those "awaiting"
    tells an operator nothing is happening."""
    from app import collections as collections_mod
    col = collections_mod.default_collection()
    con = db.connect(col)
    db.set_scope(con, {"embed_mode": "all"})
    for i in range(17):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) "
                    f"VALUES('g:{i}','g','{i}','/x','en',1,1,1,0,200)")
    web.invalidate_counts()

    shelf = web._collection_shelf(col, {})
    assert shelf["embed_ready"] == 17
    assert shelf["embed_in_flight"] == 0
    assert shelf["embed_waiting"] == 17

    shelf = web._collection_shelf(col, {col.name: {"in_flight": 4}})
    assert shelf["embed_in_flight"] == 4
    assert shelf["embed_waiting"] == 13, "in-flight documents are not waiting"

    # and it can never claim more are encoding than are pending
    shelf = web._collection_shelf(col, {col.name: {"in_flight": 999}})
    assert shelf["embed_in_flight"] == 17
    assert shelf["embed_waiting"] == 0


def test_the_recorder_reports_in_flight_before_any_document_finishes():
    """The whole point: on a slow encoder nothing completes for many
    minutes, so in-flight must be visible without a single sample."""
    r = stats.recorder()
    r.set_in_flight("g", 4)
    snap = r.snapshot()
    assert snap["g"]["in_flight"] == 4
    assert "embed" not in snap["g"], "no completed work yet, and none claimed"
    assert stats.rates(snap["g"])["in_flight"] == 4

    r.set_in_flight("g", 0)
    assert r.snapshot() == {}


def test_telemetry_is_published_by_a_thread_the_work_cannot_block(
        isolated_config, monkeypatch):
    """The main loop blocks inside _embed_parallel for the whole batch — a
    quarter of an hour on a CPU encoder. Publishing from there would make
    in-flight work visible only once it stopped being in flight."""
    from app import worker

    stats.recorder().set_in_flight("gutenberg", 3)
    monkeypatch.setattr(worker, "STATS_PUBLISH_INTERVAL_S", 0.05)

    t = threading.Thread(target=worker._stats_publisher, daemon=True)
    t.start()
    deadline = time.time() + 5
    payload = None
    while time.time() < deadline:
        payload, _ = db.read_worker_stats(db.connect())
        if payload:
            break
        time.sleep(0.05)
    worker._stop.set()
    t.join(timeout=5)
    worker._stop.clear()

    assert payload, "the publisher must run without the main loop"
    assert payload["gutenberg"]["in_flight"] == 3
