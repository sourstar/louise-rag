"""tests/test_plugins_gutenberg_epub.py — reading a Gutenberg epub.

The epub is the origin form of the Gutenberg-with-plates collection
(operator decision, 2026-09-08) because the plates exist in no other form
we hold. These tests build real epub zips rather than mocking, because
every bug this module can have is a bug about a real file's structure.
"""
import hashlib
import zipfile
from pathlib import Path

import pytest

from app.plugins.gutenberg import pg_epub
from app.plugins.gutenberg.pg_epub import EpubError

PLATE = b"\xff\xd8\xff\xe0" + b"plate-bytes" * 40
OTHER = b"\x89PNG\r\n\x1a\n" + b"other-bytes" * 40


def build(path: Path, docs, images=(), *, spine=None, linear_no=()):
    """A minimal but REAL epub: container.xml, an OPF with a manifest and a
    spine, and the documents. `docs` is [(name, xhtml)]."""
    manifest, itemrefs = [], []
    order = spine if spine is not None else [n for n, _ in docs]
    for i, (name, _) in enumerate(docs):
        manifest.append(f'<item id="d{i}" href="{name}" '
                        f'media-type="application/xhtml+xml"/>')
    ids = {name: f"d{i}" for i, (name, _) in enumerate(docs)}
    for name in order:
        no = ' linear="no"' if name in linear_no else ""
        itemrefs.append(f'<itemref idref="{ids[name]}"{no}/>')
    for j, (name, _) in enumerate(images):
        manifest.append(f'<item id="i{j}" href="{name}" media-type="image/jpeg"/>')

    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata/>'
        f'<manifest>{"".join(manifest)}</manifest>'
        f'<spine>{"".join(itemrefs)}</spine>'
        '</package>'
    )
    container = (
        '<?xml version="1.0"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'version="1.0"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, xhtml in docs:
            z.writestr(f"OEBPS/{name}", xhtml)
        for name, raw in images:
            z.writestr(f"OEBPS/{name}", raw)
    return path


def page(body: str) -> str:
    return ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>t</title></head><body>{body}</body></html>")


# ---- the text -------------------------------------------------------------

def test_text_comes_out_in_spine_order_not_zip_order(tmp_path: Path):
    """Invariant 3: chunking is a pure function of the cleaned text, so the
    reading order must be the one the epub DECLARES. Zip order is a
    property of whoever wrote the file."""
    p = build(tmp_path / "b.epub",
              [("z.xhtml", page("<p>second</p>")),
               ("a.xhtml", page("<p>first</p>"))],
              spine=["a.xhtml", "z.xhtml"])
    assert pg_epub.read_clean(p) == "first\n\nsecond"


def test_a_non_linear_document_is_not_in_the_reading_order(tmp_path: Path):
    """`linear="no"` is auxiliary matter — a cover page, an ad — that the
    reading order excludes."""
    p = build(tmp_path / "b.epub",
              [("cover.xhtml", page("<p>cover</p>")),
               ("ch1.xhtml", page("<p>body</p>"))],
              linear_no=("cover.xhtml",))
    assert pg_epub.read_clean(p) == "body"


def test_paragraphs_are_separated_and_whitespace_inside_them_collapses(
        tmp_path: Path):
    """An epub carries its line breaks as markup, so the raw text is full of
    incidental newlines from indented XHTML. Collapsing them is what makes
    the output a function of the content and not of the generator's
    indentation."""
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<p>one   two\n      three</p>\n\n  <p>next\npara</p>"))])
    assert pg_epub.read_clean(p) == "one two three\n\nnext para"


def test_script_and_style_contribute_nothing(tmp_path: Path):
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<style>p{color:red}</style><p>kept</p><script>var x=1</script>"))])
    assert pg_epub.read_clean(p) == "kept"


def test_the_gutenberg_wrapper_is_cut_at_the_same_markers_as_the_txt(
        tmp_path: Path):
    """`pg_text` cuts on these words too, so the two editions at least agree
    on where the book begins."""
    p = build(tmp_path / "b.epub", [("a.xhtml", page(
        "<p>licence blurb</p>"
        "<p>*** START OF THE PROJECT GUTENBERG EBOOK A BOOK ***</p>"
        "<p>the actual book</p>"
        "<p>*** END OF THE PROJECT GUTENBERG EBOOK A BOOK ***</p>"
        "<p>trailing licence</p>"))])
    assert pg_epub.read_clean(p) == "the actual book"


def test_reading_is_deterministic(tmp_path: Path):
    """Invariant 3 in the form that matters: the same file read twice is the
    same text, byte for byte."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page("<p>one</p><div>two</div><h1>Three</h1>"))],
              images=[("images/p1.jpg", PLATE)])
    assert pg_epub.read_clean(p) == pg_epub.read_clean(p)


def test_malformed_markup_still_yields_text(tmp_path: Path):
    """Gutenberg's epubs span twenty years of generators. A book that will
    not parse is a book we cannot serve, so the parser is forgiving."""
    p = build(tmp_path / "b.epub", [("a.xhtml",
              "<html><body><p>unclosed<p>tags & bare ampersands</body>")])
    assert "unclosed" in pg_epub.read_clean(p)


# ---- the plates -----------------------------------------------------------

def test_an_image_leaves_its_caption_in_the_text(tmp_path: Path):
    """Decision 1: a figure is a member, but ITS CAPTION STAYS IN
    `sections` AND STAYS INDEXED — and `[Illustration: …]` is the
    convention Gutenberg's own plain text uses, so it reads the same way in
    both editions."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<p>before</p>'
                                '<img src="images/p1.jpg" alt="A ship"/>'
                                '<p>after</p>'))],
              images=[("images/p1.jpg", PLATE)])
    assert pg_epub.read_clean(p) == "before\n\n[Illustration: A ship]\n\nafter"


