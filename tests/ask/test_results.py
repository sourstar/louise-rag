"""tests/ask/test_results.py — `louise_search`'s shape (05 §5.3, §5.4):
the fused lists per space, each result with the fields §5.3 fixes — the
legs that ranked it, the trust level, the producer, the origin's page in
place of a red collection's text — and the flags this brick refuses
rather than ignores."""
from types import SimpleNamespace

import pytest

from app import query_service, retrieval
from app.ask import results
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.share.test_messages import a_shard


def test_the_merge_keeps_each_leg_s_rank_the_text_leg_s_best_across_groups():
    text_en = [{"chunk_id": "g:1:0:1", "source": "fts:en"}, {"chunk_id": "g:2:0:1", "source": "fts:en"}]
    text_de = [{"chunk_id": "g:2:0:1", "source": "fts:de"}]
    dense = [{"chunk_id": "g:2:0:1", "source": "dense"}, {"chunk_id": "g:3:0:1", "source": "dense"}]
    merged = {m["chunk_id"]: m for m in retrieval._merge_rrf([text_en, text_de, dense])}
    # amended 2026-10-09 (05 §5.2): the groups are variants of ONE text leg,
    # which orders records by their best rank across them — g:2 is first in
    # the German group, ties with g:1 at 1, and goes second by uid bytes; its
    # rank in the leg is that position (it was 1, each group a vote of its own)
    assert merged["g:2:0:1"]["leg_ranks"] == {"text": 2, "vector": 1}
    assert merged["g:1:0:1"]["leg_ranks"] == {"text": 1}
    assert merged["g:3:0:1"]["leg_ranks"] == {"vector": 2}


def test_the_lists_are_per_space_and_a_result_carries_what_5_3_says(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)                      # pubmed: red, snapshot 2026-08
    red = SimpleNamespace(name=name, plugin="pubmed", codex=codex)
    legacy = SimpleNamespace(name="gutenberg", plugin="gutenberg", codex=None)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [red, legacy])
    monkeypatch.setattr(results, "_layer", lambda c, h: "abstract" if c.plugin == "pubmed" else "verbatim")

    def run(names, query, k, mode, translate, rerank_depth=50, all_languages=False, filters=None):
        assert names == [name, "gutenberg"] and k == 3 and mode == "hybrid" and translate is False
        return {"hits": [
            {"chunk_id": "pubmed:37000021:0:1", "book_id": 37000021, "source_collection": name, "text": "SECRET",
             "leg_ranks": {"text": 2, "vector": 1}, "verification": "imported", "producer": "ed25519:" + "a" * 64,
             "title": "An abstract", "lang": "en"},
            {"chunk_id": "gutenberg:1342:3:1", "book_id": 1342, "source_collection": "gutenberg", "text": "It is a truth",
             "leg_ranks": {"text": 1}, "verification": "local", "producer": "local", "title": "Pride", "lang": "en"},
            {"chunk_id": "gutenberg:11:0:1", "book_id": 11, "source_collection": "gutenberg", "text": "Alice",
             "leg_ranks": {"vector": 1}, "verification": "verified", "producer": "ed25519:" + "b" * 64,
             "title": "Alice", "lang": "en"},
        ], "facts": [], "unresolved": [], "withheld": [{"collection": "x", "reason": "quarantine"}],
            "legs_withheld": [], "translations": {}, "reranked": True, "timings": {"total": 1}}
    out = results.search("truth", limit=3, translate=False, root=node, run=run)
    assert set(out) >= {"lists", "facts", "unresolved", "withheld", "legs_withheld", "translations"}
    assert [l["space_id"] for l in out["lists"]] == [codex.space_id, "legacy:gutenberg"], \
        "one list per space, in the order the collections were named; a Codex-less collection is its own"
    assert all(l["question"] == "relevance" for l in out["lists"])
    (r,) = out["lists"][0]["hits"]
    assert (r["collection_id"], r["lineage_id"], r["snapshot_id"], r["uid"], r["chunk_id"], r["layer"], r["rank"]) == \
        (codex.collection_id, codex.collection_id, "2026-08", "pubmed:37000021", "pubmed:37000021:0:1", "abstract", 1)
    assert r["legs"] == {"text": 2, "vector": 1} and r["trust_level"] == "unverified" and r["certification"] is None
    assert r["verified_on_use"] == "imported" and r["producer_key"] == "ed25519:" + "a" * 64
    assert "text" not in r and r["origin_url"] and "37000021" in r["origin_url"], "red: the origin's page, never the text"
    assert r["figures"] is None and r["reranked"] is True
    g1, g2 = out["lists"][1]["hits"]
    assert (g1["rank"], g2["rank"]) == (1, 2) and g1["text"] == "It is a truth"
    assert g1["legs"] == {"text": 1, "vector": None} and g2["legs"] == {"text": None, "vector": 1}
    assert g1["verified_on_use"] == "local" and g1["producer_key"] is None, "no key on this test peer"
    assert g1["collection_id"] is None and g1["trust_level"] == "unverified", "a collection with no Codex"
    assert out["withheld"] == [{"collection": "x", "reason": "quarantine"}]


def test_the_flags_this_brick_does_not_build_are_refused_not_ignored(node, monkeypatch):  # noqa: F811
    monkeypatch.setattr(query_service, "resolve_collections",
                        lambda names, **kw: [SimpleNamespace(name="g", plugin="gutenberg", codex=None)])
    seen = {}

    def run(names, query, k, mode, translate, rerank_depth=50, all_languages=False, filters=None):
        seen["filters"] = filters
        return {"hits": [], "filtered": {"x": 3}}
    out = results.search("q", filters={"subjects": "Humans"}, run=run)
    assert seen["filters"] == {"subjects": "Humans"} and out["filters"] == {"subjects": "Humans"}
    assert out["filtered"] == {"x": 3}, "how many records each collection's filter admitted"
    with pytest.raises(results.AskError, match="filters is an object"):
        results.search("q", filters=["x"], run=run)
    with pytest.raises(results.AskError):
        results.search("   ", run=lambda *a, **k: {"hits": []})
    with pytest.raises(results.AskError, match="no collection held here is in space"):
        results.search("q", space="sha256:" + "0" * 64, run=lambda *a, **k: {"hits": []})


def test_everything_withheld_is_said_not_asked_of_the_search(node, monkeypatch):  # noqa: F811
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [])
    monkeypatch.setattr(query_service, "withheld_from",
                        lambda names, **kw: [{"collection": "x", "reason": "quarantine"}])
    out = results.search("q", run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no collections to search")))
    assert out["lists"] == [] and out["withheld"] == [{"collection": "x", "reason": "quarantine"}]
