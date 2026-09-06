# 2. Collections

A collection is four things: a **Codex** that never changes, a chain of
**snapshots** that grows once a month, the **records** and **vectors**
those snapshots describe, and a **coverage map** that says which
vectors exist and who made them. The first three are content-addressed
and identical on every peer. The fourth is each peer's view, kept
current by gossip, and never the same document twice.

## 2.1 The Codex

The Codex is the recipe. It is written once, when a collection is
created, and never edited: any change produces a new Codex and
therefore a new collection (§2.7 says how the two are related).

```
codex = {
  kind: "louise-codex", version: 1,
  plugin: "pubmed",
  origin: <how to fetch the source: endpoints, listing, form chosen, and why>,
  toolchain: {
    platform_image:  "sha256:…",      -- the platform build: parser, chunker, cleaner, checks, and their libraries
    encoder_image:   "sha256:…"       -- the encoder build
  },
  parser: {id, version}, cleaning_version,        -- readable aliases of what the images contain
  layers: [{chunker: {id, version}, rule: "default"}],
  slicing: {keys: ["year"], year: {field: "date", bins: ["<1950", "5y<2000", "1y"]}},
  space: {encoder, dim, dtype, ctx, tolerance, calibration, epoch},
  license: {class: "yellow", terms_sha256: "…"},
  update: {cadence: "monthly", cutoff: "origin-clock-midnight", grace_days: 7},
  publication: "open",                -- or "certified", with a certifier block (§2.8)
  continues | derived_from | replaces: <codex hash> | null      -- lineage (§2.7)
}
collection_id = H(canonical-json(codex))
```

Three rules make it work.

**The hash is the identity.** Anyone who writes the same Codex has the
same collection. The key of whoever wrote it first is recorded in
catalogues as a fact — *first published by* — not as part of the
identity. No key is needed to continue a collection; §4 depends on
this.

**The toolchain is pinned by digest, not by label.** The bytes a parser
produces depend on its extraction library and interpreter as much as on
its own source, and an encoder's vectors depend on the build, not the
model name. So the Codex names container images by digest. A peer runs
its *own* platform; the digests tell it whether that platform is the
one this collection requires. `parser.version` and friends stay as
readable aliases without guarantee. A different platform image may
still contribute if it proves itself equivalent (§3.4).

**The Codex is always public and never executable.** It names code that
lives in the platform, reviewed like any code. It does not carry code.
A collection whose recipe is secret cannot be checked and is not
listed.

## 2.2 Snapshots

A snapshot is the collection's records at one instant: the origin's
state at midnight on its own clock, on the cadence's day. Its id is that
date ("2026-09"), which every peer can compute from the Codex without
asking anyone. Anything the origin publishes after that instant — new
content, an embargo lifted — belongs to the next snapshot.

```
snapshot = {collection_id, snapshot_id: "2026-09", previous: <hash>,
            shards: [{slice, n, root, ids: [first, last], records, chunks}],
            delta: {changed: [...], added: [...], tombstoned: [...]}}
```

Because records are reproducible byte for byte (§3.1), every honest
peer that computes a snapshot produces the same bytes and the same
hash. Signatures on a snapshot are attestations — *I computed this too*
— not permissions. Two different snapshots with the same id are the
proof that one producer's toolchain drifted, and name the shard where.

**A shard is closed once published.** New records go into new shards; a
record whose text changed re-issues its shard under the next snapshot;
a deleted or retracted record becomes a tombstone. A record is
"changed" when the hash of its cleaned text changes, whatever the
origin's own revision fields say. Sources that never delete (PubMed
flags retractions) need only tombstones; sources that edit (Wikipedia)
re-issue shards, and their history is the chain of snapshots.

## 2.3 Records

Records are the parsed text of documents and nothing else — no markup,
no origin files, no local extras. They are sliced by declared keys
(year, language) and sharded so that one shard is one unit of transfer,
of embedding and of verification: about 7,000 chunks, roughly one
GPU-hour on a reference encoder.

