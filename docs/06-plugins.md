# 6. Plugins

A plugin is how a source enters Louise. It says where the source's
files are and how to fetch them, how to turn a file into documents, and
what may be done with the result. It does not embed, index, share or
check anything: those are the platform's, the same for every source.
A plugin is code in the platform, reviewed like any code, and named by
the Codex through the platform image's digest (§2.1).

## 6.1 What a source declares

```python
class CorpusSource(Protocol):
    source_id: str                 # "pubmed" — the Codex's `plugin`, and the uid prefix
    protocol_version: int          # the version of this protocol the plugin implements (§7.4)
    PARSER_VERSION: int            # bumped when and only when parse() output changes for some input
    license_class: LicenseClass    # green | yellow | red                  (§4.4)
    corpus_provider: str           # whom the TEXT is from — "Wikipedia contributors" (02 §2.7)
    corpus_attribution: str        # the credit line for them, byte-exact  (02 §2.7)
    privacy_class: PrivacyClass    # public | restricted | never-share     (§4.4)
    modality: Modality             # text | dna — what the bytes are        (§2.4)
    erasure: Erasure               # append-only | retractable | erasable   (§4.4)
    mutability: Mutability         # immutable | mutable                    (§6.3)
    native_id_order: str           # "bytes" | "numeric" — how this source's ids sort (§2.2)
    default_layers: list[Layer]    # what `hold collection create` writes unless the recipe says otherwise (§6.3)
    default_slicing: dict          # likewise (§2.2)
    default_facets: list[Facet]    # likewise: which fields are filterable and listable (05 §5.7)

    def discover(self, state: DiscoverState) -> Iterator[DocRef]: ...
    def fetch(self, ref: DocRef) -> RawDoc: ...
    def parse(self, raw: RawDoc) -> CanonicalDoc: ...

    # optional; the default yields nothing and the collection has no
    # relations member. Called once per parsed document, so an edge is
    # discovered where its source document is.
    def relations(self, doc: CanonicalDoc) -> Iterator[Edge]: ...
```

`Edge` is `{to: str, type: str}` — the uid the edge points at and the
plugin's own name for the kind of edge. The platform supplies the other
two fields 09 §9.14 requires: `from`, which is the document's own uid,
and `snapshot`, the snapshot in which the edge is first listed, which
the plugin cannot know. An edge may point at a uid this collection does
not hold; a dangling edge is data, not an error (05 §5.5).

`protocol_version` is a class attribute the plugin declares; the
platform refuses to load a plugin whose version it does not implement,
and the version is part of the image the Codex names by digest (§7.4).
`PARSER_VERSION` is bumped when and only when `parse()` produces
different output for some input. A bump is never a new snapshot of the
same collection: the Codex names `parser.version`, so the collection
that carries the new parser is a daughter (§2.7, `continues`) unless
`records-vs-origin` under the new image proves it equivalent (§3.4), in
which case nothing changes.

The classes are the plugin's **defaults**, because they are properties
of the source: `hold collection create` copies them into the Codex, and
the Codex is authoritative from then on — `license.class`, `privacy`,
`modality` and `erasure` are read from the Codex and never from the
plugin (§2.1). A recipe (§6.6) may tighten a class the plugin declares
— yellow to red, public to never-share — and never loosen it: a
hand-populated folder under a plugin may hold material its author never
saw. What each licence and privacy class permits is the table in §4.4.

`corpus_provider` and `corpus_attribution` are declared beside the
classes and for the same reason — they are properties of the source,
not of a creator's judgement — and `create` copies them into the
`kind: "corpus"` entry of `credits` (02 §2.7). They are NOT tightenable
and not overridable: a recipe may make a collection stricter about what
it shares, and may not restate whom the text is from. Every plugin
declares them, GREEN ONES INCLUDED: a public-domain corpus compels no
attribution and gets one anyway, because a provider that asks nothing is
still the provider, and a rule with an exception is a rule someone will
land on the wrong side of.
`modality` decides which encoders and rerankers are eligible: a Codex
whose encoder declares another modality than the plugin is refused at
`create` and at boot, the same class of error as a space mismatch
(§2.4). `erasure: erasable` requires `privacy: never-share`, and `hold
records build` refuses the pair — erasable with any privacy that lets a
shard travel — because sharing cannot un-publish: once a shard is
served, no later snapshot reaches the peers holding it (§4.4; §1,
promise 7). An erasable collection is built and searched like any
other; what it may not do is leave the peer.

