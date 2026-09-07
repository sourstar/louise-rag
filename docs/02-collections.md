# 2. Collections

A collection is four things: a **Codex** that never changes, a chain of
**snapshots** that grows once a month, the **records** and **vectors**
those snapshots describe, and a **coverage map** that says which
vectors exist and who made them. The Codex, the snapshots and the
records are content-addressed and identical on every peer. The vectors
are correct within a tolerance, not byte for byte (03 §3.1): they travel
as signed bundles, each naming its producer, and what a peer holds for a
shard is its merge of the bundles it accepted (§2.4). The coverage map
is each peer's view, kept current by gossip, and never the same document
twice.

## 2.1 The Codex

The Codex is the recipe. It is written once, when a collection is
created, and never edited: any change produces a new Codex and
therefore a new collection (§2.7 says how the two are related).

```
codex = {
  kind: "louise-codex", version: 1,
  descriptive: {                     -- the ONE block outside the identity, below
    title:   "PubMed",
    summary: "<one short paragraph: what this collection holds, and for whom>"
  },
  plugin: "pubmed",
  origin: {                          -- the acquisition spec, serialised (09 §9.17)
    spec: {...},                     -- what the origin is and how its files are listed and fetched
    form: "<which origin form was chosen, and why>",
    unit: "<the origin unit: the smallest artefact the plugin can re-fetch alone (§2.3)>",
    clock: "America/New_York",       -- IANA zone of the origin's clock
    cutoff_rule: "<how the origin's state at an instant is enumerated (§2.2)>",
    jurisdiction: null | "IT"
  },
  toolchain: {
    platform_image:  "sha256:…",     -- the platform build: parser, chunker, cleaner, checks, and their libraries
    parent_platform_image: null | "sha256:…"   -- daughters: the image that produced the inherited shards (§2.7)
  },
  parser: {id: "pubmed", version: 3},           -- readable aliases of what the platform image contains
  cleaning_version: 1,                          -- the cleaner is platform code; its version is a property of the image
  protocol_version: 1,                          -- the plugin protocol the plugin implements (06 §6.1)
  layers: [{chunker: {id: "abstract", version: 1, params: {}}, rule: "default"}],
  slicing: {keys: ["year"], year: {field: "date", bins: ["<1950", "5y<2000", "1y"]},
            unknown: "year=unknown", shard_records: 30000, order: "numeric"},
  space: {
    encoder: {image: "sha256:…", weights: "sha256:…", label: "medcpt-article"},
    dim: 768, dtype: "f16", ctx: 512, pooling: "cls", normalise: true,
    query: {encoder: null | {image: "sha256:…", weights: "sha256:…"}, instruction: null | "<prefix>"},
    tolerance: "0.95",
    calibration: {set: "sha256:…", vectors: "sha256:…", quorum: {n: 16, min_pass: 12}},
    envelope: "sha256:…",
    epoch: "0",
    evaluation: null | {...}         -- the creator's published score, if any (notes/embedder-choice-and-supersession.md)
  },
  work: {tokens: <int>, per: "P1M"},                  -- the work parameter (§2.4, 08 §8.5)
  modality: "text",                                   -- what the bytes are: encoder and reranker eligibility
  erasure: "append-only" | "retractable" | "erasable",
  validation: {checks: [...], sample: {...}},          -- the plan (03 §3.2)
  license: {class: "red", terms_sha256: "…" | null},
  privacy: "public" | "restricted" | "never-share",
  update: {cadence: "monthly", day: 1, grace_days: 7},
  publication: "open" | "certified",
  certifier: null | {key, anchor, is_origin},          -- certified only (§2.8)
  embedding: null | "open" | "endorsed" | "certifier-only",   -- certified only
  succession: null | "frozen" | {"open-after": "P6M"},        -- certified only
  content: null | "manifest-only",                            -- certified only, fixed
  continues: null | "<collection_id>", from_snapshot: null | "2026-08",
  origin_reachable_until: null | "2026-08",
  derived_from: null | "<collection_id>", space_from: null | "<collection_id>",
  replaces: null | {codex: "<collection_id>", defect: {...}},   -- the defect block is 03 §3.6
  profile: null | {...}                                -- plugin-defined and hashed like every other field: an ELI profile
}
collection_id = H(canonical-json(codex without "descriptive"))   -- the identity
codex_hash    = H(bytes of codex.json)                          -- these bytes
```

