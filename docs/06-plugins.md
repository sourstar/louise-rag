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
    source_id: str                 # "pubmed"
    license_class: LicenseClass    # green | yellow | red        (§4.4)
    privacy_class: PrivacyClass    # public | restricted | never-share
    mutability: Mutability         # immutable | mutable         (§6.4)
    PARSER_VERSION: int            # bumped on any change to what parse() produces

    def discover(self, state: DiscoverState) -> Iterator[DocRef]: ...
    def fetch(self, ref: DocRef) -> RawDoc: ...
    def parse(self, raw: RawDoc) -> CanonicalDoc: ...
```

*Discover* enumerates documents cheaply, without reading them, and
keeps a resumable cursor in the collection's state directory — a scan
that is interrupted continues, never restarts. *Fetch* returns the raw
bytes of one document. *Parse* turns them into the one shape every
source produces:

```python
@dataclass(frozen=True)
class CanonicalDoc:
    uid: str                        # "pubmed:38112034"
    title: str
    creators: list[str]
    language: str
    date: str | None
    subjects: list[str]
    keys: list[str]                 # canonical entity ids this document is about
    extra: dict[str, Any]
    sections: list[tuple[str, str]] # (heading, text), in order
    raw_hash: str                   # hash of the origin bytes this was parsed from
```

Everything downstream — chunking, records, indexes, vectors — works on
`CanonicalDoc` and never on a source's native form. A plugin may add
cheaper accessors for its own stages, but the platform never *requires*
them: every stage falls back to `fetch` and `parse`.

## 6.2 Acquisition

A plugin that fetches its own corpus ships an **acquisition spec**:
transport (HTTPS, rsync, a model hub, a torrent), base URL, how the
origin lists its files, how it checksums them, how it publishes updates
(a directory of update files, dated dumps, a full resync), and the
etiquette the origin asks for — connections, bandwidth, hours. Secrets
are referenced by file path, never by value.

The spec's one contract: **the origin's complete listing is enumerated
per pass and recorded**. Every progress figure and every estimate a
peer shows depends on knowing the denominator, and a partial listing
makes them lie.

The platform runs the spec. It resumes at byte granularity, draws from
the peer's single bandwidth budget (§4.3), and does not move a byte of a
yellow source before the acknowledgement of §4.4 is recorded.

## 6.3 Chunking, per layer

A collection declares its **layers** once, at creation: one chunker per
layer, and a rule that puts every document in exactly one layer. The
common case is one layer, and then the layer simply is the collection's
chunker. Layers exist for corpora whose documents come in kinds that
should be cut differently — abstracts and full papers — while staying in
one space.

Chunking is a pure function of the cleaned text and the layer's chunker
version. Two peers with the same platform image cut the same document
into the same chunks with the same ids, or a check fails and names the
document. Any change to chunking or cleaning bumps the version of the
thing that changed, explicitly.

The source's declared **mutability** decides the chunker family.
An immutable source — documents are final once published, growth is
append-only — takes fixed-width chunks with overlap: simpler, better
recall, nothing is ever re-cut. A mutable source — documents are revised
indefinitely — takes content-defined chunks that cut on the text itself,
so one edited paragraph disturbs only its own neighbourhood instead of
shifting every boundary after it and re-embedding a document for a
typo. The switch is about the cost of re-chunking, not the frequency of
change: Gutenberg adds books daily and is immutable.

## 6.4 Fidelity to the origin

A plugin may **prune** — drop the ePub when the plain text is kept, drop
markup, drop dump indexes — but never alter a record in a way that
prevents reproducing it from the origin. Concretely: every record's
`origin_hash` is the hash of the bytes it was parsed from, and the Codex
names the acquisition spec and parser version that produced it, so a
verifier can re-fetch a sample and re-parse it (§3.2). A record with no
`origin_hash` is refused at build.

Where several origin forms exist for the same document, the plugin
chooses one and **records the choice with its reason** beside itself.
For a certified source the sealed form wins even when a structured form
would parse more robustly, because the seal covers what was parsed and
authenticity is the point; the parser's fragility is then pinned by the
platform image's digest and caught by `records-vs-origin`.

## 6.5 Reference sources

Not everything should be embedded. Gene symbols, variant frequencies,
norm identifiers are lookup tables, and the right answer to a key is the
row, not a semantic neighbour. A **reference source** declares tables
with typed columns and a primary key, and typed **links** between tables
— variant → gene — that the query plan may follow (§5.2). It syncs from
its origin like a corpus source, and its rows enter answers as placed
facts, never as ranked results.

## 6.6 From a plugin to a collection

1. The plugin is added to the platform and released; the release image
   has a digest.
2. `hold collection create` writes the Codex: plugin, origin, that
   digest, the encoder image, layers, slicing, licence, cadence. The
   Codex's hash is the collection's identity.
3. `hold records fetch` runs the acquisition spec; `hold records build`
   parses, slices, shards and hashes; `hold snapshot create` freezes the
   first snapshot.
4. `hold vectors build` embeds — all of it, some of it, or none; the
   coverage map says how much. A pre-embedded public archive is imported
   through the same calibration gate as a peer's bundle, with the archive
   named as producer.
5. `hold check` runs the validation plan. `share` makes the result
   available and announces it.

From then on the platform's monthly cycle — fetch the delta, build the
changed shards, embed the changed chunks — needs nothing from the plugin
that the first pass did not.
