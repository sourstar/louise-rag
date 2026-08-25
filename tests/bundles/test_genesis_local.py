"""Local (corpus-resident) genesis archives — amendment 11: the corpus IS
the archive. Vectors are read from the collection's own parquet shards,
shards are revisited as documents become eligible, the settle-wait is
waived, and corpus files are never deleted by the importer.
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


def _different_model_embed(texts, timeout=300, recipe=None, query=False):
    return [_unit("other:" + t).tolist() for t in texts]


def _write_shard(path, specs):
    """specs: (article, seq, title, text) — embedding = the same function
    the fake local encoder computes over the title-prefixed chunk text."""
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
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "Cengiz Han", "para one"),
        ("10", 1, "Cengiz Han", "para two"),
        ("11", 0, "Ankara", "capital"),
    ])
    _write_shard(c.corpus_dir / "data" / "tr" / "001.parquet", [
        ("13", 0, "Izmir", "coastal"),
        ("14", 0, "Konya", "plains"),
    ])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c


def _make_eligible(con, uids=None):
    if uids is None:
        con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")
    else:
        marks = ",".join("?" for _ in uids)
        con.execute(f"UPDATE documents SET rdf_done=1, fts_done=1 "
                    f"WHERE uid IN ({marks})", uids)


def test_drain_imports_revisits_and_keeps_corpus_files(collection, monkeypatch):
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)

    uids = [r["uid"] for r in con.execute(
        "SELECT uid FROM documents ORDER BY CAST(native_id AS INTEGER)")]
    assert len(uids) == 4

    # only the first two documents are eligible yet — rdf/fts backlog EXISTS
    # for the rest, and a local archive must NOT wait for settle
    _make_eligible(con, uids[:2])
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 2

    # the rest become eligible later — REVISIT imports them through the
    # same (already-applied) shards
    _make_eligible(con)
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 4

    # all vectors landed with the right shape; corpus files untouched
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 5     # article 10 has two chunks
    assert (collection.corpus_dir / "data" / "tr" / "000.parquet").is_file()
    assert (collection.corpus_dir / "data" / "tr" / "001.parquet").is_file()

    # ledger coverage ACCUMULATED across revisits
    total_docs = con.execute(
        "SELECT SUM(docs) FROM genesis_imports WHERE archive=?", (ARCHIVE,)).fetchone()[0]
    assert total_docs == 4

    # fully covered → nothing left to do
    assert genesis.drain_next_shard(con, collection) is None


def test_wrong_model_archive_rejected_whole_and_never_retried(collection, monkeypatch):
    """A verdict needs evidence (amendment 42): the fixture's two small
    shards (3 + 2 chunks) are INCONCLUSIVE against an unproven archive —
    deferred, nothing written — and a shard with enough eligible
    documents is what convicts the archive; after that it is never
    retried, even once the encoder agrees."""
    con = db.connect(collection)
    _write_shard(collection.corpus_dir / "data" / "tr" / "002.parquet",
                 [(str(a), 0, f"Title {a}", f"text {a}") for a in range(20, 30)])
    discovery.sync_collection(con, collection)
    _make_eligible(con)
    monkeypatch.setattr(embeddings, "embed_texts", _different_model_embed)

    for _ in range(6):
        genesis.drain_next_shard(con, collection)
        rows = {r["shard"]: r for r in con.execute(
            "SELECT shard, state, detail FROM genesis_imports")}
        if any(r["state"] == "rejected" for r in rows.values()):
            break
    assert rows["data/tr/002.parquet"]["state"] == "rejected"
    for small in ("data/tr/000.parquet", "data/tr/001.parquet"):
        assert rows[small]["state"] == "skipped" and "inconclusive" in rows[small]["detail"], rows[small]
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()

    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    assert genesis.drain_next_shard(con, collection) is None, \
        "a rejected archive must never be retried"


def test_no_sample_within_truncation_budget_defers_then_imports_once_proven(collection, monkeypatch):
    """If every chunk exceeds the local truncation budget the same-function
    proof cannot run on THIS shard: against an unproven archive that is
    inconclusive (deferred, nothing written — never a refusal without
    evidence); once another shard has proven the archive, the over-budget
    document imports — its archived vector is strictly better than
    anything the local encoder could produce (amendment 11)."""
    con = db.connect(collection)
    big = "z" * (512 * 4 + 100)     # over recipe.ctx*4 chars
    shard = collection.corpus_dir / "data" / "tr" / "002.parquet"
    _write_shard(shard, [("77", 0, "Big", big)])
    discovery.sync_collection(con, collection)
    big_uid = f"embedded:{10 * 10_000_000_000 + 77}"
    _make_eligible(con, [big_uid])
    archive = genesis.ARCHIVES[ARCHIVE]

    with pytest.raises(genesis.GenesisUnavailable, match="truncation budget"):
        genesis.import_shard(con, collection, archive, "data/tr/002.parquet",
                             embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?",
                       (big_uid,)).fetchone()["embed_done"] == 0
    row = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert row["state"] == "skipped" and "truncation budget" in row["detail"]

    # another shard proves the archive; the big document then imports
    _make_eligible(con)
    first = genesis.import_shard(con, collection, archive, "data/tr/000.parquet",
                                 embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert first["docs"] == 2
    again = genesis.import_shard(con, collection, archive, "data/tr/002.parquet",
                                 embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert again["docs"] == 1
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?",
                       (big_uid,)).fetchone()["embed_done"] == 1


def test_a_candidate_read_touches_only_the_candidates_row_groups(collection, monkeypatch):
    """Regression (2026-08-27, measured live): every 1,000-document fts
    batch triggered a read of the WHOLE 437 MB shard to import those
    documents. A candidate read now opens only the row groups the
    candidates' locators name."""
    import pyarrow.parquet as pq
    con = db.connect(collection)
    # a shard of 6 articles in 3 row groups of 2 rows (one article per row)
    shard = collection.corpus_dir / "data" / "tr" / "003.parquet"
    specs = [(str(a), 0, f"T{a}", f"text {a}") for a in range(30, 36)]
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), shard, row_group_size=2)
    discovery.sync_collection(con, collection)
    assert pq.ParquetFile(shard).num_row_groups == 3
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    # only article 34 (row 4 -> row group 2) is eligible
    uid = f"embedded:{10 * 10_000_000_000 + 34}"
    _make_eligible(con, [uid])
    read_groups = []
    real = pq.ParquetFile.read_row_group
    monkeypatch.setattr(pq.ParquetFile, "read_row_group",
                        lambda self, g, *a, **k: (read_groups.append(g), real(self, g, *a, **k))[1])
    whole = []
    real_iter = pq.ParquetFile.iter_batches
    monkeypatch.setattr(pq.ParquetFile, "iter_batches",
                        lambda self, *a, **k: (whole.append(1), real_iter(self, *a, **k))[1])
    result = genesis.drain_next_shard(con, collection)
    assert result is not None and result["docs"] == 1
    assert read_groups == [2] and not whole, (read_groups, whole)
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?", (uid,)).fetchone()[0] == 1


def test_local_shards_listing(collection):
    shards = genesis._local_shards(collection)
    assert shards == ("data/tr/000.parquet", "data/tr/001.parquet")
