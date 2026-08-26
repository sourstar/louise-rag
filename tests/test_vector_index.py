"""The local vector index (pack-format.md §5, appendix A): built once a
table is big enough, probed by search when present, refreshed as rows
arrive, and never a wrong answer — the probe returns the flat scan's
neighbours on this data."""
import numpy as np
import pyarrow as pa
import pytest

from app import collections as collections_mod, config, embeddings, vector_index

DIM = 8


@pytest.fixture
def collection(isolated_config, tmp_path):
    return collections_mod.Collection(
        name="vx", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


def _fill(collection, n: int, seed: int = 0, start: int = 0):
    rng = np.random.default_rng(seed)
    vecs = rng.normal(size=(n, DIM)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    rows = [{"id": f"c{start + i}", "book_id": start + i, "lang": "en", "seq": 0, "chapter": "",
             "text": f"t{start + i}", "vector": vecs[i].astype(np.float16)} for i in range(n)]
    conn = embeddings._conn(collection)
    if embeddings.TABLE in conn.table_names():
        conn.open_table(embeddings.TABLE).add(rows)
    else:
        schema = pa.schema([
            pa.field("id", pa.string()), pa.field("book_id", pa.int64()), pa.field("lang", pa.string()),
            pa.field("seq", pa.int64()), pa.field("chapter", pa.string()), pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float16(), DIM))])
        conn.create_table(embeddings.TABLE, data=rows, schema=schema)
    return vecs


def _flat_top(collection, q, k=5):
    """The exact answer: a true flat scan, index or not (a plain search on
    an indexed table would silently use the index, unrefined)."""
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    return [r["id"] for r in tbl.search(q).bypass_vector_index().limit(k).to_list()]


@pytest.mark.parametrize("kind", ["ivf_flat", "ivf_pq"])
def test_build_probe_and_refresh(collection, monkeypatch, kind):
    vecs = _fill(collection, 600)
    assert vector_index.stats(collection) == {"rows": 600, "indexed": False,
                                              "book_id_indexed": False}
    assert not vector_index.has_index(collection)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", kind)
    out = vector_index.build(collection, partitions=4)
    assert out["built"] and out["type"] == kind and out["partitions"] == 4
    st = vector_index.stats(collection)
    assert st["indexed"] and st["rows"] == 600 and st["unindexed_rows"] == 0
    assert vector_index.has_index(collection)
    # the probe finds what the flat scan finds (the query IS a stored vector)
    monkeypatch.setattr(config, "VECTOR_INDEX_NPROBES", 4)
    # PQ over 8 random dims is coarse (one 8-bit code per vector), so the
    # re-rank is given the whole table here: 120 x 5 = 600 candidates.
    # What the test pins is the mechanism — probe + re-rank reproduce the
    # exact neighbours — not PQ's accuracy on toy data (appendix A has that)
    monkeypatch.setattr(config, "VECTOR_INDEX_REFINE", 120)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    for i in (3, 250, 599):
        probed = [r["id"] for r in vector_index.query(tbl.search(vecs[i]), collection).limit(5).to_list()]
        assert probed[0] == f"c{i}"
        assert set(probed) == set(_flat_top(collection, vecs[i]))
    # rows added after the build are unindexed until a refresh folds them in
    _fill(collection, 100, seed=1, start=600)
    st = vector_index.stats(collection)
    assert st["rows"] == 700 and st["unindexed_rows"] == 100
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50)
    did = vector_index.ensure(collection)
    assert did and did.get("refreshed")
    assert vector_index.stats(collection)["unindexed_rows"] == 0


def test_ensure_builds_only_past_the_row_threshold(collection, monkeypatch):
    _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 1000)
    assert vector_index.ensure(collection) is None
    assert not vector_index.has_index(collection)
    assert not vector_index.stats(collection)["book_id_indexed"]
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    first = vector_index.ensure(collection)             # book_id first (cheap)
    assert first and first["kind"] == "btree"
    out = vector_index.ensure(collection)               # then the vector index
    assert out and out["built"] and out["partitions"] == 4
    assert vector_index.stats(collection)["indexed"]


def test_search_falls_back_to_the_flat_scan_without_an_index(collection, monkeypatch):
    vecs = _fill(collection, 50)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [vecs[7].tolist()])
    hits = embeddings.search("anything", k=3, collection=collection)
    assert hits and hits[0]["chunk_id"] == "c7"


