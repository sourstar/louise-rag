"""Plugin capability protocols — the extension points every corpus/reference
source implements. See docs/architecture-v2.md §2.

No I/O and no manifest/SQLite imports live here: this module must stay generic
across every current and future CorpusSource / ReferenceSource / EntityResolver.
"""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol


class LicenseClass(str, Enum):
    """Gates P2P sharing per plane (architecture-v2.md §8)."""
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class PrivacyClass(str, Enum):
    """architecture-v2.md §9."""
    PUBLIC = "public"
    RESTRICTED = "restricted"
    NEVER_SHARE = "never-share"


@dataclass(frozen=True)
class DocRef:
    """One discoverable document — cheap to enumerate, no content read yet.

    `locator` is opaque outside the owning plugin (a filesystem path today; a
    container-file byte range for a future container-based source). `error`
    is set when a document is discoverable but not usable (e.g. no text file
    found) — the orchestrator records the error without calling fetch().

    `scope` separates the two things an errored ref can mean:

      "document"  — this IS a document, and it is broken. It belongs in the
                    manifest carrying its error (a book whose .txt is
                    missing stays a real, countable, retryable document).
      "container" — this is a SCAN REPORT: a container file could not be
                    enumerated at all (a truncated download, a corrupt
                    archive, a dump missing its index). It is not a
                    document and must never become one.

    Why the distinction is load-bearing: container failures used to be
    stored as documents keyed on the FILENAME, and nothing ever removed
    them. Observed live on PubMed — 264 orphan rows, 116 of them for
    containers that had since enumerated perfectly — and once an operator
    pressed "clear errors & retry" every one became eligible for the rdf
    stage, where fetch() can only refuse: the locator has no record index.
    A container-scope ref is logged instead, so a later clean scan heals it
    by simply not reporting it again.
    """
    source_id: str
    native_id: str
    locator: str
    mtime: int | None = None
    size: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    scope: Literal["document", "container"] = "document"


@dataclass(frozen=True)
class RawDoc:
    """Result of fetch(): bytes are available locally and hashed."""
    ref: DocRef
    raw_hash: str
    path: Path | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class CanonicalDoc:
    """architecture-v2.md §2 — the source-agnostic document shape every
    plugin's parse() produces."""
    uid: str
    title: str
    creators: list[str]
    language: str
    date: str | None
    subjects: list[str]
    keys: list[str]
    extra: dict[str, Any]
    sections: list[tuple[str, str]]
    raw_hash: str


@dataclass(frozen=True)
class DiscoverState:
    """Whatever a CorpusSource needs to enumerate its documents. The
    orchestrator constructs and supplies this; Gutenberg only reads
    `corpus_root`. `state_dir` (the collection's data directory, beside its
    manifest) is where an incremental source keeps its scan cursor — the
    "PubMed baseline+updatefiles cursor" this field was reserved for; None
    means stateless full scans (tests, ad-hoc calls)."""
    corpus_root: Path
    state_dir: Path | None = None


class CorpusSource(Protocol):
    """documents → chunks → vectors. architecture-v2.md §2."""
    source_id: str
    license_class: LicenseClass
    privacy_class: PrivacyClass
    chunking_profile: str

    def discover(self, state: DiscoverState) -> Iterator[DocRef]: ...
    def fetch(self, ref: DocRef) -> RawDoc: ...
    def parse(self, raw: RawDoc) -> CanonicalDoc: ...


@dataclass(frozen=True)
class RefColumn:
    """One column of a RefTable. `type` is a SQLite storage class:
    TEXT | INTEGER | REAL."""
    name: str
    type: str


@dataclass(frozen=True)
class RefTable:
    name: str
    columns: tuple[RefColumn, ...]
    primary_key: str   # column name; sync() upserts are keyed on this


@dataclass(frozen=True)
class RefLink:
    """A declared, typed traversal edge expand() may follow (§4) — e.g.
    variant→gene. Declared by the "child" table (the one referencing another
    source's table); the referenced source needs no knowledge of the link."""
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass(frozen=True)
class RefSchema:
    source_id: str
    tables: tuple[RefTable, ...]
    links: tuple[RefLink, ...] = ()


@dataclass(frozen=True)
class RefRecordBatch:
    """What sync() yields — table-scoped rows, applied idempotently by that
    table's declared primary key (never a raw dump the caller must interpret)."""
    table: str
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RefSyncState:
    """Parallels DiscoverState for CorpusSource. Real HTTPS-dump acquisition
    is P0.4's AcquisitionSpec job; for now a ReferenceSource just reads
    fixture/operator-supplied files under ref_root."""
    ref_root: Path


@dataclass(frozen=True)
class Entity:
    """A resolved/expanded entity — species-qualified id per §4
    (e.g. "hgnc:1101"). `kind` names the owning RefTable ("hgnc_gene") so
    expand()/fact-lookup can dispatch without re-parsing the id string."""
    entity_id: str
    kind: str
    source_id: str
    label: str | None = None


class RefLookup(Protocol):
    """Minimal read interface EntityResolver needs, implemented by
    app/refs/store.py's RefStore. Declared here (not importing app.refs) so
    this module stays free of SQLite/manifest imports, per its own docstring
    rule — the same reasoning DiscoverState follows for CorpusSource."""
    def rows(self, table: str, where: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def get(self, table: str, pk_value: Any) -> dict[str, Any] | None: ...
    def links_for(self, table: str) -> list[RefLink]: ...
    def pk_of(self, table: str) -> str: ...
    def owner_of(self, table: str) -> str: ...


class ReferenceSource(Protocol):
    """Lookup tables — no chunking, no vectors. architecture-v2.md §2/§4."""
    source_id: str
    ref_version: int

    def schema(self) -> RefSchema: ...
    def sync(self, state: RefSyncState) -> Iterator[RefRecordBatch]: ...


class EntityResolver(Protocol):
    """Query text → entity keys. architecture-v2.md §2/§4."""

    def resolve(self, query: str, refs: RefLookup) -> list[Entity]: ...
    def expand(self, entity: Entity, refs: RefLookup) -> list[Entity]: ...