### The types

The three shapes the methods exchange are these; 09 §9.17 carries their
exact fields.

```
DocRef        {source_id, native_id, locator, extra, deleted}
RawDoc        {ref, bytes, origin_hash}
DiscoverState {corpus_root, cursor, committed(uid)}
```

*Discover* enumerates documents cheaply, without reading them. It reads
only the local corpus directory that acquisition populated
(`DiscoverState.corpus_root`); a plugin never opens a network
connection — every byte that arrives on a peer arrives through the
acquisition spec (§6.2) and the budget (§4.3). A `DocRef` names one
document: `native_id`, a `locator` — the origin locator of §6.4, the
path of the document's origin bytes relative to the origin base plus,
where it is a member of a container, the member offset — and `extra`,
discovery-time hints the
platform consumes and discards. Discovery keeps a **cursor** in the
collection's state directory so that a scan that is interrupted
continues, never restarts. The cursor is the platform's, written
through `DiscoverState`: a container is staged when its last ref has
been yielded and promoted into the saved cursor only when
`committed(uid)` says the manifest has durably committed that ref. The
cursor therefore never runs ahead of the manifest: after an
interruption the next scan skips exactly the containers whose rows are
committed and re-enumerates the one in flight. A container modified
during the scan is not staged. A plugin writes cursor state by no
other route.

`fetch()` and `parse()` may be called long after discovery with a
`DocRef` rebuilt from the manifest, carrying `source_id`, `native_id`
and `locator` and nothing else. Everything `parse()` needs beyond the
bytes is encoded in the locator or derived from the bytes; nothing is
carried in `extra`.

### The uid rule

`uid = "<plugin>:<native_id>"` — exactly that, computed by the platform
from the `DocRef`, and what `parse()` returns as `uid` must equal it.
`native_id` is a **string**: the origin's own identifier, unique within
the plugin and stable for the life of the document — a PMID, a
Gutenberg ebook number, a Wikipedia page id, an ELI work key. The
platform never parses it, with one exception: shards sort records by
native_id (§2.2), and the plugin declares `native_id_order` — `numeric`
when every id is a decimal integer and integer order is the natural one,
`bytes` otherwise (UTF-8 byte order) — which `hold collection create`
writes as `slicing.order`. Where an origin's unique key is not usable as
an id, the plugin derives one by a pure function of that key, records
the rule beside itself, refuses a collision at discovery rather than
trusting it, and keeps the origin's own identifiers in `keys`. A plugin
has one `source_id`, equal to the Codex's `plugin`; a collection built
over a drag keeps the origin's `source_id`, so its uids and chunk ids
are the origin's.

### Fetch, and the error contract

*Fetch* returns the raw bytes of one document as a `RawDoc`, with
`origin_hash` = BLAKE2b-128 of that document's own origin bytes (§6.4).
Three things can go wrong, and the plugin says which:

| the plugin | means | the platform |
|---|---|---|
| raises `Deferred` | a transient condition: a locked file, a container still being written, a resource not yet there | retries later; records nothing on the document |
| raises `Unavailable` | the origin cannot supply this document — no text form exists, the entry is withdrawn | records it on the document, unprefixed; the document leaves every selection until a human intervenes or a later scan clears it |
| raises anything else | the plugin is broken | halts the stage for this collection and names the exception; no document is marked |

A plugin never converts a transient failure into `Unavailable`, and
never converts a corpus fact into an ordinary exception: the first hides
a hole in the collection behind a permanent mark, the second makes a
fact about the origin read as a blocked pipeline. Deferral is not
failure, and a container that cannot be enumerated is reported by
`discover()` as `Deferred` for its documents, never as documents with
an error.

### The deletion signal

A document the origin retracted — PubMed's `DeleteCitation`, a deleted
Wikipedia page — is reported by `discover()` as a `DocRef` with
`deleted: true` and the origin's reason in `extra.reason`. The record
keeps its row, leaves every stage, and `hold records build` writes the
tombstone into the next snapshot's `delta.tombstoned` (§2.2). Deletion
is never expressed as an error, and never as absence: a document the
scan does not report is unchanged. Under `erasure: append-only` a
removal still needs a tombstone with a reason; amendment and repeal are
metadata — `in_force` in the record's `extra`, the relation in the
relations member (§5.5) — never a tombstone.

