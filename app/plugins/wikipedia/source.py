"""WikipediaSource — CorpusSource over MediaWiki multistream dumps.

The multistream format is why this is tractable at 22 GB: the dump is a
concatenation of independent bz2 streams (~100 pages each), and the sidecar
index (`...-multistream-index.txt.bz2`, lines `offset:page_id:title`) maps
every page to its stream's byte offset. discover() reads ONLY the index;
fetch() decompresses ONLY the one stream a page lives in.

Locator format (plugin-opaque per §2, survives the manifest like pubmed's):
    "<dump path>::<stream start>::<stream end>"    end == 0 → EOF.

ONE COLLECTION, WIKIVEC'S LANGUAGES, TWO LAYERS (operator, 2026-09-10 and
2026-09-14; docs/notes/wikipedia-two-layers.md). The native id carries the
language, in wikivec's own numbering, so every wiki of the collection
coexists and a document the pre-embedded archive covers keeps the id its
vectors are keyed by. A page read from a DUMP is numbered above the
archive's whole range, which makes the layer a pure function of the id.
"""
import bz2
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ... import chunking
from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc)
from . import archive, wikitext
from .ids import DUMP_BASE, LANG_ORDINAL, decode_native_id, native_id_of  # noqa: F401

_LOCATOR_SEP = "::"
# canonical-namespace pages only; everything else is navigation/meta. The
# FALLBACK for a dump whose header names no namespaces: English names only,
# so it let every other wiki's categories and templates through (see
# `namespace_prefixes`, which is what discovery reads)
_SKIP_TITLE = re.compile(
    r"^(File|Image|Category|Template|Wikipedia|Help|Portal|Draft|Module|"
    r"MediaWiki|Special|Talk|User|User talk|Wikipedia talk|Category talk|"
    r"Template talk|File talk|Help talk|Portal talk|Module talk|TimedText|"
    r"Gadget|Topic|Book):")
_DUMP_SUFFIX = "-pages-articles-multistream.xml.bz2"
_INDEX_SUFFIX = "-pages-articles-multistream-index.txt.bz2"
#: `<namespace key="14" case="first-letter">Kategorie</namespace>` (or the
#: articles' own, `<namespace key="0" … />`) in a dump's <siteinfo>
_NAMESPACE = re.compile(r'<namespace key="(-?\d+)"[^>]*?(?:/>|>([^<]*)</namespace>)')
#: Namespaces besides 0 whose pages ARE articles, per wiki, by KEY (a key
#: never changes; a name can). eswiki's 104, `Anexo`: encyclopedic lists —
#: what enwiki keeps as namespace-0 "List of…" articles (operator,
#: 2026-09-15; 93,184 pages).
_CONTENT_NAMESPACES = {"es": frozenset({"104"})}


def namespace_prefixes(dump: Path, index: Path) -> tuple[str, ...] | None:
    """`"Name:"` for every namespace of this wiki but the articles' (key
    0, and `_CONTENT_NAMESPACES`), in the wiki's OWN names, read from the
    dump's <siteinfo> header —
    the bytes before the first page stream, whose offset the index's first
    line gives. None when there is no such header to read.

    `pages-articles` carries categories, templates, files, portals and
    modules beside the articles, and only the title's prefix says which —
    in the wiki's language. The English list this replaces let
    `Kategorie:`, `Catégorie:`, `Категория:`, `رده:`… through on ten of
    the eleven wikis: 25 % of 440 sampled dump pages (2026-09-15), about
    9 M documents that are not articles."""
    try:
        with bz2.open(index, "rt", encoding="utf-8", errors="replace") as f:
            first = f.readline().split(":", 1)[0]
        if not first.isdigit() or int(first) == 0:
            return None
        with open(dump, "rb") as f:
            head = bz2.decompress(f.read(int(first))).decode("utf-8", errors="replace")
    except (OSError, EOFError, ValueError):
        return None
    articles = {"0"} | _CONTENT_NAMESPACES.get(_lang_of(dump.name), frozenset())
    names = tuple(f"{name}:" for key, name in _NAMESPACE.findall(head)
                  if key not in articles and name)
    return names or None

