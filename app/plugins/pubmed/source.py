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
from pathlib import Path
from xml.etree import ElementTree as ET

from ...acquire.checksum import verify_sidecar
from ...acquire.spec import ChecksumSource
from ..base import CanonicalDoc, DiscoverState, DocRef, LicenseClass, PrivacyClass, RawDoc
from . import medline

DELETE_MARKER = "deleted (DeleteCitation)"
_MD5_SIDECAR = ChecksumSource(kind="sidecar", sidecar_suffix=".md5", sidecar_algo="md5")


def _open_container(path: Path):
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")


def _parse_container(path: Path) -> ET.Element:
    with _open_container(path) as f:
        return ET.parse(f).getroot()


def _verify_md5_sidecar(container: Path) -> None:
    """Delegates to the shared app/acquire/checksum.py helper (same logic
    P0.4's acquisition engine uses for origin sidecar verification)."""
    verify_sidecar(container, _MD5_SIDECAR)


class PubmedSource:
    source_id = "pubmed"
    license_class = LicenseClass.YELLOW   # publisher copyright on text; raw is pointer-only (§8)
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "abstract"

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        containers = sorted(
            p for p in root.iterdir()
            if p.is_file() and (p.name.endswith(".xml") or p.name.endswith(".xml.gz")))
        for container in containers:
            st = container.stat()
            try:
                root_el = _parse_container(container)
            except ET.ParseError as e:
                yield DocRef(source_id=self.source_id, native_id=container.name,
                             locator=str(container), error=f"unparseable container: {e}")
                continue
            for i, child in enumerate(root_el):
                if child.tag == "PubmedArticle":
                    pmid_el = child.find("MedlineCitation/PMID")
                    pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""
                    if not pmid:
                        continue
                    yield DocRef(source_id=self.source_id, native_id=pmid,
                                 locator=str(container), mtime=int(st.st_mtime), size=st.st_size,
                                 extra={"record_index": i, "kind": "article"})
                elif child.tag == "DeleteCitation":
                    for pmid in medline.parse_delete_pmids(child):
                        yield DocRef(source_id=self.source_id, native_id=pmid,
                                     locator=str(container), mtime=int(st.st_mtime), size=st.st_size,
                                     error=DELETE_MARKER,
                                     extra={"record_index": i, "kind": "delete"})

    def fetch(self, ref: DocRef) -> RawDoc:
        if ref.extra.get("kind") == "delete":
            raise ValueError("fetch() must not be called on a DeleteCitation ref "
                              "(the orchestrator skips fetch()/parse() for errored refs)")
        container = Path(ref.locator)
        _verify_md5_sidecar(container)
        root_el = _parse_container(container)
        elem = root_el[ref.extra["record_index"]]
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
