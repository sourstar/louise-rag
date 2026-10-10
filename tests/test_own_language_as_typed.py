"""The query's own language is searched with the reader's words (05 §5.2,
amended 2026-10-10).

Found on the live re-check: MADLAD "translates" a query into the language
it is already in, and the plan searched that language's index with the
rewrite — 292 of 472 queries ("Dumas novel about the diamond necklace
scandal…" → "The Diamond Necklace Scandal at the Court of Louis XVI…").
Now the query's language (the reading's, else the guess) is never sent to
the translator and its group gets the query as typed. Measured before the
reranker on 451 descriptive queries: MRR 0.219 → 0.231, R@10 0.404 →
0.421 with the reading; 0.226 / 0.419 with the guess alone. Searching the
as-typed text in EVERY group when the language is unknown was measured
too and is not done: MRR 0.212 → 0.187 (49 better, 79 worse)."""
import json

from app import collections as collections_mod, db, fts, language_plan, llm, retrieval


class Rewriter:
    """A translator that rewrites the query in every language asked —
    its own included."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def __call__(self, q, langs):
        self.asked.append(sorted(langs))
        return {lang: f"[{lang}] rewritten" for lang in langs}


def _plan(monkeypatch, query, lang, groups=("de", "en", "fr")):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups", lambda collection=None: list(groups))
    reply = {"phrase": "", "only": []} if lang is None else {"phrase": "", "only": [], "lang": lang}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(reply))
    rw = Rewriter()
    monkeypatch.setattr(llm, "translate_query", rw)
    return language_plan.plan(query, [c], True), rw


def test_the_own_language_group_gets_the_query_as_typed(isolated_config, monkeypatch):
    plan, rw = _plan(monkeypatch, "Dumas novel about the diamond necklace", "en")
    assert rw.asked == [["de", "fr"]], "English is never sent to the translator"
    assert plan.variants("en") == ["Dumas novel about the diamond necklace"]
    assert plan.variants("de") == ["[de] rewritten"]
    assert "en" not in plan.translations, "the response lists only what was translated"
    assert plan.status == "ok"


def test_the_guess_names_the_own_language_when_the_reading_does_not(isolated_config,
                                                                    monkeypatch):
    plan, rw = _plan(monkeypatch, "le roman de Dumas sur le collier de la reine", None)
    assert (plan.query_lang, plan.query_lang_by) == ("fr", "guess")
    assert rw.asked == [["de", "en"]]
    assert plan.variants("fr") == ["le roman de Dumas sur le collier de la reine"]


def test_unknown_language_keeps_todays_plan(isolated_config, monkeypatch):
    plan, rw = _plan(monkeypatch, "Hamlet Shakespeare", None)
    assert plan.query_lang is None
    assert rw.asked == [["de", "en", "fr"]]
    assert plan.variants("en") == ["[en] rewritten"]


def test_a_restriction_to_the_own_language_needs_no_translator(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups", lambda collection=None: ["de", "en"])
    rw = Rewriter()
    monkeypatch.setattr(llm, "translate_query", rw)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    plan = language_plan.plan("die Geschichte des Walfangs lang:de", [c], True)
    assert plan.only == ("de",) and plan.query_lang == "de"
    assert rw.asked == [] and plan.status == "ok", "nothing to translate is not 'unavailable'"
    assert plan.variants("de") == ["die Geschichte des Walfangs"]


def test_a_mixed_group_language_is_searched_as_typed_there(isolated_config, monkeypatch):
    plan, rw = _plan(monkeypatch, "阿喀琉斯的愤怒", "zh", groups=("cjk", "en"))
    assert "zh" not in rw.asked[0]
    assert plan.variants("cjk", ["ja", "zh"]) == ["[ja] rewritten", "阿喀琉斯的愤怒"]


def test_the_own_language_list_reaches_the_fusion_as_the_readers_own_words(
        isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _plan_, _ = _plan(monkeypatch, "Dumas novel about the diamond necklace", "en")
    ran = []
    monkeypatch.setattr(fts, "search", lambda group, q, limit=20, collection=None, langs=None:
                        ran.append((group, q)) or [{"chunk_id": f"g:{len(ran)}:0:1",
                                                    "book_id": len(ran), "source": f"fts:{group}",
                                                    "text": q}])
    monkeypatch.setattr(retrieval.embeddings, "search", lambda *a, **k: [])
    seen = {}
    real = retrieval._merge_rrf
    monkeypatch.setattr(retrieval, "_merge_rrf",
                        lambda lists, depth=None, **kw: seen.update(kw) or real(lists, depth, **kw))
    retrieval._search_one_collection(db.connect(c), c, "Dumas novel about the diamond necklace",
                                     10, "hybrid", True, [], _plan_, 50)
    assert ("en", "Dumas novel about the diamond necklace") in ran
    assert not any(q == "[en] rewritten" for _, q in ran)
    groups = [g for g, _ in ran]
    assert seen["own"] == {groups.index("en")}, "its list is the user's own words, first on a tie"
