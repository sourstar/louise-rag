"""Greek is indexed without the Snowball stemmer.

rust-stemmers 1.2.0 — the version compiled into the tantivy wheel, and the
last release of a crate dormant since 2019 — panics inside the Greek
algorithm on ordinary words. The panic kills the whole document's
indexing, and an unindexed document cannot be found at all, whereas an
unstemmed one is merely less forgiving about inflection.
"""
import pytest

from app import config, fts


def test_the_upstream_panic_is_real_and_specific():
    """Pinned so nobody 'restores' Greek stemming without re-checking:
    this is tantivy's own API, no code of ours in the path."""
    import tantivy

    def stem(word):
        b = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        b = b.filter(tantivy.Filter.lowercase())
        b = b.filter(tantivy.Filter.stemmer("greek"))
        return b.build().analyze(word)

    with pytest.raises(BaseException) as excinfo:
        stem("παγαινετε")
    assert "out of bounds" in str(excinfo.value)
    # the neighbouring word is fine, which is why only some texts trip it
    assert stem("πηγαινετε") == ["πηγαινετ"]


def test_greek_keeps_its_own_index_but_no_stemmer():
    assert "el" in config.STEMMER_LANGS, "the mapping stays, for the day it works"
    assert "el" in config.STEMMER_EXCLUDE
    assert fts.group_for("el") == "el", \
        "Greek must keep its own index — re-routing would orphan the old one"


def test_the_analyzer_greek_actually_gets_does_not_panic():
    analyzer = fts._analyzer_for("el")
    assert analyzer.analyze("παγαίνετε") == ["παγαίνετε"], \
        "unstemmed, lowercased, and above all: indexed"


def test_other_languages_still_stem():
    assert fts._analyzer_for("en").analyze("running") == ["run"]
    assert fts._analyzer_for("de").analyze("Bücher") == ["buch"]


def test_a_greek_document_indexes_end_to_end(isolated_config, tmp_path):
    """The regression that matters: the whole document used to fail."""
    from app import collections as collections_mod
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    chunks = [{"id": "gutenberg:1:0:1", "seq": 0, "chapter": "",
               "text": "να παγαίνετε στο σχολείο κάθε μέρα", "tokens": 8}]
    fts.index_book("el", 1, chunks, c)          # must not raise
    hits = fts.search("el", "σχολείο", collection=c)
    assert hits, "the Greek document is searchable"
