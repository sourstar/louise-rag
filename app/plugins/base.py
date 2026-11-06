"""Plugin capability protocols — the extension points every corpus/reference
source implements. See docs/notes/architecture-v2.md §2.

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


class Mutability(str, Enum):
    """Does this source REVISE what it has already published?

    Not a statement about how often it changes — Project Gutenberg adds
    books constantly — but about whether existing documents are edited in
    place. That is the only case where a chunk boundary moving matters, and
    it is therefore what decides how the text is chunked:

      IMMUTABLE  a document, once published, is final; growth is
                 append-only. Fixed-width chunking is right — simpler, it
                 overlaps for recall, and nothing is ever re-chunked.

      MUTABLE    documents are revised indefinitely. Fixed-width boundaries
                 are counted from the start of the text, so inserting one
                 paragraph shifts every boundary after it and re-embeds a
                 whole document for a typo. Content-defined chunking cuts on
                 the content itself, so an edit disturbs only its own
                 neighbourhood (app/chunking.py::chunk_cdc).

    A source that is mostly-immutable-but-occasionally-revised belongs in
    IMMUTABLE: this switch is about the COST of re-chunking, not about the
    fact of change. Revisit per source if one ever proves otherwise.
    """
    IMMUTABLE = "immutable"
    MUTABLE = "mutable"


class Modality(str, Enum):
    """What the bytes are (docs/02 §2.4, docs/06 §6.1). It decides encoder
    and reranker eligibility: a text encoder is not offered a DNA
    collection, and a DNA space's tolerance means nothing to a text one."""
    TEXT = "text"
    DNA = "dna"


class Erasure(str, Enum):
    """How content may leave a collection (docs/04 §4.4).

      APPEND_ONLY  nothing leaves without a trace; a removal is a
                   tombstone with a reason.
      RETRACTABLE  the publisher may retract a record, and the tombstone
                   stays.
      ERASABLE     removal may be mandatory and must leave no trace.

    Sharing cannot un-publish: once a shard is served, no later snapshot
    reaches the peers who hold it. So ERASABLE requires
    `privacy: never-share`, and a SHARD build refuses the pair otherwise.
    Every collection that travels is APPEND_ONLY or RETRACTABLE.
    """
    APPEND_ONLY = "append-only"
    RETRACTABLE = "retractable"
    ERASABLE = "erasable"


#: How a source's `native_id` sorts within a slice (docs/02 §2.2, docs/09
#: §9.3). "numeric" is integer order of the id; "bytes" is UTF-8 byte order.
#: It is what makes shard ranges a partition, so `hold records build`
#: refuses a record that sorts before the open shard's `first_id`.
NativeIdOrder = Literal["bytes", "numeric"]

#: The plugin protocol version this platform implements (docs/06 §6.1,
#: docs/09 §9.17). The platform refuses to load a plugin whose version it
#: does not implement, and the version is part of the image the Codex names
#: by digest.
PROTOCOL_VERSION = 1
#: `origin_versions` (06 §6.2): the origin keeps its history / replaces in place.
ORIGIN_HISTORY, ORIGIN_REPLACES = "history", "replaces"


def origin_versions(plugin) -> str:
    """A plugin's `origin_versions`; ORIGIN_HISTORY — keep — when it says nothing."""
    v = getattr(plugin, "origin_versions", ORIGIN_HISTORY)
    return v if v in (ORIGIN_HISTORY, ORIGIN_REPLACES) else ORIGIN_HISTORY


@dataclass(frozen=True)
class Edge:
    """One typed edge out of a document, yielded by the optional
    `relations()` (docs/06 §6.1; docs/09 §9.14). `to` is the uid the edge
    points at — which this collection need not hold, a dangling edge being
    data rather than an error — and `type` is the plugin's own name for the
    kind of edge. The platform supplies `from` (the document's own uid) and
    `snapshot` (the one in which the edge is first listed), which the plugin
    cannot know."""
    to: str
    type: str


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


class NoCurrentForm(Exception):
    """A plugin's `current()` saying there is no per-record current version
    of this record at the origin to ask for (05 §5.3's `unavailable`): a
    record read from a static archive, a form the origin does not serve
    one record at a time."""