```
records/<slice>/NNNNN.jsonl.zst        -- JSON Lines, zstd frames, one record per line, with a seek index
```

Every record carries two hashes. `content_hash` is the hash of its
cleaned text; it decides what "changed" means. `origin_hash` is the
hash of the origin bytes it was parsed from; it lets anyone re-fetch
that file and reproduce the record. A corpus may be **pruned** — what
is not used is dropped — but never altered in a way that breaks that
reproduction. A record without an `origin_hash` is refused at build,
because a pack that verifies internally while being permanently
unverifiable against its source is the worst thing a collection can
ship.

Where an origin offers several forms — an ePub and a plain text, an XML
dump and an API, a sealed PDF and a structured page — the Codex records
which one was chosen and why. The form is part of what a verifier needs.

## 2.4 Vectors, spaces and the index

Each shard's vectors are one file: f16, one row per chunk, plus the
**bucket label** of each row.

```
vectors/<space>/<slice>/NNNNN.safetensors
vectors/<space>/centres/v<n>.safetensors           -- the index's centre points, per version, a few MB
vectors/<space>/buckets/v<n>/<group>.safetensors   -- the same vectors re-sorted by bucket (optional)
```

**One collection, one space.** A space is a function from cleaned text
to vectors, and vectors from different functions cannot be compared:

```
space_id = H(encoder_image, layers, cleaning_version, epoch)
```

Chunking is declared per **layer** — one chunker per layer, every
document in exactly one layer by a declared rule, the layer list fixed
at creation. The common case is one layer, and then the layer simply is
the collection's chunker. Two collections with the same `space_id` are
in one space and can be searched as one ranked list; a collection with
a different encoder or chunker is a different collection (§2.7). A
mismatch between a vector table's space and its collection's Codex is a
refuse-to-start error, never a warning.

The **calibration** block is the contract for anyone who wants to
contribute vectors: a small set of chunk ids and their reference
vectors. A peer whose encoder differs from the reference build — another
driver, a quantised variant — embeds those chunks and must reach cosine
≥ `tolerance` on the quorum before its vectors are accepted. A peer that
fails may still search with what it holds.

The **index** is a set of centre points trained once on a corpus-wide
sample. Every vector's label is its nearest centre, and search probes a
few buckets instead of everything. Index versions are additive: centres
are added, never moved, so a label is correct at every version and
older is slower, never wrong. The bucket-ordered copy is optional and
lets a light peer or a topical download take a few groups instead of
the whole space.

## 2.5 The coverage map

Which chunks have vectors, from which producer, at what tolerance
result: this changes with every accepted bundle and has no author. Each
peer assembles its own map from the signed bundles and attestations it
has seen, and exchanges it with others by periodic digests (§4.3). It is
the one part of a collection that is not content-addressed, and the
only part that is allowed to differ between two peers for a while.

## 2.6 The pack on disk

```
<collection>/
├── codex.json
├── snapshots/2026-09.json              -- the chain; each names its predecessor
├── validation.json                     -- which checks apply, how much to sample (§3.2)
├── seals/2026-09.json                  -- certified collections: origin seal evidence (§3.7)
├── records/<slice>/NNNNN.jsonl.zst
├── index/<slice>/NNNNN/                -- the text index of that shard (optional to share)
└── vectors/<space>/…                   -- as above
```

A **member** is what a peer fetches or shares: one slice of one kind —
`records-year=1975-1979`, `vectors-<space>-year=1975-1979`,
`buckets-<space>-v2-g07`. "PubMed before 1980, records and vectors" is a
list of members, and the snapshot says their sizes and hashes before
the first byte moves.

## 2.7 Lineage: how a collection changes

Since the Codex is immutable, every change to *what a collection is*
makes a new Codex. Three kinds of change, three relations:

