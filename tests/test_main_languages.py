"""A query is translated only into a collection's MAIN languages (05 §5.2,
amended 2026-10-10).

Measured offline on the captured gutenberg legs
(docs/notes/one-language-per-work.md, "Do we need query translation?"):
translating into en, fr, fi, de alone — 92 % of the books — and not
searching the other one-language indexes by text beat translating into all
nineteen; this rule, re-scored: descriptive MRR 0.231 → 0.245, R@50 0.605
→ 0.630 (+74/−33), titles R@10 0.769 → 0.821, R@50 0.897 → 0.949. An index searched as typed in place of a
translation always lost. The main languages are the index groups, largest
first by DOCUMENTS, that hold `[ask] translate_share` of the collection;
the query's own language is still searched as typed, the mixed groups as
before, and the vector leg covers the rest."""
import pytest

from app import (collections as collections_mod, db, fts, language_plan, llm,
                 platform_config, retrieval)

#: gutenberg's books per text index group (docs/notes/one-language-per-work.md
#: §2's table; `other` and `cjk` summed from the rest)
GUTENBERG = {"en": 63131, "fr": 4219, "fi": 3690, "de": 2469, "nl": 1113, "it": 1111,
             "es": 899, "hu": 654, "pt": 650, "sv": 253, "el": 216, "da": 85, "no": 23,
             "ru": 9, "cjk": 458, "other": 520}


def test_the_main_languages_are_the_largest_groups_holding_the_share():
    assert language_plan.main_of(GUTENBERG, 0.90) == ("en", "fr", "fi", "de"), \
        "the measured big4: en+fr+fi is 89 %, de takes it past 90 %"
    assert language_plan.main_of(GUTENBERG, 0.80) == ("en", "fr")
    assert language_plan.main_of(GUTENBERG, 1.0) == tuple(
        g for g, _ in sorted(GUTENBERG.items(), key=lambda kv: -kv[1])
        if g not in ("cjk", "other")), "1.0: every one-language group, as before"


def test_a_mixed_group_counts_in_the_walk_and_is_never_a_main_language():
    assert language_plan.main_of({"other": 60, "en": 35, "fr": 5}, 0.90) == ("en",)
    assert language_plan.main_of({"en": 9, "fr": 1, "de": 0}, 1.0) == ("en", "fr"), \
        "an empty index is never needed"


def test_no_count_narrows_nothing():
    assert language_plan.main_of({"fr": 0, "en": 0, "other": 0}, 0.9) == ("en", "fr")
    assert language_plan.main_of({}, 0.9) == ()


def _chunks(book_id: int, n: int = 1, text: str = "whale hunt") -> list[dict]:
    return [{"id": f"gutenberg:{book_id}:{i}:1", "book_id": book_id, "seq": i, "chapter": "",
             "text": f"{text} {i}", "tokens": 3} for i in range(n)]


def _dominant(c) -> None:
    """en 30 books; de 2; fr ONE book of 40 chunks; hu 1; la (other) 1. By
    documents en+de hold 32/35 = 91 %; by chunks fr would come first."""
    entries = [("en", n, _chunks(n)) for n in range(1, 31)]
    entries += [("de", 31, _chunks(31)), ("de", 32, _chunks(32)),
                ("fr", 33, _chunks(33, 40)), ("hu", 34, _chunks(34)), ("la", 35, _chunks(35))]
    fts.index_books(entries, c)


def test_the_groups_are_counted_in_documents_not_chunks(isolated_config):
    c = collections_mod.default_collection()
    _dominant(c)
    assert fts.group_documents(c) == {"de": 2, "en": 30, "fr": 1, "hu": 1, "other": 1}
    assert fts.group_sizes(c)["fr"] == 40
    assert language_plan.main_languages(c, 0.90) == ("en", "de")


def _search(monkeypatch, c, query: str, lang: str | None) -> tuple[list, list]:
    """The text leg of one search: (the groups searched with what, the
    languages sent to the translator)."""
    monkeypatch.setattr(llm, "language_restriction", lambda q: llm.Reading([], q, lang))
    asked: list = []
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: asked.append(sorted(langs)) or
                        {lang_: f"[{lang_}] {q}" for lang_ in langs})
    ran: list = []
    real = fts.search
    monkeypatch.setattr(fts, "search", lambda group, q, *a, **kw:
                        ran.append((group, q)) or real(group, q, *a, **kw))
    retrieval._search_one_collection(db.connect(c), c, query, 5, "fts", True, set())
    return sorted(set(ran)), asked


