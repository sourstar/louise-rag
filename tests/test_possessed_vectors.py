"""A record held in an accepted bundle FILE is not embedded again (RULE 1).

Perugia, 2026-10-23: since its resume its embed stage embedded 12,921
chunks of gutenberg that the node's predecessor bundles — held there as
files, counted as covered by possession — already carried, and published
them as 145 new bundles: the stage asked only its vector table. Now the
vectors of an accepted bundle holding the record AS IT IS NOW are carried
(app/share/possession.py `held_vectors`, `upsert_book(given=)`)."""
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import collections as collections_mod, db, discovery, embeddings, worker
from app.share import contribute, possession


@pytest.fixture
def collection(tmp_path, isolated_config, corpus_dir):
    c = collections_mod.Collection(
        name="books", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=1, embed_done=0")
    con.commit()
    con.close()
    return c


def _multi_chunk(con, collection):
    """A document that cuts to at least two chunks, whatever order discovery
    found the fixture corpus in (a Windows bind mount lists 10002 — one
    chunk — first; München's gate, 2026-10-23)."""
    for row in con.execute("SELECT * FROM documents ORDER BY uid"):
        chunks = worker._load_chunks(row, collection)
        if len(chunks) >= 2:
            return row, chunks
    raise AssertionError("the fixture corpus holds no document of two chunks")


def _bundle(tmp_path, chunk_ids, vectors, challenge=2):
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "header.json").write_text(json.dumps({"challenge": {"set": "x", "n": challenge}}))
    ids = [f"calibration:{i}" for i in range(challenge)] + list(chunk_ids)
    vecs = [[9.0] * 4] * challenge + [list(v) for v in vectors]
    pq.write_table(pa.table({"chunk_id": ids, "vector": vecs}), d / "vectors.parquet")
    return d


def _possessing(monkeypatch, d, uid, content_hash):
    possession._vectors_cache.clear()
    held = possession.Possession({uid: [content_hash]}, {uid: {content_hash: ["b1"]}},
                                 {"b1": "nodekey"}, {}, {"b1": "lang=en/00001"})
    monkeypatch.setattr(possession, "index", lambda root, name, codex, con: held)
    monkeypatch.setattr(contribute, "bundle_dir", lambda root, name, codex, shard, b, **kw: d)


def test_the_held_vectors_are_the_bundle_s_for_the_record_as_it_is_now(tmp_path, monkeypatch):
    d = _bundle(tmp_path, ["u1:0", "u1:1", "u2:0"], [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
    _possessing(monkeypatch, d, "u1", "h-now")
    got = possession.held_vectors(tmp_path, "books", object(), None, "u1", "h-now", ["u1:0", "u1:1"])
    assert got == {"u1:0": [1.0, 0.0, 0.0, 0.0], "u1:1": [0.0, 1.0, 0.0, 0.0]}, \
        "the record's chunks, the challenge rows never"
    assert possession.held_vectors(tmp_path, "books", object(), None, "u1", "h-old", ["u1:0"]) == {}, \
        "a record that changed since the bundle is embedded"
    assert possession.held_vectors(tmp_path, "books", object(), None, "u9", "h-now", ["u9:0"]) == {}


def test_the_embed_stage_carries_a_possessed_record_and_asks_the_encoder_nothing(collection, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(embeddings, "_embed_batch",
                        lambda batch, timeout, recipe, **kw: asked.extend(batch) or [[0.5] * 4 for _ in batch])
    con = db.connect(collection)
    row, chunks = _multi_chunk(con, collection)
    given = {c["id"]: [float(i), 1.0, 0.0, 0.0] for i, c in enumerate(chunks)}
    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks, collection, given=given,
                           counts=counts)
    assert asked == [] and counts.embedded == 0 and counts.reused == len(chunks)
    stored = embeddings._conn(collection).open_table(embeddings.TABLE).to_arrow()
    assert stored.num_rows == len(chunks), "carried, and WRITTEN: search has them"
    by_id = dict(zip(stored.column("id").to_pylist(), stored.column("vector").to_pylist()))
    assert [round(by_id[c["id"]][0]) for c in chunks] == list(range(len(chunks)))


def test_a_chunk_the_bundle_lacks_is_still_embedded(collection, monkeypatch):
    asked = []
    monkeypatch.setattr(embeddings, "_embed_batch",
                        lambda batch, timeout, recipe, **kw: asked.extend(batch) or [[0.5] * 4 for _ in batch])
    con = db.connect(collection)
    row, chunks = _multi_chunk(con, collection)
    given = {chunks[0]["id"]: [1.0, 0.0, 0.0, 0.0]}
    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks, collection, given=given,
                           counts=counts)
    assert counts.reused == 1 and counts.embedded == len(chunks) - 1


def test_the_worker_asks_possession_with_the_row_s_content_hash(collection, monkeypatch):
    seen = {}

    def held_vectors(root, name, codex, con, uid, content_hash, chunk_ids):
        seen.update(uid=uid, h=content_hash, n=len(chunk_ids))
        return {"x": [1.0]}
    monkeypatch.setattr(possession, "held_vectors", held_vectors)
    con = db.connect(collection)
    con.execute("UPDATE documents SET content_hash='h1'")
    row = con.execute("SELECT * FROM documents ORDER BY rowid LIMIT 1").fetchone()
    c = collection
    monkeypatch.setattr(type(c), "codex", property(lambda self: object()), raising=False)
    got = worker._possessed_vectors(con, c, row, [{"id": "a"}, {"id": "b"}])
    assert got == {"x": [1.0]} and seen == {"uid": row["uid"], "h": "h1", "n": 2}
    con.execute("UPDATE documents SET content_hash=NULL")
    row = con.execute("SELECT * FROM documents ORDER BY rowid LIMIT 1").fetchone()
    assert worker._possessed_vectors(con, c, row, [{"id": "a"}]) == {}, "no hash, no claim: embedded"