def test_an_image_with_no_alt_still_anchors(tmp_path: Path):
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg"/>'))],
              images=[("images/p1.jpg", PLATE)])
    assert pg_epub.read_clean(p) == "[Illustration]"


def test_figures_are_hashed_and_carry_their_caption(tmp_path: Path):
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="A ship"/>'))],
              images=[("images/p1.jpg", PLATE)])
    figs = pg_epub.figures(p)
    assert len(figs) == 1
    f = figs[0]
    assert f.sha256 == "sha256:" + hashlib.sha256(PLATE).hexdigest()
    assert f.bytes == len(PLATE)
    assert f.caption == "A ship"
    assert f.media_type == "image/jpeg"
    assert f.href == "OEBPS/images/p1.jpg"


def test_figures_come_in_reading_order_across_documents(tmp_path: Path):
    p = build(tmp_path / "b.epub",
              [("z.xhtml", page('<img src="images/p2.jpg" alt="second"/>')),
               ("a.xhtml", page('<img src="images/p1.jpg" alt="first"/>'))],
              images=[("images/p1.jpg", PLATE), ("images/p2.jpg", OTHER)],
              spine=["a.xhtml", "z.xhtml"])
    assert [f.caption for f in pg_epub.figures(p)] == ["first", "second"]
    assert [f.seq for f in pg_epub.figures(p)] == [0, 1]


def test_one_plate_used_twice_is_one_figure(tmp_path: Path):
    """The same bytes, the same hash, one entry — it is one plate however
    many times the book shows it."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="x"/>'
                                '<img src="images/p1.jpg" alt="x"/>'))],
              images=[("images/p1.jpg", PLATE)])
    assert len(pg_epub.figures(p)) == 1


def test_a_broken_image_reference_is_not_a_plate(tmp_path: Path):
    """It names nothing in the zip, so there are no bytes to carry. The
    caption still anchors in the text."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/missing.jpg" alt="gone"/>'))])
    assert pg_epub.figures(p) == []
    assert "[Illustration: gone]" in pg_epub.read_clean(p)


def test_a_book_with_no_plates_has_no_figures(tmp_path: Path):
    p = build(tmp_path / "b.epub", [("a.xhtml", page("<p>text only</p>"))])
    assert pg_epub.figures(p) == []


def test_the_bytes_of_a_plate_are_fetchable_by_href(tmp_path: Path):
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="x"/>'))],
              images=[("images/p1.jpg", PLATE)])
    f, = pg_epub.figures(p)
    assert pg_epub.read_bytes(p, f.href) == PLATE


# ---- refusals -------------------------------------------------------------

def test_a_zip_that_is_not_an_epub_is_refused(tmp_path: Path):
    p = tmp_path / "x.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not an epub")
    with pytest.raises(EpubError, match="not an epub"):
        pg_epub.read_clean(p)


def test_an_empty_spine_is_refused(tmp_path: Path):
    p = build(tmp_path / "b.epub", [("a.xhtml", page("<p>x</p>"))], spine=[])
    with pytest.raises(EpubError, match="empty spine"):
        pg_epub.read_clean(p)


# ---- finding the illustrated edition --------------------------------------

def test_only_the_images_edition_is_found(tmp_path: Path):
    """Gutenberg publishes `pg<id>.epub` without plates and
    `pg<id>-images.epub` with them. The plain one is NOT a fallback: a book
    without plates belongs to the other collection."""
    d = tmp_path / "1234"
    d.mkdir()
    (d / "pg1234.epub").write_bytes(b"x")
    assert pg_epub.find_epub(d, 1234) is None

    (d / "pg1234-images.epub").write_bytes(b"x")
    assert pg_epub.find_epub(d, 1234).name == "pg1234-images.epub"


def test_the_epub3_images_edition_is_preferred(tmp_path: Path):
    d = tmp_path / "1234"
    d.mkdir()
    (d / "pg1234-images.epub").write_bytes(b"x")
    (d / "pg1234-images-3.epub").write_bytes(b"x")
    assert pg_epub.find_epub(d, 1234).name == "pg1234-images-3.epub"


# ---- the point of the whole exercise --------------------------------------

