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


def test_one_plate_used_twice_is_two_appearances(tmp_path: Path):
    """SUPERSEDES `test_one_plate_used_twice_is_one_figure`, which asserted
    one entry. 09 §9.5 now makes `figures` a list of APPEARANCES: the same
    hash twice, two `seq`, and the container storing the bytes once.

    The reason is alignment. The text carries one marker per appearance,
    so deduplicating desynchronises `figures[N]` from the Nth marker for
    every plate after the first repeat — in 39 % of Gutenberg's
    illustrated books, silently."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="x"/>'
                                '<img src="images/p1.jpg" alt="y"/>'))],
              images=[("images/p1.jpg", PLATE)])
    figs = pg_epub.figures(p)
    assert [f.seq for f in figs] == [0, 1]
    assert figs[0].sha256 == figs[1].sha256, "one plate, stored once"
    assert [f.caption for f in figs] == ["x", "y"]


def test_a_broken_image_reference_leaves_no_marker(tmp_path: Path):
    """SUPERSEDES the assertion that the caption still anchors. It named
    nothing in the zip, so there is no picture and nothing should announce
    one — and a marker with no figure behind it would desynchronise every
    figure after it (09 §9.5). It drops from BOTH or from neither."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<p>before</p>'
                                '<img src="images/missing.jpg" alt="gone"/>'
                                '<p>after</p>'))])
    assert pg_epub.figures(p) == []
    assert pg_epub.read_clean(p) == "before\n\nafter"


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


# ---- the alignment the restructure exists for -----------------------------

def markers_in(text: str) -> int:
    import re
    return len(re.findall(r"\[Illustration(?::[^\]]*)?\]", text))


def test_the_nth_marker_is_figures_n(tmp_path: Path):
    """09 §9.5's rule, and the only reason a search result can say WHICH
    plates belong to the chunk it returned (05 §5.3).

    The fixture is deliberately the awkward case: a cover, a repeat and a
    broken reference in one book — the three things that desynchronised the
    two lists before `_walk` made them one walk."""
    p = build_with_cover(
        tmp_path / "pg9-images.epub",
        [("a.xhtml", page('<img src="images/cover.jpg" alt="Cover"/>'
                          '<p>one</p><img src="images/p1.jpg" alt="first"/>'
                          '<p>two</p><img src="images/gone.jpg" alt="broken"/>'
                          '<p>three</p><img src="images/p1.jpg" alt="again"/>'
                          '<p>four</p><img src="images/p2.jpg" alt="last"/>'))],
        [("images/cover.jpg", OTHER), ("images/p1.jpg", PLATE),
         ("images/p2.jpg", b"\xff\xd8" + b"third" * 50)],
        how="epub3")
    text = pg_epub.read_clean(p)
    figs = pg_epub.figures(p)
    assert markers_in(text) == len(figs) == 3, "cover and broken ref drop from both"
    assert [f.caption for f in figs] == ["first", "again", "last"]
    assert [f.seq for f in figs] == [0, 1, 2]
    assert figs[0].sha256 == figs[1].sha256, "the repeat is the same plate"
    assert "Cover" not in text and "broken" not in text


#: an SVG with what makes it a document rather than an image: a script and
#: an external reference (08 §8.7) — carried, never executed or followed
SVG = (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
       b'<image href="https://example.invalid/x.png"/></svg>')


def _png(width: int, height: int, tail: bytes = b"") -> bytes:
    import struct
    import zlib
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + tail)


#: over 08 §8.7's byte cap (16 MiB) and its megapixel cap (100 MP declared)
OVER_CAP = _png(10_000, 10_000, b"\0" * ((16 << 20) + 1))


