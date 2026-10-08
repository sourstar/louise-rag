"""Wikipedia's layer 1 through genesis (docs/notes/wikipedia-two-layers.md,
step 3). ONE collection holds the archive's articles and the dump's pages;
the archive's vectors land under the COLLECTION's ids, a dump page is never
an archive candidate, and inside the archive's declared range the local
encoder takes only what a read cannot cover (amendment 11) — never an
article whose vectors the archive supplies.
"""
import bz2
from dataclasses import replace
from types import SimpleNamespace
from xml.sax.saxutils import escape

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import (chunking, collections as collections_mod, db, discovery, embeddings,
                 layers, licensing, worker)
from app.bundles import genesis
from app.plugins.wikipedia import SOURCE
from app.plugins.wikipedia import archive as warchive
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.archive import embed_input
from app.plugins.wikipedia.ids import native_id_of

DIM = 8
ARCHIVE = "upstash-wikipedia-2024-06"
DUMP = "enwiki-latest-pages-articles-multistream"
LAYERS = [{"chunker": "verbatim", "rule": f"archive:{ARCHIVE}"},
          {"chunker": "cdc", "rule": "default"}]

MOON = f"wikipedia:{native_id_of('en', 10, archive=True)}"
SUN = f"wikipedia:{native_id_of('en', 11, archive=True)}"      # straddles 000/001
MARS = f"wikipedia:{native_id_of('en', 12, archive=True)}"
CHEESE = f"wikipedia:{native_id_of('en', 20)}"                  # the dump's only


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
        "url": [f"https://en.wikipedia.org/wiki/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), path)


def _page(pid: int, title: str, text: str) -> str:
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id><revision>"
            f"<id>{pid}00</id><timestamp>2026-07-31T12:00:00Z</timestamp>"
            f"<text>{escape(text)}</text></revision></page>\n")


@pytest.fixture
def reads(monkeypatch):
    """Records every shard genesis actually opens."""
    opened: list[str] = []
    real = genesis.import_shard

    def recording(con, collection, archive, shard, **kw):
        opened.append(shard)
        return real(con, collection, archive, shard, **kw)
    monkeypatch.setattr(genesis, "import_shard", recording)
    return opened


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)
    wsrc._stream_cache.clear()
    warchive._groups.clear()
    warchive._layouts.clear()
    root = tmp_path / "wikipedia"
    _write_shard(root / "data" / "en" / "000.parquet", [
        ("10", 0, "Moon", "para one"), ("10", 1, "Moon", "para two"),
        ("11", 0, "Sun", "straddle start")])
    _write_shard(root / "data" / "en" / "001.parquet", [
        ("11", 1, "Sun", "straddle end"), ("12", 0, "Mars", "red")])
    d = root / "enwiki" / "latest"
    d.mkdir(parents=True)
    (d / f"{DUMP}.xml.bz2").write_bytes(bz2.compress(
        (_page(10, "Moon", "'''The Moon''' is newer text.")
         + _page(20, "Cheese", "'''Cheese''' is made from milk.")).encode()))
    (d / f"{DUMP}-index.txt.bz2").write_bytes(bz2.compress(b"0:10:Moon\n0:20:Cheese\n"))
    c = collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb",
        genesis_archive=ARCHIVE,
        embed=collections_mod.EmbedRecipe(
            url="http://fake:1", api="openai", model="bge-m3-test",
            dtype="f16", ctx=512, dim=DIM),
        layers=layers.parse_layers(LAYERS, where="test"))
    c = replace(c, license_ack=licensing.make_ack(c))
    con = db.connect(c)
    discovery.sync_collection(con, c)
    assert {r[0] for r in con.execute("SELECT uid FROM documents")} == \
        {MOON, SUN, MARS, CHEESE}, "the dump's Moon is the archive's: no twin"
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")
    worker.reset_backfill_cursors()
    return c


def _drain(con, collection):
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            return


def test_layer_one_imports_under_the_collections_own_ids(collection):
    """The archive's registry prefix is the legacy plugin's; a LOCAL
    archive's rows are the collection's documents, `wikipedia:<id>`."""
    con = db.connect(collection)
    _drain(con, collection)
    done = {r[0] for r in con.execute("SELECT uid FROM documents WHERE embed_done=1")}
    assert done == {MOON, MARS}
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 3, "Moon's two rows and Mars's one, one vector per chunk"


def test_a_dump_page_is_never_an_archive_candidate(collection, reads):
    con = db.connect(collection)
    demand = genesis._candidate_shards(con, collection)
    assert [d.shard for d in demand] == ["data/en/000.parquet", "data/en/001.parquet"]
    assert all(CHEESE not in d.uids for d in demand)
    _drain(con, collection)
    before = list(reads)
    assert genesis.drain_next_shard(con, collection) is None, \
        "a pending dump page must not keep the archive claiming the round"
    assert reads == before


def test_the_encoder_holds_off_the_archive_range_but_for_what_no_read_covers(collection):
    """Under "all" the local encoder must not compute a vector the archive
    supplies. Before any read only the dump page is its; after the walk,
    the straddler — which no single shard read can cover — is too."""
    con = db.connect(collection)
    assert {r["uid"] for r in worker._backfill_rows(con, collection, 100)} == {CHEESE}
    _drain(con, collection)
    assert {r["uid"] for r in worker._backfill_rows(con, collection, 100)} == {CHEESE, SUN}


def test_a_rejected_shard_releases_its_own_documents_and_is_not_reread(collection, reads):
    """Once the archive is proven, a rejected file is that file's problem:
    its documents go to the encoder, the rest of the range stays the
    archive's, and the file is never read again (`genesis retry`)."""
    con = db.connect(collection)
    assert genesis.drain_next_shard(con, collection)["shard"] == "data/en/000.parquet"
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) "
                "VALUES(?,?,'rejected',0)", (ARCHIVE, "data/en/001.parquet"))
    assert genesis.drain_next_shard(con, collection) is None
    assert reads == ["data/en/000.parquet"]
    assert worker._covered_ranges(con, collection) == [(0, 11 * 10 ** 10 - 1)]
    assert {r["uid"] for r in worker._backfill_rows(con, collection, 100)} == \
        {CHEESE, SUN, MARS}


def test_the_layer_is_a_function_of_the_id(collection):
    """06 §6.3's `archive:` rule over the archive's declared coverage: an
    archive id is layer 1 in every language block, a dump id never — from
    the platform registry and from a Codex's credit alike."""
    first, last = native_id_of("en", 10, archive=True), native_id_of("tr", 7, archive=True)
    dump = native_id_of("en", 10)
    assert genesis.UPSTASH_WIKIPEDIA.shard_of_id(last) == "10"
    assert genesis.UPSTASH_WIKIPEDIA.shard_of_id(dump) is None
    codex_like = SimpleNamespace(layers=collection.layers, credits=[
        {"kind": "archive", "archive": ARCHIVE,
         "coverage": genesis.UPSTASH_WIKIPEDIA.coverage()}])
    for c in (collection, codex_like):
        assert layers.layer_index(c, first) == layers.layer_index(c, last) == 0
        assert layers.layer_index(c, dump) == 1


def test_wikipedia_declares_its_two_layers():
    got = [(l.chunker.id, l.chunker.version, l.rule)
           for l in __import__("app.hold.template", fromlist=["x"]).default_layers(SOURCE)]
    assert got == [
        ("verbatim", chunking.chunk_version_for("verbatim"), f"archive:{ARCHIVE}"),
        ("cdc", chunking.chunk_version_for("cdc"), "default")]