**Two hashes, because a name is not a recipe.** A collection needs a
title and a sentence saying what it is, and neither decides a single
byte of what the collection contains. So the Codex carries them in one
block, `descriptive`, and that block is the one key the identity does
not cover: `collection_id` is `H` of the canonical JSON of the Codex
with exactly the key `descriptive` removed. Two creators who write the
same recipe under different titles write the *same collection* — same
id, same snapshots, same members, mutually verifiable — and a peer that
holds one and meets the other adopts nothing and merges nothing: it
keeps the descriptive block of the Codex it holds. `codex_hash` covers
the whole file, `descriptive` included, so a title altered in transit
is caught like any other tampering; it is the file's integrity, never
the collection's identity. What `descriptive` may never do is decide
anything: it is displayed, and never sorted on, merged on or resolved
by (08 §8.2).

The exclusion is one named key and not a list of fields, so that a
later version adding a descriptive field cannot silently move the
identity: whatever `descriptive` holds is outside, and everything else
is inside, for every version.

Every field is present, `null` when it does not apply; no field is ever
omitted, and a Codex carrying a key outside this list, or a `version`
the reader does not implement, is refused (09 §9.18). `H` is SHA-256,
and an id is the string `sha256:` followed by 64 lowercase hex digits.
`canonical-json` is the canonical form of 09 §9.1: keys sorted by code
point, no whitespace, no floats — a ratio such as `tolerance` is a
decimal string, never a JSON number. The two per-record fingerprints,
`content_hash` and `origin_hash`, are BLAKE2b-128 and are written as 32
hex digits with no prefix (§2.3). `lineage_id` is not a field: it is
computed from the chain (§2.7).

The fields, in the order a reader meets them: `origin` is the plugin's
acquisition spec as the platform serialises it, plus the choices a
verifier needs — the form taken, the origin unit, the origin's clock and
the rule by which its state at an instant is enumerated; nothing about
the peer is in it, no credential and no local path (06 §6.2).
`toolchain` pins the code by image digest; `parser`, `cleaning_version`
and `protocol_version` are what those images contain, written out.
`layers` and `slicing` say how documents are cut and how records are
divided into members (§2.3, §2.4, 06 §6.3). `space` is the function that
turns text into vectors, and the contract around it (§2.4). `work` is
the work parameter (§2.4). `modality` says what the bytes are, so that
an encoder or a reranker of the wrong kind — a language model over the
reference genomes — is refused rather than merely worse. `erasure` says
how content may leave: `append-only` (the Gazzetta Ufficiale: nothing
leaves without a recorded tombstone), `retractable` (PubMed: the
publisher may withdraw a record, and the tombstone says so), `erasable`
(removal may be mandatory and must leave no trace, which no shared copy
can honour — so `erasable` requires `privacy: never-share`, and a drag
build refuses the pair otherwise). `validation` is the plan of 03 §3.2,
inside the hash so nothing outside the Codex decides how much is
sampled. `license` and `privacy` are 04 §4.4's; the Codex's
`license.class` is authoritative, and the plugin's declared class is
only the default `hold collection create` writes. `update` fixes the
cadence, the day of the cutoff and the grace window. `publication` and
the four fields after it are §2.8's. The lineage fields are §2.7's.

Three rules make it work.

**The hash is the identity.** Anyone who writes the same Codex has the
same collection. The key of whoever wrote it first is recorded in
catalogues as a fact — *first published by* — not as part of the
identity, and it is used for nothing. No key is needed to continue a
collection; §4 depends on this. Two Codexes that differ only in
`descriptive` are one collection, because the identity is the recipe
and a title is not part of a recipe. Nothing but the hash chain of §2.7
makes two collections kin — in particular not a shared title, which
proves nothing and is compared by nobody (08 §8.2).

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
state at 00:00:00 on `update.day` of a month, in the origin's own clock
(`origin.clock`). Its id is the month that closed at that instant — the
cutoff 2026-09-01T00:00 America/New_York names the snapshot "2026-08" —
which every peer can compute from the Codex without asking anyone.
Anything the origin publishes after that instant — new content, an
embargo lifted — belongs to the next snapshot.