def test_epub_text_differs_from_hard_wrapped_text(tmp_path: Path):
    """The reason the plates collection re-embeds, asserted rather than
    assumed. `pg_text.read_clean` preserves the source's line structure
    verbatim; Gutenberg's .txt is hard-wrapped and an epub's text is
    reflowable. So the two editions cannot produce the same `sections`,
    `content_hash` moves, and no existing vector applies.

    If this test ever fails, the cost model in
    docs/notes/decisions changes and so does the plan — which is why it is
    a test and not a comment.
    """
    from app.plugins.gutenberg import pg_text
    prose = ("It is a truth universally acknowledged, that a single man in "
             "possession of a good fortune, must be in want of a wife.")

    txt = tmp_path / "pg1.txt"
    txt.write_text("\n".join([prose[i:i + 70] for i in range(0, len(prose), 70)]),
                   encoding="utf-8")
    epub = build(tmp_path / "pg1-images.epub", [("a.xhtml", page(f"<p>{prose}</p>"))])

    assert pg_text.read_clean(txt) != pg_epub.read_clean(epub)
    assert pg_epub.read_clean(epub) == prose, "the epub gives it back unwrapped"


# ---- the cover is not a plate ---------------------------------------------

def build_with_cover(path, docs, images, *, how):
    """An epub that declares its cover the way Gutenberg does — EPUB 3's
    `properties="cover-image"` or EPUB 2's `<meta name="cover">`. Measured
    over 44,477 real illustrated epubs, both appear in almost equal numbers
    and ONE HUNDRED PER CENT use one of them."""
    ids = {name: f"d{i}" for i, (name, _) in enumerate(docs)}
    manifest = [f'<item id="d{i}" href="{n}" media-type="application/xhtml+xml"/>'
                for i, (n, _) in enumerate(docs)]
    meta = ""
    for j, (name, _) in enumerate(images):
        props = ' properties="cover-image"' if (how == "epub3" and j == 0) else ""
        manifest.append(f'<item id="i{j}" href="{name}" media-type="image/jpeg"{props}/>')
    if how == "epub2":
        meta = '<meta name="cover" content="i0"/>'
    spine = "".join(f'<itemref idref="{ids[n]}"/>' for n, _ in docs)
    opf = ('<?xml version="1.0"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           f'<metadata>{meta}</metadata>'
           f'<manifest>{"".join(manifest)}</manifest>'
           f'<spine>{spine}</spine>'
           '</package>')
    container = ('<?xml version="1.0"?>'
                 '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                 'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, xhtml in docs:
            z.writestr(f"OEBPS/{name}", xhtml)
        for name, raw in images:
            z.writestr(f"OEBPS/{name}", raw)
    return path


@pytest.mark.parametrize("how", ["epub3", "epub2"])
def test_the_declared_cover_is_not_a_plate(tmp_path: Path, how):
    """09 §9.5: a figure is "a plate, a diagram or a drawing the origin
    carries BESIDE THE TEXT". Gutenberg's cover is none of those — it is
    generated for its own catalogue, it is packaging, and it is present for
    nearly every book whether the book is illustrated or not."""
    p = build_with_cover(
        tmp_path / f"pg1-images-{how}.epub",
        [("a.xhtml", page('<img src="images/cover.jpg" alt="Cover"/>'
                          '<p>text</p>'
                          '<img src="images/plate1.jpg" alt="A ship"/>'))],
        [("images/cover.jpg", OTHER), ("images/plate1.jpg", PLATE)],
        how=how)
    figs = pg_epub.figures(p)
    assert [f.caption for f in figs] == ["A ship"], "the cover is excluded"
    assert figs[0].sha256 == "sha256:" + hashlib.sha256(PLATE).hexdigest()


@pytest.mark.parametrize("how", ["epub3", "epub2"])
def test_a_cover_only_book_yields_no_figures(tmp_path: Path, how):
    """Two thirds of Gutenberg's illustrated epubs are this. Excluding the
    cover is the difference between a plates collection of 44,000 books and
    one of the books that actually have plates."""
    p = build_with_cover(
        tmp_path / f"pg2-images-{how}.epub",
        [("a.xhtml", page('<img src="images/cover.jpg" alt="Cover"/><p>t</p>'))],
        [("images/cover.jpg", OTHER)],
        how=how)
    assert pg_epub.figures(p) == []


@pytest.mark.parametrize("how", ["epub3", "epub2"])
def test_the_cover_is_found_by_both_declarations(tmp_path: Path, how):
    """Gutenberg uses EPUB 3's `properties` and EPUB 2's `<meta name=cover>`
    in almost equal numbers, so reading only one would miss half the corpus."""
    p = build_with_cover(
        tmp_path / f"pg3-{how}.epub",
        [("a.xhtml", page("<p>t</p>"))],
        [("images/cover.jpg", OTHER)], how=how)
    with zipfile.ZipFile(p) as z:
        assert pg_epub.cover_href(z) == "OEBPS/images/cover.jpg"


def test_an_epub_that_declares_no_cover_keeps_every_image(tmp_path: Path):
    """The fixtures elsewhere in this file declare none, and every image in
    them is a plate. Absence of a declaration is not a reason to guess."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="x"/>'))],
              images=[("images/p1.jpg", PLATE)])
    with zipfile.ZipFile(p) as z:
        assert pg_epub.cover_href(z) is None
    assert len(pg_epub.figures(p)) == 1