#: the archive layer 1 is read from — its credit, its layer rule, its URLs
ARCHIVE_ID = "upstash-wikipedia-2024-06"


def _archive_prefix() -> str:
    """`<base>/resolve/<revision>/` of the archive, from the platform's
    registry (06 §6.4, §6.6): an address is a changeable fact and lives
    there, beside the keys. Imported lazily — a parse child pays it once."""
    from ...bundles.genesis import ARCHIVES
    a = ARCHIVES[ARCHIVE_ID]
    if not a.revision:
        raise ValueError(f"{ARCHIVE_ID}: the registry pins no revision, and a "
                         f"record cannot be located in a moving `main`")
    return f"{a.base_url}/resolve/{a.revision}/"


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

#: what to assume a dump's LAST multistream chunk costs: its locator
#: gives end == 0 (runs to end of file), so its size cannot be read off the
#: locator. A chunk is ~1 MB compressed; this only has to be the right
#: ORDER for the balance, never exact.
_LAST_CHUNK_BYTES = 1 << 20

_stream_lock = threading.Lock()
_stream_cache: dict = {}        # (dump, start, end) -> {page_id: the page Element}


def _stream_pages(dump: Path, start: int, end: int) -> "dict[str, ET.Element]":
    key = (str(dump), start, end)
    with _stream_lock:
        hit = _stream_cache.get(key)
        if hit is not None:
            return hit
    with open(dump, "rb") as f:
        f.seek(start)
        blob = f.read((end - start) if end else -1)
    xml = bz2.decompress(blob).decode("utf-8", errors="replace")
    # A stream is a sequence of <page> elements with no root — except the
    # LAST, which the index gives as running to the end of the file: that
    # read takes Wikimedia's separate `</mediawiki>` footer stream too
    # (bz2.decompress joins concatenated streams), and `<pages>…</mediawiki>
    # </pages>` does not parse — every page of the final stream failed
    # (parser 3). Nothing after the last </page> is a page.
    last = xml.rfind("</page>")
    if last != -1:
        xml = xml[: last + len("</page>")]
    root = ET.fromstring(f"<pages>{xml}</pages>")
    # THE ELEMENT, NOT ITS BYTES. A stream holds ~100 pages and a caller wants
    # ONE of them, but this used to serialise every page it held — and
    # `ET.tostring` is the one step of this function with no C accelerator
    # behind it (xml.etree parses in C and serialises in Python), so ~99% of
    # the work was thrown away. The bytes are produced in `page_bytes` for the
    # page actually asked for, by the identical call, so `raw_hash` and
    # `origin_hash` are unchanged — the same move pubmed's reader already made
    # (app/plugins/pubmed/source.py: "byte-identical to the former
    # ET.tostring(root[index]) — pinned by tests"), pinned here the same way.
    pages: dict[str, ET.Element] = {}
    for page in root:
        pid = page.findtext("id")
        if pid:
            pages[pid] = page
    with _stream_lock:
        _stream_cache.clear()           # one slot: streams are read sequentially
        _stream_cache[key] = pages
    return pages


def page_bytes(page: ET.Element) -> bytes:
    """One page as ElementTree re-writes it — what `origin_hash` is over."""
    return ET.tostring(page, encoding="unicode").encode("utf-8")


_NOT_AVAILABLE = "not available"


def _editor(rev) -> dict | str:
    """The revision's contributor: `{username, id}` when disclosed, "not
    available" when the edit is anonymous (an <ip>) or the contributor is
    suppressed (`deleted="deleted"`). The IP is read by nothing."""
    c = rev.find("contributor") if rev is not None else None
    if c is None or c.get("deleted") or c.find("ip") is not None:
        return _NOT_AVAILABLE
    name = (c.findtext("username") or "").strip()
    if not name:
        return _NOT_AVAILABLE
    uid = (c.findtext("id") or "").strip()
    return {"username": name, **({"id": uid} if uid else {})}


#: a run of the characters an IPv4 or IPv6 address is written with;
#: `ipaddress` decides whether it is one ("12:30" and "added" are not)
_IP_RUN = re.compile(r"[0-9A-Fa-f.:]{3,}")


