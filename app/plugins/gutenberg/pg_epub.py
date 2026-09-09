"""Reading a Project Gutenberg epub: its text, and its plates.

The counterpart of `pg_text.py`, which reads the plain-text edition. This
one exists because THE PLATES ARE ONLY IN THE EPUB — the operator's
decision of 2026-09-08 makes the epub the origin form of the
Gutenberg-with-plates collection for exactly that reason (02 §2.3 requires
the choice of form be recorded with its reason).

WHAT THIS DOES NOT TRY TO DO. It does not try to reproduce
`pg_text.read_clean`'s output. It cannot: `read_clean` preserves the
source's line structure verbatim, Gutenberg's `.txt` is hard-wrapped at
~70 characters, and an epub's text is reflowable. So the two editions
yield different `sections`, a different `content_hash`, and a collection
that re-embeds. That is a known cost, not a bug to chase — and closing it
by unwrapping lines in `read_clean` would bump `cleaning_version`, which
is inside `space_id` (invariant 3), re-deriving the EXISTING Gutenberg
collection as well.

DETERMINISM (invariant 3). Chunking is a pure function of the cleaned
text, so this function must be too. Every choice here is therefore fixed
rather than discovered: documents are read in SPINE order (the reading
order the epub itself declares), never in zip or directory order; block
elements close a paragraph; whitespace inside a paragraph collapses to
single spaces; and nothing depends on a locale, a clock or a filesystem.

FIGURES (decision 1, 2026-09-08). A figure is a member, not text: it is
named in the record by hash and carried in `figures/<slice>/`, while ITS
CAPTION STAYS IN `sections` AND STAYS INDEXED. So an `<img>` becomes an
`[Illustration: …]` marker in the text — which is also the convention
Gutenberg's own plain-text edition uses, so the caption reads the same way
in both editions and the figure has an anchor in the text it belongs to.

Standard library only: `zipfile`, `html.parser`, `xml.etree`. An epub is a
zip of XHTML and there is no reason to take a dependency for it.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

CONTAINER = "META-INF/container.xml"

#: Stands in the text for one `<img>`, replaced once the filtering is
#: known. A NUL cannot occur in XHTML text, so it cannot collide.
_IMG = "\x00"

_OPF_NS = "{http://www.idpf.org/2007/opf}"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"

#: Elements after which a paragraph break is emitted. An epub's text is
#: reflowable, so the only paragraph structure it has is its markup.
_BLOCK = frozenset({
    "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr",
    "blockquote", "pre", "section", "article", "figure", "figcaption",
    "table", "hr", "dd", "dt",
})

#: Never contributes text.
_SKIP = frozenset({"script", "style", "head", "title"})

_IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}

#: Gutenberg wraps its own licence around the book inside the epub too, in
#: the same words the plain text uses. `pg_text` cuts on these; so do we,
#: so that the two editions at least agree on WHERE THE BOOK BEGINS.
_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
                    re.I | re.S)
_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
                  re.I | re.S)


class EpubError(RuntimeError):
    """The file is not an epub we can read."""


@dataclass(frozen=True)
class Figure:
    """One plate. `sha256` names it in the record; the bytes travel as a
    member under `figures/<slice>/` and never inside `content_hash`."""
    href: str            # the epub-internal path, normalised
    media_type: str
    sha256: str
    bytes: int
    caption: str         # the alt text
    seq: int             # this appearance's position in the reading order
    carried: bool = True # False where the origin HAS the image and this
                         # collection may not admit it — a media type
                         # outside 08 §8.7's list. The entry is still
                         # written: a reader that meets a caption and no
                         # picture must be able to tell "not here yet"
                         # from "not here ever" (05 §5.3)


# --------------------------------------------------------------------------
# the epub's own structure

def _opf_path(z: zipfile.ZipFile) -> str:
    """The package document, found the way the format says to find it —
    through `META-INF/container.xml` — and never by guessing at a name."""
    try:
        raw = z.read(CONTAINER)
    except KeyError:
        raise EpubError(f"no {CONTAINER}: not an epub") from None
    root = ET.fromstring(raw)
    for rf in root.iter(f"{_CONTAINER_NS}rootfile"):
        full = rf.get("full-path")
        if full:
            return full
    raise EpubError(f"{CONTAINER} names no rootfile")


def cover_href(z: zipfile.ZipFile) -> str | None:
    """The image the epub DECLARES as its cover, or None.

    A cover is not a plate. 09 §9.5 defines a figure as "a plate, a diagram
    or a drawing the origin carries BESIDE THE TEXT", and Gutenberg's cover
    is none of those: it is generated by Gutenberg for its own catalogue,
    it is packaging rather than content, and it is present for nearly every
    book whether the book is illustrated or not. Measured over the whole
    corpus: 82 % of books carry an images epub and two thirds of those hold
    the cover and nothing else.

    Both ways of declaring it are read, because Gutenberg uses both in
    almost equal numbers — EPUB 3's `properties="cover-image"` on the
    manifest item, and EPUB 2's `<meta name="cover" content="<id>">`. Over
    44,477 illustrated epubs, ONE HUNDRED PER CENT declare it one way or
    the other, so this needs no filename heuristic and there is no reason
    to guess from `cover.jpg`.
    """
    opf = _opf_path(z)
    base = posixpath.dirname(opf)
    root = ET.fromstring(z.read(opf))

    items: dict[str, tuple[str, str]] = {}
    for item in root.iter(f"{_OPF_NS}item"):
        iid, href = item.get("id"), item.get("href")
        if iid and href:
            items[iid] = (posixpath.normpath(posixpath.join(base, href)),
                          item.get("properties") or "")

    for href, props in items.values():          # EPUB 3
        if "cover-image" in props.split():
            return href
    for meta in root.iter(f"{_OPF_NS}meta"):    # EPUB 2
        if meta.get("name") == "cover":
            entry = items.get(meta.get("content") or "")
            if entry:
                return entry[0]
    return None


def _manifest_and_spine(z: zipfile.ZipFile) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """`({id: (href, media_type)}, [id…])` — the manifest and the SPINE.

    The spine is the reading order the epub declares. Reading in zip order
    instead would be a different book on a different writer's file, and
    chunking must be a pure function of the text (invariant 3).
    """
    opf = _opf_path(z)
    base = posixpath.dirname(opf)
    root = ET.fromstring(z.read(opf))

    manifest: dict[str, tuple[str, str]] = {}
    for item in root.iter(f"{_OPF_NS}item"):
        iid, href = item.get("id"), item.get("href")
        if iid and href:
            manifest[iid] = (posixpath.normpath(posixpath.join(base, href)),
                             item.get("media-type", ""))

    spine: list[str] = []
    for ref in root.iter(f"{_OPF_NS}itemref"):
        idref = ref.get("idref")
        # `linear="no"` is auxiliary matter the reading order excludes.
        if idref and ref.get("linear", "yes") != "no":
            spine.append(idref)
    if not spine:
        raise EpubError(f"{opf} declares an empty spine")
    return manifest, spine


# --------------------------------------------------------------------------
# the text

class _Text(HTMLParser):
    """XHTML to text, with `[Illustration: …]` where a plate sits.

    Deliberately forgiving: Gutenberg's epubs span twenty years of
    generators and a strict XML parse fails on a surprising number of them.
    `HTMLParser` never raises on bad markup, which is what we want — a book
    that will not parse is a book we cannot serve.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[tuple[str, str]] = []   # (src, alt) in document order
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "img":
            a = dict(attrs)
            src = a.get("src") or ""
            alt = (a.get("alt") or "").strip()
            if src:
                self.images.append((src, alt))
                # A SENTINEL, not the marker itself. Which images become
                # markers is decided later, once the cover and the broken
                # references are known — and the same decision produces the
                # figure list, so the two cannot disagree (09 §9.5). One
                # sentinel per `<img>`, in document order, is what makes
                # "the Nth marker is figures[N]" true by construction.
                self.parts.append(_IMG)
            return
        if tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _normalise(text: str) -> str:
    """One space inside a paragraph, one blank line between paragraphs.

    An epub carries its line breaks as markup, so the raw text is full of
    incidental newlines from indented XHTML. Collapsing them is what makes
    the output a function of the CONTENT rather than of how the generator
    happened to indent.
    """
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"\s+", " ", para).strip()
        if para:
            out.append(para)
    return "\n\n".join(out)


