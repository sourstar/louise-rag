"""A record IN SEVERAL LANGUAGES (`mul`, wikidata's entity: one section per
language, headed by its code) is indexed section by section in each
language's group (fts.by_section_language). Found on the node 2026-10-24:
wikidata's 75.9 M records sat in `other`, unstemmed, and every search
limited to a language skipped them."""
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, fts


@pytest.fixture(autouse=True)
def two_languages(monkeypatch):
    """A wikidata-like plugin whose records carry an English and an Italian
    section (the node's wikidata declares `("en",)`)."""
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "wd-two", SimpleNamespace(languages=("en", "it")))


def _collection(tmp_path, plugin="wd-two"):
    return collections_mod.Collection(
        name="w", plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


ENTITY = [{"id": "wikidata:42:0:1", "chapter": "en",
           "text": "Douglas Adams. English writer and humorist, author of novels about hitchhiking."},
          {"id": "wikidata:42:1:1", "chapter": "it",
           "text": "Douglas Adams. Scrittore e umorista britannico, autore di romanzi."}]


def test_each_section_goes_to_its_language_and_is_stemmed_there(tmp_path):
    c = _collection(tmp_path)
    fts.index_book("mul", 42, ENTITY, c)
    assert set(fts.existing_groups(c)) == {"en", "it"}, "not `other`"
    hits = fts.search("en", "novel hitchhiker", limit=5, collection=c)   # stemmed: novels, hitchhiking
    assert [h["book_id"] for h in hits] == [42] and hits[0]["lang"] == "en"
    assert [h["book_id"] for h in fts.search("it", "romanzo", limit=5, collection=c)] == [42]
    fts.close_all()


def test_only_a_language_the_plugin_declares_routes_a_section(tmp_path):
    """Two or three letters are not a language: pubmed's structured abstracts
    head a section "AIM", gutenberg's chapters "II" — kept `mul` in `other`,
    as before, in a plugin that declares languages and in one that does not."""
    for plugin in ("wd-two", "pubmed"):
        c = _collection(tmp_path / plugin, plugin=plugin)
        fts.index_book("mul", 7, [{"id": "x:7:0:1", "chapter": "AIM", "text": "a multilingual aim"},
                                  {"id": "x:7:1:1", "chapter": "II", "text": "a second chapter"},
                                  {"id": "x:7:2:1", "chapter": "Abstract", "text": "an abstract"}], c)
        assert fts.existing_groups(c) == ["other"], plugin
        assert {h["lang"] for h in fts.search("other", "multilingual chapter abstract", limit=5, collection=c)} \
            == {"mul"}, plugin
        fts.close_all()


def test_a_language_s_history_section_is_that_language_s_text(tmp_path):
    """wikidata parser 2: `en:history` goes where `en` goes, stemmed as
    English; a part named after a language the plugin does not declare
    stays `mul`, in `other`."""
    history = [{"id": "wikidata:42:2:1", "chapter": "en:history",
                "text": "Douglas Adams. Other statements include:\n- employer: BBC Radios (end time: 1978)"}]
    assert fts.by_section_language("mul", ENTITY + history, ("en", "it")) == [
        ("en", [ENTITY[0], history[0]]), ("it", [ENTITY[1]])]
    assert fts.by_section_language("mul", [{**history[0], "chapter": "xx:history"}], ("en", "it")) == [
        ("mul", [{**history[0], "chapter": "xx:history"}])]
    c = _collection(tmp_path)
    fts.index_book("mul", 42, ENTITY + history, c)
    assert set(fts.existing_groups(c)) == {"en", "it"}
    assert [h["book_id"] for h in fts.search("en", "radio employer", limit=5, collection=c)] == [42], "stemmed"
    fts.close_all()


def test_the_node_s_wikidata_declares_english_alone():
    assert fts.section_languages(SimpleNamespace(plugin="wikidata")) == ("en",)
    assert fts.section_languages(SimpleNamespace(plugin="gutenberg")) == ()


def test_re_indexing_is_idempotent_and_a_delete_removes_every_part(tmp_path):
    c = _collection(tmp_path)
    fts.index_book("mul", 42, ENTITY, c)
    fts.index_book("mul", 42, ENTITY, c)
    assert len(fts.search("en", "Douglas Adams", limit=10, collection=c)) == 1
    fts.delete_book("mul", 42, c)
    assert fts.search("en", "Douglas", limit=5, collection=c) == []
    assert fts.search("it", "Douglas", limit=5, collection=c) == []
    fts.close_all()


def test_every_other_record_is_indexed_as_before():
    assert fts.by_section_language("en", ENTITY, ("en", "it")) == [("en", ENTITY)]
    assert fts.by_section_language("und", ENTITY, ("en", "it")) == [("und", ENTITY)]
    assert fts.by_section_language("mul", ENTITY, ()) == [("mul", ENTITY)], "no declared languages: as before"


def test_a_limited_search_keeps_multilingual_records_in_the_legs_that_filter_by_record(monkeypatch):
    """The catalogue and vector legs filter by the RECORD's language: a `mul`
    record is kept when its plugin writes one of the asked languages
    (wikidata: English), and only then."""
    from app import retrieval
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "wd-test", SimpleNamespace(languages=("en",)))
    wd = SimpleNamespace(plugin="wd-test")
    assert retrieval.limited_langs(wd, ("en",)) == ("en", "mul")
    assert retrieval.limited_langs(wd, ("it",)) == ("it",), "an entity written in English is not Italian"
    assert retrieval.limited_langs(wd, ()) == (), "unlimited stays unlimited"
    assert retrieval.limited_langs(SimpleNamespace(plugin="gutenberg"), ("en",)) == ("en",)