def _redact_ips(text: str) -> str:
    """Every IP address in `text` → "not available". A run is tried whole
    and with a leading `:` / trailing `.` trimmed: `[[User talk:212.…|talk]]`
    runs the namespace's colon into the address."""
    def one(m: re.Match) -> str:
        tok = m.group(0)
        if "." not in tok and ":" not in tok:
            return tok
        for lead in (0, len(tok) - len(tok.lstrip(".:"))):
            for trail in (len(tok), len(tok.rstrip(".:"))):
                try:
                    ipaddress.ip_address(tok[lead:trail])
                except ValueError:
                    continue
                return tok[:lead] + _NOT_AVAILABLE + tok[trail:]
        return tok
    return _IP_RUN.sub(one, text)


def _comment(rev) -> str:
    """The edit summary, or "not available" when it was suppressed. An IP
    address IN it is discarded too: a revert's summary names the anonymous
    editor it reverted to ("…to last version by <IP>", simplewiki sample)
    — the operator's rule is that an anonymous editor's IP is not kept,
    wherever the dump writes it."""
    el = rev.find("comment") if rev is not None else None
    if el is None:
        return ""
    if el.get("deleted"):
        return _NOT_AVAILABLE
    return _redact_ips((el.text or "").strip())


def _lang_of(dump_name: str) -> str:
    """"enwiki-latest-…" → "en". "simplewiki-…" → "simple", which is not
    one of the collection's languages and is refused at discovery (it used
    to read as "en" and collide with enwiki's page ids)."""
    wiki = dump_name.split("-")[0]
    return wiki[:-4] if wiki.endswith("wiki") else wiki