def test_every_plate_is_carried_whatever_its_type_or_size(tmp_path: Path):
    """Parser 11 (operator, 2026-10-25: "Every plate"): invariant 11 wins
    over 08 §8.7's old refusal. An SVG, a GIF and a plate over the caps
    are carried like a jpeg — `carried: true`, the origin's bytes hashed —
    and only display applies the closed list (app/ask/figures.py). The
    refused SVG used to stay in the text and be named `carried: false`."""
    p = build(tmp_path / "b.epub",
              [("a.xhtml", page('<img src="images/p1.jpg" alt="kept"/>'
                                '<img src="images/d.svg" alt="diagram"/>'
                                '<img src="images/g.gif" alt="animated"/>'
                                '<img src="images/big.png" alt="huge"/>'))],
              images=[("images/p1.jpg", PLATE), ("images/d.svg", SVG),
                      ("images/g.gif", b"GIF89a" + b"gif" * 20), ("images/big.png", OVER_CAP)])
    figs = pg_epub.figures(p)
    assert [f.carried for f in figs] == [True] * 4
    assert [f.media_type for f in figs] == ["image/jpeg", "image/svg+xml", "image/gif", "image/png"]
    assert figs[1].sha256 == "sha256:" + hashlib.sha256(SVG).hexdigest() and figs[1].bytes == len(SVG)
    assert figs[3].bytes == len(OVER_CAP) > (16 << 20)
    assert [f.caption for f in figs] == ["kept", "diagram", "animated", "huge"]
    assert markers_in(pg_epub.read_clean(p)) == 4


def test_carrying_a_plate_moves_no_text(tmp_path: Path):
    """The chunk text must not change when an image becomes carried
    (parser 10 -> 11): the marker never depended on the type, so an SVG
    leaves exactly the marker a jpeg with the same caption leaves, and the
    sections, `content_hash` and every chunk stay as they were."""
    body = ('<h1>One</h1><p>Before.</p><img src="images/x.{ext}" alt="A plan of the town"/>'
            '<p>After.</p>')
    svg = build(tmp_path / "s.epub", [("a.xhtml", page(body.format(ext="svg")))],
                images=[("images/x.svg", SVG)])
    jpg = build(tmp_path / "j.epub", [("a.xhtml", page(body.format(ext="jpg")))],
                images=[("images/x.jpg", PLATE)])
    assert pg_epub.read_clean(svg) == pg_epub.read_clean(jpg) == \
        "One\n\nBefore.\n\n[Illustration: A plan of the town]\n\nAfter."
    assert pg_epub.sections(svg) == pg_epub.sections(jpg)


def test_the_container_holds_every_plate_byte_for_byte(tmp_path: Path):
    """The build's second pass (app/pack/build.py `_write_figures`) reads
    every carried plate back from the origin through the plugin and stores
    it as opaque bytes: the SVG with its script and the plate over both
    caps come back out of the container identical to what the epub holds."""
    from app.hold import figures as fig_mod
    from app.pack import build as pack_build
    from app.plugins.gutenberg.source import GutenbergSource

    book = tmp_path / "corpus" / "9"
    book.mkdir(parents=True)
    p = build(book / "pg9-images.epub",
              [("a.xhtml", page('<p>x</p><img src="images/p1.jpg" alt="kept"/>'
                                '<img src="images/d.svg" alt="diagram"/>'
                                '<img src="images/big.png" alt="huge"/>'
                                '<img src="images/d.svg" alt="again"/>'))],
              images=[("images/p1.jpg", PLATE), ("images/d.svg", SVG), ("images/big.png", OVER_CAP)])
    figs = pg_epub.figures(p)
    row = {"uid": "gutenberg:9", "path": str(book), "native_id": "9"}
    plates = {f.sha256: (row, f.href) for f in figs if f.carried}
    assert len(plates) == 3, "the repeat is stored once"
    pack = tmp_path / "pack"
    pack_build._write_figures(GutenbergSource(), pack, "lang=en", 1, plates, lambda *_: None)
    container = fig_mod.path_for(pack, "lang=en", 1)
    for raw in (PLATE, SVG, OVER_CAP):
        assert fig_mod.read_figure(container, hashlib.sha256(raw).hexdigest()) == raw
    assert fig_mod.verify(container) == (3, [])


def test_the_cover_drops_from_the_text_as_well_as_the_figures(tmp_path: Path):
    """It vanished from `figures` already; if its MARKER stayed, every
    figure after it would be off by one."""
    p = build_with_cover(
        tmp_path / "pg8-images.epub",
        [("a.xhtml", page('<img src="images/cover.jpg" alt="Cover"/>'
                          '<p>body</p><img src="images/p1.jpg" alt="plate"/>'))],
        [("images/cover.jpg", OTHER), ("images/p1.jpg", PLATE)], how="epub2")
    text = pg_epub.read_clean(p)
    assert markers_in(text) == len(pg_epub.figures(p)) == 1
    assert text == "body\n\n[Illustration: plate]"
