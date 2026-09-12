"""PubmedSource — CorpusSource adapter over MEDLINE/PubMed XML container
files (baseline + updatefiles are the same code path; see architecture-v2.md
§5.1). Proves the SPI on container files: one physical .xml(.gz) file holds
many records, so DocRef.locator names the container and
DocRef.extra["record_index"] names the record's position within it.

DeleteCitation entries are discovered as DocRefs with `error` set to
DELETE_MARKER — the generic orchestrator (app/discovery.py) already skips
fetch()/parse() for any errored ref and upserts a manifest row carrying that
error, which is exactly a tombstone: the row exists (so a later re-add is
detected as a real change, not a phantom insert) but is permanently excluded
from further stage processing by the existing `WHERE error IS NULL` gates —
no manifest schema change needed. A later baseline/updatefile pass over the
same files re-derives the identical tombstone (idempotent).
"""
import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET

from ...acquire.checksum import verify_sidecar
from ...acquire.spec import ChecksumSource
from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc)
from . import medline, pack

DELETE_MARKER = "deleted (DeleteCitation)"
_MD5_SIDECAR = ChecksumSource(kind="sidecar", sidecar_suffix=".md5", sidecar_algo="md5")

# Locator format: "<container path>::<record index>". §2 makes the locator
# opaque outside the owning plugin, and the manifest persists it — so the
# record's position inside the container survives into every later stage
# without any schema addition (DocRef.extra does NOT survive discovery).
_LOCATOR_SEP = "::"


def split_locator(locator: str) -> tuple[Path, int | None]:
    path, sep, idx = locator.rpartition(_LOCATOR_SEP)
    if sep and idx.isdigit():
        return Path(path), int(idx)
    return Path(locator), None


# Record access goes through the per-container PACK (pack.py, STATUS 5k):
# a derived Parquet file, built once per container by a streaming pass
# and read one row group at a time. The DOM cache it replaced parsed the
# whole 283 MB container (8–20 s, one slot) to serve a single record, and
# two stages walking different containers evicted each other every batch.
import threading

_cache_lock = threading.Lock()
_verified_cache: set = set()


def _verify_md5_sidecar(container: Path) -> None:
    """Delegates to the shared app/acquire/checksum.py helper (same logic
    P0.4's acquisition engine uses for origin sidecar verification).
    Verified once per (path, mtime) — the container is immutable between
    mtime changes, and per-record re-hashing of a multi-MB file is pure
    waste."""
    key = (str(container), container.stat().st_mtime_ns)
    with _cache_lock:
        if key in _verified_cache:
            return
    verify_sidecar(container, _MD5_SIDECAR)
    with _cache_lock:
        _verified_cache.add(key)


