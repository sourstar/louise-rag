"""05 §5.1–5.2 as the fusion now runs them (amended 2026-10-09;
docs/notes/one-language-per-work.md): two legs, the record as the unit.

Before: one RRF list per text index group and the chunk as the unit — a
gutenberg query entered RRF as 16 text lists and one vector list, each
group's rank 1 scoring like the vector leg's, and a book took one slot per
matching chunk. Measured on 451 captured queries (tools/measure/
one_language_rescore.py): the work in the fused top 10 for 14.4 % of them
before, 39.7 % now; in the top 50 the reranker orders, 38.1 % → 59.4 %."""
import pytest

from app import collections as collections_mod, db, fts, llm, retrieval


def h(uid: str, seq: int, source: str) -> dict:
    return {"chunk_id": f"{uid}:{seq}:1", "book_id": int(uid.split(":")[1]), "source": source,
            "text": f"{uid} chunk {seq}"}


def test_sixteen_language_groups_cast_one_vote_not_sixteen():
    """Every group's rank 1 used to score 1/61, like the vector leg's: the
    vector leg's first record lost to sixteen lexical rank-1s. Now the
    groups are variants of ONE text leg, which ranks its records once."""
    groups = [[h(f"g:{100 + i}", 0, f"fts:l{i}")] for i in range(16)]
    dense = [h("g:1", 0, "dense"), h("g:2", 0, "dense")]
    merged = retrieval._merge_rrf(groups + [dense])
    scores = {retrieval.record_of(m): m["rrf"] for m in merged}
    assert scores["g:1"] == pytest.approx(1 / 61), "the vector leg's rank 1"
    assert sorted(scores.values(), reverse=True)[:2] == [pytest.approx(1 / 61)] * 2, \
        "one text record at 1/61, the other fifteen below it — one vote per rank"
    text_ranks = sorted(m["leg_ranks"]["text"] for m in merged if "text" in m["leg_ranks"])
    assert text_ranks == list(range(1, 17)), "the text leg ranks its records 1..16, once each"


def test_a_record_is_fused_once_carrying_its_best_chunk():
    """The record is the unit (§5.1): three chunks of one book are one entry,
    its rank in a list that of its best chunk among the list's records."""
    text = [h("g:7", 3, "fts:en"), h("g:7", 9, "fts:en"), h("g:8", 0, "fts:en"), h("g:7", 1, "fts:en")]
    dense = [h("g:8", 4, "dense"), h("g:7", 9, "dense")]
    merged = retrieval._merge_rrf([text, dense])
    assert [retrieval.record_of(m) for m in merged] == ["g:7", "g:8"]
    seven, eight = merged
    assert seven["leg_ranks"] == {"text": 1, "vector": 2}
    assert eight["leg_ranks"] == {"text": 2, "vector": 1}
    assert seven["chunk_id"] == "g:7:3:1", "the chunk that ranked highest in its best leg (text)"
    assert eight["chunk_id"] == "g:8:4:1", "…here the vector leg's"
    assert seven["rrf"] == pytest.approx(1 / 61 + 1 / 62)
    assert seven["sources"] == ["dense", "fts:en"]


def test_a_variant_ranks_the_record_by_its_best_across_the_leg_and_ties_go_by_uid():
    en = [h("g:1", 0, "fts:en"), h("g:2", 0, "fts:en")]
    de = [h("g:2", 0, "fts:de")]
    merged = {retrieval.record_of(m): m for m in retrieval._merge_rrf([en, de])}
    assert merged["g:1"]["leg_ranks"] == {"text": 1}
    assert merged["g:2"]["leg_ranks"] == {"text": 2}, \
        "best rank 1 in both: the tie goes by uid bytes, and the leg's rank is the position"
    a = [h("g:10", 0, "fts:en")]
    b = [h("g:9", 0, "fts:de")]
    assert [retrieval.record_of(m) for m in retrieval._merge_rrf([a, b])] == ["g:10", "g:9"], \
        "bytes, not numbers: 'g:10' < 'g:9'"


