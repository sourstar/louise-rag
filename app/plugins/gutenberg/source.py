"""GutenbergSource — CorpusSource adapter around pg_text/rdf.

discover()/fetch()/parse() satisfy the CorpusSource protocol; read_metadata()/
read_chapters() are extra, non-protocol helpers that worker.py's per-stage
functions call directly so the `rdf` stage never pays for a full-text read and
the `fts`/`embed` stages never re-parse metadata — matching today's exact lazy
I/O cost per stage, which parse() alone (always returning the full
CanonicalDoc) can't express.
"""
import hashlib
import zipfile
from pathlib import Path

from .. import cursor
from ..base import (CanonicalDoc, CorpusSource, DiscoverState, DocRef, Erasure,
                    Figure,
                    LicenseClass, Modality, Mutability, PROTOCOL_VERSION,
                    PrivacyClass, RawDoc, UnindexableDocument)
from . import pg_text, rdf


#: The reading rule, in order (operator, 2026-09-11). ONE parser with a
#: declared branch, not two parsers: a verifier applying this rule to the
#: same book directory reaches the same form, so `records-vs-origin` can
#: re-fetch and reproduce. That is what makes a mixed-form corpus legal
#: under invariant 11 — the choice is recorded, and `extra.form` names it
#: per record.
FORMS = ("epub", "txt", "rdf")


def pick_form(book_dir: Path, book_id: int) -> tuple[str | None, Path | None, bool]:
    """(form, path, is_stub) — which edition of this book to read.

    EPUB first, because the plates are only there and because it is the
    form the origin keeps most current. TXT next, which is what this
    collection read until now. RDF last, and it carries no body text at
    all: the record is then title, author and subjects with no `sections`,
    which is a catalogue entry rather than a book. That is worth having —
    ~1,199 Gutenberg entries have nothing else, and the alternative is an
    error row that says the book does not exist.

    `is_stub` is an EMPTY directory: rsync creates the tree and then copies
    only what the filter matches, so a title the mirror skipped leaves one
    behind. Minting a document for it produced ~21,000 permanently errored
    rows on the live node.
    """
    from . import pg_epub

    try:
        entries = sorted(book_dir.iterdir())
    except OSError:
        return None, None, False
    if not entries:
        return None, None, True

    # THE FIRST FORM THAT CAN BE READ, never the first file that exists.
    # Project Gutenberg publishes files that are named like epubs and are
    # not: nine books in the live mirror carry a ZERO-BYTE
    # `pg<id>-images.epub` beside a perfectly good plain one, dated 2009,
    # and `pg2215` blew up `fts` with `BadZipFile` the first time the rule
    # reached for one. `pg_epub.readable` is a pure function of the bytes
    # on disk, so a verifier applying this rule to the same directory
    # reaches the same form — which is what invariant 11 needs of it.
    #
    # `find_epub` takes the ILLUSTRATED edition only. A book published
    # without one still reads better as an epub than as a hard-wrapped
    # .txt, so the plain edition is the next candidate — shortest name
    # first, which is stable and does not depend on directory order
    # (invariant 3).
    illustrated = pg_epub.find_epub(book_dir, book_id)
    plain = sorted((p for p in entries
                    if p.suffix.lower() == ".epub" and p.is_file()
                    and p != illustrated),
                   key=lambda p: (len(p.name), p.name))
    for candidate in ([illustrated] if illustrated is not None else []) + plain:
        if pg_epub.readable(candidate):
            return "epub", candidate, False

    txt, _, _ = pg_text.scan_dir(book_dir, book_id)
    if txt is not None and txt.stat().st_size > 0:
        return "txt", txt, False

    # An empty .rdf is no more a catalogue record than an empty file is an
    # epub. Same rule, same reason.
    for meta in ([book_dir / f"pg{book_id}.rdf"]
                 + [p for p in entries if p.suffix.lower() == ".rdf"]):
        if meta.is_file() and meta.stat().st_size > 0:
            return "rdf", meta, False
    return None, None, False


