"""Gutenberg parser 7 — the text the fidelity audit found lost or altered
(2026-09-14, invariant 11). Each of these is text that becomes vectors, so
each had to be settled before gutenberg is embedded.

Every test here fails on parser 6.
"""
import zipfile
from pathlib import Path

import pytest

from app.plugins.base import DocRef, UnindexableDocument
from app.plugins.gutenberg import pg_epub, pg_text
from app.plugins.gutenberg.source import GutenbergSource
from tests.test_plugins_gutenberg_epub import build, page


def _clean(tmp_path, body) -> str:
    return pg_epub.read_clean(build(tmp_path / "b.epub", [("a.xhtml", page(body))]))


def test_an_exponent_keeps_its_value(tmp_path: Path):
    """"10<sup>6</sup>" read "106": the exponent fused into its base and
    the number changed. The plain edition writes ^{…} and _{…}."""
    assert _clean(tmp_path, "<p>E = mc<sup>2</sup> and H<sub>2</sub>O</p>") \
        == "E = mc^{2} and H_{2}O"


def test_a_line_is_not_a_stanza(tmp_path: Path):
    """Both used to be a paragraph break. A `<br>` is a line inside one."""
    assert _clean(tmp_path, '<div class="stanza"><span>one<br/></span><span>two</span></div>'
                            '<div class="stanza"><span>three</span></div>') \
        == "one\ntwo\n\nthree"


def test_a_scene_break_leaves_the_plain_editions_mark(tmp_path: Path):
    assert _clean(tmp_path, '<p>end of a scene</p><hr class="tb"/><p>the next</p>') \
        == "end of a scene\n\n* * * * *\n\nthe next"


def test_table_cells_keep_their_boundary(tmp_path: Path):
    assert _clean(tmp_path, "<table><tr><th>Name</th><th>Age</th></tr>"
                            "<tr><td>Tom</td><td>12</td></tr></table>") \
        == "Name | Age\n\nTom | 12"


def test_a_member_is_read_in_the_encoding_it_declares(tmp_path: Path):
    """Every member was decoded as UTF-8 with errors='replace': each
    non-ASCII character of a Latin-1 member became U+FFFD, for good."""
    xhtml = ('<?xml version="1.0" encoding="iso-8859-1"?>'
             '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>café</p></body></html>')
    p = build(tmp_path / "b.epub", [("a.xhtml", xhtml.encode("latin-1"))])
    assert pg_epub.read_clean(p) == "café"


def test_a_start_marker_missing_its_stars_cannot_eat_the_book(tmp_path: Path):
    """`.*?` under re.S ran on to the next `***` anywhere in the book."""
    text = _clean(tmp_path, "<p>licence</p>"
                            "<p>*** START OF THE PROJECT GUTENBERG EBOOK X</p>"
                            "<p>the book itself</p><p>*** a line of stars ***</p>")
    assert "the book itself" in text


def test_a_wrapped_marker_in_the_txt_still_cuts(tmp_path: Path):
    """The bound must not lose the ordinary case: a long title wraps the
    marker onto a second line in the .txt."""
    p = tmp_path / "pg1.txt"
    p.write_text("licence\n*** START OF THE PROJECT GUTENBERG EBOOK THE ADVENTURES OF\n"
                 "SHERLOCK HOLMES ***\n\nthe book\n\n*** END OF THE PROJECT GUTENBERG "
                 "EBOOK ***\ntrailer", encoding="utf-8")
    assert pg_text.read_clean(p) == "the book"


def test_an_epub_is_cut_at_its_own_headings(tmp_path: Path):
    """The chapter regex read verse lines and body paragraphs as chapter
    titles (8 of 32 sampled books). The book says where its sections are.
    Consecutive headings are one path; a lone "I" in the body stays body."""
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<p>Produced by X</p>"
        "<h2>BOOK I</h2><h3>CHAPTER I.<br/>THE START</h3><p>First para.</p><p>I</p>"
        "<h3>CHAPTER II</h3><p>Second.</p>"
        "<h2>THE END</h2>"))])
    sections, unplaced = pg_epub.sections(p)
    assert sections == [("Front matter", "Produced by X"),
                        ("BOOK I › CHAPTER I. — THE START", "First para.\n\nI"),
                        ("CHAPTER II", "Second.")]
    assert unplaced == ["THE END"]
    assert pg_epub.read_clean(p).startswith("Produced by X\n\nBOOK I\n\nCHAPTER I.\nTHE START")


