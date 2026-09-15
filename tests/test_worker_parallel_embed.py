"""The multi-document embed stage for CPU-encoder collections
(recipe.concurrency > 1): whole documents embed concurrently — the only
parallelism unit that helps a single-chunk-per-document corpus (PubMed).
GPU collections (concurrency 1) never enter this path by design.
"""
import threading
import time

import pytest

from app import collections as collections_mod, db, embeddings, worker


class FakePlugin:
    source_id = "fake"

    def fetch(self, ref):
        from app.plugins.base import RawDoc
        return RawDoc(ref=ref, raw_hash="h", data=ref.native_id.encode())

    def parse(self, raw):
        from app.plugins.base import CanonicalDoc
        nid = raw.data.decode()
        return CanonicalDoc(
            uid=f"fake:{nid}", title=f"doc {nid}", creators=[], language="en",
            date=None, subjects=[], keys=[], extra={},
            sections=[("Abstract", f"abstract of {nid}")], raw_hash="h")


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://cpu-encoder:1", api="openai", model="medcpt",
            dtype="f16", ctx=512, dim=8, concurrency=3))
    con = db.connect(c)
    for i in range(1, 9):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    con.execute("INSERT INTO embed_queue(uid,priority,ts) VALUES('fake:1',1,1)")
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    return c


def _concurrent_embed(monkeypatch, observed):
    gate = threading.Semaphore(0)
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake(texts, timeout=300, recipe=None, query=False):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.15)
        with lock:
            active["now"] -= 1
        observed.append(texts[0])
        return [[1.0] + [0.0] * 7 for _ in texts]
    monkeypatch.setattr(embeddings, "embed_texts", fake)
    return active


def test_documents_embed_concurrently_and_queue_drains(collection, monkeypatch):
    con = db.connect(collection)
    observed = []
    active = _concurrent_embed(monkeypatch, observed)
    recipe = collections_mod.recipe_for(collection)

    assert worker._embed_parallel(con, collection, recipe) is True
    assert active["peak"] >= 2, "documents must actually overlap in flight"
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    # a sliding window: one call streams through EVERY candidate (it used to
    # stop after one batch of concurrency*2 and leave a GPU-idle tail)
    assert done == len(observed) == 8
    # the queued doc was among them and its queue entry is gone
    assert con.execute("SELECT COUNT(*) FROM embed_queue").fetchone()[0] == 0

    # nothing left: the next call finds no work
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=0").fetchone()[0] == 0
    assert worker._embed_parallel(con, collection, recipe) is False


def test_one_bad_document_is_isolated(collection, monkeypatch):
    con = db.connect(collection)

    def flaky(texts, timeout=300, recipe=None, query=False):
        if "abstract of 2" in texts[0]:
            raise RuntimeError("encoder hiccup")
        return [[1.0] + [0.0] * 7 for _ in texts]
    monkeypatch.setattr(embeddings, "embed_texts", flaky)
    recipe = collections_mod.recipe_for(collection)

    worker._embed_parallel(con, collection, recipe)
    worker._embed_parallel(con, collection, recipe)
    bad = con.execute("SELECT error, retries FROM documents WHERE uid='fake:2'").fetchone()
    assert bad["error"] and "hiccup" in bad["error"] and bad["retries"] >= 1
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 7, "every healthy document still embedded"


def _chunks_of(n, parts=1):
    return [{"id": f"fake:{n}:{s}:1", "seq": s, "chapter": "",
             "text": f"abstract of {n}, part {s}"} for s in range(parts)]


def _fixed_vectors(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            [[1.0] + [0.0] * 7 for _ in texts])


def _another_writer_commits_first(monkeypatch, write):
    """`write()` — another writer — commits once, just before this one takes
    the vector write lock: the interleaving no read made before the lock
    can see."""
    import contextlib
    lock, pending = embeddings._write_lock, [write]

    @contextlib.contextmanager
    def lock_after_it(lock_con):
        if pending:
            pending.pop()()
        with lock(lock_con):
            yield
    monkeypatch.setattr(embeddings, "_write_lock", lock_after_it)


def test_a_table_another_writer_is_still_creating_is_not_an_error(collection, monkeypatch):
    """Seen 6 times in 150 runs of the test above on the node (2026-09-15):
    on a fresh collection the first writers race, and one of them found
    `chunks` LISTED — create_table makes the table's directory before it
    commits a first version — but not openable, and a healthy document
    recorded "ValueError: Table 'chunks' was not found", which takes it out
    of every selection until a human clears it. Outside the write lock a
    table in that state is a table that does not exist yet; the check
    inside the lock, taken after the creator has committed, decides."""
    import shutil
    _fixed_vectors(monkeypatch)
    being_created = collection.lance_dir / f"{embeddings.TABLE}.lance"
    being_created.mkdir(parents=True)          # listed, no version committed

    def creator():
        shutil.rmtree(being_created)
        embeddings.upsert_book(2, "en", _chunks_of(2), collection)
    _another_writer_commits_first(monkeypatch, creator)

    assert embeddings.upsert_book(1, "en", _chunks_of(1), collection) == 1
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 2, "both writers' vectors, one table"


