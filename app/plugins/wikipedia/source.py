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
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc)
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


# One-slot stream cache. A multistream chunk holds ~100 pages, and every
# stage walks pages in dump order (discovery order = index order = stream
# order; `pack build` orders by page id, which follows it closely), so the
# next fetch is almost always in the stream just decompressed. Without
# this every page paid its stream's decompression + parse (~1 MB, tens of
# ms) — 100x the work. Keyed on the exact byte range; locked because the
# parallel embed stage fetches from threads.
import threading

_stream_lock = threading.Lock()
_stream_cache: dict = {}        # (dump, start, end) -> {page_id: serialised page bytes}


def _stream_pages(dump: Path, start: int, end: int) -> dict[str, bytes]:
    key = (str(dump), start, end)
    with _stream_lock:
        hit = _stream_cache.get(key)
        if hit is not None:
            return hit
    with open(dump, "rb") as f:
        f.seek(start)
        blob = f.read((end - start) if end else -1)
    xml = bz2.decompress(blob).decode("utf-8", errors="replace")
    # a stream is a sequence of <page> elements with no root
    root = ET.fromstring(f"<pages>{xml}</pages>")
    pages: dict[str, bytes] = {}
    for page in root:
        pid = page.findtext("id")
        if pid:
            pages[pid] = ET.tostring(page, encoding="unicode").encode("utf-8")
    with _stream_lock:
        _stream_cache.clear()           # one slot: streams are read sequentially
        _stream_cache[key] = pages
    return pages


def _lang_of(dump_name: str) -> str:
    wiki = dump_name.split("-")[0]          # enwiki / simplewiki / itwiki
    prefix = wiki[:-4] if wiki.endswith("wiki") else wiki
    return "en" if prefix in ("en", "simple") else prefix


