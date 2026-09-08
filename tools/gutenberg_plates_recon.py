"""tools/gutenberg_plates_recon.py — which Gutenberg books actually have plates.

Read-only, stdlib only, no network, no GPU. Safe to run on the NAS against
the live corpus while anything else is running.

WHY IT EXISTS. The decision of 2026-09-08 makes Gutenberg-with-plates a new
collection whose origin form is the epub. Two numbers follow from that and
neither is known:

  * HOW MANY BOOKS HAVE PLATES. Project Gutenberg is ~72,000 books and some
    are illustrated and some are not, so the new collection's corpus is a
    SUBSET nobody has counted. Every cost below scales with it.
  * WHETHER THE PLATES ARE ON DISK AT ALL. `app/acquire/specs.py` fetches
    the epub extra with `Filter(include=("*.epub",), exclude=("*images*",))`
    — and Gutenberg publishes the illustrated edition as
    `pg<id>-images.epub`, which that pattern drops. If the images editions
    are absent they have to be fetched, and "nothing is re-downloaded"
    (the migration ground rule) becomes a named exception with a size.

WHAT IT REPORTS. Per book: whether a `.txt` is there, which epub editions
are there, and — with `--deep` — how many images each illustrated epub
actually carries and how many bytes they are. Then the totals that decide
the plan: the size of the illustrated subset, the bytes to fetch if the
images editions are missing, and the GPU time to re-embed that subset.

    python tools/gutenberg_plates_recon.py /path/to/gutenberg/cache/epub
    python tools/gutenberg_plates_recon.py <root> --deep --limit 2000

An epub is a zip, so `--deep` reads only the central directory of each one
— entry names and sizes, never the image bytes. It is I/O-cheap and does
not decompress.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: `pg<id>.epub` is the text-only edition; `pg<id>-images.epub` and
#: `pg<id>-images-3.epub` carry the plates. Matched loosely, because the
#: point of this tool is to find out what is actually there rather than to
#: assert what should be.
_IMAGES_EPUB = re.compile(r"-images(-\d+)?\.epub$", re.I)
_IMAGE_MEMBER = re.compile(r"\.(jpe?g|png|gif|svg|webp|tiff?)$", re.I)

#: Measured on this node: six 500-chunk books at 3.5 chunks/s (STATUS.md).
CHUNKS_PER_S = 3.5

#: `app/config.py`: CHUNK_TOKENS defaults to 1000, and a chunk is at most
#: that many tokens. Four characters per token is the same rule
#: `app/chunking.py` uses (`max_chars = CHUNK_TOKENS * 4`).
CHARS_PER_CHUNK = 4000


@dataclass
class Book:
    book_id: int
    txt: Path | None = None
    txt_bytes: int = 0
    plain_epub: Path | None = None
    images_epub: Path | None = None
    images_epub_bytes: int = 0
    image_count: int = 0          # --deep only
    image_bytes: int = 0          # --deep only

    @property
    def chunks(self) -> int:
        """What the txt would cut to under the book chunker. An estimate,
        and named as one: the real count comes from the chunker."""
        return max(1, -(-self.txt_bytes // CHARS_PER_CHUNK))


@dataclass
class Totals:
    books: int = 0
    with_txt: int = 0
    with_plain_epub: int = 0
    with_images_epub: int = 0
    no_epub_at_all: int = 0
    empty_dirs: int = 0
    illustrated: list[Book] = field(default_factory=list)


def find_books(root: Path, limit: int | None = None):
    """Yield `(book_id, dir)` for every numeric book directory under `root`.

    Gutenberg's rsync tree is `cache/epub/<id>/`, flat and numeric. Anything
    that is not a number is skipped rather than guessed at.
    """
    n = 0
    for d in sorted(root.iterdir(), key=lambda p: (not p.name.isdigit(), p.name)):
        if not d.is_dir() or not d.name.isdigit():
            continue
        yield int(d.name), d
        n += 1
        if limit and n >= limit:
            return


def inspect(book_id: int, d: Path, deep: bool) -> Book:
    b = Book(book_id=book_id)
    try:
        entries = list(d.iterdir())
    except OSError as e:                            # a permission or mount hiccup
        print(f"warning: {d}: {e}", file=sys.stderr)
        return b

    for p in entries:
        if not p.is_file():
            continue
        name = p.name
        if ".txt" in name and "readme" not in name.lower():
            if b.txt is None:
                b.txt = p
                b.txt_bytes = p.stat().st_size
        elif name.lower().endswith(".epub"):
            if _IMAGES_EPUB.search(name):
                if b.images_epub is None:
                    b.images_epub = p
                    b.images_epub_bytes = p.stat().st_size
            elif b.plain_epub is None:
                b.plain_epub = p

    if deep and b.images_epub is not None:
        b.image_count, b.image_bytes = count_images(b.images_epub)
    return b


def count_images(epub: Path) -> tuple[int, int]:
    """`(count, uncompressed bytes)` of the image members of an epub.

    Reads the zip's central directory only — `namelist()` and `infolist()`
    never touch the compressed data — so this stays cheap over 70,000 files.
    """
    try:
        with zipfile.ZipFile(epub) as z:
            infos = [i for i in z.infolist() if _IMAGE_MEMBER.search(i.filename)]
            return len(infos), sum(i.file_size for i in infos)
    except (zipfile.BadZipFile, OSError) as e:
        print(f"warning: {epub}: {e}", file=sys.stderr)
        return 0, 0


def report(t: Totals, deep: bool) -> None:
    ill = t.illustrated
    say = print

    say("")
    say("=" * 66)
    say(f"  {t.books:,} book directories under the corpus root")
    say("=" * 66)
    say(f"  with a .txt                  {t.with_txt:>9,}")
    say(f"  with a text-only epub        {t.with_plain_epub:>9,}")
    say(f"  with an IMAGES epub          {t.with_images_epub:>9,}"
        f"   <- the plates collection's corpus")
    say(f"  with no epub at all          {t.no_epub_at_all:>9,}")
    say(f"  empty directories            {t.empty_dirs:>9,}")

    if not t.with_images_epub:
        say("")
        say("  NO IMAGES EPUBS ARE ON DISK.")
        say("  app/acquire/specs.py fetches the epub extra with")
        say('    Filter(include=("*.epub",), exclude=("*images*",))')
        say("  which drops exactly the illustrated editions. The plates have")
        say("  to be fetched before the collection can be built, and that is")
        say("  an explicit exception to \"nothing is re-downloaded\".")
        say("")
        say("  Re-run against a tree that has them to size the fetch, or drop")
        say("  the exclude and fetch first.")
        return

    chunks = sum(b.chunks for b in ill)
    secs = chunks / CHUNKS_PER_S
    say("")
    say("  THE ILLUSTRATED SUBSET")
    pct = 100.0 * t.with_images_epub / t.books if t.books else 0.0
    say(f"    books                      {len(ill):>9,}   ({pct:.1f}% of the corpus)")
    say(f"    images-epub bytes on disk  {_h(sum(b.images_epub_bytes for b in ill)):>9}")
    txt_total = sum(b.txt_bytes for b in ill)
    say(f"    their .txt bytes           {_h(txt_total):>9}")
    say(f"    estimated chunks           {chunks:>9,}   (txt bytes / {CHARS_PER_CHUNK})")
    say("")
    say(f"  RE-EMBEDDING THAT SUBSET at {CHUNKS_PER_S} chunks/s (measured, STATUS.md)")
    say(f"    {secs:,.0f} s = {secs / 3600:,.1f} h = {secs / 86400:,.2f} GPU-days")
    say("")
    say("  Why re-embedding is needed at all: `pg_text.read_clean` keeps the")
    say("  source's line structure verbatim, and Gutenberg's .txt is hard-")
    say("  wrapped where an epub's text is not. So epub-derived sections")
    say("  cannot equal txt-derived ones, `content_hash` moves, and no")
    say("  existing vector applies to the new collection.")

    if deep:
        with_imgs = [b for b in ill if b.image_count]
        say("")
        say("  PLATES THEMSELVES (--deep)")
        say(f"    epubs carrying images      {len(with_imgs):>9,}")
        if with_imgs:
            counts = sorted(b.image_count for b in with_imgs)
            say(f"    images per book            "
                f"min {counts[0]}, median {counts[len(counts) // 2]}, "
                f"max {counts[-1]}")
            say(f"    total images               "
                f"{sum(b.image_count for b in with_imgs):>9,}")
            say(f"    total image bytes          "
                f"{_h(sum(b.image_bytes for b in with_imgs)):>9}"
                f"   <- the figures member, uncompressed")


def _h(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return str(n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Count Gutenberg books with plates, and size the work.")
    ap.add_argument("root", type=Path,
                    help="the corpus root — Gutenberg's cache/epub directory")
    ap.add_argument("--deep", action="store_true",
                    help="open each images epub and count its image members "
                         "(central directory only; no decompression)")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N book directories — for a quick look")
    ap.add_argument("--json", type=Path, default=None,
                    help="also write the per-book table here")
    a = ap.parse_args(argv)

    if not a.root.is_dir():
        print(f"{a.root} is not a directory", file=sys.stderr)
        return 2

    t = Totals()
    rows = []
    for book_id, d in find_books(a.root, a.limit):
        b = inspect(book_id, d, a.deep)
        t.books += 1
        if b.txt:
            t.with_txt += 1
        if b.plain_epub:
            t.with_plain_epub += 1
        if b.images_epub:
            t.with_images_epub += 1
            t.illustrated.append(b)
        if not b.plain_epub and not b.images_epub:
            t.no_epub_at_all += 1
        if not b.txt and not b.plain_epub and not b.images_epub:
            t.empty_dirs += 1
        if a.json:
            rows.append({"book_id": b.book_id, "txt_bytes": b.txt_bytes,
                         "plain_epub": bool(b.plain_epub),
                         "images_epub": bool(b.images_epub),
                         "images_epub_bytes": b.images_epub_bytes,
                         "image_count": b.image_count,
                         "image_bytes": b.image_bytes})
        if t.books % 5000 == 0:
            print(f"  … {t.books:,} directories", file=sys.stderr)

    report(t, a.deep)
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\n  per-book table written to {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
