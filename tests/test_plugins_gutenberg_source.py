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


# ---- the rule takes the first form that can be READ (parser version 5) ----

def _epub(path, body="<p>Real text.</p>"):
    """A minimal but real epub, via the builder the epub tests use."""
    from tests.test_plugins_gutenberg_epub import build, page
    return build(path, [("c1.xhtml", page(body))])


def _rdf(book, book_id):
    (book / f"pg{book_id}.rdf").write_text(
        '<?xml version="1.0"?><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dcterms="http://purl.org/dc/terms/">'
        f'<pgterms:ebook rdf:about="ebooks/{book_id}" '
        'xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/">'
        f'<dcterms:title>Book {book_id}</dcterms:title>'
        '</pgterms:ebook></rdf:RDF>', encoding="utf-8")


def test_a_zero_byte_images_epub_is_not_an_epub(tmp_path):
    """Live on the node 2026-09-12: nine books carry a ZERO-BYTE
    `pg<id>-images.epub` dated 2009, beside a perfectly good plain one.
    Version 4 took the first file that EXISTED and `fts` died on
    gutenberg:2215 with `BadZipFile` — a stage-prefixed error, so the
    Library counted the book as blocked and it burned a retry.

    Version 5 takes the first form that can be READ."""
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "2215"
    book.mkdir()
    (book / "pg2215-images.epub").write_bytes(b"")      # the origin's own
    _epub(book / "pg2215.epub")
    _rdf(book, 2215)

    form, path, stub = pick_form(book, 2215)
    assert (form, stub) == ("epub", False)
    assert path.name == "pg2215.epub", "the empty one is not a candidate"


def test_an_epub_that_is_not_a_zip_falls_through_to_the_text(tmp_path):
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "31"
    book.mkdir()
    (book / "pg31-images.epub").write_bytes(b"not a zip at all, just bytes")
    (book / "pg31.txt.utf8").write_text("The plain edition.", encoding="utf-8")
    _rdf(book, 31)
    form, path, _ = pick_form(book, 31)
    assert form == "txt" and path.name == "pg31.txt.utf8"


def test_a_zip_without_a_container_is_not_an_epub(tmp_path):
    """`readable` asks the format's own question — is there a
    `META-INF/container.xml` naming a rootfile — not "does this unzip"."""
    import zipfile
    from app.plugins.gutenberg import pg_epub
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "32"
    book.mkdir()
    p = book / "pg32-images.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "a zip, but not an epub")
    assert pg_epub.readable(p) is False
    _rdf(book, 32)
    form, _path, _ = pick_form(book, 32)
    assert form == "rdf", "no readable epub and no text: the catalogue entry"


def test_empty_text_and_empty_rdf_are_not_forms_either(tmp_path):
    """Same rule, same reason: an empty file is not a published edition,
    whatever its extension."""
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "33"
    book.mkdir()
    (book / "pg33.txt.utf8").write_text("", encoding="utf-8")
    (book / "pg33.rdf").write_bytes(b"")
    assert pick_form(book, 33) == (None, None, False)


def test_the_same_form_is_chosen_by_parse_and_by_the_lazy_path(tmp_path):
    """If the two disagreed, the index and the records would describe
    different text for one document and nothing downstream could tell."""
    from app.plugins.base import DocRef
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "34"
    book.mkdir()
    (book / "pg34-images.epub").write_bytes(b"")
    _epub(book / "pg34.epub", "<p>The only real text.</p>")
    (book / "pg34.txt.utf8").write_text("A different edition.", encoding="utf-8")
    _rdf(book, 34)

    form, path, _ = pick_form(book, 34)
    assert form == "epub" and path.name == "pg34.epub"
    raw = SOURCE.fetch(DocRef(source_id="gutenberg", native_id="34",
                              locator=str(book)))
    assert raw.path == path
    doc = SOURCE.parse(raw)
    lazy = SOURCE.read_text(book, 34)
    assert "The only real text." in lazy
    assert "The only real text." in "\n".join(b for _, b in doc.sections)
    assert "A different edition." not in lazy


def test_a_broken_epub_self_heals_to_the_next_form(tmp_path):
    """The rule is applied at every read, so a file that stops being an
    epub stops being the form — no error row, the next form instead."""
    from app.plugins.gutenberg.source import pick_form
    book = tmp_path / "35"
    book.mkdir()
    p = _epub(book / "pg35-images.epub")
    (book / "pg35.txt.utf8").write_text("The plain edition.", encoding="utf-8")
    _rdf(book, 35)
    assert pick_form(book, 35)[0] == "epub"

    p.write_bytes(b"truncated to nothing useful")
    form, path, _ = pick_form(book, 35)
    assert form == "txt" and path.name == "pg35.txt.utf8"
    assert "The plain edition." in SOURCE.read_text(book, 35)