def _documents(z: zipfile.ZipFile) -> list[tuple[str, _Text]]:
    """Every spine document, parsed, in reading order."""
    manifest, spine = _manifest_and_spine(z)
    out = []
    for idref in spine:
        entry = manifest.get(idref)
        if entry is None:
            continue                        # a spine id with no manifest item
        href, media = entry
        if media and "html" not in media and "xml" not in media:
            continue
        try:
            raw = z.read(href)
        except KeyError:
            continue                        # a manifest item that is not there
        p = _Text()
        p.feed(raw.decode("utf-8", errors="replace"))
        p.close()
        out.append((href, p))
    return out


#: 08 §8.7's closed list. A type outside it is named in the record with
#: `carried: false` and its bytes are not carried.
_ADMITTED = {"image/jpeg", "image/png"}


def _walk(z: zipfile.ZipFile) -> tuple[str, list[tuple[str, str, bool]]]:
    """`(text with one sentinel per surviving image, [(target, alt, carried)])`.

    THE ONE PLACE the filtering happens, which is what makes 09 §9.5's
    alignment true rather than hoped for: the text and the figure list are
    both derived from this single sequence, so a cover, a broken reference
    and a refused type drop from both together or from neither.

    Three outcomes, and they are not the same:
      * the COVER and a BROKEN REFERENCE vanish from both. There is no
        picture and nothing announces one.
      * a REFUSED TYPE stays in both. The origin has the image, the text
        says so, and the record says `carried: false` so a reader knows to
        stop looking rather than to ask again later.
      * everything else is carried.
    """
    names = set(z.namelist())
    cover = cover_href(z)
    text_parts: list[str] = []
    images: list[tuple[str, str, bool]] = []

    for href, parsed in _documents(z):
        base = posixpath.dirname(href)
        pieces = parsed.text().split(_IMG)
        text_parts.append(pieces[0])
        for i, (src, alt) in enumerate(parsed.images):
            target = posixpath.normpath(posixpath.join(base, src.split("#")[0]))
            tail = pieces[i + 1] if i + 1 < len(pieces) else ""
            if target in names and target != cover:
                images.append((target, alt, _media_type(target) in _ADMITTED))
                text_parts.append(_IMG)
            text_parts.append(tail)

    return "".join(text_parts), images


