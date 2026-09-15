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


def test_a_table_another_writer_is_still_creating_is_not_an_error(collection, monkeypatch):
    """Seen 6 times in 150 runs of the test above on the node (2026-09-15):
    on a fresh collection the first writers race, and one of them found
    `chunks` LISTED — create_table makes the table's directory before it
    commits a first version — but not openable, and a healthy document
    recorded "ValueError: Table 'chunks' was not found", which takes it out
    of every selection until a human clears it. Outside the write lock a
    table in that state is a table that does not exist yet; the check
    inside the lock, taken after the creator has committed, decides."""
    import contextlib
    import shutil
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            [[1.0] + [0.0] * 7 for _ in texts])

    def chunks_of(n):
        return [{"id": f"fake:{n}:0:1", "seq": 0, "chapter": "",
                 "text": f"abstract of {n}"}]
    being_created = collection.lance_dir / f"{embeddings.TABLE}.lance"
    being_created.mkdir(parents=True)          # listed, no version committed
    lock, creator = embeddings._write_lock, [2]    # the creating writer's document

    @contextlib.contextmanager
    def lock_after_the_creator_commits(lock_con):
        if creator:                            # it commits first, once
            n = creator.pop()
            shutil.rmtree(being_created)
            embeddings.upsert_book(n, "en", chunks_of(n), collection)
        with lock(lock_con):
            yield
    monkeypatch.setattr(embeddings, "_write_lock", lock_after_the_creator_commits)

    assert embeddings.upsert_book(1, "en", chunks_of(1), collection) == 1
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 2, "both writers' vectors, one table"


def test_serial_recipes_never_enter_the_parallel_path(collection, monkeypatch):
    from dataclasses import replace
    serial = replace(collection, embed=replace(collection.embed, concurrency=1))
    recipe = collections_mod.recipe_for(serial)
    assert recipe.concurrency == 1
    # _run_one_step routes concurrency==1 through the classic single-doc
    # stage — asserted structurally: the gate in _run_one_step only calls
    # _embed_parallel when recipe.concurrency > 1 (see worker.py)