def test_an_epub_that_breaks_BETWEEN_the_choice_and_the_read_is_a_corpus_fact(
        tmp_path, monkeypatch):
    """The race the guard exists for: `pick_form` proved the file
    readable, and a sync replaced it before `read_clean` opened it. The
    read must raise UnindexableDocument — unprefixed, burning no retry —
    and never let BadZipFile escape as a STAGE error, which the Library
    counts as blocked (tests/test_unindexable_stability.py)."""
    import zipfile

    import pytest

    from app.plugins.base import UnindexableDocument
    from app.plugins.gutenberg import pg_epub
    book = tmp_path / "36"
    book.mkdir()
    _epub(book / "pg36-images.epub")
    _rdf(book, 36)

    def boom(_path):
        raise zipfile.BadZipFile("File is not a zip file")

    monkeypatch.setattr(pg_epub, "read_clean", boom)
    with pytest.raises(UnindexableDocument, match="not a readable epub"):
        SOURCE.read_text(book, 36)


def test_readable_asks_the_same_question_read_clean_will(tmp_path):
    """If the two could diverge on structure, a book would be CHOSEN as an
    epub and then fail to be read as one — which is the bug `readable`
    exists to remove, not to move one step later."""
    import zipfile
    from app.plugins.gutenberg import pg_epub
    good = _epub(tmp_path / "ok.epub")
    assert pg_epub.readable(good)
    assert pg_epub.read_clean(good).strip()

    # a container that names a rootfile which is not there
    broken = tmp_path / "broken.epub"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken, "w") as dst:
        for info in src.infolist():
            if info.filename != "OEBPS/content.opf":
                dst.writestr(info.filename, src.read(info.filename))
    assert pg_epub.readable(broken) is False


# ---------------- §9.5's `title` is "cleaned" ----------------

def test_marc_subfield_delimiters_leave_the_title():
    """4,648 of 79,286 titles on the reference node (5.86%, measured
    2026-09-12) carried a MARC subfield marker out of PG's RDF, almost all
    the `… : $b subtitle` form. §9.5 says `title` is cleaned."""
    from app.plugins.gutenberg.rdf import clean_title
    assert clean_title("Ties mikä lie! : $b Nelinäytöksinen huvinäytelmä") == \
        "Ties mikä lie! : Nelinäytöksinen huvinäytelmä", \
        "the marker goes; the ISBD colon before a subtitle is style, not artefact"
    assert clean_title("Autobiography of Friedrich Froebel $c translated by E. M.") == \
        "Autobiography of Friedrich Froebel translated by E. M."
    assert clean_title("Collected Works $v 3") == "Collected Works 3"


def test_a_dollar_amount_in_a_title_is_not_a_subfield_marker():
    """THE REASON THE PATTERN TAKES LETTERS AND NOT `[a-z0-9]`. `$1`, `$3`
    and `$5` all occur in this corpus and are MONEY, in real titles. A
    tidier pattern would mutilate Mark Twain to remove a delimiter."""
    from app.plugins.gutenberg.rdf import clean_title
    for title in ("The $100 Prize Essay on the Cultivation of the Potato",
                  "The $30,000 Bequest, and Other Stories",
                  "A $5 Note and What It Bought"):
        assert clean_title(title) == title, title


def test_clean_title_normalises_whitespace_and_leaves_prose_alone():
    from app.plugins.gutenberg.rdf import clean_title
    assert clean_title("  Moby   Dick \n or, The Whale  ") == "Moby Dick or, The Whale"
    assert clean_title("Pride and Prejudice") == "Pride and Prejudice"
    # a bare '$' that is not a marker survives
    assert clean_title("Dollars $ and Sense") == "Dollars $ and Sense"


def test_a_catalogue_revision_moves_the_origin_hash_and_the_change_signature(tmp_path):
    """Parser 9 (2026-10-11): PG revised pg23054.rdf — a translator's dates —
    without touching its epub. A hash over the text alone could not see it:
    records-vs-origin read the revision as a parser fault, and discovery,
    keyed on the text file's mtime and size, never rebuilt the record. The
    catalogue entry is now part of both."""
    import os
    book = tmp_path / "42"
    book.mkdir()
    (book / "pg42.txt").write_text("*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\nText.\n\n"
                                   "*** END OF THE PROJECT GUTENBERG EBOOK X ***\n", encoding="utf-8")
    _rdf(book, 42)

    def ref_and_hash():
        ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=tmp_path))
                   if r.native_id == "42")
        return ref, SOURCE.fetch(ref).raw_hash
    ref1, hash1 = ref_and_hash()
    rdf_file = book / "pg42.rdf"
    rdf_file.write_text(rdf_file.read_text(encoding="utf-8").replace("Book 42", "Book 42, revised"),
                        encoding="utf-8")
    later = ref1.mtime + 10
    os.utime(rdf_file, (later, later))
    ref2, hash2 = ref_and_hash()
    assert hash2 != hash1, "a catalogue revision is an origin revision"
    assert (ref2.mtime, ref2.size) != (ref1.mtime, ref1.size), "discovery sees it"


def test_a_catalogue_only_book_hashes_its_one_file_plainly(tmp_path):
    """With one origin file the hash is that file's blake2b-128, as parsers
    before 9 wrote — the RDF is not counted twice."""
    import hashlib
    book = tmp_path / "7"
    book.mkdir()
    _rdf(book, 7)
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=tmp_path)) if r.native_id == "7")
    data = (book / "pg7.rdf").read_bytes()
    assert SOURCE.fetch(ref).raw_hash == hashlib.blake2b(data, digest_size=16).hexdigest()
    assert ref.size == len(data)
