"""Genesis import — published vector archives admitted as same-function
compatible data (architecture-v2.md §8, amendment 10). Admission, not trust:
possession-scoped, chunk-shape-matched, sample re-encoded against the
collection's own encoder, refused whole on failure, cursor-resumable.
"""
import json

import numpy as np
import pytest

from app import collections as collections_mod, config, db, embeddings, fts
from app.bundles import genesis
from app.bundles.genesis import GenesisArchive, GenesisError, GenesisUnavailable

DIM = 8


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


class FakePlugin:
    """Protocol-only plugin: one section per doc, text derived from the id."""
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
            sections=[("Abstract", f"abstract text of document {nid}")],
            raw_hash="h")


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://fake-encoder:1", api="openai", model="fake-encoder",
            dtype="f16", ctx=512, dim=DIM))
    con = db.connect(c)
    for i in range(1, 21):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    return c


def _archive_vector(i: int) -> np.ndarray:
    """What the archive holds — by construction identical to what the 'local
    encoder' fake produces for the doc's text (same function)."""
    return _unit(f"abstract text of document {i}")


def _write_medcpt_shard(cache: dict, shard: str, ids, scale=1.0):
    vectors = np.stack([_archive_vector(i) * scale for i in ids])
    cache[f"embeds_chunk_{shard}.npy"] = vectors
    cache[f"pmids_chunk_{shard}.json"] = [str(i) for i in ids]


def _fake_fetch(cache: dict):
    def fetch(name, dest):
        if name not in cache:
            raise GenesisUnavailable(f"archive missing {name}")
        value = cache[name]
        if name.endswith(".npy"):
            np.save(dest.with_suffix(""), value)   # np.save appends .npy
        else:
            dest.write_text(json.dumps(value), encoding="utf-8")
    return fetch


def _same_function_embed(texts, **kwargs):
    return [_unit(t).tolist() for t in texts]


def _different_model_embed(texts, **kwargs):
    return [_unit("other-model:" + t).tolist() for t in texts]


def _archive(shards):
    return GenesisArchive(archive_id="test-arc", base_url="http://arc",
                           adapter="medcpt-npy", shards=tuple(shards),
                           uid_prefix="fake:")


def test_import_applies_held_docs_and_is_resumable(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    _write_medcpt_shard(cache, "1", list(range(11, 16)) + [999])   # 999 not held

    archive = _archive(["0", "1"])
    result = genesis.import_archive(con, collection, archive,
                                     embed_fn=_same_function_embed,
                                     fetch=_fake_fetch(cache))
    assert result["docs"] == 15 and result["chunks"] == 15
    shard1 = [r for r in result["shards"] if r["shard"] == "1"][0]
    assert shard1["skipped_absent"] == 1

    # vectors landed, f16, unit-norm; embed_done set
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 15
    import pyarrow as pa
    assert pa.types.is_float16(tbl.schema.field("vector").type.value_type)
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 15

    # resume: both shards short-circuit, nothing re-fetched
    def exploding_fetch(name, dest):
        raise AssertionError("applied shard must not re-fetch")
    again = genesis.import_archive(con, collection, archive,
                                    embed_fn=_same_function_embed,
                                    fetch=exploding_fetch)
    assert all(r["state"] == "already-applied" for r in again["shards"])


def test_incompatible_archive_is_refused_whole(collection):
    """The same-function proof: a well-formed archive from a DIFFERENT model
    must not land a single row."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    with pytest.raises(GenesisError, match="NOT produced by this"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_different_model_embed,
                              fetch=_fake_fetch(cache))
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0
    state = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert state["state"] == "rejected" and "cos" in state["detail"]


def test_endpoint_down_is_retryable_not_a_verdict(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))

    def down(texts, **kwargs):
        raise ConnectionError("no encoder")
    with pytest.raises(GenesisUnavailable):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=down, fetch=_fake_fetch(cache))
    assert con.execute("SELECT state FROM genesis_imports").fetchone()["state"] == "skipped"

    # the same shard imports cleanly once the encoder is back
    result = genesis.import_shard(con, collection, _archive(["0"]), "0",
                                   embed_fn=_same_function_embed,
                                   fetch=_fake_fetch(cache))
    assert result["state"] == "applied" and result["docs"] == 10


def test_zero_or_nonfinite_vectors_reject_the_shard(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 6), scale=0.0)   # all-zero vectors
    with pytest.raises(GenesisError, match="zero"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_same_function_embed,
                              fetch=_fake_fetch(cache))


def test_dim_mismatch_rejects(collection):
    con = db.connect(collection)
    vectors = np.stack([np.ones(4, dtype=np.float32) for _ in range(3)])
    cache = {"embeds_chunk_0.npy": vectors,
             "pmids_chunk_0.json": ["1", "2", "3"]}
    with pytest.raises(GenesisError, match="dim"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_same_function_embed,
                              fetch=_fake_fetch(cache))


def test_multi_chunk_documents_are_skipped_not_half_embedded(collection, monkeypatch):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 6))

    plugin = collections_mod.PLUGIN_REGISTRY["fake"]
    original_parse = plugin.parse

    def two_chunk_parse(raw):
        doc = original_parse(raw)
        if doc.uid == "fake:3":
            from dataclasses import replace
            return replace(doc, sections=[("A", "x " * 3000), ("B", "y " * 3000)])
        return doc
    monkeypatch.setattr(plugin, "parse", two_chunk_parse)

    result = genesis.import_shard(con, collection, _archive(["0"]), "0",
                                   embed_fn=_same_function_embed,
                                   fetch=_fake_fetch(cache))
    assert result["docs"] == 4 and result["skipped_shape"] == 1
    row = con.execute("SELECT embed_done FROM documents WHERE uid='fake:3'").fetchone()
    assert row["embed_done"] == 0, "half-covered documents must stay unembedded"