@dataclass(frozen=True)
class RawDoc:
    """Result of fetch(): bytes are available locally and hashed."""
    ref: DocRef
    raw_hash: str
    path: Path | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class Figure:
    """One plate, as 09 §9.5 names it in a record line.

    A figure is NOT text and NOT derived: it travels as the origin's own
    bytes under `figures/<slice>/`, addressed by `sha256`, while its
    CAPTION stays in `sections` and stays indexed. So `content_hash` —
    which is over title and sections — never covers a figure, and a
    collection that gains plates re-uses every text vector it had.

    One entry per APPEARANCE, in reading order, so `figures[N]` is the Nth
    marker in the text. A plate used twice is two entries with one hash:
    the container stores the bytes once. Deduplicating would break the
    alignment from the first repeat onward, and 39% of Gutenberg's
    illustrated books repeat a plate.

    `carried` is True for every image the origin has (operator, 2026-10-25:
    "Every plate", 08 §8.7 amended): a type outside 08 §8.7's closed list
    or a figure over its caps is carried as opaque bytes like any other,
    and only DISPLAY refuses it. False remains in the format for records
    written before the amendment (gutenberg parser ≤ 10), which named an
    SVG without carrying it; a reader must still tell "not here yet" from
    "not here ever" for those.
    """
    href: str
    media_type: str
    sha256: str
    bytes: int
    caption: str
    seq: int
    carried: bool = True

    def as_record(self) -> dict:
        """The record line's shape: 09 §9.5's "exactly these keys in this
        order" — `hash`, `media_type`, `bytes`, `seq`, `carried`.

        `hash` there, `sha256` here. The record line mixes two functions on
        one line — `content_hash` and `origin_hash` are BLAKE2b-128, a
        figure's hash is SHA-256 — so the Python field says WHICH, where
        the wire format has a table beside it that does. `href` and
        `caption` are ours alone: the first locates the bytes inside the
        origin unit for the container pass, the second is already in
        `sections`, and §9.18 refuses an unknown key on write.
        """
        return {"hash": self.sha256, "media_type": self.media_type,
                "bytes": self.bytes, "seq": self.seq, "carried": self.carried}


@dataclass(frozen=True)
class Issue:
    """An INCONSISTENCY IN THE ORIGIN that a parse met (06 §6.1, amended
    2026-10-25, operator: "any inconsistency is logged and flagged"): a
    catalogue entry missing or damaged, an image the epub references and
    does not hold, a record with no date of any kind. Not an error — the
    document parses and its record is written — and not part of the
    record: it never reaches a record line, `content_hash`, `origin_hash`
    or a chunk, so reporting one more or one fewer moves no byte any peer
    holds and bumps no PARSER_VERSION. Whoever runs the parse records it in
    the collection's local state (app/issues.py), keyed (uid, code).

    `code` is one the plugin declares in its `ISSUES` (or a platform code,
    app/issues.py `PLATFORM`); `detail` says which file, element or value,
    in plain words."""
    code: str
    detail: str = ""


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
    #: 09 §9.5's plates, for a `record_line` 2 collection. Empty for every
    #: plugin that yields none, which is what keeps a figureless
    #: collection's record line byte-identical to `record_line` 1.
    figures: list[Figure] = field(default_factory=list)
    #: the origin's inconsistencies this parse met (`Issue`), OUTSIDE the
    #: record: `pack/build.py` writes the line from the fields above and
    #: never reads this one. Empty for a plugin that reports none.
    issues: list[Issue] = field(default_factory=list)


@dataclass
class DiscoverState:       # not frozen: received/committed advance during the scan
    """Whatever a CorpusSource needs to enumerate its documents. The
    orchestrator constructs and supplies this; Gutenberg only reads
    `corpus_root`. `state_dir` (the collection's data directory, beside its
    manifest) is where an incremental source keeps its scan cursor — the
    "PubMed baseline+updatefiles cursor" this field was reserved for; None
    means stateless full scans (tests, ad-hoc calls).

    The last three fields are the orchestrator's durability signal back to
    the plugin, so a scan cursor can be saved INCREMENTALLY without ever
    getting ahead of the manifest (app/plugins/cursor.py `Progress`):
    `received` is the count of refs the orchestrator has taken from
    discover() so far, `committed` the count it has durably written, and
    `on_commit` is called after every commit (including the last). A
    container is safe to remember as done only once `committed` covers its
    last ref — otherwise an interrupted scan would skip a container whose
    rows died with the transaction.

    `snapshot` is the collection's pinned dated snapshot of its origin
    (`Collection.snapshot`), or None. Acquisition fetches only that
    snapshot, but dated snapshots coexist in one corpus by design, so a
    source whose origin publishes several reads the pinned one alone —
    otherwise two dumps of one wiki mint the same ids and every scan flips
    between them. An optional input the platform supplies; sources that
    have no snapshots ignore it."""
    corpus_root: Path
    state_dir: Path | None = None
    received: int = 0
    committed: int = 0
    on_commit: list = field(default_factory=list)
    snapshot: str | None = None
    #: the scan's stop (an event: SIGTERM, the node's mode, the collection's
    #: removal), or None. A source whose discover runs long work of its own
    #: (wikidata's pack pass) ends it at its unit when this is set, and the
    #: node profile's pause in that work waits on it (app/cpu_limit.py)
    stop: Any = None