class GutenbergSource:
    source_id = "gutenberg"
    license_class = LicenseClass.GREEN
    # 06 §6.1's corpus credit. Green, so nothing compels it — and it is here
    # anyway: a provider that asks for nothing is still the provider, and
    # 02 §2.7 puts a corpus entry in every collection's `credits`.
    corpus_provider = "Project Gutenberg"
    corpus_license = "public-domain"
    corpus_attribution = ("Project Gutenberg — public-domain texts from "
                          "www.gutenberg.org")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"
    # Books are published finished. The catalogue grows; a text
    # already in it does not get edited.
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 4 since 2026-09-11: the reading rule became EPUB, then TXT, then RDF
    #: (metadata only). Every record this parser produces for a book with an
    #: epub differs from what version 3 produced for it — measured, 0 of 60
    #: books agreed on `content_hash` — so the version must move, and a
    #: collection built with 3 is a different collection, not a stale one.
    #:
    #: 5 since 2026-09-12: the rule takes the first form that can be READ,
    #: where 4 took the first that existed. Project Gutenberg publishes
    #: zero-byte `pg<id>-images.epub` files — nine in the live mirror, each
    #: beside a good plain epub — and version 4 chose them, which is a
    #: record parsed from bytes that are not a book. The version moves for
    #: the same reason as last time: for those nine books the two versions
    #: produce different `origin`, different `origin_hash` and different
    #: `content_hash`, so they are different records and a collection built
    #: with 4 is a different collection.
    PARSER_VERSION = 5
    modality = Modality.TEXT
    # PG does withdraw a text on a copyright claim, and the tombstone
    # stays: a removal is never silent (04 §4.4).
    erasure = Erasure.RETRACTABLE
    native_id_order = "numeric"          # PG book numbers, issued ascending
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
        {"field": "subjects", "kind": "flat", "separator": None,
         "authority": {"name": "LCSH"}},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        # Scan cursor, like every other plugin: a book directory whose
        # (mtime, size) is unchanged is skipped without stat-ing its
        # contents. Gutenberg was the last plugin re-examining its whole
        # corpus on every scan — 79k directories, which cost ~5 minutes on
        # an idle pool and 25+ minutes once wikivec/pubmed/wikipedia were
        # saturating the same spindles (~150 IOPS, measured).
        #
        # A DIRECTORY's mtime is the right signature because every way this
        # platform writes a book lands with a rename into that directory
        # (the rsync transport stages then os.replace()s), and a rename
        # changes it. An external mirror rewriting a file IN PLACE would
        # not be seen — the Library's "rescan" button clears the cursor for
        # exactly that case.
        progress = cursor.Progress(state, self.source_id)
        stubs = 0
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            if progress.unchanged(entry):
                continue
            book_id = int(entry.name)
            # One pass answers both questions (see pg_text.scan_dir): where
            # the text is, and whether this is an EMPTY directory — a
            # transfer stub, not a book we hold. rsync creates the directory
            # tree and then copies only files matching the spec filter
            # (*.txt*, *.rdf), so a title published solely as epub/mobi/html
            # leaves an empty one behind. Minting a document for it produced
            # ~21,000 permanently errored rows on the live node that "clear
            # errors & retry" could never fix (the next scan re-applied the
            # error) and that held every progress bar short of 100%.
            form, path, is_stub = pick_form(entry, book_id)
            # staged either way — AFTER its ref is yielded, so the cursor
            # can never remember a book whose row was lost with an
            # interrupted scan (cursor.Progress): a stub examined once should
            # not be re-examined until something lands in it. Staging
            # declines when the directory changed during this scan, so a
            # concurrent rsync cannot make us skip it forever.
            if is_stub:
                stubs += 1
                progress.stage(entry)
                continue
            if form is None:
                # Nothing readable at all — not even an .rdf. After the
                # three-way rule this is a genuinely empty entry rather
                # than "we only take .txt", so the message says so.
                yield DocRef(source_id=self.source_id, native_id=str(book_id),
                             locator=str(entry),
                             error="no epub, txt or rdf")
                progress.stage(entry)
                continue
            st = path.stat()
            # THE LANGUAGE, HERE, because `slicing.slice_hint` reads it from
            # the manifest and gutenberg slices by it. Without it the
            # planning pass of `records build` has no hint and parses every
            # book — measured 2026-09-10 at 19.2 documents/s
            # single-threaded over 77,866 books, 68 minutes, before the
            # packing pass parsed them all again.
            #
            # The cost lands in the right place. This reads the RDF, a few
            # kilobytes of XML beside a text file that averages 300 KB, and
            # only for a book whose directory changed — `progress.unchanged`
            # skips the rest. A parse that would otherwise happen once per
            # book in a serial pass happens once per NEW book in the scan.
            yield DocRef(source_id=self.source_id, native_id=str(book_id),
                         locator=str(entry), mtime=int(st.st_mtime), size=st.st_size,
                         extra={"txt_name": path.name, "form": form,
                                "lang": rdf.parse(entry, book_id)["lang"]})
            progress.stage(entry)
        if stubs:
            # one scan report, not 21,000 documents
            yield DocRef(source_id=self.source_id, native_id="(scan)",
                         locator=str(root), scope="container",
                         error=f"skipped {stubs} empty book director"
                               f"{'y' if stubs == 1 else 'ies'} (transfer "
                               f"stubs: the mirror had no .txt/.rdf to copy)")
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        # The rule is re-applied rather than trusting `ref.extra`: a ref can
        # outlive the scan that made it (a queued document, a retry after a
        # sync landed the epub), and the form must be whatever the corpus
        # holds NOW — with `origin_hash` over the file actually read, which
        # is invariant 11's half of the bargain.
        form, path, _ = pick_form(Path(ref.locator), int(ref.native_id))
        if form is None:
            raise UnindexableDocument(
                f"no epub, txt or rdf for book {ref.native_id}")
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return RawDoc(ref=ref, raw_hash=h.hexdigest(), path=path)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        native_id = int(raw.ref.native_id)
        book_dir = Path(raw.ref.locator)
        from . import pg_epub

        meta = rdf.parse(book_dir, native_id)
        suffix = raw.path.suffix.lower()
        plates: list = []
        if suffix == ".epub":
            # `pick_form` proved this file readable, and between then and
            # now a sync can have replaced it. A file that will not open is
            # a CORPUS FACT — retrying reads the same bytes — so it is
            # raised unprefixed rather than escaping as a stage error,
            # which the Library counts as blocked and which burns a retry
            # (tests/test_unindexable_stability.py).
            try:
                text = pg_epub.read_clean(raw.path)
                found = pg_epub.figures(raw.path)
            except (zipfile.BadZipFile, pg_epub.EpubError, OSError) as e:
                raise UnindexableDocument(
                    f"{raw.path.name} is not a readable epub for book "
                    f"{native_id} ({type(e).__name__})") from e
            sections = pg_text.split_chapters(text)
            # The caption is already an `[Illustration: …]` marker inside
            # `sections`, so it is indexed; only the BYTES are a member.
            plates = [Figure(href=f.href, media_type=f.media_type,
                             sha256=f.sha256, bytes=f.bytes, caption=f.caption,
                             seq=f.seq, carried=f.carried)
                      for f in found]
        elif suffix == ".rdf":
            # Metadata only: a catalogue entry, not a book. `sections` is
            # empty and stays empty — inventing a section from the title
            # would put a chunk in the index that no page of the book
            # contains.
            sections = []
        else:
            sections = pg_text.split_chapters(pg_text.read_clean(raw.path))
        creators = [a.strip() for a in meta["authors"].split(";") if a.strip()]
        subjects = [s.strip() for s in meta["subjects"].split("|") if s.strip()]
        return CanonicalDoc(
            uid=f"{self.source_id}:{native_id}",
            title=meta["title"],
            creators=creators,
            language=meta["lang"],
            date=meta.get("issued"),        # the PG release date: the snapshot cutoff key
            subjects=subjects,
            keys=[],
            extra={"bookshelves": meta["bookshelves"]},
            sections=sections,
            figures=plates,
            raw_hash=raw.raw_hash,
        )

    # --- non-protocol helpers used by worker.py/summarize.py for lazy,
    # granular I/O matching pre-refactor per-stage cost exactly ---

    def read_metadata(self, book_dir: Path, native_id: int) -> dict:
        return rdf.parse(book_dir, native_id)

    def read_text(self, book_dir: Path, native_id: int) -> str:
        """THE SAME RULE `parse` uses, and it must stay that way.

        This is the worker's lazy path — `read_chapters` below feeds the
        fts and embed stages — while `parse` builds the record. If the two
        chose different forms, the index and the records would describe
        different text for one document, and every chunk id would name a
        chunk whose content the record does not contain. Nothing downstream
        could detect it: both halves would be internally consistent.
        """
        from . import pg_epub

        form, path, _ = pick_form(book_dir, native_id)
        if form is None:
            # the CORPUS fact, not a stage failure — see UnindexableDocument
            raise UnindexableDocument(
                f"no epub, txt or rdf for book {native_id}")
        if form == "epub":
            # As in `parse`: a file that will not open is a corpus fact,
            # raised unprefixed, never a stage error that burns a retry.
            try:
                return pg_epub.read_clean(path)
            except (zipfile.BadZipFile, pg_epub.EpubError, OSError) as e:
                raise UnindexableDocument(
                    f"{path.name} is not a readable epub for book "
                    f"{native_id} ({type(e).__name__})") from e
        if form == "rdf":
            return ""       # a catalogue entry has no body; `sections` is []
        return pg_text.read_clean(path)

    def split_text(self, text: str) -> list[tuple[str, str]]:
        return pg_text.split_chapters(text)

    def read_chapters(self, book_dir: Path, native_id: int) -> list[tuple[str, str]]:
        text = self.read_text(book_dir, native_id)
        # An rdf-only book has no body. `split_chapters` answers empty text
        # with `[("Text", "")]` — one section holding nothing — which would
        # index an empty chunk and put a hit with no content in every
        # result set. `parse` returns `[]` for the same book, and these two
        # must agree.
        return self.split_text(text) if text.strip() else []

    def read_figures(self, book_dir: Path, native_id: int,
                     hrefs: dict[str, str]) -> dict[str, bytes]:
        """`{hash: href}` -> `{hash: bytes}` — the container pass (09 §9.5),
        which re-opens the book rather than carrying 50 GB of image bytes
        back out of the parse pool.

        ONE open and ONE application of the reading rule for all of a
        book's plates: the most illustrated book in this corpus carries
        3,503 images, and doing it per plate would re-open that zip 3,503
        times.

        `pick_form` again, and not the epub directly: only an epub-form
        book has plates at all, so a book whose form has changed since the
        record was computed refuses here instead of silently reading an
        href out of a different edition.
        """
        import zipfile as _zip

        form, path, _ = pick_form(book_dir, native_id)
        if form != "epub" or path is None:
            raise UnindexableDocument(
                f"book {native_id} is read as {form or 'nothing'} and carries "
                f"no plates; {len(hrefs)} named plate(s) cannot be read from it")
        with _zip.ZipFile(path) as z:
            return {h: z.read(href) for h, href in hrefs.items()}
