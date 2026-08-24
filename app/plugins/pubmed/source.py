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
import gzip
import hashlib
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from ...acquire.checksum import verify_sidecar
from ...acquire.spec import ChecksumSource
from .. import cursor
from ..base import CanonicalDoc, DiscoverState, DocRef, LicenseClass, Mutability, PrivacyClass, RawDoc
from . import medline

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


def _open_container(path: Path):
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")


# One-slot caches keyed on (path, mtime): the worker walks documents in
# PMID order, which is container order — without this, EVERY record access
# would re-verify and re-parse a ~16 MB container (observed live: 30k
# records per container file). Guarded by a lock: the parallel embed stage
# (recipe.concurrency > 1) fetches from multiple threads, and neighbors in
# PMID order share a container, so one thread parses and the rest reuse.
import threading

_cache_lock = threading.Lock()
_parsed_cache: dict = {}
_verified_cache: set = set()


def _parse_container(path: Path) -> ET.Element:
    key = (str(path), path.stat().st_mtime_ns)
    with _cache_lock:
        if _parsed_cache.get("key") == key:
            return _parsed_cache["root"]
        # Cold parses are ~6.5 s each (measured live), so every one must be
        # visible: name the container coming in and the one evicted. A
        # healthy walk logs one line per container (~150 batches); a line
        # per batch means the cache is thrashing and this IS the pipeline's
        # cost. Straight to stderr — the manifest may be mid-transaction.
        prev = _parsed_cache.get("key")
        t0 = time.monotonic()
        with _open_container(path) as f:
            root = ET.parse(f).getroot()
        print(f"[pubmed cache] cold parse {path.name} "
              f"({time.monotonic() - t0:.1f}s), evicted "
              f"{prev[0].rsplit('/', 1)[-1] if prev else '(empty)'}",
              file=sys.stderr, flush=True)
        _parsed_cache.clear()
        _parsed_cache["key"] = key
        _parsed_cache["root"] = root
        return root


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
    license_class = LicenseClass.YELLOW   # publisher copyright on text; raw is pointer-only (§8)
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "abstract"
    # Articles are occasionally revised or retracted, but an abstract
    # is a single chunk — there are no downstream boundaries for an
    # edit to shift, so content-defined chunking would buy nothing.
    mutability = Mutability.IMMUTABLE

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
                root_el = _parse_container(container)
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
            for i, child in enumerate(root_el):
                if child.tag == "PubmedArticle":
                    pmid_el = child.find("MedlineCitation/PMID")
                    pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""
                    if not pmid:
                        continue
                    yield DocRef(source_id=self.source_id, native_id=pmid,
                                 locator=f"{container}{_LOCATOR_SEP}{i}",
                                 mtime=int(st.st_mtime), size=st.st_size,
                                 extra={"record_index": i, "kind": "article"})
                elif child.tag == "DeleteCitation":
                    for pmid in medline.parse_delete_pmids(child):
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
        root_el = _parse_container(container)
        elem = root_el[index]
        data = ET.tostring(elem, encoding="unicode").encode("utf-8")
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
            extra={"journal": meta["journal"], "date": meta["date"]},
            sections=sections,
            raw_hash=raw.raw_hash,
        )