| | **daughter** | **sister** | **corrective sister** |
|---|---|---|---|
| what changed | the outside world: origin format or address, licence of new items; the toolchain adapts | the space: another encoder or chunker over the same corpus | a defect in the parent's records or vectors |
| records up to the transition | inherited by hash | inherited by hash | recomputed from the origin |
| records after it | the new toolchain's | identical to the parent's — same Codex fields, same bytes | the corrected toolchain's |
| space | the same; vectors inherited | new; vectors from zero | new by construction; from zero |
| Codex field | `continues`, `from_snapshot` | `derived_from` | `derived_from` + `replaces: {codex, defect}` |

A **daughter** continues the collection. The transition is a snapshot
boundary: records up to `N` are the parent's, from `N+1` the daughter's;
none is ever produced by both. Vectors survive because the space is
unchanged — the daughter keeps the parent's `space.epoch`, and a
daughter whose `space` block differs is refused. If the origin moved,
old shards may no longer verify against it; they keep verifying
internally, their trust level freezes at what it reached, and the
daughter says so (`origin_reachable_until`) and carries the parent's
toolchain digests beside its own so a verifier knows which parser
applies to which range.

Two peers may publish two daughters when an origin changes. If they
parse it the same way, their snapshots have the same hash and they are
the same thing under two names; a peer that holds both says so and
catalogues merge them. If they differ, §4.6 decides which one the
network embeds — at the records stage, before anyone spends GPU.

A **sister** shares the corpus and starts a new space: a domain-trained
encoder over PubMed, for instance. Because its origin and toolchain are
the parent's, its future snapshots are the parent's future snapshots —
same bytes, same hash, no coordination — for as long as neither
changes. `space_from: <codex>` copies a `space` block byte for byte, so
two collections meant to be searched together land in one space by
construction rather than by hoping their settings match.

A **corrective sister** replaces a collection with a defect. It starts
from zero — records and vectors both — even when the defect touched a
few percent of records, because a protocol whose product is trust
cannot offer a repair that is "complete enough", and because a defect in
the vectors is invisible to any hash and separable from good vectors
only by recomputing all of them. It must carry a reproducible statement
of the defect (§3.6), and until a peer has reproduced it, the sister has
no right to mark anything superseded. `space.epoch` is salted with the
replaced Codex so the new space is fresh even when encoder and chunker
are identical.

Identity has two levels for this reason. `collection_id` is exact: the
hash of one Codex. `lineage_id` is the root Codex's id along
`continues`, and it is what a catalogue shows as *the collection*: one
entry with a history, not a list of ids. Adopting a daughter, for a
peer holding the parent, costs reading one Codex.

Taking a part of a collection — one slice, one kind — is none of these.
It is the same collection with fewer members.

## 2.8 Open and certified

A collection is **open** or **certified**; nothing else.

An open collection is what everything above describes: anyone may read
it, anyone may compute and publish the next snapshot, anyone may embed.

A certified collection adds one authority, on purpose: only the
**certifier** — a named key, anchored outside Louise — may publish a
snapshot that peers treat as certified. This is for sources whose value
is *that this is the text*: the Gazzetta Ufficiale, the Official
Journal of the EU. The certifier publishes records. It does not choose
the encoder (the Codex did), it does not exempt the collection from any
check, and unless the Codex says otherwise it does not embed: vectors
are derived from text by an open encoder within a declared tolerance,
anyone can check them, and a bad vector cannot alter the text a search
returns — it can only fail to find it.

```
publication: "certified",
certifier: {key: <ed25519>, anchor: <url>, is_origin: false},
embedding: "open",                  -- or "certifier-only"
succession: "frozen",               -- or {"open-after": "P6M"}: what happens if the certifier falls silent
content: "manifest-only"            -- fixed: nothing outside a signed snapshot may exist in the copy
```

How the anchor is checked, what the seal of an origin adds, and what
"manifest-only" enforces are in §3.7–3.8. What certification is *not*:
a private collection. Access restriction in a peer-to-peer system means
encryption, membership and a second authority; Louise does not do it.
Sources that must not be redistributed are handled by the licence
classes (§4.4), which is the actual need.
