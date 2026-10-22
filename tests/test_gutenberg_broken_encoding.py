"""Parser 10 (operator, 2026-10-22): a book whose catalogue entry says its
encoding is broken is not indexable — pg673, the 1913 Webster in raw GCIDE
markup, showed on the Ask page as code — and comes back by itself once a
release of its catalogue entry no longer says so."""
import pytest

from app.plugins.base import DocRef, UnindexableDocument
from app.plugins.gutenberg import SOURCE, source
from tests.test_gutenberg_promise9 import RDF

NOTE = "This book's encoding is broken, and will not be fixed."


def _book(tmp_path, description: str):
    (tmp_path / "pg76.rdf").write_text(
        RDF.replace("<dcterms:description>Wikipedia page on this work.</dcterms:description>",
                    f"<dcterms:description>{description}</dcterms:description>"), encoding="utf-8")
    (tmp_path / "pg76.txt").write_text(
        "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n"
        "<h1>A</h1>\n<hw>A</hw> <def>The first letter.</def>\n", encoding="utf-8")
    return DocRef(source_id="gutenberg", native_id="76", locator=str(tmp_path))


def test_a_book_declared_broken_is_not_indexable_and_says_why(tmp_path):
    ref = _book(tmp_path, NOTE)
    with pytest.raises(UnindexableDocument) as e:
        SOURCE.parse(SOURCE.fetch(ref))
    assert NOTE in str(e.value) and "readmitted" in str(e.value)


def test_the_book_comes_back_when_the_note_goes(tmp_path):
    """The record's hash covers the catalogue entry (parser 9), so the
    release that drops the note is a change the next scan rebuilds."""
    broken = SOURCE.fetch(_book(tmp_path, NOTE))
    fixed_ref = _book(tmp_path, "Wikipedia page on this work.")
    fixed = SOURCE.fetch(fixed_ref)
    assert fixed.raw_hash != broken.raw_hash, "the catalogue's change is seen"
    assert SOURCE.parse(fixed).title


@pytest.mark.parametrize("text, hit", [
    (NOTE, True),
    ("this eBook’s encoding is broken", True),
    ("Note: the text encoding is broken in places.", True),
    ("Encoding: UTF-8. Wikipedia page on this work.", False),
    ("A study of broken codes and their encoding.", False),
])
def test_the_note_is_matched_on_its_words(text, hit):
    assert bool(source.BROKEN_ENCODING.search(text)) is hit


def test_the_parser_moved():
    assert SOURCE.PARSER_VERSION == 10
