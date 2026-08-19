"""Greek is stemmed by PyStemmer, not by tantivy's built-in filter.

tantivy compiles rust-stemmers 1.2.0 — the last release of a crate dormant
since November 2019 — whose Greek algorithm panics on ordinary words, and a
panic kills the whole document's indexing. PyStemmer wraps libstemmer from
the Snowball project itself, stems the word that crashes rust-stemmers, and
agrees with it everywhere it works.
"""
import threading

import pytest

from app import config, fts, stemming


def test_the_upstream_panic_is_real_and_specific():
    """Pinned so nobody hands Greek back to tantivy without re-checking:
    this is tantivy's own API, no code of ours in the path. If a future
    wheel fixes rust-stemmers, THIS test fails — which is the signal that
    the workaround can go."""
    import tantivy

    def stem(word):
        b = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        b = b.filter(tantivy.Filter.lowercase())
        b = b.filter(tantivy.Filter.stemmer("greek"))
        return b.build().analyze(word)

    with pytest.raises(BaseException) as excinfo:
        stem("παγαινετε")
    assert "out of bounds" in str(excinfo.value)
    assert stem("πηγαινετε") == ["πηγαινετ"]


def test_pystemmer_stems_what_rust_stemmers_crashes_on():
    assert stemming.stem_word("greek", "παγαίνετε")      # no exception, non-empty
    assert stemming.stem_word("greek", "παγαινετε")


def test_pystemmer_agrees_with_rust_stemmers_where_it_works():
    """Same Snowball algorithm — a different answer would mean we quietly
    swapped the language's stemming behaviour, not just its implementation."""
    for word, expected in (("πηγαινετε", "πηγαινετ"), ("ανθρωπος", "ανθρωπ"),
                            ("βιβλιο", "βιβλι"), ("καλημέρα", "καλημερ"),
                            ("άνθρωπος", "ανθρωπ")):
        assert stemming.stem_word("greek", word) == expected, word


def test_greek_is_routed_to_pystemmer_and_keeps_its_own_index():
    assert "el" in config.PYSTEM_LANGS
    assert fts.pystem_algorithm("el") == "greek"
    assert fts.group_for("el") == "el"
    # and tantivy is told NOT to stem it a second time
    assert fts._analyzer_for("el").analyze("παγαίνετε") == ["παγαίνετε"]


def test_other_languages_still_stem_inside_tantivy():
    assert fts.pystem_algorithm("en") is None
    assert fts._analyzer_for("en").analyze("running") == ["run"]
    assert fts._analyzer_for("de").analyze("Bücher") == ["buch"]


def test_only_pystem_groups_carry_the_extra_field(isolated_config, tmp_path,
                                                   monkeypatch):
    """Scoping body_stem per group is what keeps adoption to ONE re-index
    instead of every index on the node. tantivy's Schema exposes no
    introspection, so this is asserted the way it actually bites: an index
    built under one schema refuses to open under the other."""
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    doc = [{"id": "a:0:1", "seq": 0, "chapter": "", "text": "word", "tokens": 1}]
    fts.index_book("el", 1, doc, c)          # el: schema WITH body_stem
    fts.index_book("en", 2, doc, c)          # en: schema WITHOUT it
    fts._indexes.clear()

    monkeypatch.setattr(config, "PYSTEM_LANGS", {"en"})   # swap who uses it
    with pytest.raises(RuntimeError, match="different schema"):
        fts.get_index("el", c)               # el's index HAD the field
    with pytest.raises(RuntimeError, match="different schema"):
        fts.get_index("en", c)               # en's index LACKED it


def test_stem_text_is_tokenized_lowercased_and_joined():
    assert stemming.stem_text("greek", "Τα ΒΙΒΛΙΑ, τα βιβλίο!") \
        == " ".join(stemming.stem_word("greek", w)
                    for w in ("τα", "βιβλια", "τα", "βιβλίο"))
    assert stemming.stem_text("greek", "   ") == ""


def test_each_thread_gets_its_own_stemmer():
    """PyStemmer objects hold C state and are documented as not
    thread-safe; the worker stems from several threads at once."""
    seen = {}
    def grab(name):
        seen[name] = id(stemming._stemmer("greek"))
    threads = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen.values())) == 3, "stemmers must not be shared"


def test_a_greek_document_indexes_and_matches_an_INFLECTED_query(
        isolated_config, tmp_path):
    """The capability this buys back: before PyStemmer, Greek was indexed
    unstemmed and only an exact form matched."""
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    chunks = [{"id": "gutenberg:1:0:1", "seq": 0, "chapter": "",
               "text": "να παγαίνετε στο σχολείο κάθε μέρα με τα βιβλία",
               "tokens": 9}]
    fts.index_book("el", 1, chunks, c)          # must not raise

    assert fts.search("el", "σχολείο", collection=c), "exact form"
    # σχολείο and σχολεία both stem to σχολει, so the inflected query hits.
    # (Snowball collapses SOME inflections, not all: βιβλίο -> βιβλι while
    # βιβλία -> βιβλ, so that pair legitimately does not match. Stemming is
    # a recall aid, not a morphological oracle.)
    assert fts.search("el", "σχολεία", collection=c), \
        "a different inflection must match — that is what stemming is for"
    # the snippet must still be the ORIGINAL text, not the stems
    hit = fts.search("el", "σχολείο", collection=c)[0]
    assert "σχολείο" in hit["text"]


def test_a_schema_mismatch_says_what_to_do(isolated_config, tmp_path, monkeypatch):
    """Changing PYSTEM_LANGS invalidates that group's index; search()
    swallows exceptions, so the error has to be actionable when it is
    raised."""
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    fts.index_book("el", 1, [{"id": "a:0:1", "seq": 0, "chapter": "",
                               "text": "βιβλίο", "tokens": 1}], c)
    fts._indexes.clear()
    monkeypatch.setattr(config, "PYSTEM_LANGS", set())   # el leaves the set

    with pytest.raises(RuntimeError, match="different schema"):
        fts.get_index("el", c)