### Parse

*Parse* turns the bytes into the one shape every source produces:

```python
@dataclass(frozen=True)
class CanonicalDoc:
    uid: str                        # "pubmed:38112034" — equal to "<plugin>:<native_id>"
    title: str
    creators: list[str]
    language: str                   # ISO 639-1 (639-3 where no two-letter code exists); "und" when unknown
    date: str | None                # ISO 8601, "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    subjects: list[str]
    keys: list[str]                 # canonical entity ids this document is about: "<namespace>:<id>" (§5.2)
    extra: dict[str, Any]           # plugin-defined, fixed keys; origin_url for a red source
    sections: list[tuple[str, str]] # (heading, text), in order
    figures: list[Figure]           # the plates, in order of first appearance; [] when none
    origin_hash: str                # BLAKE2b-128 of this document's own origin bytes (§6.4)
```

```python
@dataclass(frozen=True)
class Figure:
    hash: str                       # H over `data` — SHA-256, not BLAKE2b (09 §9.5)
    media_type: str                 # one of 08 §8.7's admitted types
    bytes: int                      # len(data)
    seq: int                        # order of first appearance in the text, from 0
    data: bytes                     # the plate itself; the only field that does not
                                    # reach the record line — it goes to the container
```

A plugin whose source carries no plates returns `[]` and is done: the
protocol is what the platform may CALL, not what a source happens to
hold. A plugin that DOES yield figures leaves the caption in `sections`
where the text is — a caption is text, it is what a search matches, and
a reader holding no plates still wants it — so a figure appears twice, in
two different roles, and that is deliberate.

Everything downstream — chunking, records, indexes, vectors — works on
`CanonicalDoc` and never on a source's native form. The platform, not
the plugin, **cleans** the sections between the parser and the record
(§6.3): the `sections` written into the record are the cleaned text —
UTF-8, NFC, LF newlines, no trailing whitespace — and `content_hash`
covers exactly that (§2.3, 09 §9.5). A document with no sections is
still a record, with zero chunks; only `Unavailable` removes a document.
A red-class source sets `extra.origin_url`, the page a reader shows in
place of the text; its records travel as pointer records, `sections`
empty, and the text stays on the peers that fetched it (§4.4).

A plugin may add cheaper accessors for its own stages — `prepare(row)`
and `prepare_key(row)` build a per-container cache once, ahead of the
stage that reads it; `read_metadata`, `read_chapters` and `read_text`
read one part of a document — but the platform never *requires* them:
every stage falls back to `fetch` and `parse`, and any other method is
ignored.

### The terms text

A yellow or red source ships its terms as published, byte-exact, at a
path the acquisition spec names (`terms_text_path`; the repository marks
the file binary so no checkout converts its line endings). `hold
collection create` hashes it into `license.terms_sha256`; `hold records
build` copies it into the drag as `LICENSE-TERMS`; the acknowledgement
of §4.4 is a timestamp and that hash (09 §9.13). A green source ships
none and its Codex carries `terms_sha256: null`.

## 6.2 Acquisition

A plugin that fetches its own corpus ships an **acquisition spec**:
transport (HTTPS, rsync, a model hub, a torrent), base URL, how the
origin lists its files, how it checksums them, how it publishes updates
(a directory of update files, dated dumps, a full resync), and the
etiquette the origin asks for — connections, bandwidth, hours. Secrets
are referenced by file path, never by value.

The spec has two halves, and 09 §9.17 lists which field is in which.
What **identifies** the origin enters the Codex as `origin.spec`,
serialised in the canonical form of 09 §9.1, and is therefore part of
the collection's identity: the transport, the origin's own URL (never a
mirror), the layout and how the complete listing is enumerated, the
checksum scheme, the update form, and for a dated-dump origin the dump
pinned. What is **local** stays out of it: mirrors, etiquette, the
profiles and their estimated sizes, the probe file, and every path,
including the terms file and any key or token. Two peers that create
the same collection from the same plugin write the same `origin` block
byte for byte.

Beside the spec, the Codex `origin` block records four things the
plugin declares and a snapshot cannot be computed without (§2.2):