A record belongs to the snapshot when the origin artefact it was parsed
from is one the plugin's `cutoff_rule` enumerates at that instant: for
dated dumps (Wikipedia) the last dump before it; for a baseline plus
update files (PubMed) those published before it; for a catalogue that
only grows (Gutenberg) the entries issued before it. The plugin declares
the rule (06 §6.2), the Codex records it, and a peer that fetches late
reproduces the same state by the same rule (04 §4.5). When the origin
no longer offers the artefacts a snapshot was built from, that snapshot
is reproducible only from peers, and the next daughter says so
(`origin_reachable_until`, §2.7).

```
snapshot = {kind: "louise-snapshot", version: 1,
            collection_id, snapshot_id: "2026-08", previous: <hash> | null,
            shards: [{slice, n, root, records_root, ids: [first, last],
                      records, chunks, tokens, toolchain: "own" | "parent"}],
            delta: {added: [...], changed: [...], tombstoned: [...]}}
snapshot hash = H(canonical-json(snapshot))
```

The snapshot is records only: no field of it depends on vectors, on
centres or on coverage. Per shard it carries `root`, the BitTorrent v2
merkle root of the stored `.jsonl.zst` file (SHA-256, 16 KiB leaves),
which is what the transport checks; `records_root`, SHA-256 over the
shard's `content_hash` values concatenated in order, which is what
equivalence (03 §3.4), inheritance (§2.7) and `records-vs-origin`
compare; and `tokens`, the shard's chunk count times `space.ctx`, an
upper bound on the embedding work it represents, which §2.4's work
parameter and 04 §4.5's eligibility use. The bytes are 09 §9.4.

Because records are reproducible byte for byte (§3.1), every honest
peer that computes a snapshot produces the same bytes and the same
hash: the platform image pins the compressor (zstd 1.5.x, level 9,
frames of 1,000 records), so honest peers agree on `root` as well as
on `records_root`. The snapshot object contains no signature.
Signatures are a sidecar, `snapshots/<id>.sig.json`, a list of
`{key, time, sig}` (09 §9.4); attestations of the `signature` check and
a certifier's signature (§2.8) both go there. Signatures on a snapshot
are attestations — *I computed this too* — not permissions. Two
different snapshots with the same id are the proof that one producer's
toolchain drifted, or that one of them enumerated the origin under a
state the cutoff rule does not name, and they name the shard where: a
differing `records_root` is the records, a differing `root` alone is the
compressor.

**Shards are cut by a rule, not by judgement.** Within a slice, records
are sorted by `native_id` — `slicing.order: bytes` is UTF-8 byte order,
`numeric` is integer order — and cut every `slicing.shard_records`
records; shards are numbered 00001… per slice in the order they are
created. **A shard is closed when it is full**, and a closed shard
never changes again except to be re-issued. The last shard of a slice
is open: it holds fewer than `shard_records` records, and each snapshot
re-issues it with the records that arrived since, under its own number,
until it fills and closes. New records go into the open shard first and
open a new number only when it is full, so a slice has at most one open
shard at a time and every closed shard holds exactly `shard_records`
records. The cut therefore falls on the same records whatever the
cadence: a collection updated weekly and one updated monthly close
their shards at the same boundaries, which is what lets two sisters
share members (§2.7). A record whose text changed
re-issues its shard under the same number in the next snapshot; a
deleted or retracted record becomes a tombstone: it is listed in that
snapshot's `delta.tombstoned`, it is excluded from search and from
embedding on every peer that holds the snapshot, and its text stays in
its closed shard — so the shard still verifies and 01's promise 7 holds
— until that shard is next re-issued, when it is dropped from the file.
A record whose slicing field is corrected while its text is unchanged
stays in its shard: a slice is fixed when a record is first published,
and the correction shows in the record's fields at its next re-issue.
A record is "changed" when the hash of its cleaned text changes,
whatever the origin's own revision fields say. Sources that never
delete (PubMed flags retractions) need only tombstones; sources that
edit (Wikipedia) re-issue shards, and their history is the chain of
snapshots.

