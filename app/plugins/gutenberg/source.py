"""GutenbergSource — CorpusSource adapter around pg_text/rdf.

discover()/fetch()/parse() satisfy the CorpusSource protocol; read_metadata()/
read_chapters() are extra, non-protocol helpers that worker.py's per-stage
functions call directly so the `rdf` stage never pays for a full-text read and
the `fts`/`embed` stages never re-parse metadata — matching today's exact lazy
I/O cost per stage, which parse() alone (always returning the full
CanonicalDoc) can't express.
"""
import hashlib
from pathlib import Path

from .. import cursor
from ..base import (CanonicalDoc, CorpusSource, DiscoverState, DocRef, Erasure,
                    LicenseClass, Modality, Mutability, PROTOCOL_VERSION,
                    PrivacyClass, RawDoc, UnindexableDocument)
from . import pg_text, rdf


class GutenbergSource:
    source_id = "gutenberg"
    license_class = LicenseClass.GREEN
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"
    # Books are published finished. The catalogue grows; a text
    # already in it does not get edited.
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    PARSER_VERSION = 3
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
            txt, is_stub = pg_text.scan_dir(entry, book_id)
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
            if txt is None:
                # files present but no text: a real Gutenberg entry whose
                # text we cannot index (metadata-only, or an unsupported
                # format). That IS a document, and it keeps its error.
                yield DocRef(source_id=self.source_id, native_id=str(book_id),
                             locator=str(entry), error="no txt file")
                progress.stage(entry)
                continue
            st = txt.stat()
            yield DocRef(source_id=self.source_id, native_id=str(book_id),
                         locator=str(entry), mtime=int(st.st_mtime), size=st.st_size,
                         extra={"txt_name": txt.name})
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
        txt = pg_text.find_txt(Path(ref.locator), int(ref.native_id))
        if txt is None:
            raise UnindexableDocument(f"no txt file for book {ref.native_id}")
        h = hashlib.blake2b(digest_size=16)
        with open(txt, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return RawDoc(ref=ref, raw_hash=h.hexdigest(), path=txt)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        native_id = int(raw.ref.native_id)
        book_dir = Path(raw.ref.locator)
        meta = rdf.parse(book_dir, native_id)
        text = pg_text.read_clean(raw.path)
        sections = pg_text.split_chapters(text)
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
            raw_hash=raw.raw_hash,
        )

    # --- non-protocol helpers used by worker.py/summarize.py for lazy,
    # granular I/O matching pre-refactor per-stage cost exactly ---

    def read_metadata(self, book_dir: Path, native_id: int) -> dict:
        return rdf.parse(book_dir, native_id)

    def read_text(self, book_dir: Path, native_id: int) -> str:
        txt = pg_text.find_txt(book_dir, native_id)
        if txt is None:
            # the CORPUS fact, not a stage failure — see UnindexableDocument
            raise UnindexableDocument(f"no txt file for book {native_id}")
        return pg_text.read_clean(txt)

    def split_text(self, text: str) -> list[tuple[str, str]]:
        return pg_text.split_chapters(text)

    def read_chapters(self, book_dir: Path, native_id: int) -> list[tuple[str, str]]:
        return self.split_text(self.read_text(book_dir, native_id))