def test_a_dominant_language_collection_is_translated_into_its_main_languages_only(
        isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    ran, asked = _search(monkeypatch, c, "la chasse à la baleine", "fr")
    assert asked == [["de", "en"]], "the main languages; never the query's own"
    assert ran == [("de", "[de] la chasse à la baleine"), ("en", "[en] la chasse à la baleine"),
                   ("fr", "la chasse à la baleine"), ("other", "la chasse à la baleine")], \
        "French (not main) is the query's own: searched as typed; Hungarian not at all"


def test_a_small_index_is_not_searched_by_the_text_leg(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    ran, asked = _search(monkeypatch, c, "whale hunt", "en")
    assert asked == [["de"]]
    assert {g for g, _ in ran} == {"de", "en", "other"}, "fr and hu are left to the vector leg"
    assert ("en", "whale hunt") in ran


def test_an_unknown_query_language_translates_into_the_main_languages(isolated_config,
                                                                       monkeypatch):
    """No language named and none guessed: no group is the query's own, so
    nothing one-language is searched as typed — as before — and only the
    main languages are searched, by their translations."""
    c = collections_mod.default_collection()
    _dominant(c)
    ran, asked = _search(monkeypatch, c, "Hamlet Shakespeare", None)
    assert asked == [["de", "en"]]
    assert ran == [("de", "[de] Hamlet Shakespeare"), ("en", "[en] Hamlet Shakespeare"),
                   ("other", "Hamlet Shakespeare")]


def test_a_restriction_translates_into_what_it_names_main_or_not(isolated_config,
                                                                 monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: {lang_: f"[{lang_}] {q}" for lang_ in langs})
    plan = language_plan.plan("whale lang:hu", [c], True)
    assert plan.translations == {"hu": "[hu] whale"} and plan.main == ()
    assert plan.main_for(c) is None and plan.variants("hu") == ["[hu] whale"]


def test_every_language_asked_for_is_not_narrowed(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    monkeypatch.setattr(llm, "language_restriction", lambda q: llm.Reading([], q, "en"))
    asked: list = []
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: asked.append(sorted(langs)) or {x: q for x in langs})
    plan = language_plan.plan("whale", [c], True, every=True)
    assert asked == [["de", "fr", "hu", "la"]] and plan.main == ()


def test_the_response_names_the_main_languages_and_lists_only_their_translations(
        isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, h: None)
    monkeypatch.setattr(retrieval, "_one_hit_per_work", lambda ranked, c, plan, show_in: ranked)
    monkeypatch.setattr(llm, "language_restriction", lambda q: llm.Reading([], q, "en"))
    monkeypatch.setattr(llm, "translate_query",
                        lambda q, langs: {lang_: f"[{lang_}] {q}" for lang_ in langs})
    out = retrieval.search(db.connect(c), "whale hunt", k=5, mode="fts", collection=c)
    assert out["main_languages"] == ["en", "de"]
    assert out["translations"] == {"de": "[de] whale hunt"}


def test_translate_share_is_platform_toml_ask(isolated_config, tmp_path):
    p = tmp_path / "platform.toml"
    p.write_text("[ask]\ntranslate_share = 0.5\n", encoding="utf-8")
    assert platform_config._load_one(p).ask.translate_share == 0.5
    p.write_text("[ask]\n", encoding="utf-8")
    assert platform_config._load_one(p).ask.translate_share == 0.90, "07 §7.8's default"
    p.write_text("[ask]\ntranslate_share = 1\n", encoding="utf-8")
    assert platform_config._load_one(p).ask.translate_share == 1.0
    for bad in ("0", "1.5", "-0.2", '"most"', "true"):
        p.write_text(f"[ask]\ntranslate_share = {bad}\n", encoding="utf-8")
        with pytest.raises(Exception, match="translate_share"):
            platform_config._load_one(p)


def test_the_node_s_share_decides_the_main_languages(isolated_config, config_dir):
    c = collections_mod.default_collection()
    _dominant(c)
    assert language_plan.main_languages(c) == ("en", "de")
    (config_dir / "platform.toml").write_text("[ask]\ntranslate_share = 1.0\n",
                                              encoding="utf-8")
    assert language_plan.translate_share() == 1.0
    assert language_plan.main_languages(c) == ("en", "de", "fr", "hu")


def test_an_unchanged_index_is_never_recounted(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    _dominant(c)
    assert language_plan.main_languages(c, 0.9) == ("en", "de")
    counted: list = []
    real = fts.group_documents
    monkeypatch.setattr(fts, "group_documents", lambda col=None: counted.append(1) or real(col))
    assert language_plan.main_languages(c, 0.9) == ("en", "de")
    assert counted == [], "the cache holds while no index changed"
    monkeypatch.setattr(language_plan, "_MAIN_TTL_S", 0.0)
    fts.index_books([("fr", 40 + n, _chunks(40 + n)) for n in range(40)], c)
    assert language_plan.main_languages(c, 0.9) == ("fr", "en")
    assert counted == [1], "a changed index is recounted once its bound has passed"
