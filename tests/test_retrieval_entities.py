"""P0.3 acceptance tests: degeneration (identical to the pre-entity-layer
golden baseline when no entities resolve), entity boost + pinned facts
demonstrated together, and the type-level invariant-4 guarantee.
"""
import json
import shutil
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db, refs, retrieval
from app.refs import store as refs_store
from tests.capture_degeneration_golden import QUERIES, _index_gutenberg, _index_pubmed

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "degeneration-golden.json").read_text())
FIXTURE_REFS = FIXTURES / "refs-mini"


def _gutenberg_collection(tmp_path):
    corpus = tmp_path / "gutenberg_corpus"
    shutil.copytree(FIXTURES / "gutenberg-mini" / "corpus", corpus)
    return collections_mod.Collection(
        name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "gutenberg" / "manifest.db",
        fts_dir=tmp_path / "gutenberg" / "fts", lance_dir=tmp_path / "gutenberg" / "lancedb")


def _pubmed_collection(tmp_path):
    return collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=FIXTURES / "pubmed-mini", manifest_path=tmp_path / "pubmed" / "manifest.db",
        fts_dir=tmp_path / "pubmed" / "fts", lance_dir=tmp_path / "pubmed" / "lancedb")


def _sync_refs(tmp_path, monkeypatch):
    """Points config.REFS_DB_PATH at a fresh per-test file and syncs the real
    hgnc/clinvar-mini fixtures into it — the resolver is genuinely live, not
    mocked away, even in the degeneration test."""
    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "refs.db")
    con = refs.connect()
    refs_store.sync_all(con, FIXTURE_REFS)
    con.close()


@pytest.mark.parametrize("name", ["gutenberg", "pubmed"])
def test_degeneration(name, tmp_path, monkeypatch):
    """20 entity-free queries produce IDENTICAL hit ids/order to the
    pre-P0.3 golden baseline (tests/capture_degeneration_golden.py), and
    resolve zero false-positive entities against ordinary fixture prose."""
    _sync_refs(tmp_path, monkeypatch)

    if name == "gutenberg":
        collection = _gutenberg_collection(tmp_path)
        _index_gutenberg(collection)
    else:
        collection = _pubmed_collection(tmp_path)
        _index_pubmed(collection)

    con = db.connect(collection)
    for q in QUERIES:
        result = retrieval.search(con, q, k=12, mode="hybrid", translate=False,
                                   collection=collection)
        got = [{"book_id": h["book_id"], "chunk_id": h["chunk_id"]} for h in result["hits"]]
        assert got == GOLDEN[name][q], f"query {q!r} diverged from the pre-P0.3 golden baseline"
        assert result["facts"] == [], f"query {q!r} spuriously resolved an entity"


def test_entity_boost_and_facts(tmp_path, monkeypatch):
    """Both acceptance-criterion halves together: a BRCA2 query returns
    pinned HGNC/ClinVar facts above ranked results, AND ranked results are
    boosted by the entity — proven causally (same query, before/after a
    doc_keys tag), not just by presence."""
    _sync_refs(tmp_path, monkeypatch)
    collection = _pubmed_collection(tmp_path)
    _index_pubmed(collection)
    con = db.connect(collection)

    query = "BRCA2 widget"   # "BRCA2" resolves the entity; "widget" gives
                              # genuine FTS competition among several PMIDs

    before = retrieval.search(con, query, k=12, mode="fts", translate=False, collection=collection)
    fact_ids_before = {f["entity_id"] for f in before["facts"]}
    assert fact_ids_before == {"hgnc:1101", "clinvar-mini:55074"}
    rank_before = next(i for i, h in enumerate(before["hits"]) if h["book_id"] == 30000001)
    assert rank_before > 0   # not already first on lexical merit alone

    con.execute("INSERT INTO doc_keys(uid, entity_id) VALUES ('pubmed:30000001','hgnc:1101')")

    after = retrieval.search(con, query, k=12, mode="fts", translate=False, collection=collection)
    rank_after = next(i for i, h in enumerate(after["hits"]) if h["book_id"] == 30000001)
    assert rank_after < rank_before   # causally moved up by the boost, not just present

    fact_ids_after = {f["entity_id"] for f in after["facts"]}
    assert fact_ids_after == {"hgnc:1101", "clinvar-mini:55074"}

    messages = retrieval.build_answer_messages(query, after)
    content = messages[1]["content"]
    ref_pos = content.find("[reference]")
    first_hit_pos = content.find("[1]")
    assert ref_pos != -1 and first_hit_pos != -1
    assert ref_pos < first_hit_pos


def test_pinned_fact_never_enters_rrf_input():
    """Type-level enforcement of CLAUDE.md invariant 4: a PinnedFact cannot
    silently be treated as a ranked hit dict, even under a future refactor
    mistake — _merge_rrf's h["chunk_id"] access raises immediately."""
    fact = retrieval.PinnedFact(entity_id="hgnc:1101", kind="hgnc_gene",
                                 source_id="hgnc", label="BRCA2", fields={})
    with pytest.raises(TypeError):
        retrieval._merge_rrf([[fact]])