#: the id scheme (language, page, source) lives in `ids.py`; the archive's
#: reading in `archive.py` — docs/notes/wikipedia-two-layers.md


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
    #: 3 since 2026-09-14: the operator's decisions (`date` = the revision's
    #: date, the full timestamp kept; a disclosed contributor kept, an
    #: anonymous one "not available" with its IP discarded; everything else
    #: the dump carries kept), the field map over 3,052 real pages, and three
    #: defects: `lang` lost through the pipeline's DocRef, the dump's last
    #: stream unparseable, and the prose leaking image options, categories,
    #: interlanguage links and magic words while dropping quotations, math
    #: and the value of inline templates. Still free: no collection exists.
    #: 4 the same evening: the native id carries the language (wikivec's
    #: numbering) so the collection holds all of wikivec's languages, and
    #: `extra.page_id` keeps the Wikipedia page id the id no longer is.
    PARSER_VERSION = 4
    #: 09 §9.5's `origin` is `<dump>#<start>::<end>`: the member is the
    #: stream's byte range, TWO fields (plugin_io.origin_locator). Without
    #: these the record carried the raw `::` locator, which the reparse
    #: could not find, so records-vs-origin reparsed no wikipedia record.
    LOCATOR_SEP = _LOCATOR_SEP
    LOCATOR_MEMBER_FIELDS = 2
    #: an archive article that straddles two shards is two pieces, each in
    #: its own file: `origin` locates both, joined the same way
    LOCATOR_SEGMENT_SEP = archive.SEG_SEP
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE        # pages are deleted; the tombstone stays
    native_id_order = "numeric"          # page_id, issued ascending
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
    ]
    #: ONE collection, two layers (docs/notes/wikipedia-two-layers.md, 06
    #: §6.3): the archive's articles in the archive's own cut, so its
    #: vectors attach one to one; every dump-read page by content-defined
    #: chunks. Which is which is the archive's coverage in the Codex, over
    #: the id. Versions are the chunkers' own, never literals here.
    default_layers = [
        {"chunker": {"id": chunking.VERBATIM,
                     "version": chunking.chunk_version_for(chunking.VERBATIM), "params": {}},
         "rule": "archive:upstash-wikipedia-2024-06"},
        # 06 §6.3's own Wikipedia example; a piece longer than the space's
        # `ctx` is truncated to it (09 §9.3), never re-cut
        {"chunker": {"id": chunking.CDC,
                     "version": chunking.chunk_version_for(chunking.CDC),
                     "params": {"target": 4000, "min": 1000, "max": 8000}},
         "rule": "default"},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        # LAYER 1's SOURCE FIRST: the pre-embedded archive's articles, under
        # its own ids. `covered` is every page the archive holds, whatever
        # the scan cursor skips, so the dump pass below never mints a second
        # document for one of them.
        covered = archive.covered_articles(root, state.state_dir)
        yield from archive.discover_articles(
            state, self.source_id, cursor.Progress(state, f"{self.source_id}-archive"),
            self._archive_ref)
        progress = cursor.Progress(state, self.source_id)
        dumps = sorted(root.rglob(f"*{_DUMP_SUFFIX}"))
        # THE PINNED SNAPSHOT ONLY. Dated dumps coexist in one corpus by
        # design (acquire/specs.py `_wiki_files`), and the node keeps the
        # legacy `latest/` beside them: two dumps of one wiki mint the same
        # ids, and every scan would flip each page between them. A pinned
        # collection reads `<wiki>/<snapshot>/` and nothing else.
        pin = getattr(state, "snapshot", None)
        if pin:
            dumps = [d for d in dumps if d.parent.name == pin]
        seen_wikis: set[str] = set()
        # EVERY WIKI OF THE COLLECTION, apart by id (06 §6.1: refuse a
        # collision rather than trust it). A page id is unique only within
        # its wiki, and the native id now carries the wiki's language
        # (`native_id_of`), so two dumps can no longer share a uid — the
        # refusal of a second language that stood here (a page-id-only id
        # let the second overwrite the first, silently) is no longer needed.
        # A wiki outside the collection's languages — simplewiki included,
        # which used to pass as "en" — has no number, and is reported.
        for dump in dumps:
            if _lang_of(dump.name) not in LANG_ORDINAL:
                yield DocRef(source_id=self.source_id, native_id=dump.name,
                             locator=str(dump), scope="container",
                             error=(f"{dump.name}: {_lang_of(dump.name)!r} is not "
                                    f"one of the collection's languages "
                                    f"({', '.join(LANG_ORDINAL)})"))
                continue
            wiki = dump.name.split("-")[0]
            if wiki in seen_wikis:
                # 06 §6.1: refuse a collision rather than trust it — an
                # unpinned corpus holding two dumps of one wiki
                yield DocRef(source_id=self.source_id, native_id=dump.name,
                             locator=str(dump), scope="container",
                             error=(f"{dump.name}: a second {wiki} dump in the corpus; "
                                    f"pin one snapshot (the collection's `snapshot`)"))
                continue
            seen_wikis.add(wiki)
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
            # articles only, by THIS wiki's own namespace names
            skip = namespace_prefixes(dump, index)
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
                            yield from self._flush(pending, offset, dump, st, lang, covered)
                            pending = []
                        last_offset = offset
                        if (title.startswith(skip) if skip else _SKIP_TITLE.match(title)):
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
                yield from self._flush(pending, 0, dump, st, lang, covered)
            # both files staged together, recorded once the dump's refs are
            # committed (cursor.Progress) — an interrupted scan resumes here
            progress.stage(dump)
            progress.stage(index)
        progress.flush()

    def _flush(self, pending, end_offset, dump, st, lang, covered):
        for offset, page_id, title in pending:
            if archive.covers(covered, lang, int(page_id)):
                continue            # layer 1: read from the archive, not here
            yield DocRef(
                source_id=self.source_id, native_id=str(native_id_of(lang, int(page_id))),
                locator=f"{dump}{_LOCATOR_SEP}{offset}{_LOCATOR_SEP}{end_offset}",
                mtime=int(st.st_mtime), size=st.st_size,
                extra={"title": title, "lang": lang, "page_id": page_id})

    def origin_url_of(self, rel_path: str) -> str | None:
        """06 §6.4 (main 8068727): an archive row is located IN the archive,
        by URL at the revision the registry pins — never by this node's
        path. None for a dump page, which stays relative to `origin.spec`'s
        base. Read by plugin_io through getattr, like LOCATOR_SEP."""
        if not (rel_path.startswith("data/") and rel_path.endswith(".parquet")):
            return None
        return _archive_prefix() + rel_path

    def local_rel_of(self, url: str) -> str | None:
        """`origin_url_of` read back: the archive file's corpus-relative path."""
        prefix = _archive_prefix()
        return url[len(prefix):] if url.startswith(prefix) else None

    def _archive_ref(self, pending: dict, lang: str) -> DocRef:
        """An archive article's ref: wikivec's own id, so the archive's
        vectors attach to it one to one. Size is the article's ROW COUNT —
        the change gate is (mtime, size), and a shard's size says nothing
        about one article in it (the legacy plugin's lesson)."""
        try:
            page_id = int(pending["article"])
        except (KeyError, ValueError):
            return DocRef(source_id=self.source_id, native_id=str(pending.get("article")),
                          locator=archive.join_segments(pending["segments"]),
                          error=f"malformed article id {pending.get('article')!r}")
        return DocRef(
            source_id=self.source_id,
            native_id=str(native_id_of(lang, page_id, archive=True)),
            locator=archive.join_segments(pending["segments"]),
            mtime=pending["mtime"], size=pending["rows"],
            extra={"title": pending["title"], "lang": lang, "page_id": str(page_id),
                   "source": "archive"})

    def _fetch_archive(self, ref: DocRef) -> RawDoc:
        """The article's rows, as the legacy plugin fetched them — the same
        bytes, so the same `origin_hash`."""
        rows: list[dict] = []
        for path, start, end in archive.split_locator(ref.locator):
            rows.extend(archive.read_rows(path, start, end))
        data = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return RawDoc(ref=ref, raw_hash=hashlib.blake2b(data, digest_size=16).hexdigest(),
                      data=data)

    def _parse_archive(self, raw: RawDoc, lang: str, page_id: int) -> CanonicalDoc:
        """LAYER 1: the archive's own text in the archive's own cut — one
        row, one section, each as its producer embedded it (`embed_input`)
        — so `verbatim` makes exactly the chunks its vectors were computed
        from. Everything the archive gives about the article is here: its
        rows' id, url, title and text (the vectors enter by the archive
        route)."""
        rows = json.loads(raw.data.decode("utf-8"))
        rows.sort(key=lambda r: archive.split_row_id(r["id"])[1])
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=rows[0]["title"] if rows else "",
            creators=[],
            language=lang,
            date=None,            # the archive carries no revision date
            subjects=[],
            keys=[],
            extra={k: v for k, v in {
                "page_id": str(page_id),
                "wiki": f"{lang}wiki",
                "source": "archive",
                "archive": "upstash-wikipedia-2024-06",
                "url": rows[0]["url"] if rows else "",
            }.items() if v},
            sections=[("", archive.embed_input(r["title"], r["text"])) for r in rows],
            raw_hash=raw.raw_hash,
        )

    def work_span(self, row) -> tuple[str, int] | None:
        """The multistream CHUNK this page lives in, and its compressed
        size — `plugin_io.work_span` for the parse pool.

        A chunk holds ~100 pages and `_stream_pages` caches one per
        process, so the chunk is what a child actually does work on: the
        pool must keep a chunk's pages together (splitting it decompresses
        it twice) and must weigh a part by chunk bytes, not by page count.

        Cheap on purpose — it runs once per row while a batch is being cut,
        and reads only the locator already in the manifest row. An archive
        locator names parquet segments instead; its first segment is the
        shared unit for the same reason. Anything unparseable returns None
        and simply falls back to the plain cut.
        """
        locator = str(row.get("path") or "")
        if not locator:
            return None
        try:
            if archive.is_archive_locator(locator):
                path, start, end = archive.split_locator(locator)[0]
                return f"a:{path}|{start}", max(1, int(end) - int(start))
            dump, start, end = split_locator(locator)
            # the LAST chunk of a dump is indexed as running to the end of
            # the file (end == 0); it is one chunk like any other, and its
            # size is not knowable from the locator alone
            size = int(end) - int(start) if end else _LAST_CHUNK_BYTES
            return f"{dump}|{start}", max(1, size)
        except (ValueError, IndexError):
            return None

    def fetch(self, ref: DocRef) -> RawDoc:
        """The page's bytes AS ELEMENTTREE RE-WRITES THEM, and `origin_hash`
        is their hash — not the dump's bytes, which differ on every page
        (06 §6.4: how a unit is extracted is part of the parser). A
        verifier reproduces this serialisation to check a record."""
        if archive.is_archive_locator(ref.locator):
            return self._fetch_archive(ref)
        dump, start, end = split_locator(ref.locator)
        pages = _stream_pages(dump, start, end)
        page = pages.get(str(decode_native_id(int(ref.native_id))[1]))
        if page is None:
            raise RuntimeError(f"page {ref.native_id} not found in its stream "
                                f"({ref.locator})")
        data = page_bytes(page)
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        return RawDoc(ref=ref, raw_hash=digest, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        lang_of_id, page_of_id, from_archive = decode_native_id(int(raw.ref.native_id))
        if from_archive:
            return self._parse_archive(raw, lang_of_id, page_of_id)
        page = ET.fromstring(raw.data.decode("utf-8"))
        title = page.findtext("title") or ""
        text = page.findtext("revision/text") or ""
        # THE DUMP'S OWN NAME, not `ref.extra`: the pipeline rebuilds the
        # DocRef from the manifest row without `extra`
        # (plugin_io.canonical_doc), so every page parsed there read "en" —
        # an itwiki page included (parser 3).
        dump_name = split_locator(raw.ref.locator)[0].name
        lang, page_id, _from_archive = decode_native_id(int(raw.ref.native_id))
        parsed: dict = {}
        rev = page.find("revision")
        stamp = (page.findtext("revision/timestamp") or "").strip()
        if wikitext.is_redirect(text) or page.find("redirect") is not None:
            sections: list[tuple[str, str]] = []
            parsed = wikitext.redirect_info(text)
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
            # THE DATE OF THE REVISION WE HOLD (operator, 2026-09-14): the
            # only date a pages-articles dump gives, and that revision is
            # the work in hand. The full instant is in extra.revised.
            date=stamp[:10] or None,
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
                # the revision's timestamp, WHOLE (it was cut to [:10]).
                # Wikipedia's --until is the dump date (pack/cutoff.py
                # returns None for it), so nothing downstream compares this
                "revised": stamp or None,
                # which encyclopedia: simplewiki and enwiki are both
                # `language` "en", and they are not the same work
                "wiki": dump_name.split("-")[0],
                # the Wikipedia page id — the native id carries it inside the
                # language's block now, and is no longer it (parser 4)
                "page_id": str(page_id),
                # which source this record was read from: the dump, or (layer
                # 1) the pre-embedded archive — invariant 11's form, per record
                "source": "dump",
                "revision": (page.findtext("revision/id") or "").strip(),
                "parent_revision": (page.findtext("revision/parentid") or "").strip(),
                "revision_origin": (page.findtext("revision/origin") or "").strip(),
                "minor_edit": rev is not None and rev.find("minor") is not None,
                "comment": _comment(rev),
                "revision_sha1": (page.findtext("revision/sha1") or "").strip(),
                "text_bytes": ((rev.find("text").get("bytes") or "").strip()
                               if rev is not None and rev.find("text") is not None else ""),
                "namespace": (page.findtext("ns") or "").strip(),
                # THE LAST EDITOR — a pages-articles dump holds one revision,
                # so this is not the article's author, and it is not in
                # `creators`. Disclosed: kept. Anonymous: "not available",
                # and the IP address is never written (operator, 2026-09-14).
                "last_editor": _editor(rev),
                "redirect_to": (page.find("redirect").get("title")
                                if page.find("redirect") is not None else ""),
                "redirect_target": parsed.get("redirect_target", ""),
                "section_levels": parsed.get("section_levels", []),
                "category_sortkeys": parsed.get("category_sortkeys", {}),
                "defaultsort": parsed.get("defaultsort", ""),
                "images": parsed.get("images", []),
                "interlanguage": parsed.get("interlanguage", []),
                "hatnotes": parsed.get("hatnotes", []),
                # THE PAGE AS THE ORIGIN WROTE IT. Every template, link
                # target, sort key, table, comment and maintenance tag the
                # structured fields above do not name is in here — the
                # operator's rule, "keep the same relevant information
                # present in the original corpus", taken literally.
                "wikitext": text,
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