A daughter's first snapshot (§2.7) has `previous` = the hash of the
parent's snapshot `from_snapshot`; the shards it inherits are listed
with the parent's roots and `toolchain: "parent"`, its own with
`toolchain: "own"`; the chain check accepts a `previous` that belongs to
the Codex named by `continues`, and nowhere else.

`hold snapshot create` run again for an id that exists is a no-op when
the bytes are identical (exit 0) and is refused when they differ, naming
the differing shard (exit 2); it cannot create a snapshot while a fetch
for that cutoff is incomplete (exit 3; the codes are 07 §7.7).

## 2.3 Records

Records are the parsed text of documents and nothing else — no markup,
no origin files, no local extras. They are sliced by declared keys
(year, language) and sharded so that one shard is one unit of transfer,
of embedding and of verification. `slicing.shard_records` is chosen per
collection so that a shard is about 7,000 chunks — 30,000 PubMed
abstracts, 2,000 Wikipedia articles, 50 Gutenberg books; what a shard
costs in time is not a property of any machine but of the collection's
work parameter (§2.4). A slice is named `<key>=<value>`, joined by `/`
in `keys` order — `year=1975-1979`, `lang=de/pages=3M`. The year bins
of §2.1's example read: `<1950` is one slice for everything before 1950
(`year=pre1950`); `5y<2000` is five-year slices up to 2000; `1y` is one
slice per year after that (`year=2023`). A record whose field the rule
cannot read goes to the slice `slicing.unknown` names.

```
records/<slice>/NNNNN.jsonl.zst                     -- the newest version of shard NNNNN: JSON Lines, zstd frames, seekable (09 §9.5)
records/<slice>/NNNNN.<snapshot id>.jsonl.zst       -- a superseded version, kept; the id is the last snapshot that listed it
```

A re-issued shard keeps its path; the version it supersedes is renamed
with the id of the last snapshot that listed it and kept, so an old
snapshot can still be served and checked (01, promise 7).

A record is one line: the keys `uid`, `native_id`, `lang`, `title`,
`creators`, `date`, `subjects`, `keys`, `sections`, `extra`, `layer`,
`origin`, `content_hash`, `origin_hash`, in that order, compact, UTF-8,
`\n`-terminated; 09 §9.5 fixes the bytes and the shard's frame layout.
`uid` is `<plugin>:<native_id>`; `native_id` is a string, unique within
the plugin and stable for the life of the document, which the platform
never parses except under `slicing.order: numeric`. `sections` — pairs
of heading and text, in order — are the cleaned text: cleaning sits
between the parser and the record, so what the record holds is exactly
what the chunker sees, and `cleaning_version` names the cleaner that
produced it (06 §6.3). `layer` is the index of the layer the document
falls in (§2.4).

Every record carries two hashes. `content_hash` is BLAKE2b-128 of
`canonical-json([title, [[heading, text], …]])` — the cleaned text and
nothing else; it decides what "changed" means. `origin_hash` is
BLAKE2b-128 of the bytes of the record's **origin unit**: the smallest
artefact the plugin can re-fetch independently — a Gutenberg `.txt`, one
PubMed baseline container, one Wikipedia page's XML as the multistream
index extracts it. The plugin declares the unit (`origin.unit`, 06
§6.4) and every record carries `origin`, the locator of that unit — a
URL or a path relative to the origin base and, for a container, the
member offset — so `records-vs-origin` knows what to re-fetch and
reproduce the record from. A corpus may be **pruned** — what is not used
is dropped — but never altered in a way that breaks that reproduction.
A record without an `origin_hash` is refused at build, because a drag
that verifies internally while being permanently unverifiable against
its source is the worst thing a collection can ship.

Where an origin offers several forms — an ePub and a plain text, an XML
dump and an API, a sealed PDF and a structured page — the Codex records
which one was chosen and why (`origin.form`). The form is part of what
a verifier needs.

A red-class collection (04 §4.4) ships **pointer records**: the same
line with `sections` empty and `extra.origin_url` set. The text is
fetched from the origin by every peer that holds the collection, and
never exchanged; vectors, pointers and the index travel; a check that
needs the text — `chunks`, T4 — runs where the text is (03 §3.2).

## 2.4 Vectors, spaces and the index

Each shard's vectors are one file: f16, one row per chunk, plus the
**bucket label** of each row.

