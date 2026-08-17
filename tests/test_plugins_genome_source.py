"""Genome plugin — FASTA assemblies as chromosome documents with
deterministic window chunks (architecture-v2.md §9), validated at
yeast-fixture scale exactly as the doc prescribes; plus the HomoloGene
orthology reference (§9 multi-species adjustment 2).
"""
import gzip

import pytest

from app import chunking, collections as collections_mod, config, db, discovery
from app.plugins.base import DiscoverState
from app.plugins.genome import SOURCE
from app.plugins.genome.source import split_locator

CHR1 = "ACGT" * 3000          # 12 kb — several windows at CHUNK_TOKENS=1000
CHR_MT = "GATTACA" * 40       # 280 b — a single tiny window


@pytest.fixture
def corpus(tmp_path):
    d = tmp_path / "genome-corpus"
    d.mkdir()
    fasta = (">chrI mitochondrion-free\n"
             + "\n".join(CHR1[i:i + 80] for i in range(0, len(CHR1), 80))
             + "\n>chrM organelle\n" + CHR_MT + "\n")
    (d / "sacCer3.fa.gz").write_bytes(gzip.compress(fasta.encode()))
    return d


def test_discover_yields_one_doc_per_contig(corpus):
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    assert len(refs) == 2
    assert [r.extra["contig"] for r in refs] == ["chrI", "chrM"]
    assert refs[0].size == len(CHR1)
    path, ordinal = split_locator(refs[0].locator)
    assert path.name == "sacCer3.fa.gz" and ordinal == 0


def test_fetch_parse_reconstructs_the_sequence(corpus):
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    doc = SOURCE.parse(SOURCE.fetch(refs[0]))
    assert doc.uid == "genome:0"
    assert doc.title == "sacCer3 chrI"
    assert doc.language == "dna"
    assert doc.keys == ["sacCer3:chrI"], "species-qualified key (§9)"
    (name, seq), = doc.sections
    assert name == "chrI" and seq == CHR1

    mt = SOURCE.parse(SOURCE.fetch(refs[1]))
    assert mt.sections[0][1] == CHR_MT


def test_window_chunks_are_deterministic_coordinates(corpus):
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    doc = SOURCE.parse(SOURCE.fetch(refs[0]))
    chunks = list(chunking.chunk_book(doc.uid, 0, doc.sections))
    assert len(chunks) > 1
    window = config.CHUNK_TOKENS * 4
    stride = (config.CHUNK_TOKENS - config.CHUNK_OVERLAP) * 4
    # window k starts at k*stride: the chunk id IS a coordinate
    assert chunks[0]["text"] == CHR1[:window]
    assert chunks[1]["text"].startswith(CHR1[stride:stride + 100])
    # re-chunking yields identical ids and texts (invariant 3)
    again = list(chunking.chunk_book(doc.uid, 0, doc.sections))
    assert [(c["id"], c["text"]) for c in again] == \
        [(c["id"], c["text"]) for c in chunks]


def test_worker_stages_run_end_to_end(tmp_path, corpus, isolated_config):
    from app import worker
    collection = collections_mod.Collection(
        name="saccer3", plugin="genome", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2

    row = con.execute("SELECT * FROM documents WHERE uid='genome:0'").fetchone()
    worker.stage_rdf(con, row, collection)
    refreshed = con.execute("SELECT * FROM documents WHERE uid='genome:0'").fetchone()
    assert refreshed["title"] == "sacCer3 chrI" and refreshed["rdf_done"] == 1
    chunks = worker._load_chunks(refreshed, collection)
    assert chunks and set(chunks[0]["text"]) <= set("ACGT")


HOMOLOGENE_DATA = (
    "3\t9606\t34\tACADM\t4557231\tNP_000007.1\n"
    "3\t10090\t11364\tAcadm\t6680618\tNP_031408.1\n"
    "5\t9606\t7157\tTP53\t120407068\tNP_000537.3\n"
    "5\t10090\t22059\tTrp53\t148747303\tNP_035770.2\n"
    "bad\trow\n"
)


def test_homologene_transform_and_sync(tmp_path):
    from app.refs import connect, store as refs_store
    from app.refs.acquire import _homologene_transform

    raw = tmp_path / "homologene.data"
    raw.write_text(HOMOLOGENE_DATA, encoding="utf-8")
    final = tmp_path / "homologene.tsv"
    _homologene_transform(raw, final)

    con = connect(tmp_path / "refs.db")
    result = refs_store.sync_all(con, tmp_path)
    assert result["homologene"]["rows"] == 4

    # the orthology walk: human TP53's group also holds mouse Trp53,
    # species-qualified so the symbols never collide
    group = con.execute(
        "SELECT group_id FROM homologene WHERE symbol='TP53' AND taxid=9606"
    ).fetchone()["group_id"]
    orthologs = con.execute(
        "SELECT taxid, symbol FROM homologene WHERE group_id=?", (group,)).fetchall()
    assert {(r["taxid"], r["symbol"]) for r in orthologs} == \
        {(9606, "TP53"), (10090, "Trp53")}
