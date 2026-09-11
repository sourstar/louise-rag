"""Locate, clean and structure the plain-text edition of a Gutenberg book.

The gutenberg-epub module ships `pg{id}.txt.utf8` (and sometimes `.utf-8`)
inside each book directory, so no EPUB parsing is needed.
"""
import re
from pathlib import Path

_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)
_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)

_CHAPTER = re.compile(
    r"^\s{0,6}("
    r"(chapter|chap\.?|capitolo|capítulo|capitulo|chapitre|kapitel|kapittel|hoofdstuk|"
    r"luku|глава|rozdział|fejezet|book|libro|livre|part|parte|teil|canto)"
    r"\s+([0-9]+|[ivxlcdm]+|[a-z]+)\b.*"
    r"|[IVXLCDM]+\.?\s*"
    r")$",
    re.I | re.M,
)

def scan_dir(book_dir: Path, book_id: int) -> tuple[Path | None, bool, str | None]:
    """(text file or None, directory_is_empty, other_form) — at most ONE
    listing. `other_form` names a readable edition present when there is no
    .txt ("epub", "mobi", "html"), or None.

    The canonical names are probed by stat first, which is the overwhelming
    common case and lists nothing at all. Only when they all miss do we list
    the directory, and that single listing answers both remaining questions:
    "is there some other text file" and "is this an empty transfer stub"
    (rsync creates the directory, then copies only files matching the spec
    filter, so a title published solely as epub/mobi leaves an empty one).

    Discovery asks both questions for all ~79k books on every cold scan, so
    doing them in one pass is the difference between one stat and two full
    directory listings per book.
    """
    for name in (f"pg{book_id}.txt.utf8", f"pg{book_id}.txt.utf-8", f"pg{book_id}.txt"):
        p = book_dir / name
        if p.is_file():
            return p, False, None
    entries = sorted(book_dir.iterdir())
    if not entries:
        return None, True, None
    # fall back to any .txt-ish file (the old glob("*.txt*"), same order)
    for p in entries:
        if ".txt" in p.name and "readme" not in p.name.lower() and p.is_file():
            return p, False, None
    # NO TEXT — but is there text in ANOTHER FORM? The listing is already in
    # hand, so naming it costs nothing, and the difference matters to a
    # person: "the source has no usable content" is TRUE of a title whose
    # directory holds an .rdf and a cover, and FALSE of one published as an
    # epub. Counted on the node 2026-09-11, gutenberg's 1,454 unindexable
    # titles are 255 with an .epub, 159 with a .mobi, 8 with an .html, and
    # ~1,199 with no text at all — so a sixth of them were being described
    # as unreadable when they are merely not readable AS .txt, by this
    # collection, whose origin form is the .txt (docs/notes/pack-format.md).
    for suffix in (".epub", ".mobi", ".html", ".htm"):
        if any(p.suffix.lower() == suffix and p.is_file() for p in entries):
            return None, False, suffix.lstrip(".")
    return None, False, None


def find_txt(book_dir: Path, book_id: int) -> Path | None:
    return scan_dir(book_dir, book_id)[0]

def read_clean(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    m = _START.search(text)
    if m:
        text = text[m.end():]
    m = _END.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()

def split_chapters(text: str, max_flat_chars: int = 60_000) -> list[tuple[str, str]]:
    """Return [(chapter_title, chapter_text)].

    Uses heading heuristics; if a book yields no usable headings (or absurdly
    many false positives) fall back to fixed-size sections so downstream code
    always gets bounded pieces.
    """
    matches = list(_CHAPTER.finditer(text))
    if 1 <= len(matches) <= 400:
        parts = []
        first = matches[0]
        if first.start() > 200:
            parts.append(("Front matter", text[: first.start()].strip()))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end(): end].strip()
            if body:
                parts.append((m.group(0).strip()[:120], body))
        if parts:
            return parts
    # fallback: flat slicing on paragraph boundaries
    parts, buf, size, idx = [], [], 0, 1
    for para in text.split("\n\n"):
        buf.append(para)
        size += len(para)
        if size >= max_flat_chars:
            parts.append((f"Section {idx}", "\n\n".join(buf).strip()))
            buf, size, idx = [], 0, idx + 1
    if buf:
        parts.append((f"Section {idx}", "\n\n".join(buf).strip()))
    return parts or [("Text", text)]