- `clock` — the IANA zone of the origin's own clock, in which the
  cutoff instant is 00:00:00 on `update.day` (`America/New_York` for
  PubMed, `UTC` for the Wikimedia dumps);
- `cutoff_rule` — how the origin's state at an instant is enumerated,
  so a peer fetching days after the cutoff reproduces the snapshot the
  cutoff names: for an origin of dated dumps, the last dump published
  before the instant (Wikipedia); for an origin of update files, the
  files published before it (PubMed); for a listing with a per-record
  date, the records whose date is at or before it (Gutenberg, by the
  release date). A record without the date the rule needs is excluded
  and counted, never kept;
- `unit` — how a record's origin bytes are reached (§6.4);
- `form` — which origin form was taken, and why (§6.4).

A **profile** — `demo`, `starter`, `full`, a language — is a named
selection of the collection's slices and nothing else; it decides which
members a peer holds, never what a snapshot contains. A capped selection
(three books of Gutenberg) is a fetch that does not complete, and `hold
snapshot create` refuses while the fetch for its cutoff is incomplete
(§2.2). The listing is enumerated in full whatever the profile.

The spec's one contract: **the origin's complete listing is enumerated
per pass and recorded**. Every progress figure and every estimate a
peer shows depends on knowing the denominator, and a partial listing
makes them lie.

The platform runs the spec. It resumes at byte granularity, draws from
the peer's single bandwidth budget (§4.3), and does not move a byte of a
yellow or red source before the acknowledgement of §4.4 is recorded. A
spec with several endpoints names a **probe file**, a small file at its
official relative path; a mirror that cannot serve it has another layout
and is not used. A destination file already present at the origin's
declared size is checksummed and adopted, never re-fetched. An origin
that is an API is still a spec: its listing is the API's catalogue and
its files are the per-document responses, fetched by the platform at
the cutoff into the corpus directory — the plugin's `discover()` and
`fetch()` then read them there like any other origin.

## 6.3 Chunking, per layer

A collection declares its **layers** once, at creation: one chunker per
layer, and a rule that puts every document in exactly one layer. The
common case is one layer, and then the layer simply is the collection's
chunker. Layers exist for corpora whose documents come in kinds that
should be cut differently — abstracts and full papers — while staying in
one space.

The chunkers are platform code, registered by id; a new chunker is a
reviewed addition to the platform with its own version, never a global
setting. Every input a chunker takes other than the cleaned text is a
parameter in its layer block, so the layer — id, version and params — is
what `space_id` hashes (§2.4); a chunker reads no node configuration.

| id | cuts | params | overlap |
|---|---|---|---|
| `fixed` | windows of `tokens` with `overlap` between neighbours, paragraph-aligned; a paragraph larger than the window is split hard | `{tokens, overlap}` | by construction |
| `cdc` | content-defined boundaries from a rolling hash over the text, per section, between `min` and `max` characters and concentrated near `target`; an edit moves only its own neighbourhood | `{target, min, max}` | none |
| `abstract` | one chunk per record: title and sections joined | none | none |
| `verbatim` | one chunk per section, exactly as the plugin emitted it — the chunking a pre-embedded archive delivered, preserved so its vectors map one to one | none | none |

```
layers: [{chunker: {id: "verbatim", version: 1, params: {}},     rule: "archive:upstash-wikipedia-2024-06"},
         {chunker: {id: "cdc", version: 1, params: {target: 4000, min: 1000, max: 8000}}, rule: "default"}]
```

A layer's **rule** is one of three forms: `default`, every document;
`archive:<archive id>`, the documents a pre-embedded archive covers —
read from that archive's `coverage` in the Codex's `credits` (02 §2.7),
so the rule is computable from the record and the Codex alone, and a
platform build cannot move a document between layers. Only the
`kind: "archive"` entries are read here: the corpus and community
entries carry `coverage: "all"` because they are owed for everything,
and a layer rule that read them would put every document in the first
layer;
`field:<name>=<value>`, the documents whose record field (a top-level
field, or `extra.<key>`) has that value. Rules are
evaluated in list order and the first match wins; the last layer's rule
is `default` and no other layer's is.

A **chunk id** is `<uid>:<seq>:<chunker version>`, `seq` counting from 0
over the document's chunks in section order; the chunker id is implied
by the document's layer, which is derived from the Codex's rules and
the record's own fields — never stored in the record (02 §2.3), so
there is one answer to which chunker cut a document and it is always
the current one. A chunk's hash is BLAKE2b-128 of its text. The
overlap that T1 measures (§3.5) holds by construction for `fixed`
layers only; the band applies to those and is skipped for the others.

