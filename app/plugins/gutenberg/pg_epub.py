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

#: STRUCTURE THE REFLOW USED TO ERASE (parser 7, the 2026-09-14 audit).
#: Control characters cannot occur in XHTML text either, so none collides.
#: A `<br>` is a line break INSIDE a paragraph — a verse line — where a
#: block element is a paragraph break (a stanza): before this both became a
#: paragraph break and the record could not tell a line from a stanza.
_LINE = "\x01"
#: A heading paragraph: `_HEAD` + its level digit, the text, `_HEAD_END`.
#: The book's OWN headings decide its sections; the chapter regex read
#: verse lines and body paragraphs as chapter titles in 8 of 32 sampled
#: books.
_HEAD = "\x02"
_HEAD_END = "\x03"
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
#: An illustrated initial's marker, deferred to its paragraph's end.
_DEFER = "\x04"
#: What an `<hr>` leaves: the plain-text edition's own thought-break line.
#: It is a scene break in the book, and the reflow left nothing behind.
_SCENE = "* * * * *"
#: ITALICS, as the book sets them (operator, 2026-09-14: "if it is italic
#: in the book text, keep it"), written `_…_` — the plain-text edition's
#: own convention, so the two editions read alike. Sentinels until
#: `_normalise`, which moves the spaces a tag enclosed to its outside.
_I_ON = "\x05"
_I_OFF = "\x06"
_ITALIC_TAGS = frozenset({"i", "em", "cite"})
#: elements with no end tag: never on the open-element stack
_VOID = frozenset({"br", "hr", "img", "meta", "link", "input", "col", "area",
                   "base", "wbr", "source", "param", "embed", "track"})
_ITALIC_STYLE = re.compile(r"font-style\s*:\s*(?:italic|oblique)", re.I)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.I | re.S)


def italic_classes(css: str) -> frozenset[str]:
    """The classes a stylesheet sets in italics: the class on the element a
    rule styles (its selector's last part), pseudo-elements aside. PG sets
    stage directions, verse and epigraphs this way — 4,888 `scenedesc`
    spans in the 32-book sample, beside 242,568 `<i>` and 416 `<em>`."""
    out: set[str] = set()
    for selectors, decls in _CSS_RULE.findall(_CSS_COMMENT.sub("", css)):
        if not _ITALIC_STYLE.search(decls):
            continue
        for sel in selectors.split(","):
            parts = sel.split()
            if parts and ":" not in parts[-1]:
                out.update(re.findall(r"\.([\w-]+)", parts[-1]))
    return frozenset(out)

_OPF_NS = "{http://www.idpf.org/2007/opf}"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"

