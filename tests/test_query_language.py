"""The query's language without the chat model (05 §5.2, amended
2026-10-10; app/query_language.py).

Found live on 906f06b: the reading needs louise-chat-qwen3-8b, which the
node's runner evicts; without it the work's one hit was shown in the
query's language for 0.59 of found queries (0.72 before one hit per work,
0.85 with the reading). A deterministic guess from shipped word lists now
stands in when the reading is unavailable or names no language. On the 490
labelled queries: 86.9 % right, 0.8 % wrong, 12.2 % no guess (bare titles,
mostly); edition shown 0.57 → 0.79."""
import json

import pytest

from app import collections as collections_mod, language_plan, llm, query_language

HELD = {"en", "de", "fr", "it", "es", "pt", "nl", "fi", "hu", "sv", "da", "no", "ro",
        "la", "eo", "pl", "tl", "el", "ru", "zh", "ja", "ko"}


@pytest.mark.parametrize("text, want", [
    ("the anger of Achilles and his quarrel with Agamemnon", "en"),
    ("l'ira di Achille e la sua lite con Agamennone", "it"),
    ("der Zorn des Achilles und sein Streit mit Agamemnon", "de"),
    ("la colère d'Achille et sa querelle avec Agamemnon", "fr"),
    ("la ira de Aquiles y su disputa con Agamenón", "es"),
    ("de toorn van Achilles en zijn ruzie met Agamemnon", "nl"),
    ("Akhilleuksen viha ja hänen riitansa Agamemnonin kanssa", "fi"),
    ("η οργή του Αχιλλέα", "el"),
    ("гнев Ахиллеса и его ссора с Агамемноном", "ru"),
    ("阿喀琉斯的愤怒", "zh"),
    ("アキレウスの怒り", "ja"),
])
def test_a_query_names_its_language(text, want):
    assert query_language.guess(text, HELD) == want


@pytest.mark.parametrize("text", ["Hamlet Shakespeare", "Kalevala", "Anna Karenina Tolstoy", ""])
def test_a_bare_title_says_nothing(text):
    assert query_language.guess(text, HELD) is None


def test_only_a_held_language_is_named():
    assert query_language.guess("η οργή του Αχιλλέα", {"en", "fr"}) is None
    assert query_language.guess("la colère d'Achille et sa querelle avec Agamemnon",
                                {"en", "it"}) != "fr"


def test_two_close_languages_too_close_to_call_are_no_guess():
    # Danish and Norwegian share most function words
    assert query_language.guess("en roman om en mand og hans liv", {"da", "no"}) is None


def test_every_shipped_list_is_lowercase_words():
    lists = query_language.word_lists()
    assert {"en", "de", "fr", "it", "es"} <= set(lists)
    for code, words in lists.items():
        assert words and all(w == w.lower() and " " not in w for w in words), code


def test_the_guess_stands_in_when_the_reading_is_unavailable(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups",
                        lambda collection=None: ["en", "it"])
    monkeypatch.setattr(llm, "language_restriction", lambda q: None)
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {"en": "the white whale"})
    plan = language_plan.plan("la caccia alla balena bianca", [c], True)
    assert plan.status == "unavailable"
    assert (plan.query_lang, plan.query_lang_by) == ("it", "guess")


def test_the_reading_stays_first_when_it_answers(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups",
                        lambda collection=None: ["en", "it"])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"phrase": "", "only": [], "lang": "es"}))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {"en": "x"})
    plan = language_plan.plan("la caccia alla balena bianca", [c], True)
    assert (plan.query_lang, plan.query_lang_by) == ("es", "reading"), \
        "the model's reading wins, even against the guess"


def test_a_reading_that_names_no_language_is_completed_by_the_guess(isolated_config,
                                                                    monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups",
                        lambda collection=None: ["en", "de"])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"phrase": "", "only": []}))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {"en": "x"})
    plan = language_plan.plan("die Geschichte des Walfangs und der Wal", [c], True)
    assert (plan.query_lang, plan.query_lang_by) == ("de", "guess")


def test_without_translation_the_guess_still_names_the_language(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups", lambda collection=None: ["fr"])
    monkeypatch.setattr(llm, "language_restriction",
                        lambda q: (_ for _ in ()).throw(AssertionError("no model off")))
    plan = language_plan.plan("la chasse à la baleine et le capitaine", [c], False)
    assert (plan.query_lang, plan.query_lang_by) == ("fr", "guess")
    assert plan.translations == {}