class WikipediaSource:
    source_id = "wikipedia"
    license_class = LicenseClass.YELLOW     # CC BY-SA: attribution + share-alike
    # 06 §6.1's corpus credit — and here the licence does compel it. The
    # authors are the contributors, not the Wikimedia Foundation, which
    # hosts rather than writes.
    corpus_provider = "Wikipedia contributors"
    corpus_license = "CC-BY-SA-4.0"
    corpus_attribution = "Wikipedia contributors, CC BY-SA 4.0"
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "wiki"
    # The one source here that revises in place, indefinitely, and
    # the reason content-defined chunking exists in this codebase.
    mutability = Mutability.MUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 2 since 2026-09-12: promise 9. Version 1 published the prose and
    #: nothing else, so an article's categories, citations, infobox,
    #: short description and apparatus were all discarded by
    #: `strip_code`. Invariant 3 — bumped explicitly. Free to take: this
    #: is not a configured collection on any node, so no records exist to
    #: fork. See docs/notes/origin-fidelity-audit.md.
    PARSER_VERSION = 2
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE        # pages are deleted; the tombstone stays
    native_id_order = "numeric"          # page_id, issued ascending
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        progress = cursor.Progress(state, self.source_id)
        dumps = sorted(root.rglob(f"*{_DUMP_SUFFIX}"))
        # REFUSE A COLLISION AT DISCOVERY (06 §6.1) rather than trusting it.
        #
        # `native_id` is the page id and carries no language, while `lang`
        # goes only into `DocRef.extra` — so two dumps share a uid for every
        # page id they both use. `documents.uid` is the primary key, and the
        # collision is not an error anywhere downstream: discovery finds the
        # existing row, sees a different hash, and runs `UPDATE … rdf_done=0,
        # fts_done=0, embed_done=0 WHERE uid=?`. The second language
        # overwrites the first and resets its stages, and every scan
        # flip-flops the two — silently, and for as long as both are mounted.
        #
        # Refusing here is the narrow fix. Making the id language-bearing is
        # the other one, and it would rewrite every uid of an existing 19.4M
        # document collection — not something to do as a side effect of a
        # scan. See docs/notes/wikivec-homogeneous.md §6.
        langs = {_lang_of(d.name): d.name for d in dumps}
        if len(langs) > 1:
            yield DocRef(
                source_id=self.source_id, native_id="", locator=str(root),
                scope="container",
                error=(f"{len(langs)} Wikipedia dumps of different languages "
                       f"under one corpus ({', '.join(sorted(langs))}): a page "
                       f"id is unique per language, not across them, so these "
                       f"would share uids and silently overwrite each other. "
                       f"One language per collection (06 §6.1)"))
            return
        for dump in dumps:
            index = dump.with_name(dump.name.replace(_DUMP_SUFFIX, _INDEX_SUFFIX))
            if not index.is_file():
                yield DocRef(source_id=self.source_id, native_id=dump.name,
                             locator=str(dump), scope="container",
                             error=f"multistream index missing for {dump.name}")
                continue
            if progress.unchanged(dump) and progress.unchanged(index):
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
            # both files staged together, recorded once the dump's refs are
            # committed (cursor.Progress) — an interrupted scan resumes here
            progress.stage(dump)
            progress.stage(index)
        progress.flush()

    def _flush(self, pending, end_offset, dump, st, lang):
        for offset, page_id, title in pending:
            yield DocRef(
                source_id=self.source_id, native_id=page_id,
                locator=f"{dump}{_LOCATOR_SEP}{offset}{_LOCATOR_SEP}{end_offset}",
                mtime=int(st.st_mtime), size=st.st_size,
                extra={"title": title, "lang": lang})

    def fetch(self, ref: DocRef) -> RawDoc:
        dump, start, end = split_locator(ref.locator)
        pages = _stream_pages(dump, start, end)
        data = pages.get(ref.native_id)
        if data is None:
            raise RuntimeError(f"page {ref.native_id} not found in its stream "
                                f"({ref.locator})")
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        return RawDoc(ref=ref, raw_hash=digest, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        page = ET.fromstring(raw.data.decode("utf-8"))
        title = page.findtext("title") or ""
        text = page.findtext("revision/text") or ""
        lang = raw.ref.extra.get("lang") or "en"
        parsed: dict = {}
        if wikitext.is_redirect(text) or page.find("redirect") is not None:
            sections: list[tuple[str, str]] = []
        else:
            # PROMISE 9. `to_sections` alone discarded most of what an
            # article asserts — categories on 93% of enwiki pages,
            # citations on 89.6%, infoboxes on 51.4% — because
            # `strip_code` removes every template and tag, and none of
            # that is wrapping, boilerplate, our corpus-add date or the
            # provider's packaging.
            parsed = wikitext.parse_all(text)
            sections = parsed["sections"]
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=title,
            creators=[],
            language=lang,
            date=None,
            #: the article's own categories, verbatim and in the origin's
            #: order (operator, 2026-09-12). The sortkey after a "|" is a
            #: collation hint for the category page, not a second subject.
            subjects=parsed.get("categories", []),
            keys=[],
            # EVERYTHING ELSE THE PAGE SAYS. Kept, not embedded: a
            # bibliography or an infobox chunked into the vector space
            # would compete with the prose it annotates, and promise 9
            # requires holding what the origin said, not chunking it.
            extra={k: v for k, v in {
                # the revision's timestamp — informative in a dated snapshot
                # (a dump IS the snapshot: pack build --until cuts on it)
                "revised": (page.findtext("revision/timestamp") or "")[:10] or None,
                "revision": (page.findtext("revision/id") or "").strip(),
                "redirect_to": (page.find("redirect").get("title")
                                if page.find("redirect") is not None else ""),
                "short_description": parsed.get("short_description", ""),
                "infoboxes": parsed.get("infoboxes", []),
                "references": parsed.get("references", []),
                "external_links": parsed.get("external_links", []),
                "coordinates": parsed.get("coordinates", []),
                # References/External links/See also: out of the CHUNKS,
                # where they are citation noise, but not out of the record
                "apparatus": parsed.get("apparatus", {}),
                # presence only: the identifiers live in Wikidata, and 573
                # of 574 sampled templates carry no parameters at all
                "has_authority_control": parsed.get("has_authority_control", False),
            }.items() if v not in ("", None, [], {}, False)},
            sections=sections,
            raw_hash=raw.raw_hash,
        )
