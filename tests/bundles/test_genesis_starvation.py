"""A local archive must not starve the local encoder.

Local archives REVISIT their shards (documents become eligible gradually),
so `drain_next_shard` can always find a shard to re-read. If a revisit that
imports nothing still counts as work, the worker spends every round
re-reading parquet files and the embed stage never runs — the documents the
archive cannot cover (chunk-shape mismatches at shard boundaries) would
never be gap-filled locally, which is exactly what amendment 11 promises.
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import collections as collections_mod, db, discovery, embeddings
from app.bundles import genesis
from app.plugins.embedded.source import embed_input

DIM = 8
ARCHIVE = "upstash-wikipedia-2024-06"


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _same_function_embed(texts, timeout=300, recipe=None, query=False):
    return [_unit(t).tolist() for t in texts]


def _write_shard(path, specs):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), path)


@pytest.fixture
def collection(isolated_config, tmp_path):
    c = collections_mod.Collection(
        name="wikivec", plugin="embedded", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        genesis_archive=ARCHIVE,
        embed=collections_mod.EmbedRecipe(
            url="http://fake:1", api="openai", model="bge-m3-test",
            dtype="f16", ctx=512, dim=DIM))
    # article 12 STRADDLES the two shards: each shard yields a partial
    # vector list, so the chunk-shape gate refuses it — a residual candidate
    # the archive can never cover, exactly like a real shard boundary.
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "A", "alpha one"),
        ("12", 0, "C", "straddle start"),
    ])
    _write_shard(c.corpus_dir / "data" / "tr" / "001.parquet", [
        ("12", 1, "C", "straddle end"),
        ("13", 0, "D", "delta one"),
    ])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")
    return c


def test_exhausted_revisits_yield_the_round_to_local_embed(collection, monkeypatch):
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)

    # drain until the archive has covered everything it can
    for _ in range(40):
        if genesis.drain_next_shard(con, collection) is None:
            break
    covered = con.execute(
        "SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    residual = con.execute(
        "SELECT COUNT(*) FROM documents WHERE fts_done=1 AND embed_done=0 "
        "AND error IS NULL").fetchone()[0]
    assert covered >= 2, "the archive should have covered the whole articles"
    assert residual >= 1, "the straddling article is the residual candidate"

    # THE POINT: with a residual candidate still present, genesis must stop
    # claiming the round, or the local encoder never gets to gap-fill it.
    assert genesis.drain_next_shard(con, collection) is None, (
        "an exhausted revisit cycle must yield the round — otherwise the "
        "worker re-reads shards forever and local gap-fill never runs")


def test_a_new_candidate_reopens_the_revisit_cycle(collection, monkeypatch):
    """Yielding must not be permanent: when documents become eligible later
    (rdf/fts progress, or a grown corpus), revisits resume."""
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    for _ in range(40):
        if genesis.drain_next_shard(con, collection) is None:
            break

    _write_shard(collection.corpus_dir / "data" / "tr" / "002.parquet", [
        ("20", 0, "E", "epsilon one")])
    discovery.sync_collection(con, collection)
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")

    result = genesis.drain_next_shard(con, collection)
    assert result is not None and result.get("docs"), \
        "a newly eligible document must be picked up by the archive"