def _media_type(target: str) -> str:
    return _IMAGE_TYPES.get(posixpath.splitext(target)[1].lower(),
                            "application/octet-stream")


def _marker(alt: str) -> str:
    """What an image leaves in the text. The caption stays in `sections` and
    stays indexed (decision 1, 2026-09-08), and `[Illustration: …]` is the
    convention Gutenberg's own plain-text edition already uses — so a
    caption reads the same way in both editions."""
    return f"\n\n[Illustration: {alt}]\n\n" if alt else "\n\n[Illustration]\n\n"


def read_clean(path: Path) -> str:
    """The book's text, in reading order, with the PG wrapper removed.

    The counterpart of `pg_text.read_clean`, and NOT expected to equal it —
    see the module docstring. Feed the result to
    `pg_text.split_chapters`, which is edition-independent.
    """
    with zipfile.ZipFile(path) as z:
        raw, images = _walk(z)

    # One marker per surviving image, substituted into the sentinels the
    # parser left. Both come from `_walk`, so the count matches by
    # construction and `figures[N]` is the Nth marker (09 §9.5).
    pieces = raw.split(_IMG)
    text = pieces[0]
    for i, (_, alt, _carried) in enumerate(images):
        text += _marker(alt) + pieces[i + 1]

    m = _START.search(text)
    if m:
        text = text[m.end():]
    m = _END.search(text)
    if m:
        text = text[: m.start()]
    return _normalise(text)


# --------------------------------------------------------------------------
# the plates

def figures(path: Path) -> list[Figure]:
    """Every APPEARANCE of a plate, in reading order — one entry per marker
    in the text, so `figures[N]` is the Nth `[Illustration…]` (09 §9.5).

    A plate used twice is two entries with one hash: the container stores
    the bytes once, being addressed by hash, and the two entries are what
    keep the alignment. Deduplicating would break it from the first repeat
    onward — 39 % of Gutenberg's illustrated books repeat a plate, one of
    them 703 times over 66 distinct images — and break it silently.

    Hashed here and never later: `sha256` is what names the figure in the
    record, and the bytes travel separately as a member.
    """
    out: list[Figure] = []
    with zipfile.ZipFile(path) as z:
        _, images = _walk(z)
        raws: dict[str, bytes] = {}
        for seq, (target, alt, carried) in enumerate(images):
            if target not in raws:
                raws[target] = z.read(target)
            raw = raws[target]
            out.append(Figure(
                href=target,
                media_type=_media_type(target),
                sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                bytes=len(raw),
                caption=alt,
                seq=seq,
                carried=carried,
            ))
    return out


def read_bytes(path: Path, href: str) -> bytes:
    """One plate's bytes, for writing the member. Kept separate from
    `figures` so a caller can list the plates cheaply and fetch only the
    ones it is going to store."""
    with zipfile.ZipFile(path) as z:
        return z.read(href)


def find_epub(book_dir: Path, book_id: int) -> Path | None:
    """The ILLUSTRATED edition, or None.

    Gutenberg publishes `pg<id>.epub` without plates and
    `pg<id>-images.epub` (and sometimes `-images-3.epub`, epub3) with them.
    Only the illustrated edition is of any use to this collection, so the
    plain one is not a fallback — a book without plates is a book that
    belongs to the other collection.
    """
    for name in (f"pg{book_id}-images-3.epub", f"pg{book_id}-images.epub"):
        p = book_dir / name
        if p.is_file():
            return p
    hits = sorted(p for p in book_dir.glob("*.epub") if "-images" in p.name.lower())
    return hits[0] if hits else None
