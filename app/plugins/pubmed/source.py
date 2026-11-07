"""PubmedSource — CorpusSource adapter over MEDLINE/PubMed XML container
files (baseline + updatefiles are the same code path; see architecture-v2.md
§5.1). Proves the SPI on container files: one physical .xml(.gz) file holds
many records, so DocRef.locator names the container and
DocRef.extra["record_index"] names the record's position within it.

DeleteCitation entries are discovered as DocRefs with `deleted` set (06
§6.1's deletion signal; reason `retracted`, 09 §9.4: "the origin says the
record should not have existed") and `error` set to DELETE_MARKER — the
generic orchestrator (app/discovery.py) skips fetch()/parse() for any
errored ref, takes a deleted one out of every index it is in
(app/unindexable.py), and upserts a manifest row carrying that error, which
is exactly a tombstone: the row exists (so a later re-add is detected as a
real change, not a phantom insert) but is permanently excluded from further
stage processing by the existing `WHERE error IS NULL` gates — no manifest
schema change needed. A later baseline/updatefile pass over the same files
re-derives the identical tombstone (idempotent).

UPDATE FILES APPLY IN THE ORIGIN'S ORDER (docs/notes/pubmed-updatefiles.md).
NLM numbers its files in one sequence per baseline year — the baseline
pubmed26n0001…n1334 in `baseline/`, the daily files from pubmed26n1335 in
`updatefiles/`, which acquisition lands under `updatefiles/` in the corpus
(09 §9.17 `updatefiles-dir`) — and a later file's record supersedes an
earlier one's. Two rules make the manifest the origin's state whatever
order files land or are scanned in:
  - an update file is enumerated only once its predecessor in the sequence
    is held (a baseline file, or an update file enumerated before it) and
    its own `.md5` beside it checks out; until then it is reported once per
    scan and waits, and every later file of its year waits behind it;
  - `origin_order` gives discovery each locator's place in the sequence
    (year, number, member), and a ref from an EARLIER place than the one a
    row holds is superseded and changes nothing (app/discovery.py) — so a
    cleared cursor or a re-fetched old file re-opens no revised record.

THE ORIGIN FORM, and why — invariant 11 wants it recorded beside the
plugin (06 §6.4). NLM serves the citations as the annual BASELINE and the
daily UPDATEFILES (MEDLINE XML, gzipped, each with an `.md5` sidecar) and
through the E-utilities API. Taken: the baseline and updatefiles
containers, read as published (`PUBMED_SPEC`) — the archival form the
origin itself versions and checksums, and the only one that carries the
`DeleteCitation` tombstones. The API returns the same records paginated,
unversioned and with no checksum a verifier could re-fetch against.
Both directories are one form: the baseline at the Codex's `url`
(`pubmed/baseline`), the update files in `delta.subdir` resolved against it
(`pubmed/updatefiles`), applied after the baseline in NLM's numbering; a
record parsed from an update file is located `updatefiles/<file>#<member>`
(09 §9.5's own example). docs/notes/pack-format.md, "Which origin form, and
why"; docs/notes/pubmed-updatefiles.md for the update files.
"""
import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ...acquire.checksum import verify_sidecar
from ...acquire.spec import ChecksumSource
from .. import cursor
from app.backend import OriginForm, Template
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, Issue, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc)
from . import medline, pack

DELETE_MARKER = "deleted (DeleteCitation)"
#: 09 §9.4's tombstone reason for a DeleteCitation: the origin says the
#: citation should not have existed (a duplicate, one made in error) — not
#: `withdrawn`, an origin that merely stops supplying a record
DELETE_REASON = "retracted"
_MD5_SIDECAR = ChecksumSource(kind="sidecar", sidecar_suffix=".md5", sidecar_algo="md5")
#: where acquisition lands the update files inside the corpus: the spec's
#: `delta.subdir` (PUBMED_SPEC, and the Codex's `origin.spec.delta`), which
#: tests/test_pubmed_updatefiles.py holds equal
UPDATES_SUBDIR = "updatefiles"
#: NLM's container names: `pubmed<YY>n<NNNN>.xml[.gz]` — one sequence per
#: baseline year, the update files numbered on from the baseline's last
_CONTAINER_NAME = re.compile(r"^pubmed(\d+)n(\d+)\.xml(?:\.gz)?$")