**What a changed record costs.** When a record's `content_hash` changes
it is recut, and its vectors are carried over **by chunk hash, never by
chunk id**: the peer maps the hashes of the chunks it already holds for
that record to their vectors, and every recut chunk whose hash is in
that map keeps its vector under whatever id it now has. Only a chunk
whose text is new is embedded, and a vector whose hash no longer appears
is dropped. The map is local — a peer rebuilds it from the records and
vectors it already holds — so nothing new travels and no format carries
it.

Matching on the hash and not the id is what makes the rule worth having.
`seq` is a position, so inserting one paragraph at the top of an article
shifts the id of every chunk after it while changing the text of none of
them; matched by id, correcting one word would re-embed the whole
document — the very cost `cdc` exists to avoid, returning through the
index instead of the character offset. Matched by hash, an inserted
paragraph costs one chunk in every family: `cdc` and `verbatim` because
their boundaries follow the text, `fixed` because the unchanged prefix
before the edit still hashes the same and only the tail is re-embedded.

Chunking is a pure function of the cleaned text and the layer's chunker
version. Two peers with the same platform image cut the same document
into the same chunks with the same ids, or a check fails and names the
document. Any change to chunking or cleaning bumps the version of the
thing that changed, explicitly. A chunker id and version once named by
a published Codex stay callable in every later platform image; a
release that removes one names the collections it orphans.

**Cleaning is the platform's**, not the plugin's: it runs between
`parse()` and the record, the same code for every source, and the
record's `sections` are its output (§6.1). `cleaning_version` is a
property of the platform image, read from the Codex and from nowhere
else — a node has no global chunk or cleaning version.

A cleaning change re-derives every record, so it is not a sister: a
sister inherits the corpus, and there is no corpus to inherit when the
cleaned text has moved (02 §2.7). It is a new collection over the same
origin — and not a corrective sister either, however much better the
new cleaning is, because cleaning that merely drops what a reader
wanted fails no check: the records still reproduce, the chunks still
recompute (03 §3.4, §3.6). A cleaner that is non-deterministic is a
different matter, and a defect like any other. The re-derivation is
the price of a record that means one thing, and `cleaning_version` is
in `space_id` so that no vector outlives the text it was computed
from.

The source's declared **mutability** decides the chunker a plugin puts
in its `default_layers`: an immutable source —
documents are final once published, growth is append-only — takes
`fixed` chunks with overlap: simpler, better recall, nothing is ever
re-cut. A mutable source — documents are revised indefinitely — takes
`cdc` chunks that cut on the text itself, so one edited paragraph
disturbs only its own neighbourhood instead of shifting every boundary
after it and re-embedding a document for a typo. The switch is about the
cost of re-chunking, not the frequency of change: Gutenberg adds books
daily and is immutable. The layer's `chunker.id` is always what counts:
a plugin over a mutable origin may declare `abstract` or a
structure-aware chunker contributed to the registry, and a Codex naming
it is valid for either mutability.

## 6.4 Fidelity to the origin

A plugin may **prune** — drop the ePub when the plain text is kept, drop
markup, drop dump indexes — but never alter a record in a way that
prevents reproducing it from the origin. Concretely: every record's
`origin_hash` is the hash of the bytes it was parsed from, and the Codex
names the acquisition spec and parser version that produced it, so a
verifier can re-fetch a sample and re-parse it (§3.2). A record with no
`origin_hash` is refused at build.

"The bytes it was parsed from" are this document's own origin bytes —
a Gutenberg book's `.txt`; one PubMed article's XML; one Wikipedia
page's as the multistream index extracts it — and `origin_hash` is
BLAKE2b-128 of them, 32 lowercase hex, no prefix (09 §9.1).

**The unit of verification is the unit a query returns.** A book for
Gutenberg, an article for PubMed, a page for Wikipedia: one record, one
`origin_hash`, one thing anyone can check. What the origin happens to
ship them in does not change what a reader asked for, and so must not
change what can be verified. A container-wide hash would answer a
question nobody asks — "do these thirty thousand abstracts verify?" —
and would leave the one that is asked unanswerable without fetching all
of them. It would also break the red class outright (04 §4.4): every
peer holding PubMed fetches article text from the origin one article at
a time, and could not check a single one of them against a hash that
covers a container.

