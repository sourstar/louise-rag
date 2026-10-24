"""A record IN SEVERAL LANGUAGES (`mul`, wikidata's entity: one section per
language, headed by its code) is indexed section by section in each
language's group (fts.by_section_language). Found on the node 2026-10-24:
wikidata's 75.9 M records sat in `other`, unstemmed, and every search
limited to a language skipped them."""
from app import collections as collections_mod, fts


def _collection(tmp_path):
    return collections_mod.Collection(
        name="w", plugin="wikidata", profile=None, embed_mode_default=None,
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


def test_a_heading_that_names_no_language_keeps_the_record_in_other(tmp_path):
    c = _collection(tmp_path)
    fts.index_book("mul", 7, [{"id": "pubmed:7:0:1", "chapter": "Abstract", "text": "a multilingual abstract"}], c)
    assert fts.existing_groups(c) == ["other"]
    assert fts.search("other", "multilingual", limit=5, collection=c)[0]["lang"] == "mul"
    fts.close_all()


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
    assert fts.by_section_language("en", ENTITY) == [("en", ENTITY)]
    assert fts.by_section_language("und", ENTITY) == [("und", ENTITY)]