def test_an_illustrated_heading_keeps_its_title_and_its_plate(tmp_path: Path):
    """Pride and Prejudice's chapter headings are pictures inside `<h2>`.
    The marker split the heading, and every chapter read "Text". The title
    is the heading's words, or the picture's alt text when it has none;
    the marker stays in the body, so the Nth marker is still figures[N]."""
    from tests.test_plugins_gutenberg_epub import PLATE, markers_in
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        '<h2><img src="images/c1.jpg" alt="Chapter I."/></h2><p>It is a truth.</p>'
        '<h2><img src="images/c2.jpg" alt="A ship"/>CHAPTER II.</h2><p>Mr. Bennet.</p>'
        # the live shape: picture, a caption paragraph, then the title
        '<h2><img src="images/c3.jpg" alt=""/><p>I hope he will like it.</p>CHAPTER III.</h2>'
        '<p>Not all that Mrs. Bennet.</p>'))],
        images=[("images/c1.jpg", PLATE), ("images/c2.jpg", PLATE), ("images/c3.jpg", PLATE)])
    sections, _ = pg_epub.sections(p)
    assert sections == [
        ("Chapter I.", "[Illustration: Chapter I.]\n\nIt is a truth."),
        ("CHAPTER II.", "[Illustration: A ship]\n\nMr. Bennet."),
        ("CHAPTER III.", "[Illustration]\n\nI hope he will like it.\n\nNot all that Mrs. Bennet.")]
    assert sum(markers_in(b) for _, b in sections) == len(pg_epub.figures(p)) == 3


def test_an_illustrated_initial_stays_in_its_word(tmp_path: Path):
    """`<img alt="T"/>HE discussion`: the marker cut "THE" in two. The
    letter goes back into the word; the marker follows its paragraph, and
    the markers stay in reading order with the figures."""
    from tests.test_plugins_gutenberg_epub import PLATE, markers_in
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        '<img src="images/p.jpg" alt="a plate"/>'
        '<p><span class="letra"><img src="images/t.jpg" alt="T"/></span>HE discussion ended.</p>'
        '<p>Then <img src="images/q.jpg" alt="Q"/> stood alone.</p>'))],
        images=[("images/p.jpg", PLATE), ("images/t.jpg", PLATE), ("images/q.jpg", PLATE)])
    text = pg_epub.read_clean(p)
    assert text == ("[Illustration: a plate]\n\nTHE discussion ended.\n\n[Illustration: T]"
                    "\n\nThen\n\n[Illustration: Q]\n\nstood alone.")
    assert markers_in(text) == len(pg_epub.figures(p)) == 3
    assert [f.caption for f in pg_epub.figures(p)] == ["a plate", "T", "Q"]


def test_without_heading_markup_a_verse_line_is_never_a_chapter(tmp_path: Path):
    """The regex fallback matches WHOLE paragraphs only. "I" and "Mild" are
    roman-numeral-shaped words; inside a stanza they are verse."""
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<p>Chapter 1</p><p>Text one.</p><div class='stanza'>I<br/>Mild<br/>sun</div>"
        "<p>Chapter 2</p><p>Text two.</p>"))])
    sections, _ = pg_epub.sections(p)
    assert sections == [("Chapter 1", "Text one.\n\nI\nMild\nsun"), ("Chapter 2", "Text two.")]


def test_an_epub_record_carries_its_pg_header_fields(tmp_path: Path):
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<p>Title: A Book</p><p>Author: Someone</p><p>Most recently updated: May 1, 2024</p>"
        "<p>*** START OF THE PROJECT GUTENBERG EBOOK A BOOK ***</p><p>body</p>"))])
    assert pg_epub.header_fields(p) == {"Title": "A Book", "Author": "Someone",
                                        "Most recently updated": "May 1, 2024"}


def _book(tmp_path: Path, n: int, docs) -> Path:
    d = tmp_path / str(n)
    d.mkdir()
    build(d / f"pg{n}-images.epub", docs)
    return d


def _parse(d: Path, n: int):
    src = GutenbergSource()
    ref = DocRef(source_id="gutenberg", native_id=str(n), locator=str(d))
    return src.parse(src.fetch(ref))


def test_the_record_and_the_index_cut_the_same_sections(tmp_path: Path):
    """`parse` (the record) and `read_chapters` (the fts and embed stages)
    must agree, or every chunk id names a chunk the record does not hold."""
    d = _book(tmp_path, 7, [("a.xhtml", page(
        "<h2>ONE</h2><p>a</p><h2>TWO</h2><p>b</p><h2>LAST</h2>"))])
    doc = _parse(d, 7)
    assert [tuple(s) for s in doc.sections] == [("ONE", "a"), ("TWO", "b")]
    assert GutenbergSource().read_chapters(d, 7) == [("ONE", "a"), ("TWO", "b")]
    assert doc.extra["form"] == "epub"
    assert doc.extra["text_toc"] == ["LAST"]


def test_a_damaged_member_is_unindexable_not_a_stage_error(tmp_path: Path):
    """zlib.error escaped as a stage error and burned the book's retries."""
    d = tmp_path / "8"
    d.mkdir()
    p = d / "pg8-images.epub"
    build(p, [("a.xhtml", page("<p>" + "words " * 400 + "</p>"))])
    # rewrite the chapter DEFLATED, then break its deflate stream's first
    # block header (BTYPE=11 is reserved): the container and the package
    # still read, so the reading rule picks the epub, and the chapter fails
    with zipfile.ZipFile(p) as z:
        members = {n: z.read(n) for n in z.namelist()}
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for n, b in members.items():
            z.writestr(n, b)
    with zipfile.ZipFile(p) as z:
        info = z.getinfo("OEBPS/a.xhtml")
    raw = bytearray(p.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[start] = 0xFF
    p.write_bytes(bytes(raw))
    with pytest.raises(UnindexableDocument):
        _parse(d, 8)