def test_each_leg_contributes_only_its_top_records_to_the_depth():
    text = [h(f"g:{i}", 0, "fts:en") for i in range(1, 6)]
    dense = [h("g:5", 0, "dense")]
    merged = retrieval._merge_rrf([text, dense], depth=3)
    by = {retrieval.record_of(m): m for m in merged}
    assert set(by) == {"g:1", "g:2", "g:3", "g:5"}
    assert by["g:5"]["leg_ranks"] == {"vector": 1}, "past the text leg's depth: no text rank"
    assert by["g:5"]["chunk_id"] == "g:5:0:1"


def test_a_native_id_with_a_colon_is_one_record():
    assert retrieval.record_of({"chunk_id": "wikipedia:en:Rome:12:3"}) == "wikipedia:en:Rome"


def test_every_variant_reaches_the_fusion_as_its_own_list(isolated_config, monkeypatch):
    """A mixed group's variants were merged per CHUNK before the fusion
    (`_best_rank`); now each variant's list is handed to `_merge_rrf`, which
    ranks a record by its best across every variant of the text leg."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["en", "other"])
    monkeypatch.setattr(fts, "group_languages", lambda g, collection=None: ["la", "eo"])
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {l: f"{l}:{q}" for l in langs})
    hits = {"en:whale": [h("gutenberg:1", 0, "fts:en")],
            "la:whale": [h("gutenberg:2", 0, "fts:other")],
            "eo:whale": [h("gutenberg:3", 0, "fts:other"), h("gutenberg:2", 5, "fts:other")]}
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        hits.get(q, []))
    seen = []
    real = retrieval._merge_rrf
    monkeypatch.setattr(retrieval, "_merge_rrf", lambda lists, depth=None, **kw: seen.append(lists)
                        or real(lists, depth, **kw))
    from app import language_plan
    plan = language_plan.plan("whale", [c], True, every=True)
    out = retrieval._search_one_collection(db.connect(c), c, "whale", 5, "fts", True, set(), plan)
    assert len(seen[0]) == 3, "en, and each of 'other''s two variants"
    ranks = {retrieval.record_of(m): m["leg_ranks"]["text"] for m in out["merged"]}
    assert ranks == {"gutenberg:1": 1, "gutenberg:2": 2, "gutenberg:3": 3}


def hl(uid: str, source: str, lang: str) -> dict:
    return {**h(uid, 0, source), "lang": lang}


def test_equal_best_ranks_go_to_the_users_own_words_then_the_query_language_then_uid():
    """A book is in one language group, so sixteen variants give sixteen
    different rank-1 records; uid bytes alone made their order arbitrary
    and title queries paid (R@10 0.821 → 0.615 on 39). Measured with this
    order: 0.744, MRR 0.344 → 0.501 (tools/measure/one_language_rescore.py)."""
    de = [hl("g:1", "fts:de", "de")]
    hu = [hl("g:2", "fts:hu", "hu")]
    en = [hl("g:3", "fts:en", "en")]
    it = [hl("g:4", "fts:it", "it")]
    order = lambda **kw: [retrieval.record_of(m) for m in retrieval._merge_rrf([de, hu, en, it], **kw)]
    assert order() == ["g:1", "g:2", "g:3", "g:4"], "no plan: uid bytes, as before"
    assert order(own={2}) == ["g:3", "g:1", "g:2", "g:4"], "the list run with the query as typed first"
    assert order(query_lang="it") == ["g:4", "g:1", "g:2", "g:3"], "then a record in the query's language"
    assert order(own={2}, query_lang="IT") == ["g:3", "g:4", "g:1", "g:2"]


def test_the_tie_break_never_outranks_a_better_rank():
    """Only equal best ranks are ordered: rank 1 in Hungarian still beats
    rank 2 in the user's own words — the keys order, they never boost."""
    en = [hl("g:5", "fts:en", "en"), hl("g:3", "fts:en", "en")]
    hu = [hl("g:2", "fts:hu", "hu")]
    merged = retrieval._merge_rrf([en, hu], own={0}, query_lang="en")
    ranks = {retrieval.record_of(m): m["leg_ranks"]["text"] for m in merged}
    assert ranks == {"g:5": 1, "g:2": 2, "g:3": 3}


