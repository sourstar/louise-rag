"""The parallel embed path keeps its worker window FULL.

Measured live on the AMD instance (60 s GPU trace at 1 Hz): the old
batch-and-join form ran ~20 s at 90-100 % then sat ~9 s idle — every batch
ended with a tail (one worker on the longest book) and a full stop while
the main loop turned around; 30 % of the GPU wasted, visibly oscillating.
A sliding window hands a freed worker the next document immediately.
"""
import threading
import time

from app import collections as collections_mod, db, worker


def _seed(con, n):
    for i in range(n):
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks) VALUES(?,?,?,?,?,1,1,1,0,3)",
                    (f"gutenberg:{i}", "gutenberg", str(i), f"/x/{i}", "en"))
    con.commit()


def test_window_stays_full_and_streams_past_the_first_batch(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, 12)
    scope = db.get_scope(con); scope["embed_mode"] = "all"; db.set_scope(con, scope)

    recipe = collections_mod.EmbedRecipe(url="u", api="openai", model="m", dtype="f16",
                                         ctx=512, concurrency=3)
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, col: [{"text": "t" * 10, "tokens": 3}] * 3)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "starts": []}

    def fake_upsert(native_id, lang, chunks, collection, lock_con=None, **kw):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["starts"].append((native_id, time.time()))
        # uneven work, like real books: one straggler per few documents
        time.sleep(0.25 if native_id % 4 == 0 else 0.05)
        with lock:
            state["active"] -= 1
        return len(chunks)
    monkeypatch.setattr(worker.embeddings, "upsert_book", fake_upsert)
    monkeypatch.setattr(worker.config, "EMBED_STREAM_S", 5.0)

    t0 = time.time()
    assert worker._embed_parallel(con, c, recipe) is True
    elapsed = time.time() - t0

    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 12, "one call streams through every candidate, not one batch of 6"
    assert state["max_active"] == 3, "the window is exactly the concurrency"
    # no batch-join tail: a straggler (0.25 s) must not stop the others —
    # total wall time is close to the critical path, far from 4 batches x 0.25
    assert elapsed < 0.25 * 4 + 0.3, f"took {elapsed:.2f}s — batch-and-join tail detected"


def test_a_deferral_stops_refilling_the_window(isolated_config, monkeypatch):
    """A dead encoder must not be walked document by document: once a
    deferral lands, no new document is submitted; in-flight ones drain."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, 10)
    scope = db.get_scope(con); scope["embed_mode"] = "all"; db.set_scope(con, scope)
    recipe = collections_mod.EmbedRecipe(url="u", api="openai", model="m", dtype="f16",
                                         ctx=512, concurrency=2)
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, col: [{"text": "t", "tokens": 1}])
    calls = []

    def dead(native_id, lang, chunks, collection, lock_con=None, **kw):
        calls.append(native_id)
        raise ConnectionError("encoder down")
    monkeypatch.setattr(worker.embeddings, "upsert_book", dead)
    monkeypatch.setattr(worker, "endpoint_unavailable", lambda e: True)

    assert worker._embed_parallel(con, c, recipe) is False
    assert len(calls) <= recipe.concurrency * 2, \
        f"walked {len(calls)} documents against a dead encoder"
    rows = con.execute("SELECT error, retries FROM documents").fetchall()
    assert all(r["error"] is None and r["retries"] == 0 for r in rows), \
        "a deferral leaves every row untouched"


def test_short_documents_are_cut_by_the_parse_pool_and_connections_are_lent(isolated_config, tmp_path,
                                                                              corpus_dir, monkeypatch):
    """The embed threads cut each document under the GIL and opened a
    manifest connection (its ALTERs and schema script) per document: on
    short documents that bound the stage — pubmed 31 documents/s while the
    encoder answered in 5–36 ms. Short candidates are cut by the parse
    pool's children for the whole group; connections are lent, one per
    document in flight. The chunks are the ones the thread would cut."""
    from app import config, discovery, layers, parallel, plugin_io
    d = tmp_path / "g"
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus_dir,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=0, chunks=3 WHERE error IS NULL")
    scope = db.get_scope(con); scope["embed_mode"] = "all"; db.set_scope(con, scope)
    rows = [dict(r) for r in con.execute("SELECT * FROM documents WHERE error IS NULL")]
    want = {int(r["native_id"]): layers.chunks_for_document(c, r, plugin_io.sections(c.plugin_instance(), r))
            for r in rows}

    def never(row, col):
        raise AssertionError("a short document was cut by its embed thread")
    monkeypatch.setattr(worker, "_load_chunks", never)
    got: dict = {}

    def fake_upsert(native_id, lang, chunks, collection, lock_con=None, **kw):
        got[native_id] = chunks
        return len(chunks)
    monkeypatch.setattr(worker.embeddings, "upsert_book", fake_upsert)
    lent = []
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda *a, **k: (lent.append(1) if k.get("shared") else None)
                        or real_connect(*a, **k))
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    parallel.shutdown()
    recipe = collections_mod.EmbedRecipe(url="u", api="openai", model="m", dtype="f16",
                                         ctx=512, concurrency=2)
    try:
        assert worker._embed_parallel(con, c, recipe) is True
    finally:
        parallel.shutdown()
    assert got == want and len(got) == len(rows) > 2
    assert 0 < len(lent) <= 2, "one connection per document in flight, not one per document"
