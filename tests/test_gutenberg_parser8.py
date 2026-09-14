"""Gutenberg parser 8 — the operator's answers of 2026-09-14: the main
`date` is the physical edition's, PG's release date kept beside it; and
"if it is italic in the book text, keep it"."""
from pathlib import Path

from app.plugins.base import DocRef
from app.plugins.gutenberg import pg_epub
from app.plugins.gutenberg.source import GutenbergSource, edition_date
from tests.test_plugins_gutenberg_epub import PLATE, build, page


def _read(tmp_path: Path, body: str, css: str | None = None, images=()) -> str:
    extra = [("style.css", css.encode())] if css else []
    p = build(tmp_path / "b.epub", [("a.xhtml", page(body))], images=list(images) + extra)
    return pg_epub.read_clean(p)


# ---- italics ---------------------------------------------------------------

def test_italic_elements_read_as_underscores(tmp_path):
    assert _read(tmp_path, "<p>A <i>word</i>, <em>more words</em> and <cite>Hamlet</cite>.</p>") \
        == "A _word_, _more words_ and _Hamlet_."


def test_a_space_the_tag_enclosed_goes_outside_it(tmp_path):
    assert _read(tmp_path, "<p>A<i> word </i>here.</p>") == "A _word_ here."


def test_adjacent_runs_join_and_empty_runs_vanish(tmp_path):
    assert _read(tmp_path, "<p><i>Pride</i><i> and Prejudice</i> <i> </i>end</p>") \
        == "_Pride and Prejudice_ end"


def test_a_class_the_books_stylesheet_sets_in_italics(tmp_path):
    """PG's stage directions: 4,888 `scenedesc` spans in the 32-book sample."""
    css = ".scenedesc { font-style: italic; margin-left: 2em }"
    assert _read(tmp_path, '<p class="scenedesc">Enter HAMLET.</p><p>To be.</p>', css) \
        == "_Enter HAMLET._\n\nTo be."


def test_an_italic_block_is_italic_paragraph_by_paragraph(tmp_path):
    css = "div.scenedesc { font-style: italic }"
    assert _read(tmp_path, '<div class="scenedesc"><p>One.</p><p>Two.</p></div>', css) \
        == "_One._\n\n_Two._"


def test_italics_inside_italics_are_marked_once(tmp_path):
    css = ".scenedesc { font-style: italic }"
    assert _read(tmp_path, '<p class="scenedesc">Enter <i>the Ghost</i>.</p>', css) \
        == "_Enter the Ghost._"


def test_an_inline_style_and_a_documents_own_style_block(tmp_path):
    assert _read(tmp_path, '<p><span style="font-style: italic">So</span> it goes.</p>') \
        == "_So_ it goes."
    assert _read(tmp_path, '<style>.x{font-style:italic}</style><p><span class="x">Y</span>.</p>') \
        == "_Y_."


def test_a_line_break_inside_italics_closes_the_line(tmp_path):
    assert _read(tmp_path, "<p><i>line one<br/>line two</i></p>") == "_line one_\n_line two_"


def test_an_illustrated_initial_joins_its_italic_word(tmp_path):
    got = _read(tmp_path, '<p><img src="images/t.jpg" alt="T"/><i>HE</i> end</p>',
                images=[("images/t.jpg", PLATE)])
    assert got == "_THE_ end\n\n[Illustration: T]"


def test_a_self_closing_script_does_not_silence_the_document(tmp_path):
    """It counted as an open <script> that nothing closed."""
    assert _read(tmp_path, '<script src="x.js"/><p>kept</p>') == "kept"


def test_which_classes_a_stylesheet_sets_in_italics():
    css = ("p.verse, .epigraph { font-style: italic } .x:first-letter{font-style:italic} "
           "@media print { .y { font-style: oblique } } .z{color:red} /* .w{font-style:italic} */")
    assert pg_epub.italic_classes(css) == {"verse", "epigraph", "y"}


# ---- the date --------------------------------------------------------------

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dcterms="http://purl.org/dc/terms/"
         xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/">
  <pgterms:ebook rdf:about="ebooks/76">
    <dcterms:title>A Book</dcterms:title>
    <dcterms:issued rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2022-12-23</dcterms:issued>
    {marc}
  </pgterms:ebook>
</rdf:RDF>
"""


def _doc(tmp_path: Path, marc: str = "", header: str | None = None):
    d = tmp_path / "76"
    d.mkdir()
    (d / "pg76.rdf").write_text(RDF.replace("{marc}", marc), encoding="utf-8")
    if header is not None:
        (d / "pg76.txt").write_text(
            f"{header}\n\n*** START OF THE PROJECT GUTENBERG EBOOK A BOOK ***\n\n"
            "CHAPTER I\n\nText.\n\n*** END OF THE PROJECT GUTENBERG EBOOK A BOOK ***\n",
            encoding="utf-8")
    src = GutenbergSource()
    return src.parse(src.fetch(DocRef(source_id="gutenberg", native_id="76", locator=str(d))))


def test_the_date_is_the_physical_editions_and_the_release_date_stays(tmp_path):
    """gutenberg:69627, as PG writes it: 906, 907 and the 260 imprint."""
    doc = _doc(tmp_path, "<pgterms:marc260>  $aUnited States :$bDodge Publishing Company,"
                         "$c1901.</pgterms:marc260><pgterms:marc906>1901</pgterms:marc906>"
                         "<pgterms:marc907>US</pgterms:marc907>")
    assert doc.date == "1901"
    assert doc.extra["issued"] == "2022-12-23", "the day it entered PG, kept"
    assert doc.extra["date_source"] == "marc906"
    assert doc.extra["marc"]["907"] == ["US"]


def test_the_imprint_alone_gives_the_year(tmp_path):
    doc = _doc(tmp_path, "<pgterms:marc260>[S.l. : $b s.n.], $c 1853</pgterms:marc260>")
    assert (doc.date, doc.extra["date_source"]) == ("1853", "marc260")


def test_the_header_alone_gives_the_year(tmp_path):
    doc = _doc(tmp_path, header="Title: A Book\nOriginal publication: London: George Bell, 1851")
    assert (doc.date, doc.extra["date_source"]) == ("1851", "text_header")


def test_no_edition_date_is_null_never_the_release_date(tmp_path):
    doc = _doc(tmp_path, "<pgterms:marc260>$bDoubleday, Page &amp; Co..</pgterms:marc260>")
    assert doc.date is None and "date_source" not in doc.extra


def test_edition_date_reads_the_forms_the_catalogue_uses():
    assert edition_date({"260": ["$aLondon :$bBell, $cc1898."]}, {}) == ("1898", "marc260")
    assert edition_date({"260": ["$aParis, $c[1872?]"]}, {}) == ("1872", "marc260")
    assert edition_date({"906": ["18--"], "260": ["$c 1850"]}, {}) == ("1850", "marc260")
    assert edition_date({}, {"Original publication": "Dijon: Victor Lagier, 1841"}) \
        == ("1841", "text_header")
    assert edition_date({}, {}) == (None, None)
