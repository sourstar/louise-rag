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
    caption: str         # the alt text, or the figcaption beside it
    seq: int             # order of first appearance in the reading order


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
                # The caption stays in the text and stays indexed
                # (decision 1); the bytes leave as a member.
                self.parts.append(f"\n\n[Illustration: {alt}]\n\n" if alt
                                  else "\n\n[Illustration]\n\n")
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


def read_clean(path: Path) -> str:
    """The book's text, in reading order, with the PG wrapper removed.

    The counterpart of `pg_text.read_clean`, and NOT expected to equal it —
    see the module docstring. Feed the result to
    `pg_text.split_chapters`, which is edition-independent.
    """
    with zipfile.ZipFile(path) as z:
        text = "\n\n".join(p.text() for _, p in _documents(z))

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
    """Every plate, in the order it first appears in the reading order.

    Hashed here and never later: `sha256` is what names the figure in the
    record, and the bytes travel separately as a member. A figure that
    appears twice is one figure — the same bytes, the same hash, one entry.
    """
    out: list[Figure] = []
    seen: dict[str, Figure] = {}

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for href, parsed in _documents(z):
            base = posixpath.dirname(href)
            for src, alt in parsed.images:
                target = posixpath.normpath(posixpath.join(base, src.split("#")[0]))
                if target not in names:
                    continue                # a broken reference; not a plate
                if target in seen:
                    continue                # the same plate used twice
                raw = z.read(target)
                ext = posixpath.splitext(target)[1].lower()
                fig = Figure(
                    href=target,
                    media_type=_IMAGE_TYPES.get(ext, "application/octet-stream"),
                    sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                    bytes=len(raw),
                    caption=alt,
                    seq=len(out),
                )
                seen[target] = fig
                out.append(fig)
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
