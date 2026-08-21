"""WikipediaSource — CorpusSource over MediaWiki multistream dumps.

The multistream format is why this is tractable at 22 GB: the dump is a
concatenation of independent bz2 streams (~100 pages each), and the sidecar
index (`...-multistream-index.txt.bz2`, lines `offset:page_id:title`) maps
every page to its stream's byte offset. discover() reads ONLY the index;
fetch() decompresses ONLY the one stream a page lives in.

Locator format (plugin-opaque per §2, survives the manifest like pubmed's):
    "<dump path>::<stream start>::<stream end>"    end == 0 → EOF.
"""
import bz2
import hashlib
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import cursor
from ..base import CanonicalDoc, DiscoverState, DocRef, LicenseClass, Mutability, PrivacyClass, RawDoc
from . import wikitext

_LOCATOR_SEP = "::"
# canonical-namespace pages only; everything else is navigation/meta
_SKIP_TITLE = re.compile(
    r"^(File|Image|Category|Template|Wikipedia|Help|Portal|Draft|Module|"
    r"MediaWiki|Special|Talk|User|User talk|Wikipedia talk|Category talk|"
    r"Template talk|File talk|Help talk|Portal talk|Module talk|TimedText|"
    r"Gadget|Topic|Book):")
_DUMP_SUFFIX = "-pages-articles-multistream.xml.bz2"
_INDEX_SUFFIX = "-pages-articles-multistream-index.txt.bz2"


def split_locator(locator: str) -> tuple[Path, int, int]:
    path, _, tail = locator.rpartition(_LOCATOR_SEP)
    path2, _, start = path.rpartition(_LOCATOR_SEP)
    return Path(path2), int(start), int(tail)


def _lang_of(dump_name: str) -> str:
    wiki = dump_name.split("-")[0]          # enwiki / simplewiki / itwiki
    prefix = wiki[:-4] if wiki.endswith("wiki") else wiki
    return "en" if prefix in ("en", "simple") else prefix


class WikipediaSource:
    source_id = "wikipedia"
    license_class = LicenseClass.YELLOW     # CC BY-SA: attribution + share-alike
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "wiki"
    # The one source here that revises in place, indefinitely, and
    # the reason content-defined chunking exists in this codebase.
    mutability = Mutability.MUTABLE

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        scan = cursor.load(state.state_dir, self.source_id)
        started_ns = time.time_ns()   # see cursor.mark_if_settled
        for dump in sorted(root.rglob(f"*{_DUMP_SUFFIX}")):
            index = dump.with_name(dump.name.replace(_DUMP_SUFFIX, _INDEX_SUFFIX))
            if not index.is_file():
                yield DocRef(source_id=self.source_id, native_id=dump.name,
                             locator=str(dump), scope="container",
                             error=f"multistream index missing for {dump.name}")
                continue
            if cursor.unchanged(scan, dump, root) and cursor.unchanged(scan, index, root):
                continue     # already enumerated at this (mtime, size)
            st = dump.stat()
            lang = _lang_of(dump.name)
            # group index lines by stream offset so each page knows its
            # stream's end (= the next distinct offset)
            pending: list[tuple[int, str, str]] = []
            last_offset = -1
            try:
                with bz2.open(index, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        offset_s, _, rest = line.rstrip("\n").partition(":")
                        page_id, _, title = rest.partition(":")
                        if not offset_s.isdigit() or not page_id.isdigit():
                            continue
                        offset = int(offset_s)
                        if offset != last_offset and pending:
                            yield from self._flush(pending, offset, dump, st, lang)
                            pending = []
                        last_offset = offset
                        if _SKIP_TITLE.match(title):
                            continue
                        pending.append((offset, page_id, title))
            except Exception as e:  # noqa: BLE001 — truncated/corrupt index: one
                # error ref, never a dead scan (same rule as pubmed containers)
                yield DocRef(source_id=self.source_id, native_id=dump.name,
                             locator=str(dump), scope="container",
                             error=f"unreadable index for {dump.name}: "
                                    f"{type(e).__name__}: {e}"[:200])
                continue
            if pending:
                yield from self._flush(pending, 0, dump, st, lang)
            cursor.mark_if_settled(scan, dump, started_ns, root)
            cursor.mark_if_settled(scan, index, started_ns, root)
        cursor.save(state.state_dir, self.source_id, scan)

    def _flush(self, pending, end_offset, dump, st, lang):
        for offset, page_id, title in pending:
            yield DocRef(
                source_id=self.source_id, native_id=page_id,
                locator=f"{dump}{_LOCATOR_SEP}{offset}{_LOCATOR_SEP}{end_offset}",
                mtime=int(st.st_mtime), size=st.st_size,
                extra={"title": title, "lang": lang})

    def fetch(self, ref: DocRef) -> RawDoc:
        dump, start, end = split_locator(ref.locator)
        with open(dump, "rb") as f:
            f.seek(start)
            blob = f.read((end - start) if end else -1)
        xml = bz2.decompress(blob).decode("utf-8", errors="replace")
        # a stream is a sequence of <page> elements with no root
        root = ET.fromstring(f"<pages>{xml}</pages>")
        for page in root:
            pid = page.findtext("id")
            if pid == ref.native_id:
                data = ET.tostring(page, encoding="unicode").encode("utf-8")
                digest = hashlib.blake2b(data, digest_size=16).hexdigest()
                return RawDoc(ref=ref, raw_hash=digest, data=data)
        raise RuntimeError(f"page {ref.native_id} not found in its stream "
                            f"({ref.locator})")

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        page = ET.fromstring(raw.data.decode("utf-8"))
        title = page.findtext("title") or ""
        text = page.findtext("revision/text") or ""
        lang = raw.ref.extra.get("lang") or "en"
        if wikitext.is_redirect(text) or page.find("redirect") is not None:
            sections: list[tuple[str, str]] = []
        else:
            sections = wikitext.to_sections(text)
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=title,
            creators=[],
            language=lang,
            date=None,
            subjects=[],
            keys=[],
            extra={},
            sections=sections,
            raw_hash=raw.raw_hash,
        )
