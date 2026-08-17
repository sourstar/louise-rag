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

from ..base import CanonicalDoc, CorpusSource, DiscoverState, DocRef, LicenseClass, PrivacyClass, RawDoc
from . import pg_text, rdf


class GutenbergSource:
    source_id = "gutenberg"
    license_class = LicenseClass.GREEN
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            book_id = int(entry.name)
            txt = pg_text.find_txt(entry, book_id)
            if txt is None:
                yield DocRef(source_id=self.source_id, native_id=str(book_id),
                             locator=str(entry), error="no txt file")
                continue
            st = txt.stat()
            yield DocRef(source_id=self.source_id, native_id=str(book_id),
                         locator=str(entry), mtime=int(st.st_mtime), size=st.st_size,
                         extra={"txt_name": txt.name})

    def fetch(self, ref: DocRef) -> RawDoc:
        txt = pg_text.find_txt(Path(ref.locator), int(ref.native_id))
        if txt is None:
            raise RuntimeError(f"no txt file for book {ref.native_id}")
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
            date=None,
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
            raise RuntimeError(f"no txt file for book {native_id}")
        return pg_text.read_clean(txt)

    def split_text(self, text: str) -> list[tuple[str, str]]:
        return pg_text.split_chapters(text)

    def read_chapters(self, book_dir: Path, native_id: int) -> list[tuple[str, str]]:
        return self.split_text(self.read_text(book_dir, native_id))
