"""Genesis-imported vectors must not claim local provenance.

The Ask drawer reads absence-of-a-verification-row as "local ✓" — this
node computed it. Genesis wrote no rows, so every NCBI/Upstash vector
badged itself local: a false claim, and provenance is exactly what
quarantine and §6's proof-of-forgery rest on.
"""
import numpy as np
import pytest

from app import collections as collections_mod, db, embeddings
from app.bundles import genesis, ledger
from app.bundles.genesis import GenesisArchive

DIM = 8


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


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
            url="http://fake:1", api="openai", model="fake-encoder",
            dtype="f16", ctx=512, dim=DIM))
    con = db.connect(c)
    for i in range(1, 6):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    return c


def _fetch(cache: dict):
    import json as _json

    def fetch(name, dest):
        value = cache[name]
        if name.endswith(".npy"):
            np.save(dest.with_suffix(""), value)
        else:
            dest.write_text(_json.dumps(value), encoding="utf-8")
    return fetch


def _same_function(texts, **kwargs):
    return [_unit(t).tolist() for t in texts]


def test_imported_chunks_are_badged_imported_not_local(collection):
    con = db.connect(collection)
    ids = list(range(1, 6))
    cache = {
        "embeds_chunk_0.npy": np.stack(
            [_unit(f"abstract text of document {i}") for i in ids]),
        "pmids_chunk_0.json": [str(i) for i in ids],
    }
    archive = GenesisArchive(archive_id="test-arc", base_url="http://arc",
                              adapter="medcpt-npy", shards=("0",),
                              uid_prefix="fake:")
    result = genesis.import_shard(con, collection, archive, "0",
                                   embed_fn=_same_function, fetch=_fetch(cache))
    assert result["docs"] == 5

    chunk_ids = [r["id"] for r in
                 embeddings._conn(collection).open_table(embeddings.TABLE)
                 .search().limit(50).to_list()]
    states = ledger.verification_states(con, chunk_ids)
    assert len(states) == len(chunk_ids), \
        "every imported chunk needs provenance — absence means 'local'"
    for row in states.values():
        assert row["state"] == "imported"
        assert row["producer"] == "genesis:test-arc"
        assert row["bundle_id"] == "genesis:test-arc/0"


def test_local_reembed_clears_imported_provenance(collection, monkeypatch):
    """A document later embedded locally must lose the imported badge —
    otherwise stale provenance outlives the vectors it described."""
    from app import worker
    con = db.connect(collection)
    cache = {
        "embeds_chunk_0.npy": np.stack(
            [_unit(f"abstract text of document {i}") for i in (1,)]),
        "pmids_chunk_0.json": ["1"],
    }
    archive = GenesisArchive(archive_id="test-arc", base_url="http://arc",
                              adapter="medcpt-npy", shards=("0",),
                              uid_prefix="fake:")
    genesis.import_shard(con, collection, archive, "0",
                          embed_fn=_same_function, fetch=_fetch(cache))
    assert con.execute(
        "SELECT COUNT(*) FROM chunk_verification WHERE uid='fake:1'").fetchone()[0] == 1

    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda texts, **kw: [_unit(t).tolist() for t in texts])
    row = con.execute("SELECT * FROM documents WHERE uid='fake:1'").fetchone()
    worker.stage_embed(con, row, collection)
    assert con.execute(
        "SELECT COUNT(*) FROM chunk_verification WHERE uid='fake:1'").fetchone()[0] == 0