Two guarantees are separate and stay separate. The ORIGIN's own
checksums — PubMed's `.md5` beside each container — verify that a FETCH
arrived intact; the acquisition spec declares them and the platform
applies them (§6.2). `origin_hash` verifies that a RECORD is faithful
to the document it was parsed from. Conflating the two buys the first's
provenance at the cost of the second's granularity, which is the wrong
trade: the fetch is checked once, the record is checked forever.

Where the document is a member of a container, how it is extracted is
part of the parser, pinned by the platform image, and a change to it
bumps `PARSER_VERSION` — so two peers on one image cut the same bytes
and hash the same value. Every record carries `origin`: the locator of
its own bytes — a URL, or a path relative to the origin base followed
by the member the plugin extracted — so `records-vs-origin` knows what
to re-fetch without the producer's manifest. The locator is the
`DocRef.locator` discovery produced (§6.1); an absolute path never
enters a record.

Where several origin forms exist for the same document, the plugin
chooses one and **records the choice with its reason** in the Codex, as
`origin.form`, at creation; the note beside the plugin explains the
choice and binds nothing. A different form is a different Codex. The
rule for a new plugin: prefer the form the origin itself versions and
checksums, at the lowest level of processing the parser can consume —
that is the form a verifier can re-fetch and reproduce byte for byte.
For a certified source the sealed form wins even when a structured form
would parse more robustly, because the seal covers what was parsed and
authenticity is the point; the parser's fragility is then pinned by the
platform image's digest and caught by `records-vs-origin`.

**Where only one form carries the figures, that form wins, and the cost
is named rather than discovered.** Gutenberg is the worked case: the
plates are in the epub and in nothing else the project holds, so a
collection that carries them takes the epub as its `origin.form` and
says so. The cost is that the two forms do not clean to the same text —
the plain-text edition is hard-wrapped and an epub's text is reflowable,
so `sections` differ, `content_hash` differs, and no vector computed
against the other form is admissible (09 §9.7). That is a corpus to
re-embed, and it is a decision to take with the number in hand, not a
surprise to meet during a build. It is also why the two are separate
collections rather than one amended: a Codex is never edited, and a
collection whose records gain plates is a new collection (02 §2.7).

## 6.5 Reference sources

Not everything should be embedded. Gene symbols, variant frequencies,
norm identifiers are lookup tables, and the right answer to a key is the
row, not a semantic neighbour. A **reference source** declares tables
with typed columns and a primary key, and typed **links** between tables
— variant → gene — that the query plan may follow (§5.2). It syncs from
its origin like a corpus source, and its rows enter answers as placed
facts, never as ranked results.

A reference source is **not a collection** in this version. It has no
Codex, no snapshots and no members; it is a set of node-local tables
that every peer syncs from the origin, and a query answers from what
this peer has synced. Sharing a reference source as a drag is a later
amendment.

```python
class ReferenceSource(Protocol):
    source_id: str
    protocol_version: int
    ref_version: int               # bumped when the schema or the derivation of rows from the origin changes
    license_class: LicenseClass    # the same gate as §6.1: no sync before the acknowledgement
    namespaces: list[str]          # the canonical-id namespaces this source owns: "hgnc", "rs", "eli"

    def schema(self) -> RefSchema: ...
    def sync(self, state: RefSyncState) -> Iterator[RefRecordBatch]: ...
```

A `RefSchema` names tables; a table names its columns, each typed
`TEXT`, `INTEGER` or `REAL`, and its primary key; a link names its
source table and column and its target table and column, and is
declared by the referencing table — the referenced source need not know
it exists (09 §9.17). `sync()` yields batches that each name one table;
the platform upserts rows by the table's primary key, so a sync is
idempotent and resumable; keys the schema does not declare are ignored,
a declared column missing from a row is an error. A yellow or red
reference source is not synced before its acknowledgement is recorded,
by the resolution of §4.4 and nothing re-implemented beside it.

Canonical ids are `<namespace>:<id>`, species- or
jurisdiction-qualified from the start (`hgnc:11998`, `rs:80359351`,
`eli:it:…`, `pmid:38112034`), and a namespace is owned by the reference
source that registers it. A reference source may ship the **resolver**
§5.2 describes — `resolve(query) → entities`, `expand(entity) →
entities` — as a pure function of the query text and the tables' current
content; a resolver never returns a bare symbol.

