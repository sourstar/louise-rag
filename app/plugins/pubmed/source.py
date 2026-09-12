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
    #: 6 since 2026-09-12. 5 corrected `creators`, `lang` and `date`
    #: against 09 §9.5; 6 satisfies PROMISE 9 — parser 5 still published
    #: 16 of the ~103 things a <PubmedArticle> asserts about its article,
    #: and the other 87 were not wrapping, not boilerplate, not our
    #: corpus-add date and not the provider's packaging. See
    #: docs/notes/origin-fidelity-audit.md for the field-by-field list.
    #: Folded into one bump because nothing is built from 5 — there are no
    #: v1.0 records yet, so there is no artefact for 5 and 6 to be
    #: distinguished in. Invariant 3: bumped explicitly either way.
    PARSER_VERSION = 6
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
        meta = medline.parse_full(elem)
        sections = [("Title", meta["title"])]
        if meta["abstract"]:
            sections.append(("Abstract", meta["abstract"]))
        return CanonicalDoc(
            uid=f"{self.source_id}:{meta['pmid']}",
            title=meta["title"],
            # PARSER VERSION 5. Three §9.5 fields were wrong or absent in
            # version 4, and the origin carried all three all along:
            #
            #   creators  was always []          — AuthorList, on 99.0%
            #   language  was hardcoded "en"     — <Language>, on 100%,
            #                                      and 9.6% of this corpus
            #                                      is NOT English
            #   date      was always None        — PubDate, on 100%
            #
            # None of them is in `content_hash` (BLAKE2b-128 over
            # [title, sections] alone, §9.5), so correcting them moves no
            # chunk id, no chunks_root and no vector.
            creators=medline.authors(elem),
            language=medline.language(elem),
            date=medline.publication_date(elem),
            # WITH THE QUALIFIER. "Neoplasms/genetics", not "Neoplasms" —
            # §9.5's own pubmed example writes it that way and this
            # plugin declares `subjects` a tree facet with separator "/",
            # which until now had nothing to split.
            subjects=meta["subjects"],
            # §6.5 canonical ids for WHAT THE DOCUMENT IS ABOUT: its MeSH
            # descriptors, by UI, sorted by bytes as §9.5 requires. The
            # article's OWN identifiers (doi, pmc, pii) are not what it is
            # about and live in extra.ids.
            keys=sorted({f"mesh:{m['ui']}" for m in meta["extra"].get("mesh", [])
                         if m.get("ui")}),
            # `date` is GONE from extra: the top-level field now carries it
            # in §9.5's own form, and holding it twice — once as
            # "1953-11" and once as the pack's "1953-Nov" — is two answers
            # to one question in a published artefact. `revised` stays
            # because the cutoff (`--until`) is denominated in it, and
            # `journal` because a reader is shown it.
            # EVERYTHING ELSE THE ORIGIN SAYS (promise 9). Parser 5 put
            # two keys here and published 16 of the ~103 things a
            # <PubmedArticle> asserts; `parse_full` collects the rest and
            # empty values are omitted, so a sparse record stays small.
            # `authors_full` is separate from `creators` because §9.5
            # makes `creators` a list of STRINGS — an ORCID and an
            # affiliation need objects, and neither is droppable.
            extra={**meta["extra"], "authors": meta["authors_full"]},
            sections=sections,
            raw_hash=raw.raw_hash,
        )
