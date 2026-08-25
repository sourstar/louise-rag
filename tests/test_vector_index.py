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
    assert vector_index.stats(collection) == {"rows": 600, "indexed": False}
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
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    out = vector_index.ensure(collection)
    assert out and out["built"] and out["partitions"] == 4
    assert vector_index.stats(collection)["indexed"]


def test_search_falls_back_to_the_flat_scan_without_an_index(collection, monkeypatch):
    vecs = _fill(collection, 50)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [vecs[7].tolist()])
    hits = embeddings.search("anything", k=3, collection=collection)
    assert hits and hits[0]["chunk_id"] == "c7"


def test_the_worker_maintains_the_index_off_the_gpu_loop_and_throttled(collection, monkeypatch):
    from app import db, worker
    _fill(collection, 300)
    con = db.connect(collection)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    worker._vindex_last.clear()
    assert worker._maintain_vector_index(con, collection) is True
    assert vector_index.stats(collection)["indexed"]
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='index'")]
    assert any("vector index built" in m for m in events)
    # within the refresh interval nothing runs again, however many rows arrived
    _fill(collection, 100, seed=2, start=300)
    assert worker._maintain_vector_index(con, collection) is False
    assert vector_index.stats(collection)["unindexed_rows"] == 100
    # past the interval, the new rows are folded in
    worker._vindex_last.clear()
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50)
    assert worker._maintain_vector_index(con, collection) is True
    assert vector_index.stats(collection)["unindexed_rows"] == 0