class PubmedSource:
    source_id = "pubmed"
    #: What `plugin_io.origin_locator` translates to 09 §9.5's `#` when it
    #: writes the record's `origin`. The locator stays opaque outside this
    #: plugin; only its member SEPARATOR is declared, because the record
    #: travels and is read by verifiers that do not have this plugin.
    LOCATOR_SEP = _LOCATOR_SEP
    license_class = LicenseClass.YELLOW   # publisher copyright on text; raw is pointer-only (§8)
    # 06 §6.1's corpus credit. NLM compiles and distributes MEDLINE; the
    # abstracts inside remain their publishers', which is what the yellow
    # class is about — the credit names who we obtained the corpus from
    # and under what, and `license.terms_sha256` hashes the terms text.
    corpus_provider = "the U.S. National Library of Medicine"
    corpus_license = "nlm-terms"
    corpus_attribution = ("PubMed/MEDLINE, U.S. National Library of "
                          "Medicine, National Institutes of Health")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "abstract"
    # Articles are occasionally revised or retracted, but an abstract
    # is a single chunk — there are no downstream boundaries for an
    # edit to shift, so content-defined chunking would buy nothing.
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    PARSER_VERSION = 4
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE        # a retraction notice is a fact of the corpus
    native_id_order = "numeric"          # PMIDs, issued ascending
    default_facets = [
        {"field": "subjects", "kind": "tree", "separator": "/",
         "authority": {"name": "MeSH"}},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        containers = sorted(
            p for p in root.iterdir()
            if p.is_file() and (p.name.endswith(".xml") or p.name.endswith(".xml.gz")))
        progress = cursor.Progress(state, self.source_id)
        for container in containers:
            if progress.unchanged(container):
                continue     # already enumerated at this (mtime, size)
            st = container.stat()
            try:
                # builds the pack on first sight (one streaming pass — the
                # only full read a container ever gets), then enumerates
                # three small columns
                children = pack.index_columns(container)
            except Exception as e:  # noqa: BLE001 — ParseError, zlib.error, EOFError, OSError:
                # a truncated/corrupt container (e.g. a download that failed
                # checksum, or one racing the background fetch) must cost ONE
                # error ref, never the whole scan — observed live once
                # acquisition moved off the round loop
                yield DocRef(source_id=self.source_id, native_id=container.name,
                             locator=str(container), scope="container",
                             error=f"unparseable container {container.name}: "
                                    f"{type(e).__name__}: {e}"[:200])
                continue
            for i, tag, pmid, delete_pmids in children:
                if tag == "PubmedArticle":
                    if not pmid:
                        continue
                    yield DocRef(source_id=self.source_id, native_id=pmid,
                                 locator=f"{container}{_LOCATOR_SEP}{i}",
                                 mtime=int(st.st_mtime), size=st.st_size,
                                 extra={"record_index": i, "kind": "article"})
                elif tag == "DeleteCitation":
                    for pmid in delete_pmids:
                        yield DocRef(source_id=self.source_id, native_id=pmid,
                                     locator=f"{container}{_LOCATOR_SEP}{i}",
                                     mtime=int(st.st_mtime), size=st.st_size,
                                     error=DELETE_MARKER,
                                     extra={"record_index": i, "kind": "delete"})
            # enumerated cleanly at this signature — unless the container
            # landed WHILE we were scanning, in which case marking it would
            # store a signature that already includes the arrival and we
            # would never enumerate it properly (staging declines then).
            # Containers land constantly during an active fetch, so this is
            # not hypothetical here. Staged, not marked: the cursor records
            # it only once its last ref is committed (cursor.Progress), so
            # an interrupted scan resumes at this container, never after it.
            progress.stage(container)
        progress.flush()

    def prepare(self, row) -> None:
        """Build this document's CONTAINER pack if it is missing — the one
        expensive derived artefact of this plugin (a streaming parse of a
        ~280 MB gz container, ~17 s). Called by the parse pool ahead of the
        stage that will read it (app/parallel.py: prefetch), so the batch
        that first touches a container no longer waits for it. Idempotent
        and cheap when the pack is fresh."""
        container, _index = split_locator(str(row.get("path") or ""))
        if container.is_file():
            pack.ensure(container)

    def prepare_key(self, row) -> str | None:
        """The CONTAINER — one preparation per container, not per record."""
        container, _index = split_locator(str(row.get("path") or ""))
        return str(container) or None

    def fetch(self, ref: DocRef) -> RawDoc:
        if ref.extra.get("kind") == "delete":
            raise ValueError("fetch() must not be called on a DeleteCitation ref "
                              "(the orchestrator skips fetch()/parse() for errored refs)")
        # index comes from the LOCATOR (which the manifest persists), with
        # extra["record_index"] as the discovery-time shortcut; a ref
        # reconstructed from a manifest row has only the locator
        container, index = split_locator(ref.locator)
        if index is None:
            index = ref.extra.get("record_index")
        if index is None:
            raise ValueError(f"pubmed locator carries no record index: {ref.locator!r}")
        _verify_md5_sidecar(container)
        # byte-identical to the former ET.tostring(root[index]) — pinned by
        # tests: raw_hash is how discovery detects a changed record
        data = pack.record_xml(container, index)
        h = hashlib.blake2b(data, digest_size=16)
        return RawDoc(ref=ref, raw_hash=h.hexdigest(), data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        elem = ET.fromstring(raw.data.decode("utf-8"))
        meta = medline.parse_article(elem)
        sections = [("Title", meta["title"])]
        if meta["abstract"]:
            sections.append(("Abstract", meta["abstract"]))
        return CanonicalDoc(
            uid=f"{self.source_id}:{meta['pmid']}",
            title=meta["title"],
            creators=[],
            language="en",
            date=None,
            subjects=meta["mesh"],
            keys=[],
            extra={"journal": meta["journal"], "date": meta["date"],
                   "revised": meta.get("revised")},
            sections=sections,
            raw_hash=raw.raw_hash,
        )

    def read_metadata(self, path: Path, native_id: int) -> dict:
        """The MANIFEST's view of a record — what the Library and the Ask
        page display. `plugin_io.metadata` prefers this hook and falls
        back to `canonical_doc()` when a plugin has none.

        THIS EXISTS ONLY BECAUSE `parse()` DROPS THE AUTHORS. `parse()`
        sets `creators=[]` (it has since the first commit), so the
        fallback computed `authors=""` and all 39,994,988 pubmed
        documents displayed no author while the origin XML carried them
        all along — `pack.authors_of` has been extracting them into the
        pack the whole time.

        It is a DISPLAY hook and nothing else. `parse()` is untouched, so
        the records keep their bytes, `content_hash` its value and the
        Codex's `parser.version` its truth: `hold records build` reads
        `plugin_io.canonical_doc`, never this. Correcting `creators`
        itself would bump PARSER_VERSION into `collection_id` and fork
        the collection, which is a different decision (operator,
        2026-09-12).

        Cost-neutral: the fallback already read this record's XML through
        the same pack and the same `medline.parse_article`. Every field
        below is byte-identical to what the fallback returned — lang is
        the same hardcoded "en", subjects the same mesh list — except
        `authors`, which stops being empty.
        """
        container, index = split_locator(str(path))
        if index is None:
            # a container-level ref (discover() yields one per file); it
            # has no record to read and never had metadata
            return {"title": "", "authors": "", "lang": "en",
                    "subjects": "", "bookshelves": ""}
        # the fallback reached this record through fetch(), which verifies
        # the container's md5 sidecar first; a display hook that skipped
        # that would be a quietly WEAKER path to the same bytes. Cached
        # per (path, mtime), so it costs a stat after the first record.
        _verify_md5_sidecar(container)
        elem = ET.fromstring(pack.record_xml(container, index).decode("utf-8"))
        meta = medline.parse_article(elem)
        return {"title": meta["title"], "authors": pack.authors_of(elem),
                "lang": "en", "subjects": "|".join(meta["mesh"]),
                "bookshelves": ""}
