"""GutenbergEpubSource — the plates edition of Project Gutenberg.

A SEPARATE plugin, not a mode of `GutenbergSource`, and the separation is
forced rather than chosen. A collection has one `plugin`, one `parser` and
one `cleaning_version`, all inside `collection_id` (09 §9.3), and `plugin`
is the uid prefix. Reading some books from `.txt` and others from the epub
would put records from two different cleaning functions in one collection
with nothing in the Codex able to say which, and a verifier re-deriving
from the origin would compute a different `content_hash` for half of them.

So this is `gutenberg-epub:<book id>`, a collection of its own, and the
decision of 2026-09-08 is what it implements: the epub is the origin form
because THE PLATES ARE ONLY IN THE EPUB.

WHAT IT COSTS, measured on the node 2026-09-11 before any of it was built:
parsing the epub and parsing the `.txt` agree on NOTHING. Over 60 books
holding both forms, 0 produced an identical `content_hash`; at chunk
level, 6 of 1,228 chunks hashed the same — 0.5%. `read_clean` preserves
the `.txt`'s hard wrap at ~70 characters and an epub is reflowable, so
every line differs and every chunk with it. This collection therefore
embeds from zero. That number was the one docs/notes/figures-in-the-corpus.md
said "the whole plan hangs on"; it had never been taken.

WHAT IT GAINS: the plates, and the 255 books Gutenberg publishes only as
an epub, which the `.txt` collection records as unindexable.
"""
import hashlib
from pathlib import Path

from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, Figure,
                    LicenseClass, Modality, Mutability, PROTOCOL_VERSION,
                    PrivacyClass, RawDoc, UnindexableDocument)
from . import pg_epub, pg_text, rdf


def find_epub(book_dir: Path, book_id: int) -> Path | None:
    """The epub to read, chosen deterministically (invariant 3).

    Gutenberg ships several: `pg{id}-images-3.epub`, `pg{id}-images.epub`,
    `pg{id}.epub`. The IMAGES edition wins; among equals the shortest name
    wins, which is stable and does not depend on directory order.

    DELIBERATELY NOT `pg_epub.find_epub`, and the difference matters. That
    one returns the illustrated edition ONLY — "the plain one is not a
    fallback: a book without plates is a book that belongs to the other
    collection" — which was right while the plates collection stood BESIDE
    the text one. The operator's decision of 2026-09-11 makes this
    collection the REPLACEMENT for it, so a book with no illustrated
    edition must still be carried here or retiring the text collection
    would lose it. Two rules, two premises; this is the one that belongs to
    a collection that has to hold everything.
    """
    candidates = [p for p in sorted(book_dir.iterdir())
                  if p.suffix.lower() == ".epub" and p.is_file()]
    if not candidates:
        return None
    with_images = [p for p in candidates if "-images" in p.name]
    return sorted(with_images or candidates, key=lambda p: (len(p.name), p.name))[0]


