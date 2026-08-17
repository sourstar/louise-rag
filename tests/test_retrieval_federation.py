"""P0.5 acceptance tests: with one collection, federated_search() is
identical to the single-collection search() baseline; with two, results
demonstrably interleave by rank (never by comparing raw scores across
collections — CLAUDE.md invariant 4's spirit and this task's own non-goal).
"""
from pathlib import Path

import pytest

from app import collections as collections_mod, db, fts, retrieval
from tests.capture_degeneration_golden import QUERIES, _index_gutenberg, _index_pubmed


# ---------------- (a) one collection == single-collection baseline ----------------

def _gutenberg_collection(tmp_path):
    import shutil
    fixtures = Path(__file__).parent / "fixtures"
    corpus = tmp_path / "gutenberg_corpus"
    shutil.copytree(fixtures / "gutenberg-mini" / "corpus", corpus)
    return collections_mod.Collection(
        name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "gutenberg" / "manifest.db",
        fts_dir=tmp_path / "gutenberg" / "fts", lance_dir=tmp_path / "gutenberg" / "lancedb")


def test_federated_search_with_one_collection_matches_search_baseline(tmp_path, monkeypatch):
    import app.config as config
    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "refs.db")

    collection = _gutenberg_collection(tmp_path)
    _index_gutenberg(collection)
    con = db.connect(collection)

    for q in QUERIES[:8]:   # a representative subset is enough; full parity isn't query-specific
        baseline = retrieval.search(con, q, k=12, mode="hybrid", translate=False,
                                     collection=collection)
        federated = retrieval.federated_search([collection], q, k=12, mode="hybrid",
                                                translate=False)
        base_ids = [(h["book_id"], h["chunk_id"]) for h in baseline["hits"]]
        fed_ids = [(h["book_id"], h["chunk_id"]) for h in federated["hits"]]
        assert fed_ids == base_ids, f"query {q!r} diverged from the single-collection baseline"
        assert federated["facts"] == baseline["facts"]


# ---------------- (b) two collections interleave by rank ----------------

def _synthetic_collection(tmp_path, name):
    d = tmp_path / name
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d / "corpus", manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb")


def _insert_and_index(collection, docs):
    """docs: [(native_id, lang, chunks)], chunks already in chunking.py's dict shape.
    Bypasses discovery/plugins entirely (matching tests/test_entities.py's and
    tests/test_scope_sample_per_lang_ordering.py's precedent), so this never
    touches the committed gutenberg-mini/pubmed-mini fixtures or their golden
    baselines — deliberately disjoint vocabulary in those fixtures means no
    existing query would ever surface real overlapping relevance from both."""
    con = db.connect(collection)
    for native_id, lang, chunks in docs:
        uid = f"{collection.plugin}:{native_id}"
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,rdf_done,fts_done,updated_at) "
            "VALUES(?,?,?,?,?,1,1,0)",
            (uid, collection.plugin, str(native_id), f"/synthetic/{native_id}", lang))
        fts.index_book(lang, native_id, chunks, collection)
    return con


def _chunk(uid, native_id, seq, chapter, text):
    return {"id": f"{uid}:{seq}:1", "book_id": native_id, "seq": seq,
            "chapter": chapter, "text": text, "tokens": max(1, len(text) // 4)}


def test_federated_search_interleaves_by_rank_not_raw_score(tmp_path, monkeypatch):
    """Regression guard for the non-goal "scores never compared across
    collections": alpha's chunks are long/noisy (low BM25), beta's are
    short/keyword-dense (high BM25) — if federation were (incorrectly)
    implemented by comparing raw scores across collections, beta would
    dominate every position and the result would NOT interleave."""
    import app.config as config
    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "refs.db")

    alpha = _synthetic_collection(tmp_path, "alpha")
    beta = _synthetic_collection(tmp_path, "beta")

    filler = ("the quiet fishing village stood beside winding roads past misty hills "
              "where old stone cottages faced the water and fishermen mended their nets "
              "every morning before the tide turned and the gulls began circling ")
    long_noisy_1 = f"{filler * 4} lighthouse {filler * 4}"
    long_noisy_2 = f"{filler * 5} lighthouse {filler * 3}"
    _insert_and_index(alpha, [
        (1, "en", [_chunk("alpha:1", 1, 0, "One", long_noisy_1)]),
        (2, "en", [_chunk("alpha:2", 2, 0, "Two", long_noisy_2)]),
    ])

    short_dense_1 = "lighthouse lighthouse lighthouse"
    short_dense_2 = "lighthouse lighthouse"
    _insert_and_index(beta, [
        (1, "en", [_chunk("beta:1", 1, 0, "One", short_dense_1)]),
        (2, "en", [_chunk("beta:2", 2, 0, "Two", short_dense_2)]),
    ])

    # Sanity check the premise: beta's raw BM25 scores really are much
    # higher than alpha's — otherwise this test wouldn't actually exercise
    # the "never compare raw scores across collections" guarantee.
    alpha_hits = fts.search("en", "lighthouse", collection=alpha)
    beta_hits = fts.search("en", "lighthouse", collection=beta)
    assert len(alpha_hits) == 2 and len(beta_hits) == 2
    assert beta_hits[0]["score"] > alpha_hits[0]["score"] * 1.3, \
        f"test premise too weak: beta={beta_hits[0]['score']} alpha={alpha_hits[0]['score']}"

    # Expected interleaving is derived from each collection's OWN observed
    # rank order (not a hand-predicted book_id sequence) — alpha sorts
    # before beta alphabetically, so at each matching rank position (tied
    # federated rrf), alpha's hit comes first (stable sort over insertion
    # order — see federated_search()'s outcomes.sort(key=...name)).
    expected = []
    for a, b in zip((h["book_id"] for h in alpha_hits), (h["book_id"] for h in beta_hits)):
        expected.append(("alpha", a))
        expected.append(("beta", b))

    federated = retrieval.federated_search([alpha, beta], "lighthouse", k=12, mode="fts",
                                            translate=False)
    got = [(h["source_collection"], h["book_id"]) for h in federated["hits"]]
    assert got == expected, f"expected alternating alpha/beta interleaving by rank, got {got}"


def test_pinned_fact_never_enters_federated_rrf_input():
    """Type-level enforcement of invariant 4, mirroring
    test_retrieval_entities.py's single-collection version: a PinnedFact
    cannot silently be treated as a ranked hit dict in the federated fuse
    either."""
    fact = retrieval.PinnedFact(entity_id="hgnc:1101", kind="hgnc_gene",
                                 source_id="hgnc", label="BRCA2", fields={})
    with pytest.raises(TypeError):
        retrieval._merge_rrf_federated([[fact]])