def test_the_search_marks_the_variant_that_is_the_query_as_typed(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["de", "en"])
    monkeypatch.setattr(llm, "language_restriction",
                        lambda q: llm.Reading([], q, "en"))
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: {"de": "Walfang", "en": "whaling"})
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        [hl("g:1" if group == "de" else "g:9", f"fts:{group}", group)])
    seen = {}
    real = retrieval._merge_rrf
    monkeypatch.setattr(retrieval, "_merge_rrf", lambda lists, depth=None, **kw: seen.update(kw)
                        or real(lists, depth, **kw))
    out = retrieval._search_one_collection(db.connect(c), c, "whaling", 5, "fts", True, set())
    assert seen == {"own": {1}, "query_lang": "en"}, "the English list's variant is the query itself"
    assert [retrieval.record_of(m) for m in out["merged"]] == ["g:9", "g:1"]


def test_a_title_the_catalogue_finds_is_a_variant_of_the_text_leg(isolated_config, monkeypatch):
    """Query campaign, 2026-10-20: "War and Peace" missed the top 100 though
    2600 is held, indexed and embedded — the catalogue's hits (title,
    creators, subjects) reached only the legacy prompt, and the text leg
    indexes chunk text alone, so anthologies quoting the title won. The
    catalogue is now one more variant of the text leg, in its own order,
    each record carried by its best-matching chunk (else its first); it
    casts the leg's one vote per rank, as the user's own words."""
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: ["en"])
    monkeypatch.setattr(fts, "group_languages", lambda g, collection=None: ["en"])
    monkeypatch.setattr(fts, "group_for", lambda lang: "en")
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {})
    anthology = h("gutenberg:900", 4, "fts:en")

    def search(group, q, limit=20, collection=None, langs=None, book_ids=None, validity=None):
        if book_ids == [2600]:
            return []                                  # the title's words are not in the book's text
        if book_ids == [77]:
            return [h("gutenberg:77", 12, "fts:en")]
        return [anthology] if not book_ids else []
    monkeypatch.setattr(fts, "search", search)
    monkeypatch.setattr(fts, "book_chunks", lambda group, bid, collection=None:
                        [h(f"gutenberg:{bid}", s, "fts:en") for s in (5, 0, 3)])
    monkeypatch.setattr(retrieval, "catalog_search", lambda con, q, limit=10, langs=None, collection=None:
                        [{"book_id": 2600, "title": "War and Peace", "lang": "en"},
                         {"book_id": 77, "title": "War and Peace (abridged)", "lang": "en"}]
                        + [{"book_id": 1000 + i, "title": f"x{i}", "lang": "en"} for i in range(30)])
    monkeypatch.setattr(retrieval.embeddings, "search_records", lambda *a, **k: [])
    from app import language_plan
    plan = language_plan.plan("War and Peace", [c], False)
    out = retrieval._search_one_collection(db.connect(c), c, "War and Peace", 5, "hybrid", False, set(), plan)
    by_uid = {retrieval.record_of(m): m for m in out["merged"]}
    assert by_uid["gutenberg:2600"]["leg_ranks"]["text"] == 1, "the title's record: the text leg's first"
    assert by_uid["gutenberg:2600"]["chunk_id"] == "gutenberg:2600:0:1", "no matching chunk: its first"
    assert by_uid["gutenberg:77"]["chunk_id"] == "gutenberg:77:12:1", "its best-matching chunk"
    assert "catalog" in by_uid["gutenberg:2600"]["sources"]
    assert by_uid["gutenberg:900"]["leg_ranks"]["text"] == 2, "the anthology: the text variant's first, tied, by uid"
    assert len(out["catalog"]) == retrieval.CATALOG_PROMPT, "the legacy prompt's block is as before"