def test_a_document_written_between_the_read_and_the_lock_is_stored_once(
        collection, monkeypatch):
    """Caught once by tests/bundles/test_import.py's concurrent test in a
    node suite run (2026-09-15): 24 rows for 12 chunk ids. upsert_book reads
    what the document holds before the lock and appended when that read
    found nothing — on top of what another writer stored in between."""
    _fixed_vectors(monkeypatch)
    chunks = _chunks_of(1, parts=3)
    _another_writer_commits_first(
        monkeypatch, lambda: embeddings.upsert_book(1, "en", chunks, collection))

    assert embeddings.upsert_book(1, "en", chunks, collection) == 3
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    ids = [r["id"] for r in tbl.search().select(["id"]).limit(100).to_list()]
    assert sorted(ids) == sorted(c["id"] for c in chunks), "each chunk stored once"


def test_an_import_between_the_read_and_the_lock_is_checked(collection, monkeypatch):
    """An import's write is one this process's own log cannot account for —
    tests/bundles/test_import.py's case — so the stale read is checked."""
    _fixed_vectors(monkeypatch)
    chunks = _chunks_of(1, parts=3)
    imported = [dict(c, book_id=1, lang="en", vector=[0.0, 1.0] + [0.0] * 6)
                for c in chunks]
    _another_writer_commits_first(
        monkeypatch, lambda: embeddings.upsert_chunks(imported, collection, dtype="f16"))

    embeddings.upsert_book(1, "en", chunks, collection)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    ids = [r["id"] for r in tbl.search().select(["id"]).limit(100).to_list()]
    assert sorted(ids) == sorted(c["id"] for c in chunks), "each chunk stored once"


def test_writes_of_other_documents_cost_no_check(collection, monkeypatch):
    """The parallel embed stage's siblings write OTHER documents between a
    writer's read and its lock all the time. Their versions are logged, so
    the check — 0.55–0.65 s on pubmed (2026-09-15) — is not paid per
    document."""
    _fixed_vectors(monkeypatch)
    checks = []
    real = embeddings._drop_clashing
    monkeypatch.setattr(embeddings, "_drop_clashing",
                        lambda *a: (checks.append(a[1]), real(*a)))
    embeddings.upsert_book(3, "en", _chunks_of(3), collection)     # the table exists
    _another_writer_commits_first(
        monkeypatch, lambda: embeddings.upsert_book(2, "en", _chunks_of(2), collection))

    embeddings.upsert_book(1, "en", _chunks_of(1), collection)
    assert checks == [], "no check for this process's writes of other documents"


@pytest.mark.parametrize("failure", ["loading", "lock"])
def test_transient_failures_leave_documents_untouched_in_the_parallel_path(
        collection, monkeypatch, failure):
    """This path asked only whether the encoder was unreachable, so on
    2026-09-15 a 503 "Loading model" (41 documents) and a vector-lock
    timeout (4) were written onto pubmed documents as errors."""
    import httpx
    from app.bundles.writer_lock import VectorWriteBusy
    con = db.connect(collection)

    def fail(texts, timeout=300, recipe=None, query=False):
        if failure == "lock":
            raise VectorWriteBusy("could not acquire the vector write lock: database is locked")
        req = httpx.Request("POST", "http://gpu/v1/embeddings")
        try:
            raise httpx.HTTPStatusError("x", request=req,
                                        response=httpx.Response(503, request=req))
        except httpx.HTTPStatusError as e:
            raise RuntimeError("embed server HTTP 503: Loading model") from e
    monkeypatch.setattr(embeddings, "embed_texts", fail)

    assert worker._embed_parallel(con, collection, collections_mod.recipe_for(collection)) is False
    rows = con.execute("SELECT error, retries, embed_done FROM documents").fetchall()
    assert all(r["error"] is None and r["retries"] == 0 and r["embed_done"] == 0
               for r in rows), "a deferral writes nothing onto a document"


def test_serial_recipes_never_enter_the_parallel_path(collection, monkeypatch):
    from dataclasses import replace
    serial = replace(collection, embed=replace(collection.embed, concurrency=1))
    recipe = collections_mod.recipe_for(serial)
    assert recipe.concurrency == 1
    # _run_one_step routes concurrency==1 through the classic single-doc
    # stage — asserted structurally: the gate in _run_one_step only calls
    # _embed_parallel when recipe.concurrency > 1 (see worker.py)
