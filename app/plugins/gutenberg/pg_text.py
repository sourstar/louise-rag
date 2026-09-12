"""Locate, clean and structure the plain-text edition of a Gutenberg book.

The gutenberg-epub module ships `pg{id}.txt.utf8` (and sometimes `.utf-8`)
inside each book directory, so no EPUB parsing is needed.
"""
import re
from pathlib import Path

_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)
_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.I | re.S)

#: a PG header field line: "Credits: Produced by ...". Anchored and
#: bounded so a colon inside prose ("Note: see below") in the body is not
#: mistaken for one — the key is a short run of letters and spaces only.
_HEADER_FIELD = re.compile(r"^([A-Za-z][A-Za-z ]{1,28}):\s*(.*)$")

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

#: "Produced by A, B and C" / "Transcribed by …" / "Updated by …" — the
#: lead-in PG writes before the people who made the edition.
_PRODUCED_BY = re.compile(
    r"^\s*(?:produced|prepared|transcribed|updated|proofread|created|e-?text prepared)"
    r"\s+by\s+", re.I)
#: names separated by commas and "and", but NOT inside a collective name
#: ("the Online Distributed Proofreading Team" is one credit, not four).
_NAME_SPLIT = re.compile(r",\s*and\s+|,\s*|\s+and\s+", re.I)


def producers(credit: str) -> list[str]:
    """The people named in a PG production credit, one per name.

    Operator, 2026-09-12: "PG producers are credited as PG producers". So
    they are parsed into names like any other credit rather than kept as
    one opaque sentence, and `source.parse` gives them the role
    `producer` beside the author and the illustrator.

    The lead-in ("Produced by") goes; a trailing note about HOW the work
    was done ("with additional proofreading by volunteers", "from page
    images generously made available by …") is not a name and is kept
    whole as its own credit rather than chopped into fragments.
    """
    if not credit or not credit.strip():
        return []
    # PEOPLE ONLY, and the notes are NOT lost by leaving them out: the
    # whole credit string is kept verbatim in `extra.marc["508"]` and
    # `extra.text_header["Credits"]`. Turning a note into an entry of the
    # credits list does not preserve more, it just invents a person —
    # book 17832's marc508 is "Produced by Ebooks Libres et Gratuits;
    # this text is also available in multiple formats at …", and the
    # first draft of this credited all of that as one contributor.
    body = _PRODUCED_BY.sub("", credit.strip())
    body = body.split(";")[0]                       # a trailing remark
    for sep in (" from page images", " from images", " with additional",
                " using scans", " from scans", " at http", " at www"):
        cut = body.lower().find(sep)
        if cut != -1:
            body = body[:cut]
            break
    names = [n.strip(" .") for n in _NAME_SPLIT.split(body)]
    return list(dict.fromkeys(n for n in names if len(n) > 1))


def header_fields(path: Path) -> dict[str, str]:
    """The `Key: value` lines of the PG header, before `*** START ***`.

    `read_clean` throws that whole block away as promise 9(b)'s "common
    header", and most of it is exactly that — "This eBook is for the use
    of anyone anywhere at no cost…" is boilerplate repeated on every
    text. But `Credits: Produced by David Widger` is IN there, and the
    operator settled what that is (2026-09-12): "keep the PG production
    credits, project louise respects the credits".

    So the boilerplate PROSE still goes and the structured fields stay —
    Title, Author, Release Date, Language, Credits, Editor, Illustrator,
    Translator. Taking `Key: value` lines rather than a list of known
    keys means a field this code has never seen survives too.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    m = _START.search(text)
    head = text[: m.start()] if m else text[:4000]
    out: dict[str, str] = {}
    key = None
    for line in head.splitlines():
        got = _HEADER_FIELD.match(line)
        if got:
            key = got.group(1).strip()
            out[key] = got.group(2).strip()
        elif key and line.startswith((" ", "\t")) and line.strip():
            # a continuation line: a long credit wraps, and half a credit
            # is not a credit
            out[key] = f"{out[key]} {line.strip()}".strip()
        elif not line.strip():
            key = None
    return {k: v for k, v in out.items() if v}


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

def headings_without_body(text: str) -> list[str]:
    """The headings `split_chapters` cannot place, in the order they
    appear — a heading immediately followed by another, with nothing
    between them.

    `split_chapters` drops these (its `if body:`), and they are NOT
    noise: measured over 400 real books, 415 of them, and they are
    mostly the book's own TABLE OF CONTENTS, whose lines each match the
    chapter pattern and so leave no body behind. Dropping them meant a
    record held no trace of the book's contents page, which promise 9
    does not allow.

    They go to `extra`, not to `sections`. Promise 9 requires keeping
    what the origin said; it does not require EMBEDDING it, and a
    contents page cut into chunks is retrieval noise competing with the
    prose it indexes. Keeping them out of `sections` also keeps them out
    of `content_hash`, so restoring them moves no chunk id.
    """
    matches = list(_CHAPTER.finditer(text))
    if not 1 <= len(matches) <= 400:
        return []
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        if not text[m.end(): end].strip():
            out.append(m.group(0).strip())
    return out


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
        # ANY front matter, not only more than 200 characters of it. The
        # threshold dropped it on 17 of 400 sampled books, and what sits
        # there is usually PG's production credit plus the title page —
        # "Produced by David Widger and Pat Castevens". The operator
        # settled it (2026-09-12): "keep the PG production credits,
        # project louise respects the credits".
        if first.start() and text[: first.start()].strip():
            parts.append(("Front matter", text[: first.start()].strip()))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end(): end].strip()
            if body:
                # THE HEADING IS NOT TRUNCATED. `[:120]` cut the tail off
                # a long one silently. It fires on none of 400 sampled
                # books, so this costs nothing and removes a cap that
                # would have lost text the day it did fire (promise 9).
                parts.append((m.group(0).strip(), body))
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