```
vectors/<space>/<slice>/NNNNN.safetensors         -- that shard's vectors and labels (09 §9.6)
vectors/<space>/index.json                        -- the index: version, k, seed rule, sample rule, iterations, group_size, training record
vectors/<space>/centres/v<n>.safetensors          -- the centre points of index version n, a few MB
vectors/<space>/buckets/v<n>/<group>.safetensors  -- the same vectors re-sorted by bucket (optional)
```

The file holds two tensors, `vectors` (f16, rows in the order of the
shard's chunk ids) and `labels` (u32), and a header carrying
`chunk_ids_sha256` — SHA-256 over the chunk ids in row order —
`producers`, the ids of the bundles merged into it, and `index_min`, the
oldest index version whose centres label every row correctly. A row
with no vector yet is a zero row with label `0xFFFFFFFF`. Vectors reach
a peer as signed bundles (03 §3.5; 09 §9.7) — Parquet is the travelling
form, this file the at-rest one — and the file is that peer's merge of
the bundles it accepted: when two accepted bundles cover one chunk, the
first accepted is the row and the second is compared, and the
comparison is a `vectors-sample` attestation naming both producers.
Two peers that accepted different producers hold different rows for
the same shard, each within tolerance; the coverage map (§2.5) says
which.

**One collection, one space.** A space is a function from cleaned text
to vectors, and vectors from different functions cannot be compared.
The function is the encoder — a container image and the weights it
serves, both by digest, because under a model pool the image alone does
not identify the weights — with `dim`, `dtype`, `ctx`, `pooling`,
`normalise`, the query side, and the layers and cleaning that produce
the text it reads:

```
space_id = H(canonical-json({encoder: {image, weights}, layers, cleaning_version,
                             space: {dim, dtype, ctx, pooling, normalise, query, epoch}}))
```

Not `tolerance`, not `calibration`, not `envelope`, not `work`: those
are the contract around the function, not the function. `space.epoch`
is `"0"` for a root or a sister, the parent's for a daughter, and
`H(canonical-json({"replaces": <replaced collection_id>}))` for a
corrective sister (§2.7). The `<space>` segment of a path or a
member name is the first 16 hex characters of the digest. The query side
is part of the function: a query is embedded by `space.query.encoder`
when it is not null — bi-encoder pairs such as MedCPT's query and
article encoders — and prefixed with `space.query.instruction` when
that is not null; a document is embedded by `space.encoder` and never
prefixed (05 §5.1).

**The work parameter.** Alongside the space, the Codex states the *work
parameter*: the amount of embedding work, in tokens of the declared
encoder's tokenizer, that a key must produce per unit time to count as
holding the collection. It is measured by the creator on their own
hardware with the collection's encoder and recorded with no reference to
that hardware; every participant adopts the number as stated. What it is
for, and why a declared number cannot be inflated, is §8.5.

The field is `work: {tokens, per}`: `tokens` is that amount and `per` an
ISO 8601 duration, by default the update cadence (`P1M`). One block,
two uses. Holding: a key holds the collection actively while the signed
bundles it produced for the collection, as seen by the peer doing the
counting, total at least `tokens` per `per`; a collection with no such
key is dormant on that peer's catalogue — still findable, no longer
listed as active (04 §4.7, 08 §8.6). Eligibility: a peer is eligible for
a shard when `3 × shard_tokens / benchmark_tokens_per_s ≤ seconds(per)`
— it must be able to finish the shard within one work period, with a
factor of three for other load — where `shard_tokens` is the shard's
`tokens` in the snapshot (§2.2) and the benchmark is 03 §3.3's; small
shards admit weak peers, large shards admit few (04 §4.5). The tokens a
key claims live in bundles whose vectors `vectors-sample` reproduces on
the collection's own records; a declared rate that nobody meets makes
the collection dormant, which is true information.

Chunking is declared per **layer** — one chunker per layer, every
document in exactly one layer by a declared rule, the layer list fixed
at creation. The common case is one layer, and then the layer simply is
the collection's chunker. A layer's chunker parameters live in the
layer block and are therefore in the space; the chunker ids, the rule
grammar and the chunk id `<uid>:<seq>:<chunker version>` are 06 §6.3.
Two collections with the same `space_id` are in one space and can be
searched as one ranked list; a collection with a different encoder or
chunker is a different collection (§2.7). A mismatch between a vector
table's space and its collection's Codex is a refuse-to-start error,
never a warning. The refusal is per collection: the node keeps running
its other collections, the refused one is not served, and `hold
collection show` names the reason (07 §7.8).

The **calibration** block is the contract for anyone who wants to
contribute vectors. The calibration set is corpus-independent: a fixed
file of 512 short texts shipped with the platform,
`calibration/<set>.jsonl`, identified by its SHA-256, which
`space.calibration.set` names; `space.calibration.vectors` is the
SHA-256 of the reference vectors the creator produced for those texts
with the collection's encoder. Because the set does not depend on the
corpus, a Codex is complete before a single record exists (06 §6.6),
and every collection on one encoder shares one calibration (08 §8.3).
A peer whose encoder differs from the reference build — another driver,
a quantised variant — embeds `quorum.n` texts sampled from the set and
passes when at least `quorum.min_pass` of them reach cosine ≥
`tolerance` and the median does (`hold embedder check`, 03 §3.3). A
peer that fails may still search with what it holds. The same
reference vectors are the probe set of 03 §3.5's T1, their first
sixteen texts are T2's golden pairs, and their per-dimension mean and
standard deviation are T0's envelope, whose bytes `space.envelope`
hashes. All of it is one drag member, `calibration/<set>.safetensors`
(§2.6; 09 §9.12).

The **index** is a set of centre points trained once on a corpus-wide
sample. Every vector's label is its nearest centre, and search probes a
few buckets instead of everything. The training is fixed so that anyone
can reproduce it (`centres`, 03 §3.2): k-means with k = ⌈√n⌉, capped at
65,536, on a sample of min(200,000, n) chunks chosen by a deterministic
sampler seeded with the snapshot hash, k-means++ initialisation with the
same seed, 25 iterations, in f32; trained by whoever first runs `hold
index build --vector` and shipped as the `centres` member; a later
`build` labels against the shipped centres and never retrains them.
Index versions are additive: centres are added, never moved, so a label
is correct at every version and older is slower, never wrong. A label
carries the index version it was computed against, and `labels` checks
it against the centres of that version, so additive versions and the
check agree. An index generation is `index.version`, an integer; the
word *epoch* belongs to `space.epoch` alone. The index's metadata —
version, k, the seed rule, the sample rule, iterations, `group_size`,
the training record — is `vectors/<space>/index.json`, never the
snapshot. The bucket-ordered copy is optional and lets a light peer or a
topical download take a few groups instead of the whole space; a
vector's group is `label // group_size`.

## 2.5 The coverage map

Which chunks have vectors, from which producer, at what tolerance
result: this changes with every accepted bundle and has no author. Each
peer assembles its own map from the signed bundles and attestations it
has seen, and exchanges it with others by periodic digests (§4.2). It is
the one part of a collection that is not content-addressed, and the
only part that is allowed to differ between two peers for a while.

The map is a set of accepted bundles per shard; every bundle names its
chunk range, so a shard is covered by the union of the bundles that
cover it. A shard counts as covered on a peer only when that peer has
fetched the bundle a claim names and it has passed T0–T3 (03 §3.5); a
claim heard in a digest is a hint about where work is happening, and
never by itself removes a shard from the work of §4.5. The map is
local, and its form is 09 §9.10.

## 2.6 The drag on disk

```
<collection>/
├── codex.json
├── LICENSE-TERMS                        -- the origin's terms, byte-exact; sha256 = license.terms_sha256 (§4.4; 09 §9.13); absent when null
├── snapshots/2026-08.json               -- the chain; each names its predecessor
├── snapshots/2026-08.sig.json           -- the sidecar: signatures and attestations on that snapshot (§2.2)
├── validation.json                      -- the Codex's validation block written out for tools; checked to equal it (§3.2)
├── calibration/<set>.safetensors        -- the calibration set, its reference vectors and the envelope (§2.4)
├── seals/2026-08.json                   -- certified collections: origin seal evidence (§3.7)
├── records/<slice>/NNNNN.jsonl.zst      -- and NNNNN.<snapshot id>.jsonl.zst for superseded versions (§2.3)
├── relations/<slice>/NNNNN.jsonl.zst    -- typed edges between records (§5.5; 09 §9.14)
├── index/<slice>/NNNNN/                 -- the text index of that shard (optional to share; a cache)
└── vectors/<space>/…                    -- as above
```

A collection on disk is a **drag** — *distributed RAG*, the format 09
fixes byte for byte. The drag is the copy:
`<LOUISE_ROOT>/collections/<name>/drag/`, exactly what a peer shares.
What never travels — the manifest, cursors, the acknowledgement, the
trust list, the working text index and the vector table a search reads
— sits beside it (07 §7.8). A text-index member is
a cache: a peer imports it only when a signed snapshot of the chain
lists its root and the pin in the member's own header — `fts_version`
and tokenizer, written by the peer that built it (09 §9.6) — matches
the platform that would read it; otherwise it rebuilds, which is always
possible and is not a check. Green collections have no `LICENSE-TERMS`
(`license.terms_sha256: null`); for every other class the members
fetchable before the acknowledgement of §4.4 is recorded are exactly
`codex.json`, `validation.json`, `snapshots/*` and `LICENSE-TERMS`.

A **member** is what a peer fetches or shares: one slice of one kind —
`records-year=1975-1979`, `vectors-<space>-year=1975-1979`,
`buckets-<space>-v2-g07` — plus the small ones, `calibration` and
`centres`. Each member is a BitTorrent v2 torrent, grouped by one
torrent collection per (collection, snapshot); a member's root is its v2
piece-layer root (04 §4.3; 09 §9.16). "PubMed before 1980, records and
vectors" is a list of members, and the snapshot says their sizes and
hashes before the first byte moves.

## 2.7 Lineage: how a collection changes

Since the Codex is immutable, every change to *what a collection is*
makes a new Codex. Three kinds of change, three relations:

| | **daughter** | **sister** | **corrective sister** |
|---|---|---|---|
| what changed | the outside world: origin format or address, licence of new items; the toolchain adapts | the space, the schedule, or both, over the same corpus | a defect in the parent's records or vectors |
| records up to the transition | inherited by hash | inherited by hash | recomputed from the origin |
| records after it | the new toolchain's | identical to the parent's — same Codex fields, same bytes | the corrected toolchain's |
| space | the same; vectors inherited | new when the `space` block, `layers` or `cleaning_version` change, and then vectors from zero; the parent's when only `update` changes, and then vectors shared | new by construction; from zero |
| Codex field | `continues`, `from_snapshot` | `derived_from` | `derived_from` + `replaces: {codex, defect}` |

A **daughter** continues the collection. The transition is a snapshot
boundary: `from_snapshot` names the parent's snapshot `N`; records up to
`N` are the parent's, from `N+1` the daughter's, whose first snapshot
names the hash of the parent's `N` as `previous` (§2.2); none is ever
produced by both. Vectors survive because the space is unchanged — the
daughter keeps the parent's `space` block, `layers` and
`cleaning_version` byte for byte, and `hold collection continue`
refuses a daughter in which any of them differs. A toolchain change that
is not equivalent (§3.4) and touches only the records from the
transition on is a daughter, whatever the origin did; one that changes
records already published is a corrective sister. If the origin moved,
old shards may no longer verify against it; they keep verifying
internally, their trust level freezes at what it reached, and the
daughter says so (`origin_reachable_until`) and carries the parent's
platform image beside its own (`toolchain.parent_platform_image`) so a
verifier knows which parser applies to which range.

Two peers may publish two daughters when an origin changes. If they
parse it the same way, their snapshots have the same `records_root` and
they are the same thing under two names; a peer that holds both says so
and catalogues merge them. If they differ, §4.6 decides which one the
network embeds — at the records stage, before anyone spends GPU.

A **sister** shares the corpus and starts a line of its own: a
domain-trained encoder over PubMed, or the same PubMed on a weekly
cadence for a laboratory that needs currency. A sister keeps identical
to its parent `plugin`, `origin`, `toolchain.platform_image`, `parser`,
`slicing`, `license`, `privacy`, `erasure` and `modality` — everything
except `layers`, `cleaning_version`, `space` (which carries the
encoder), `update` and the lineage fields — and `hold collection
derive` refuses anything else, and refuses a sister that differs in
nothing.

Whether the vectors are shared follows from one comparison and nothing
else: **a sister whose `space_id` equals its parent's shares the
parent's vectors; one whose `space_id` differs starts from zero.** A
sister that changes the encoder, the layers or `cleaning_version`
changes `space_id` (§2.4) and so starts empty — no vector crosses a
space boundary, and that includes a sister that only removes a layer. A
sister that changes only `update` leaves `space_id` untouched, so every
bundle either of them produces is admissible to the other (03 §3.5) and
the two are searched as one list (05 §5.1).

Because its origin and platform image are the parent's, a sister that
keeps the parent's `update` produces the parent's future snapshots —
same bytes, same hash, no coordination — for as long as neither
changes; a sister that changes `cleaning_version` is the exception,
since the cleaned text is the record (§2.3), and it produces its own
shards. A sister that changes only `update` produces its own chain,
which meets the parent's at every cutoff the two share: at those
instants the origin's state is one state, so the records are the same
records, the shards are byte-identical, their roots are equal and the
members are interchangeable. A weekly sister of a monthly collection
holds every shard the monthly holds, and more snapshots naming them.
`space_from: <codex>` copies the whole `space` block, `layers` and
`cleaning_version` byte for byte, and the
encoder image must match too or `derive` refuses — so two collections
meant to be searched together land in one space by construction rather
than by hoping their settings match.

A **corrective sister** replaces a collection with a defect. It starts
from zero — records and vectors both — even when the defect touched a
few percent of records, because a protocol whose product is trust
cannot offer a repair that is "complete enough", and because a defect in
the vectors is invisible to any hash and separable from good vectors
only by recomputing all of them. It must carry a reproducible statement
of the defect (§3.6), and until a peer has reproduced it, the sister has
no right to mark anything superseded. `space.epoch` is salted with the
replaced Codex — `H(canonical-json({"replaces": <replaced
collection_id>}))`, and any other value is refused — so the new space is
fresh even when encoder and chunker are identical.

Identity has two levels for this reason. `collection_id` is exact: the
hash of one Codex. `lineage_id` is the root Codex's id reached by
following `continues` only — never `derived_from` or `replaces` — and it
is what a catalogue shows as *the collection*: one entry with a history,
not a list of ids. A peer computes it from the chain of Codexes, which
every announcement carries back to the root (§4.2), so two peers never
disagree about it. Adopting a daughter, for a peer holding the parent,
costs reading one Codex. Kinship is the hash chain of `continues`,
`derived_from` and `replaces`, and nothing else makes two collections
kin.

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
certifier: {key: "ed25519:…", anchor: "https://<domain>/.well-known/louise-certifier.json", is_origin: false},
embedding: "open",                  -- or "endorsed" | "certifier-only": the three levels of §3.8
succession: "frozen",               -- or {"open-after": "P6M"}: what happens if the certifier falls silent
content: "manifest-only"            -- fixed: nothing outside a signed snapshot may exist in the copy
```

`is_origin: true` means the certifier is the body that publishes the
origin — the Gazzetta running Louise; `false` means an intermediary
signing what it built from the origin's files, and the Library says
which. The anchor is a document at that URL listing the certifier's
keys with their validity and the collections it certifies (09 §9.11);
the certifier also publishes a signed pointer to its current snapshot
(§4.2), and its signature on a snapshot lives in the sidecar of §2.2.

A certifier is **silent** when no certified snapshot has appeared for
the number of cadences `open-after` spans and no newer pointer is at
the anchor. Then the collection continues as an open daughter —
`continues` names the certified Codex, `publication` is `open` — and
the certifier's return cannot reclaim the lineage: a later certified
daughter is a new lineage. Under `frozen` nothing continues it.

**Manifest-only** means a shard enters the copy only if its root is in a
signed snapshot of the chain — not only the newest, so an old snapshot
can still be served; superseded shards stay on disk (§2.3) and are
excluded; and the unit of halt, when the gate is found bypassed, is the
collection on that peer.

How the anchor is checked, what the seal of an origin adds, and what
"manifest-only" enforces are in §3.7–3.8. What certification is *not*:
a private collection. Access restriction in a peer-to-peer system means
encryption, membership and a second authority; Louise does not do it.
Sources that must not be redistributed are handled by the licence
classes (§4.4), which is the actual need.