class GutenbergEpubSource:
    source_id = "gutenberg-epub"
    license_class = LicenseClass.GREEN
    corpus_provider = "Project Gutenberg"
    corpus_license = "public-domain"
    corpus_attribution = ("Project Gutenberg — public-domain texts from "
                          "www.gutenberg.org")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    #: Its own, and 1 rather than continuing gutenberg's 3: this parser has
    #: no history with that one — a different form, a different cleaning,
    #: and not one `content_hash` in common (see the module docstring).
    PARSER_VERSION = 1
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE
    native_id_order = "numeric"
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
        {"field": "subjects", "kind": "flat", "separator": None,
         "authority": {"name": "LCSH"}},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        progress = cursor.Progress(state, self.source_id)
        stubs = 0
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            if progress.unchanged(entry):
                continue
            book_id = int(entry.name)
            try:
                epub = find_epub(entry, book_id)
            except OSError:
                continue
            if epub is None:
                # No epub at all. An EMPTY directory is a transfer stub and
                # not a document (the same lesson as the .txt plugin: minting
                # one produced ~21,000 permanently errored rows). A directory
                # with files but no epub IS a document this collection cannot
                # read, and it keeps its error — named by the form that IS
                # there, so a person can tell a dead entry from one the text
                # collection holds.
                try:
                    present = sorted(p.suffix.lower().lstrip(".")
                                     for p in entry.iterdir() if p.is_file())
                except OSError:
                    present = []
                if not present:
                    stubs += 1
                    progress.stage(entry)
                    continue
                txt, _, _ = pg_text.scan_dir(entry, book_id)
                yield DocRef(
                    source_id=self.source_id, native_id=str(book_id),
                    locator=str(entry),
                    error=("no epub — published only as txt" if txt is not None
                           else "no epub"))
                progress.stage(entry)
                continue
            st = epub.stat()
            # `lang` here for the same reason the .txt plugin reads it here:
            # `slicing.slice_hint` takes it from the manifest and gutenberg
            # slices by it, so without it `records build`'s planning pass
            # parses every book to learn its language.
            yield DocRef(source_id=self.source_id, native_id=str(book_id),
                         locator=str(entry), mtime=int(st.st_mtime),
                         size=st.st_size,
                         extra={"epub_name": epub.name,
                                "lang": rdf.parse(entry, book_id)["lang"]})
            progress.stage(entry)
        if stubs:
            yield DocRef(source_id=self.source_id, native_id="(scan)",
                         locator=str(root), scope="container",
                         error=f"skipped {stubs} empty book director"
                               f"{'y' if stubs == 1 else 'ies'} (transfer "
                               f"stubs: the mirror had nothing to copy)")
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        epub = find_epub(Path(ref.locator), int(ref.native_id))
        if epub is None:
            raise UnindexableDocument(f"no epub for book {ref.native_id}")
        h = hashlib.blake2b(digest_size=16)
        with open(epub, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        # `origin_hash` is the hash of the EPUB, which is the origin unit
        # this collection parses — invariant 11's other half, and what lets
        # `records-vs-origin` re-fetch and reproduce.
        return RawDoc(ref=ref, raw_hash=h.hexdigest(), path=epub)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        native_id = int(raw.ref.native_id)
        book_dir = Path(raw.ref.locator)
        meta = rdf.parse(book_dir, native_id)
        # One read of the zip for the text, one for the plates. The caption
        # is already an `[Illustration: …]` marker inside `sections`, so it
        # is indexed and searchable; the bytes are a member.
        sections = pg_text.split_chapters(pg_epub.read_clean(raw.path))
        plates = [Figure(href=f.href, media_type=f.media_type, sha256=f.sha256,
                         bytes=f.bytes, caption=f.caption, seq=f.seq,
                         carried=f.carried)
                  for f in pg_epub.figures(raw.path)]
        creators = [a.strip() for a in meta["authors"].split(";") if a.strip()]
        subjects = [s.strip() for s in meta["subjects"].split("|") if s.strip()]
        return CanonicalDoc(
            uid=f"{self.source_id}:{native_id}",
            title=meta["title"],
            creators=creators,
            language=meta["lang"],
            date=meta.get("issued"),
            subjects=subjects,
            keys=[],
            extra={"bookshelves": meta["bookshelves"]},
            sections=sections,
            raw_hash=raw.raw_hash,
            figures=plates,
        )

    # --- the non-protocol helpers worker.py calls for lazy per-stage I/O ---

    def read_metadata(self, book_dir: Path, native_id: int) -> dict:
        return rdf.parse(book_dir, native_id)

    def read_text(self, book_dir: Path, native_id: int) -> str:
        epub = find_epub(book_dir, native_id)
        if epub is None:
            raise UnindexableDocument(f"no epub for book {native_id}")
        return pg_epub.read_clean(epub)

    def read_chapters(self, book_dir: Path, native_id: int):
        return pg_text.split_chapters(self.read_text(book_dir, native_id))


SOURCE = GutenbergEpubSource()
