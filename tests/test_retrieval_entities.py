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


def test_a_reached_fact_names_the_link_everywhere_it_is_shown(tmp_path, monkeypatch):
    """docs/05 §5.4: "— when expand reached it — the link it was reached
    through". The real hgnc/clinvar-mini fixtures: BRCA2 is named, ClinVar
    55074 is reached through `variant_to_gene` — in the response, in the
    answer's [reference] block, and on the fact card."""
    from app import web
    _sync_refs(tmp_path, monkeypatch)
    collection = _pubmed_collection(tmp_path)
    _index_pubmed(collection)
    result = retrieval.search(db.connect(collection), "BRCA2 widget", k=5, mode="fts",
                              translate=False, collection=collection)
    via = {f["entity_id"]: f["via"] for f in result["facts"]}
    assert via == {"hgnc:1101": None, "clinvar-mini:55074": {
        "from": "hgnc:1101", "link": "variant_to_gene",
        "on": "clinvar_variant.gene_symbol → hgnc_gene.symbol"}}

    prompt = retrieval.build_answer_messages("BRCA2 widget", result)[1]["content"]
    assert "reached from hgnc:1101 through variant_to_gene" in prompt
    gene_block = prompt[prompt.index("[reference] BRCA2"):].split("\n\n", 1)[0]
    assert "reached from" not in gene_block, "the gene was named, not reached"

    card = web.TEMPLATES.env.get_template("_ask_results.html").render(result=result)
    assert card.count("reached from hgnc:1101 through variant_to_gene") == 1