class UnindexableDocument(RuntimeError):
    """The CORPUS cannot supply this document's content — no text file in
    the mirror, a retracted entry — as opposed to a stage failing at its
    own work. A plugin raises this from fetch()/read paths so the worker
    records the corpus fact UNPREFIXED (the Library's 'not indexable'
    bucket: retrying cannot change it, the next scan re-applies it) instead
    of a stage-prefixed error that reads as a blocked pipeline. Subclasses
    RuntimeError so pre-existing broad handlers keep working."""


class CorpusSource(Protocol):
    """documents → chunks → vectors. architecture-v2.md §2.

    A new plugin that also fetches its corpus ships an AcquisitionSpec
    (app/acquire/spec.py) — read its docstring's LISTING CONTRACT
    (amendment 33): the origin's complete listing is enumerated per pass
    and recorded, and every UI denominator and ETA depends on it.

    What a source declares is docs/06 §6.1. It is what `hold collection
    create` writes into a Codex unless the recipe says otherwise — the
    plugin supplies `plugin`, `origin`, `parser`, `layers`, `slicing`, the
    classes and the cadence, and the recipe supplies what only a creator
    knows: the space, the work parameter, the descriptive block, the
    validation plan and the archives to draw on (docs/06 §6.6).
    `app/hold/template.py` is where the two meet."""
    source_id: str
    protocol_version: int          # the version of this protocol implemented (06 §6.1)
    PARSER_VERSION: int            # bumped when and only when parse() output changes
    license_class: LicenseClass
    #: 06 §6.1: whom the TEXT is from, and the credit line for them.
    #: `create` copies both into the `kind: "corpus"` entry of `credits`
    #: (02 §2.7), which every collection carries — green ones included,
    #: because a provider that asks for nothing is still the provider.
    #: Not tightenable and not overridable by a recipe: a creator free to
    #: phrase an attribution is a creator free to phrase it away.
    corpus_provider: str
    corpus_attribution: str
    corpus_license: str | None
    privacy_class: PrivacyClass
    modality: Modality
    erasure: Erasure
    chunking_profile: str
    mutability: Mutability
    #: 06 §6.1 (amended 2026-10-30): each origin form read and its template
    #: (app/backend.py — tuple[backend.OriginForm, ...]); refused when missing
    #: (tests/test_backend_templates.py)
    backend: tuple
    native_id_order: NativeIdOrder
    #: 06 §6.2 (amended 2026-10-25, operator: "what really matters in the
    #: end is the verifiability"): does the origin keep earlier versions a
    #: verifier can still fetch (`ORIGIN_HISTORY`, the default when a plugin
    #: says nothing) or overwrite them in place (`ORIGIN_REPLACES`)? A
    #: superseded part version is kept only where it can still be checked
    #: against its origin (01 promise 7, 02 §2.3; app/hold/shard.py).
    #: Outside the Codex: it decides what a node keeps, never a byte.
    origin_versions: str

    # 06 §6.1's `default_layers`, `default_slicing` and `default_facets`, as
    # plain dicts: this module stays free of app.hold so that it can be
    # imported by anything. `app/hold/template.py` materialises them into
    # the Codex's Layer, slicing and Facet objects, and derives the first
    # two from `chunking_profile`/`mutability` and app/pack/slicing.py
    # where a plugin does not override them — a plugin declares its cut
    # once, not twice.
    default_layers: list           # [{chunker: {id, version, params}, rule}]
    default_slicing: dict          # 09 §9.3's slicing block
    default_facets: list           # [{field, kind, separator, authority}] — 05 §5.7
    # Optional (06 §6.1, amended 2026-10-25): {code: one-line meaning} for
    # every `Issue` this plugin's parse may report; a plugin without it
    # reports none. A code outside it is refused when recorded.
    ISSUES: dict[str, str]

    def discover(self, state: DiscoverState) -> Iterator[DocRef]: ...
    def fetch(self, ref: DocRef) -> RawDoc: ...
    def parse(self, raw: RawDoc) -> CanonicalDoc: ...

    # Optional. The default yields nothing and the collection has no
    # relations member. Called once per parsed document, so an edge is
    # discovered where its source document is (06 §6.1; 09 §9.14).
    def relations(self, doc: CanonicalDoc) -> Iterator[Edge]: ...

    # Optional, for a collection whose profile declares a validity axis
    # (app/validity.py; 05 §5.1): what THIS version states ends ANOTHER
    # Work's validity — `(key, day)` pairs, the Work by one of its canonical
    # ids (05 §5.2) and the ISO day it is no longer in force from. Read at
    # the rdf stage and applied by key, in either order of arrival (06 §6.1;
    # docs/notes/normattiva-multivigenza-review.md §3.2, case 2).
    def closures(self, doc: CanonicalDoc) -> Iterator[tuple[str, str]]: ...


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
    def rows(self, table: str, where: dict[str, Any] | None = None, *,
             order_by: str | None = None, limit: int | None = None) -> list[dict[str, Any]]: ...
    def count(self, table: str, where: dict[str, Any] | None = None) -> int: ...
    def columns(self, table: str) -> set[str]: ...
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
