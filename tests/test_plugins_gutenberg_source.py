"""GutenbergSource.discover/fetch/parse in isolation from the orchestrator —
Protocol conformance and hash/parse correctness against the fixture corpus.
"""
from pathlib import Path

from app.plugins.base import DiscoverState
from app.plugins.gutenberg import SOURCE

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def test_discover_finds_all_fixture_books():
    refs = list(SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS)))
    native_ids = {r.native_id for r in refs}
    assert native_ids == {"10001", "10002", "10003"}
    for r in refs:
        assert r.error is None
        assert r.source_id == "gutenberg"
        assert r.mtime is not None and r.size is not None
        assert r.extra["txt_name"].startswith("pg")


def test_an_rdf_only_book_is_read_as_a_catalogue_entry(tmp_path):
    """The reading rule is EPUB, then TXT, then RDF (operator, 2026-09-11).

    Before it, a book holding only its `.rdf` was an error row saying "no
    txt file", which the Library renders as "the source has no usable
    content" — and ~1,199 Gutenberg entries have nothing else. They are
    now records: title, author and subjects with no `sections`. A
    catalogue entry is not a book, and it is a great deal more than an
    error that says the book does not exist.

    An EMPTY directory is still a transfer stub, not a document — see
    test_corpus_stubs.py."""
    book = tmp_path / "99999"
    book.mkdir()
    (book / "pg99999.rdf").write_text("<rdf/>", encoding="utf-8")
    refs = [r for r in SOURCE.discover(DiscoverState(corpus_root=tmp_path))
            if r.scope == "document"]
    assert len(refs) == 1
    assert refs[0].native_id == "99999"
    assert refs[0].error is None
    assert refs[0].extra["form"] == "rdf"


def test_fetch_hashes_content_deterministically():
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.native_id == "10001")
    raw1 = SOURCE.fetch(ref)
    raw2 = SOURCE.fetch(ref)
    assert raw1.raw_hash == raw2.raw_hash
    assert raw1.path is not None and raw1.path.is_file()


def test_parse_produces_canonical_doc():
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.native_id == "10001")
    raw = SOURCE.fetch(ref)
    doc = SOURCE.parse(raw)
    assert doc.uid == "gutenberg:10001"
    assert doc.title == "A Study in Fixtures"
    assert doc.creators == ["Fixture, Ada"]
    assert doc.language == "en"
    assert "Fixtures -- Fiction" in doc.subjects
    assert [s[0] for s in doc.sections] == ["CHAPTER I", "CHAPTER II"]
    assert doc.raw_hash == raw.raw_hash


def test_parse_french_book():
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.native_id == "10003")
    doc = SOURCE.parse(SOURCE.fetch(ref))
    assert doc.language == "fr"
    assert [s[0] for s in doc.sections] == ["Chapitre I", "Chapitre II"]


def test_read_metadata_and_chapters_helpers():
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=FIXTURE_CORPUS))
               if r.native_id == "10002")
    book_dir = Path(ref.locator)
    meta = SOURCE.read_metadata(book_dir, 10002)
    chapters = SOURCE.read_chapters(book_dir, 10002)
    assert meta["title"] == "A Plain Walk"
    assert len(chapters) == 1
    assert chapters[0][0] == "Section 1"  # flat-slicing fallback (no headings)