def sequence_of(container: Path) -> tuple[int, int] | None:
    """(baseline year, number) of an NLM container, or None for a name that
    is not NLM's."""
    m = _CONTAINER_NAME.match(container.name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _is_container(p: Path) -> bool:
    return p.is_file() and (p.name.endswith(".xml") or p.name.endswith(".xml.gz"))


def ordered_containers(root: Path) -> list[tuple[Path, bool]]:
    """Every container of the corpus as `(path, is_update)`, in the ORIGIN's
    order: by NLM's (year, number), the baseline (the corpus root) before
    the update files (`updatefiles/`) where one number were ever in both;
    names NLM does not use first, by name, as before."""
    found = [(p, False) for p in root.iterdir() if _is_container(p)]
    updates = root / UPDATES_SUBDIR
    if updates.is_dir():
        found += [(p, True) for p in updates.iterdir() if _is_container(p)]

    def key(item):
        path, is_update = item
        seq = sequence_of(path)
        return ((0, -1, -1) if seq is None else (1, *seq)), is_update, path.name
    return sorted(found, key=key)

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


def _update_unverified(container: Path) -> str | None:
    """Why an update file may not be applied yet, or None: its `.md5` must
    be beside it and agree. Acquisition renames a file into place before it
    hashes it (app/acquire/engine.py), and a file placed by hand was never
    checked at all — an update applied from unchecked bytes and then
    re-fetched would be applied again after its successors."""
    sidecar = Path(str(container) + _MD5_SIDECAR.sidecar_suffix)
    if not sidecar.is_file():
        return f"no {sidecar.name} beside it"
    try:
        _verify_md5_sidecar(container)
    except Exception as e:  # noqa: BLE001 — TransportError (a mismatch), OSError: not applied
        return f"{type(e).__name__}: {e}"[:200]
    return None


#: The origin's inconsistencies this plugin's parse reports (06 §6.1,
#: amended 2026-10-25; app/issues.py). The parser already falls back from
#: PubDate to its MedlineDate year and then to ArticleDate, so a record with
#: no date at all is one NCBI gave no date of any kind (operator's choice A,
#: STATUS 2026-10-25).
ISSUES = {
    "no-publication-date": "the citation states no publication date of any kind (PubDate year, "
                           "MedlineDate year, ArticleDate); `date` is null",
    "no-title": "neither ArticleTitle nor VernacularTitle has text; the record's title is empty",
    "other-abstract-only": "the citation's only abstract is an OtherAbstract; `sections` hold "
                           "the title alone (docs/notes/pubmed-other-abstracts.md)",
}


def issues_of(meta: dict, date: str | None) -> list[Issue]:
    """What `parse` met in one citation — read off what it already parsed."""
    out = []
    if date is None:
        raw = (meta.get("extra") or {}).get("pub_date_raw") or ""
        out.append(Issue("no-publication-date",
                         f"PubDate {raw!r} has no year and there is no ArticleDate" if raw
                         else "no PubDate and no ArticleDate"))
    if not (meta.get("title") or "").strip():
        out.append(Issue("no-title", "ArticleTitle and VernacularTitle are both empty or absent"))
    others = (meta.get("extra") or {}).get("other_abstracts") or []
    if not meta.get("abstract") and others:
        out.append(Issue("other-abstract-only", ", ".join(
            f"{o.get('type') or 'untyped'}/{o.get('language') or '?'}" for o in others)))
    return out


class PubmedSource:
    source_id = "pubmed"
    ISSUES = ISSUES
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
    #: app/backend.py: a record is a member of a gzip XML container, read from its pack
    backend = (OriginForm("the baseline and update XML containers", Template.CONTAINER_PACK, ("pubmed-pack",)),)
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
    #:
    #: 7: the VERNACULAR TITLE fallback. 771,527 documents (1.9%) had an
    #: empty `<ArticleTitle />` and a full `<VernacularTitle>`, so they
    #: carried no title, no authors, and an EMPTY "Title" chunk into the
    #: vectors — blank rows in the Library over a title the origin states
    #: outright. Unlike 5 and 6 this one DOES move `content_hash`, because
    #: §9.5 hashes [title, sections] and the title is a section here;
    #: it is free for the same reason, no v1.0 records exist yet.
    #: 8 since 2026-09-14, the fidelity sample check (180,000 records over
    #: six baseline files): OtherAbstract read from MedlineCitation, where it
    #: lives — parser 7 looked under Article and wrote none; investigators;
    #: chemical registry numbers; MeSH Type/AutoHM; supplementary-concept
    #: Type; every ELocationID with ValidYN and every ArticleId; the lists'
    #: CompleteYN; NumberOfReferences; general notes' owners; correction
    #: PMID versions; History times; cited works' doi/pmcid and nested
    #: reference lists; affiliation identifiers; the title and abstract as
    #: marked up. RECORDS ONLY: `sections` is untouched, so no chunk id and
    #: none of the archive's 35.4M vectors moves.
    PARSER_VERSION = 8
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE        # a retraction notice is a fact of the corpus
    native_id_order = "numeric"          # PMIDs, issued ascending
    default_facets = [
        {"field": "subjects", "kind": "tree", "separator": "/",
         "authority": {"name": "MeSH"}},
    ]

    #: 06 §6.2 (amended 2026-11-07, operator: C): NLM keeps every published
    #: file — the baseline and each update file — at its own path for the
    #: life of its baseline year, and removes the year's files when it
    #: publishes the next baseline. So a superseded part version is kept
    #: while the files that verify it are at the origin, and dropped at the
    #: rollover (docs/notes/pubmed-updatefiles.md §5; app/hold/shard.py
    #: `drop_past_period`), by `origin_period`.
    origin_versions = "history-until-baseline"

    @staticmethod
    def origin_period(origin: str) -> int | None:
        """The baseline year of the NLM container a record's `origin` names
        (`pubmed26n0001.xml.gz#3`, `updatefiles/pubmed26n1401.xml.gz#17`:
        26), or None for an origin NLM did not name. A later year's
        baseline replaces every earlier year's files at the origin."""
        seq = sequence_of(Path(str(origin).split("#", 1)[0]))
        return seq[0] if seq else None

    @staticmethod
    def tombstone_reason(error: str | None) -> str | None:
        """09 §9.4's tombstone reason for a manifest row carrying `error`:
        `retracted` for a DeleteCitation, None for any other error (a
        document that is merely broken is not tombstoned for it)."""
        return DELETE_REASON if error == DELETE_MARKER else None

    def origin_order(self, locator: str) -> tuple[int, int, int] | None:
        """A locator's place in the origin's sequence: (baseline year, file
        number, member index) — what app/discovery.py compares, so that a
        ref from an earlier place than a row holds is superseded. None for
        a locator this plugin cannot place (a container NLM did not name)."""
        container, index = split_locator(str(locator))
        seq = sequence_of(container)
        if seq is None or index is None:
            return None
        return (seq[0], seq[1], index)

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        progress = cursor.Progress(state, self.source_id)
        # the places in NLM's sequence held here: every baseline container,
        # and each update file once its predecessor is held and it checks out
        ready: set[tuple[int, int]] = set()
        waiting: set[int] = set()          # years whose update files wait behind a gap
        for container, is_update in ordered_containers(root):
            seq = sequence_of(container)
            if is_update:
                why = None
                if seq is None:
                    why = "not an NLM container name"
                elif seq[0] in waiting:
                    continue                # behind the file already reported
                elif progress.unchanged(container):
                    ready.add(seq)          # it was applied in order when it was applied
                    continue
                elif (seq[0], seq[1] - 1) not in ready:
                    why = f"its predecessor pubmed{seq[0]:02d}n{seq[1] - 1:04d} is not held yet"
                else:
                    why = _update_unverified(container)
                if why is not None:
                    if seq is not None:
                        waiting.add(seq[0])
                    # a scan REPORT, never a document (DocRef.scope): logged, and
                    # healed by a later scan that no longer reports it
                    yield DocRef(source_id=self.source_id, native_id=container.name,
                                 locator=str(container), scope="container",
                                 error=(f"update file {container.name} waits: {why} — update "
                                        f"files apply in NLM's order, each checked "
                                        f"(docs/notes/pubmed-updatefiles.md)")[:300])
                    continue
                ready.add(seq)
            elif seq is not None:
                ready.add(seq)
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
                                     error=DELETE_MARKER, deleted=True,
                                     extra={"record_index": i, "kind": "delete",
                                            "reason": DELETE_REASON})
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

    EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def current(self, ref: DocRef, *, client, workdir) -> RawDoc:
        """05 §5.3's freshness: the article as E-utilities serves it now —
        the same PubmedArticle element the baseline and update files
        carry, so `parse` reads it unchanged. One article per request,
        the `tool` named as NCBI's etiquette asks."""
        r = client.get(self.EFETCH_URL, params={"db": "pubmed", "id": str(ref.native_id), "retmode": "xml",
                                                "tool": "louise"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        article = root.find("PubmedArticle")
        if article is None:
            raise RuntimeError(f"PMID {ref.native_id}: the origin serves no article for it now")
        data = ET.tostring(article)
        return RawDoc(ref=ref, raw_hash=hashlib.blake2b(data, digest_size=16).hexdigest(), data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        elem = ET.fromstring(raw.data.decode("utf-8"))
        meta = medline.parse_full(elem)
        sections = [("Title", meta["title"])]
        if meta["abstract"]:
            sections.append(("Abstract", meta["abstract"]))
        date = medline.publication_date(elem)
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
            date=date,
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
            issues=issues_of(meta, date),
        )