## 6.6 From a plugin to a collection

1. The plugin is added to the platform and released; the release image
   has a digest.
2. `hold collection create <plugin> --recipe FILE` writes the Codex and
   prints its id. The recipe is a TOML file carrying every Codex field
   the plugin does not default — the encoder and the rest of the `space`
   block, the descriptive block, the work parameter, the validation
   plan, the publication kind, and the archives this collection will
   draw vectors from, by id, which `create` expands into `credits` from
   the platform's registry (02 §2.7) — and any default it tightens; the
   plugin supplies
   `plugin`, `origin`, `parser`, `layers`, `slicing`, the classes, the
   cadence and the CORPUS PROVIDER, and the platform supplies the
   toolchain digests. `credits` is assembled from all three and from
   nothing a creator writes: the corpus entry from the plugin's declared
   provider and attribution, the archive entries from the recipe's
   `archives` expanded through the registry, and the community entry
   from `embedding` — present when it is `"open"` or `"endorsed"`,
   absent when it is `"certifier-only"`. A recipe that tried to name a
   corpus or community credit is refused: those are facts about the
   plugin and the collection's own policy, and a creator free to phrase
   them could phrase them away. The
   calibration comes first and needs no record: the calibration set is
   a corpus-independent file shipped with the platform, `create` embeds
   it with the named encoder, writes the reference vectors' hash into
   `space.calibration` and their envelope into `space.envelope`, and
   stores the vectors as the drag member `calibration/<set>.safetensors`
   (§2.4, 09 §9.12). The Codex's hash is the collection's identity.
3. `hold collection ack` prints the terms and records the
   acknowledgement (§4.4). For a yellow or red collection nothing below
   runs before it.
4. `hold records fetch` runs the acquisition spec to the cutoff; `hold
   records build` parses, cleans, slices, shards and hashes; `hold
   snapshot create` freezes the first snapshot.
5. `hold vectors build` embeds — all of it, some of it, or none; the
   collection's TOML says which (`off`, `on-demand`, `all`, §7.8), and
   the coverage map says how much. Vectors computed on demand by a
   compliant encoder are bundled and shared like any other. A
   pre-embedded public archive is imported through the same ladder as a
   peer's bundle, with the archive named as producer. An archive whose
   entry is not already among the Codex's `kind: "archive"` credits is
   REFUSED, not
   credited late: the Codex is written once and never edited (02 §2.1),
   so an archive a collection means to draw on is named in the recipe at
   step 2 and hashed into the identity there. The platform's registry is
   where an archive is declared — its id, coverage, producer, the
   licence its vectors carry and the attribution that licence requires —
   and `create` copies that entry into the Codex whole. So an importer
   neither composes the acknowledgement nor is free to omit it, no later
   build can alter it, and a collection cannot acquire a debt it never
   declared. The registry ALSO holds, per archive, the SET OF KEYS that
   archive's bundles are signed with, and `create` does not copy those
   into the Codex. They are what 03 §3.5's refusal resolves a
   `producer_pubkey` through, and they are the one part of an archive's
   entry that legitimately changes: a key is lost and reissued, and an
   archive that published nothing signed may start. A collection's
   credit is a permanent statement about whom it stands on; the keys are
   how a peer recognises that archive's work this year. The first is
   hashed into the identity, the second ships in a platform release and
   may be updated by one. An archive that publishes no signed bundles at
   all — a directory of `.npy` files, a dataset on a model hub — has an
   empty key set: it is credited and imported exactly as any other, and
   §3.5's refusal simply never fires for it, because no bundle can carry
   a key nobody has. What a peer converts from such an archive it signs
   with its own key, and those bundles are its own work under its own
   name (01, promise 3); the archive is credited in the Codex, which is
   where the licence's attribution clause is satisfied.
6. `hold check` runs the validation plan.
7. `share` makes the result available and announces it; for a
   `privacy: public` collection it follows a completed fetch on its own
   unless the collection's TOML says otherwise.

From then on the platform's monthly cycle — fetch the delta by the
cutoff rule, build the changed shards, embed the changed chunks — needs
nothing from the plugin that the first pass did not.