def test_the_book_id_index_is_built_first_and_writes_still_agree(collection, monkeypatch):
    """Regression (2026-08-28, measured): every vector write is
    delete-then-insert keyed on book_id, and without an index the
    predicate scanned the whole table — 0.90 s per 500-document genesis
    flush on wikivec's 35M rows, about a fifth of an archive pass. The
    maintainer builds the book_id index BEFORE the vector index, and the
    filtered reads/writes return exactly what they did."""
    vecs = _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    st = vector_index.stats(collection)
    assert st["indexed"] is False and not st.get("book_id_indexed")
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    before = [r["id"] for r in tbl.search().where("book_id IN (7,9)").limit(10).to_list()]

    first = vector_index.ensure(collection)
    assert first["built"] and first["kind"] == "btree" and first["column"] == "book_id"
    assert vector_index.stats(collection)["book_id_indexed"] is True
    assert not vector_index.stats(collection)["indexed"], "the vector index is the NEXT round"
    # the same rows, through the index
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert [r["id"] for r in tbl.search().where("book_id IN (7,9)").limit(10).to_list()] == before
    # the next round builds the vector index, and a third does neither
    second = vector_index.ensure(collection)
    assert second["built"] and second["type"] == "ivf_flat"
    assert vector_index.ensure(collection) is None
    # writes still delete-then-insert correctly under the index
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [vecs[0].tolist()])
    embeddings.upsert_book(7, "en", [{"id": "c7", "seq": 0, "chapter": "", "text": "t7"}], collection)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    rows = [r for r in tbl.search().where("book_id = 7").limit(10).to_list()]
    assert len(rows) == 1 and rows[0]["id"] == "c7"


def test_refresh_is_due_by_rows_and_by_fraction(monkeypatch):
    """Regression (2026-08-27): a refresh compacted the 16.8M-row wikivec
    table for 41 minutes after 115k new rows. It is due at 50k rows AND
    2 % of the table — never every 50k rows on a table that size."""
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50_000)
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_FRACTION", 0.02)
    assert not vector_index.refresh_due({"indexed": True, "rows": 16_800_000, "unindexed_rows": 115_000})
    assert vector_index.refresh_due({"indexed": True, "rows": 16_800_000, "unindexed_rows": 340_000})
    assert vector_index.refresh_due({"indexed": True, "rows": 300_000, "unindexed_rows": 50_000})
    assert not vector_index.refresh_due({"indexed": True, "rows": 300_000, "unindexed_rows": 49_999})
    assert not vector_index.refresh_due({"indexed": False, "rows": 10, "unindexed_rows": None})
    assert not vector_index.refresh_due(None)


def test_maintenance_marks_the_collection_busy_and_the_writers_stand_down(collection, monkeypatch):
    """Regression (2026-08-27): index maintenance ran on the pipeline
    thread and idled it. It now runs on its own thread; while it holds a
    collection, the worker's vector writers (embed step, archive pass)
    skip that collection — rdf/fts continue — instead of racing it."""
    from app import worker
    _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    seen = []
    real_build = vector_index.build
    monkeypatch.setattr(vector_index, "build",
                        lambda c, **k: (seen.append(vector_index.busy(c)), real_build(c, **k))[1])
    assert worker._vector_writes_allowed(collection)
    assert vector_index.ensure(collection)["kind"] == "btree"   # book_id index first
    assert vector_index.ensure(collection)["built"]
    assert seen == [True], "busy for the duration of the build"
    assert worker._vector_writes_allowed(collection), "released afterwards"
    with vector_index.maintenance(collection):
        assert not worker._vector_writes_allowed(collection)
        # the archive pass and the embed step defer for a busy collection
        from dataclasses import replace
        assert worker._drain_remote_archive(None, replace(collection, genesis_archive="medcpt-pubmed"), None) is False
    assert worker._vector_writes_allowed(collection)


def test_the_worker_maintains_the_index_off_the_gpu_loop_and_throttled(collection, monkeypatch):
    from app import db, worker
    _fill(collection, 300)
    con = db.connect(collection)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    worker._vindex_last.clear()
    assert worker._maintain_vector_index(con, collection) is True      # book_id index
    worker._vindex_last.clear()
    assert worker._maintain_vector_index(con, collection) is True      # vector index
    assert vector_index.stats(collection)["indexed"]
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='index'")]
    assert any("vector index built" in m for m in events)
    assert any("book_id index built" in m for m in events)
    # within the refresh interval nothing runs again, however many rows arrived
    _fill(collection, 100, seed=2, start=300)
    assert worker._maintain_vector_index(con, collection) is False
    assert vector_index.stats(collection)["unindexed_rows"] == 100
    # past the interval, the new rows are folded in
    worker._vindex_last.clear()
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50)
    assert worker._maintain_vector_index(con, collection) is True
    assert vector_index.stats(collection)["unindexed_rows"] == 0