#: Elements after which a paragraph break is emitted. An epub's text is
#: reflowable, so the only paragraph structure it has is its markup.
_BLOCK = frozenset({
    "p", "div", "li", "tr",
    "blockquote", "pre", "section", "article", "figure", "figcaption",
    "table", "dd", "dt",
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
#:
#: BOUNDED TO ONE PARAGRAPH (parser 7). `.*?` under re.S let a START line
#: that lost its closing `***` run on to the next `***` anywhere in the
#: book and cut everything in between as licence. Wrapped lines inside the
#: marker are still allowed; a blank line is not.
from .pg_text import _END, _START  # noqa: E402 — one definition for both editions


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
    carried: bool = True # always True since parser 11 ("Every plate",
                         # 08 §8.7 amended 2026-10-25): every image the
                         # origin has is carried, whatever its type or size;
                         # the closed list binds display only. False stays
                         # in the format for records written before it


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

    def __init__(self, italic: frozenset[str] = frozenset()) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[tuple[str, str]] = []   # (src, alt) in document order
        self._skip = 0
        self._cells = 0                            # cells so far in this table row
        self._italic_classes = italic              # classes the book's CSS sets italic
        self._open: list[tuple[str, bool]] = []    # open elements, and whether italic
        self._ital = 0                             # open italic elements

    def _is_italic(self, tag, attrs) -> bool:
        if tag in _ITALIC_TAGS:
            return True
        a = dict(attrs)
        if self._italic_classes and \
                set((a.get("class") or "").split()) & self._italic_classes:
            return True
        return bool(_ITALIC_STYLE.search(a.get("style") or ""))

    def _brk(self, s: str) -> None:
        """A break inside italics closes the run and reopens it after, so
        a run never spans a paragraph, a heading or a line."""
        self.parts.append(f"{_I_OFF}{s}{_I_ON}" if self._ital else s)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        ital = tag not in _VOID and self._is_italic(tag, attrs)
        if tag not in _VOID:
            self._open.append((tag, ital))
        self._start(tag, attrs)          # the element's own break or marker first
        if ital:
            if not self._ital:
                self.parts.append(_I_ON)
            self._ital += 1

    def _close(self, tag) -> None:
        """Pop `tag` and anything left open inside it (forgiving, as the
        parser is); a run ends when its last italic element does."""
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                n = sum(1 for _, it in self._open[i:] if it)
                del self._open[i:]
                if n:
                    was, self._ital = self._ital, max(0, self._ital - n)
                    if was and not self._ital:
                        self.parts.append(_I_OFF)
                return

    def _start(self, tag, attrs):
        if tag == "br":
            self._brk(_LINE)
            return
        if tag == "hr":
            self._brk(f"\n\n{_SCENE}\n\n")
            return
        if tag in _HEADINGS:
            self._brk(f"\n\n{_HEAD}{tag[1]}")
            return
        if tag in ("sup", "sub"):
            # An exponent FUSED into its base changes the number: "10<sup>
            # 6</sup>" read "106". The plain-text edition writes ^{…} and
            # _{…}; so does this, and the value survives.
            self.parts.append("^{" if tag == "sup" else "_{")
            return
        if tag in ("td", "th"):
            # Cells ran together with no boundary at all ("NameAgeTom12").
            if self._cells:
                self.parts.append(" | ")
            self._cells += 1
            return
        if tag == "tr":
            self._cells = 0
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
            self._brk("\n\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _HEADINGS or tag in ("sup", "sub") or tag in _SKIP or self._skip:
            # empty: an opening marker here would never close — and a
            # self-closing `<script src=…/>` used to count as an OPEN skip
            # that nothing closed, silencing the rest of the document
            return
        self._start(tag, attrs)          # never opens an italic run or an element

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        self._close(tag)                 # a run ends BEFORE the element's break
        if tag in _HEADINGS:
            self._brk(f"{_HEAD_END}\n\n")
            return
        if tag in ("sup", "sub"):
            self.parts.append("}")
            return
        if tag in _BLOCK:
            self._brk("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _italics(line: str) -> str:
    """The run sentinels of one line as `_…_`: adjacent runs joined
    (`<i>Pride</i><i> and Prejudice</i>`), a space a tag enclosed moved
    outside it ("A<i> word </i>here" reads "A _word_ here"), a run with
    nothing in it gone."""
    line = line.replace(_I_OFF + _I_ON, "")
    line = re.sub(_I_ON + r"(\s+)", r"\1" + _I_ON, line)
    line = re.sub(r"(\s+)" + _I_OFF, _I_OFF + r"\1", line)
    line = line.replace(_I_ON + _I_OFF, "")
    return line.replace(_I_ON, "_").replace(_I_OFF, "_")


def _normalise(text: str) -> str:
    """One space inside a paragraph, one blank line between paragraphs.

    An epub carries its line breaks as markup, so the raw text is full of
    incidental newlines from indented XHTML. Collapsing them is what makes
    the output a function of the CONTENT rather than of how the generator
    happened to indent.
    """
    out = []
    for para in re.split(r"\n\s*\n", text):
        # `_LINE` is the one line break that is CONTENT (a `<br>`), so it
        # survives as "\n" while every incidental one collapses
        lines = (re.sub(r"\s+", " ", _italics(ln)).strip() for ln in para.split(_LINE))
        para = "\n".join(ln for ln in lines if ln)
        if para:
            out.append(para)
    return "\n\n".join(out)


#: an XHTML member's declared encoding (`<?xml … encoding="…"?>`)
_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?encoding=["']([A-Za-z0-9._-]+)["']""")


def _decode(raw: bytes) -> str:
    """The member's text in the encoding IT DECLARES. Decoding every member
    as UTF-8 with errors='replace' turned each non-ASCII character of a
    Latin-1 member into U+FFFD — an irreversible alteration of the text
    (audit, 2026-09-14). UTF-8 remains the default, as XML says it is."""
    m = _XML_ENCODING.match(raw[:200])
    enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _documents(z: zipfile.ZipFile) -> list[tuple[str, _Text]]:
    """Every spine document, parsed, in reading order."""
    manifest, spine = _manifest_and_spine(z)
    # the classes the book's stylesheets set in italics (`_Text`), and the
    # document's own <style> on top
    css = italic_classes(" ".join(_decode(z.read(n)) for n in z.namelist()
                                  if n.lower().endswith(".css")))
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
        text = _decode(raw)
        inline = _STYLE_BLOCK.findall(text)
        p = _Text(css | italic_classes(" ".join(inline)) if inline else css)
        p.feed(text)
        p.close()
        out.append((href, p))
    return out


def _walk(z: zipfile.ZipFile) -> tuple[str, list[tuple[str, str, bool]]]:
    """`(text with one sentinel per surviving image, [(target, alt, carried)])`.

    THE ONE PLACE the filtering happens, which is what makes 09 §9.5's
    alignment true rather than hoped for: the text and the figure list are
    both derived from this single sequence, so a cover and a broken
    reference drop from both together or from neither.

    Two outcomes:
      * the COVER and a BROKEN REFERENCE vanish from both. There is no
        picture and nothing announces one.
      * EVERY OTHER IMAGE IS CARRIED, whatever its type or size (parser 11,
        operator 2026-10-25: "Every plate"). Until parser 10 a type outside
        08 §8.7's closed list — SVG, GIF, WEBP — stayed in the text and was
        named `carried: false` with its bytes left behind, which is holding
        less than the origin gave us (invariant 11). The closed list and the
        caps now bind DISPLAY, not storage (08 §8.7, app/ask/figures.py):
        the bytes are stored and shared as opaque bytes and only shown when
        they are a jpeg or png within the caps.

    `carried` stays in the tuple and on the record line (09 §9.5), and is
    always True here: an image this walk keeps is one whose member the zip
    names, so its bytes exist; a member that will not read fails the whole
    book (`source.parse` -> `UnindexableDocument`), never one plate.
    The TEXT is exactly what parser 10 wrote — the marker never depended on
    the type — so no chunk moves.
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
                images.append((target, alt, True))
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


def _read(path: Path) -> tuple[str, str]:
    """`(header, structured text)` — the PG header before `*** START`,
    and the book after it, normalised, with the heading sentinels in."""
    with zipfile.ZipFile(path) as z:
        raw, images = _walk(z)

    # One marker per surviving image, substituted into the sentinels the
    # parser left. Both come from `_walk`, so the count matches by
    # construction and `figures[N]` is the Nth marker (09 §9.5).
    pieces = raw.split(_IMG)
    text = pieces[0]
    deferred: list[str] = []
    for i, (_, alt, _carried) in enumerate(images):
        tail = pieces[i + 1]
        if tail[:1] == _I_ON and len(alt) == 1 and alt.isalpha() and tail[1:2].isalpha():
            # the same, where the word goes on in italics: the initial
            # joins the run (`<img alt="T"/><i>HE</i>` reads "_THE_")
            text += _I_ON + alt + _DEFER + tail[1:]
            deferred.append(alt)
        elif len(alt) == 1 and alt.isalpha() and tail[:1].isalpha():
            # AN ILLUSTRATED INITIAL: `<img alt="T"/>HE discussion`. The
            # marker's paragraph breaks cut the book's first word in two
            # ("T" / "HE"), in the text that becomes vectors (audit,
            # 2026-09-14). The letter goes back into its word — the plain
            # edition reads "THE discussion" — and the marker to the end of
            # its paragraph, which keeps every marker in reading order, so
            # the Nth is still figures[N].
            text += alt + _DEFER + tail
            deferred.append(alt)
        else:
            text += _marker(alt) + tail

    header = ""
    m = _START.search(text)
    if m:
        header, text = text[: m.start()], text[m.end():]
    m = _END.search(text)
    if m:
        text = text[: m.start()]
    return (_plain(_undefer(_normalise(header), deferred)),
            _undefer(_normalise(text), deferred))


def _undefer(text: str, alts: list[str]) -> str:
    """Each paragraph holding deferred initials, followed by their markers
    as paragraphs of their own — in the order they occurred. `alts` is
    consumed front to back across the header and then the body, which is
    the order `_read` met them in."""
    if _DEFER not in text:
        return text
    out = []
    for para in text.split("\n\n"):
        n = para.count(_DEFER)
        out.append(para.replace(_DEFER, ""))
        for _ in range(n):
            out.append(_marker(alts.pop(0)).strip())
    return "\n\n".join(out)


_SENTINELS = re.compile(f"{_HEAD}[1-6]?|{_HEAD_END}")


def _plain(structured: str) -> str:
    """The structured text without its heading sentinels: a heading stays a
    paragraph of its own, as it always read."""
    paras = (_SENTINELS.sub("", p).strip() for p in structured.split("\n\n"))
    return "\n\n".join(p for p in paras if p)


def read_clean(path: Path) -> str:
    """The book's text, in reading order, with the PG wrapper removed.

    The counterpart of `pg_text.read_clean`, and NOT expected to equal it —
    see the module docstring. The SECTIONS of an epub come from `sections`,
    which reads the book's own headings, not from a regex over this.
    """
    return _plain(_read(path)[1])


def header_fields(path: Path) -> dict[str, str]:
    """The `Key: value` fields of the PG header inside the epub — Title,
    Author, Release date, Most recently updated, Language, Credits.
    `pg_text.header_fields` read them for the txt edition only, so an
    epub-form record carried none (audit, 2026-09-14): the prose around
    them is boilerplate, the fields are not."""
    from .pg_text import _HEADER_FIELD
    out: dict[str, str] = {}
    for line in _read(path)[0].replace("\n\n", "\n").splitlines():
        got = _HEADER_FIELD.match(line.strip())
        if got and got.group(2).strip():
            out[got.group(1).strip()] = got.group(2).strip()
    return out


def _is_marker(para: str) -> bool:
    return para.startswith("[Illustration") and para.endswith("]")


def _heading(paras: list[str], i: int) -> tuple[str, list[str], int]:
    """`(title, markers, next index)` for the heading that opens at `i`.

    A heading is a BLOCK, from `_HEAD` to `_HEAD_END`, not one paragraph:
    an `<img>` inside it leaves its marker as a paragraph of its own, which
    split Pride and Prejudice's illustrated chapter headings into an empty
    title and stray paragraphs — every chapter read "Text". Its words are
    the title, a `<br>` inside becoming a dash ("CHAPTER I.<br/>DOWN THE
    RABBIT-HOLE" is one title); a heading that is only a picture takes its
    alt text. The markers go back to the BODY, in order, so the Nth marker
    is still `figures[N]` (09 §9.5). The block only extends over markers and
    the paragraph that closes it, so a heading whose close is missing
    cannot swallow the prose after it.

    A CAPTION INSIDE THE HEADING (Pride and Prejudice: the picture, then
    "I hope Mr. Bingley will like it.", then "CHAPTER II."): the words
    between the first picture and the heading's last paragraph are that
    picture's caption. They go to the body after its marker — a caption
    stays in `sections` and stays embedded (decision 1, 2026-09-08) — and
    the title is the heading's own words before the picture and its last
    paragraph.
    """
    j = i
    block = [paras[i]]
    while (_HEAD_END not in block[-1] and j + 1 < len(paras)
           and not paras[j + 1].startswith(_HEAD) and len(block) < _HEAD_SPAN):
        j += 1
        block.append(paras[j])
    if _HEAD_END not in block[-1]:
        # never closed within the bound: the heading is its first paragraph
        block, j = [paras[i]], i
    parts = [q for q in (_SENTINELS.sub("", x).strip() for x in block) if q]
    first = next((k for k, q in enumerate(parts) if _is_marker(q)), None)
    last = len(parts) - 1
    if first is None:
        titled, lead = parts, []
    else:
        titled = [q for k, q in enumerate(parts)
                  if not _is_marker(q) and (k < first or k == last)]
        lead = [q for k, q in enumerate(parts)
                if _is_marker(q) or (first < k < last)]
    title = " — ".join(ln.strip() for w in titled for ln in w.split("\n") if ln.strip())
    if not title and first is not None:
        title = parts[first][len("[Illustration"):-1].lstrip(":").strip()
    return title, lead, j + 1


#: how many paragraphs one heading may span before it is taken as unclosed
_HEAD_SPAN = 8


def sections(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """`(sections, unplaced)` — the epub's sections, cut at ITS OWN headings.

    Parser 7. The chapter regex over reflowed text read verse lines and
    body paragraphs as chapter titles (8 of 32 sampled books, ~23,000
    characters moved out of prose into titles), and with `<br>` kept as a
    line break it would read every one-word verse line as one. The book
    already says where its sections are: its `<h1>`…`<h6>`.

    Consecutive headings are one path — "BOOK I › CHAPTER I" — since a
    heading followed at once by another is a level of the hierarchy, not an
    empty section. Text before the first heading is "Front matter". A
    heading path left with no body at all (the book ends on it) is
    `unplaced`, for `extra`, as the regex path's contents page is.

    A book whose markup has no headings falls back to the regex, applied
    to WHOLE PARAGRAPHS only, so a verse line inside a stanza can never be
    taken for a chapter.
    """
    from .pg_text import flat_sections, paragraph_chapters
    text = _read(path)[1]
    paras = text.split("\n\n")
    first = next((i for i, p in enumerate(paras) if p.startswith(_HEAD)), None)
    if first is None:
        plain = _plain(text)
        return paragraph_chapters(plain) or (flat_sections(plain), [])
    out: list[tuple[str, str]] = []
    front = [q for q in (_SENTINELS.sub("", p).strip() for p in paras[:first]) if q]
    if front:
        out.append(("Front matter", "\n\n".join(front)))
    path_titles: list[str] = []
    body: list[str] = []
    i = first
    while i < len(paras):
        if paras[i].startswith(_HEAD):
            title, markers, i = _heading(paras, i)
            if body:
                out.append((" › ".join(path_titles) or "Text", "\n\n".join(body)))
                path_titles, body = [], []
            if title:
                path_titles.append(title)
            body.extend(markers)
            continue
        q = _SENTINELS.sub("", paras[i]).strip()
        if q:
            body.append(q)
        i += 1
    if body:
        out.append((" › ".join(path_titles) or "Text", "\n\n".join(body)))
        path_titles = []
    return out, [" › ".join(path_titles)] if path_titles else []


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
    """One plate's bytes. Kept separate from `figures` so a caller can
    list the plates cheaply and fetch only the ones it wants.

    The BUILD does not use this — `source.read_figures` takes a whole
    record's plates through one open, because a book with 3,503 images
    would otherwise open its zip 3,503 times. This is the shape a reader
    serving one picture wants.
    """
    with zipfile.ZipFile(path) as z:
        return z.read(href)


def readable(path: Path) -> bool:
    """Can this file be read as an epub at all?

    A CORPUS FACT, and cheap: the zip's end-of-central-directory and one
    small member. It exists because Project Gutenberg publishes files that
    are named like epubs and are not — nine books in the live mirror carry
    a ZERO-BYTE `pg<id>-images.epub` beside a perfectly good plain one,
    dated 2009, and `pg2215` blew up `fts` with `BadZipFile` when the
    reading rule reached for it.

    So the rule is "the first form that can be READ", never "the first
    file that exists". It is a pure function of the bytes on disk, so a
    verifier applying it to the same directory reaches the same form —
    which is what invariant 11 needs of the choice.

    It asks the same question `read_clean` will ask — the zip opens, the
    container names a rootfile, and the package document parses into a
    manifest and a spine — so "readable" and "read_clean succeeds" cannot
    diverge on the STRUCTURE. If they could, a book would be chosen as an
    epub and then fail to be read as one, which is the bug this exists to
    remove rather than to move. It reads two small members and no
    document; the text is not touched here.

    NOT a validity check on the content: an epub whose spine resolves and
    whose documents are junk is a different failure, and it belongs where
    it is discovered.
    """
    try:
        with zipfile.ZipFile(path) as z:
            _manifest_and_spine(z)
    except Exception:                       # noqa: BLE001 — any failure is "no"
        return False
    return True


def find_epub(book_dir: Path, book_id: int) -> Path | None:
    """The ILLUSTRATED edition, or None.

    Gutenberg publishes `pg<id>.epub` without plates and
    `pg<id>-images.epub` (and sometimes `-images-3.epub`, epub3) with them.
    This function answers ONLY "is there an illustrated edition", which is
    the question the plates are in; choosing what to read from is
    `source.pick_form`, and it falls back to the plain epub, then the txt,
    then the rdf. Keeping the two apart is deliberate: the preference
    order is one rule in one place, and `figures` calls this one to know
    whether to look for plates at all.
    """
    for name in (f"pg{book_id}-images-3.epub", f"pg{book_id}-images.epub"):
        p = book_dir / name
        if p.is_file():
            return p
    hits = sorted(p for p in book_dir.glob("*.epub") if "-images" in p.name.lower())
    return hits[0] if hits else None
