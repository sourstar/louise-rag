# The pack: one distribution format for corpus, vectors, index and proofs

*Proposal, 2026-08-26 — operator brief: "a common, open and efficient
distribution format within this platform". Status: DESIGN. Nothing here is
built yet except the pieces it reuses (named in §8). Numbers are measured
on this node unless marked "estimate".*

## 1. What it is, in one paragraph

A **pack** is the unit a collection is distributed in. It is a directory
of shards plus a signed manifest, published as a **torrent collection**
(amendment 40: one magnet, several member torrents). The corpus shards
hold *parsed records* — the text the platform actually uses, nothing
else — as JSON Lines in independent zstd frames (§17), so a record is a
1 ms frame read and a shard is a few hundred MB. Vectors, the lexical index and the change
proofs are **separate member torrents** of the same collection, so a peer
takes the corpus alone (a full peer that will embed), or corpus + index +
vectors (a light peer that will only search). The manifest names the
collection, its snapshot, the embedding space (encoder, reference version,
compatibility tolerance), the update cadence, and — for every snapshot
after the first — exactly which records changed, so re-embedding is the
changed records' chunks and nothing else.

## 2. Why a record pack and not the origin's files

| origin file | what the platform uses | waste |
|---|---|---|
| PubMed gz XML (51 GB, 380 GB unpacked) | title + abstract + 4 fields (~45 GB raw text, 16 GB packed) | 90 % markup; no random access |
| Gutenberg epub + txt | txt | epub is a "local extra", never p2p (spec.extras) |
| Wikipedia bz2 multistream (26 GB) | article text after wikitext cleaning | markup, templates; a whole dump per change |
| Upstash wikipedia-2024-06 (590 GB) | text + vectors | vectors stored **f32**: f16 loses nothing for cosine search and halves it (est. −295 GB) |

The PubMed pack (STATUS 5k) is the prototype: 30,000 records → 45 MB with
the XML kept, **12 MB with the six fields only**; one record in 16 ms
(vs 8–20 s to parse the container); discovery reads three columns in
30 ms. Generalised to every plugin, the pack means: the publisher runs
the plugin's `discover/fetch/parse` once; peers never see origin files,
never run a parser, and never store markup. Invariant 3 is untouched:
chunking and cleaning stay pure functions of the record text, versioned
in the manifest and recomputed by each peer (a few ms per record).

## 3. Layout

```
<collection>/
  manifest.json                 signed (node key), see §4
  records/<slice>/NNNNN.jsonl.zst  corpus shards, member torrent "records-<slice>"  (+ NNNNN.idx)
  index/<slice>/NNNNN/             tantivy segment per shard, member "index-<slice>"  (optional)
  vectors/<space_id>/<slice>/NNNNN.safetensors   member "vectors-<space_id>-<slice>"   (optional)
  deltas/<snapshot>.json           what changed since the previous snapshot
```

*(Formats were Parquet in the first draft; §17 replaced them with JSON
Lines in seekable zstd frames and safetensors after the measurements of
§9 — same access cost, smaller, and readable without our tools.)*

**Record shard** (JSON Lines: one record per line, UTF-8, keys in the
order below; zstd level 9 in independent frames of 1,000 records with a
frame index, so a record costs one frame; one shard ≈ 5–10k chunks so
that a shard is also one lease — amendment 37's unit):

| key | type | note |
|---|---|---|
| uid | string | `<plugin>:<native id>` (the platform's key) |
| native_id | string | |
| lang | string | |
| title, creators, date, subjects, keys | string / list | as CanonicalDoc |
| sections | list<struct<name, text>> | the text the chunker sees |
| extra | string (json) | plugin-specific (journal, url…) |
| content_hash | binary(16) | blake2b of the canonical text — change detection |
| origin_hash | binary(16) | hash of the origin bytes the record came from (today's raw_hash) |

**Vector shard**: a safetensors file — a JSON header (`chunk_ids`,
`producer` per run, dtype f16, shape) followed by the raw array, rows
aligned with the record shard's chunks. One directory per **space id** —
a corpus can have several spaces.

**Index shard**: the tantivy segment built from this shard's chunks,
plus the index pin (FTS_VERSION, ENTITY_VERSION, chunker id/version,
tokenizer/stemmer ids). A peer with a different pin rebuilds instead of
importing; a peer with the same pin imports and spot-checks (re-index a
sample, compare postings) — STATUS 5f, decided by the numbers this
format makes cheap to measure.

Hashes: torrent v2 already gives every file a merkle root; the manifest
lists those roots per shard, so a signed manifest binds every byte, and
a shard that fails its root is refused whole. `content_hash` per record
is what detects *change* between snapshots and what the search-time
freshness check compares (§6).

## 4. The manifest

```json
{
  "kind": "louise-pack", "version": 1,
  "collection_id": "urn:louise:pubmed:8f2c…",       stable across snapshots (publisher key + name)
  "plugin": "pubmed", "license": {"class": "yellow", "terms_sha256": "…"},
  "snapshot": "2026-08-26", "previous": "2026-07-26",
  "publisher": "<node public key>", "signature": "…",
  "chunker": {"id": "abstract", "version": 1}, "cleaning_version": 1,
  "slices": {"all": {"records": 1334, "chunks": 40100000}},
  "shards": [{"path": "records/all/00000.parquet", "root": "<v2 merkle root>", "records": 30000, "chunks": 30000}, …],
  "spaces": [{
     "space_id": "41844ebb…", "encoder": "medcpt-article", "reference_version": "tei-1.8/medcpt-2023",
     "dim": 768, "dtype": "f16", "ctx": 512, "tolerance": 0.95,
     "calibration": {"chunk_ids": ["pubmed:10329736:0:1", …16], "vectors_root": "…"},
     "vectors": "vectors/41844ebb…/all", "coverage": 0.62
  }],
  "index": {"pin": {"fts_version": 3, "entity_version": 2, "tokenizer": "…"}, "path": "index/all"},
  "update": {"cadence": "monthly", "deadline": "2026-09-26T00:00Z", "grace_days": 7,
             "coordinator": "<node key or URL>"},
  "members": [ …the torrent collection's member list, amendment 40… ]
}
```

The **calibration** block is the compatibility contract: a peer whose
encoder differs from the reference (a newer TEI, a Vulkan build, a
quantised GGUF) embeds the calibration chunks and must reach cosine ≥
`tolerance` on the quorum (the genesis proof, amendment 38's rule) before
it may *contribute* vectors to that space; a peer that fails may still
*search* with what it holds. This is exactly today's backend gate and
`docs/approved-encoders.md`, moved into the artefact instead of a page
someone has to read.

## 5. Snapshots, deltas, and who embeds what

- A **snapshot** is immutable. Snapshot N+1 is built by the publisher (a
  trusted source: whoever holds the origin) at the manifest's deadline:
  re-run discover/fetch/parse against the origin, compare `content_hash`
  per record with snapshot N, and write only the shards that contain a
  changed or new record (torrent v2 shares every unchanged file, so the
  peers' delta download is the changed shards alone).
- `deltas/<N+1>.json` lists: added records, removed records (tombstones —
  PubMed's DeleteCitation, a deleted Wikipedia page), changed records,
  and — because chunk ids are deterministic per record — the **work
  list**: the chunk ids that need vectors in each space. Unchanged records
  in a rewritten shard keep their chunk ids, and their vectors are copied
  forward. With content-defined chunking (mutable sources: Wikipedia's
  `chunk_cdc`) even a changed article keeps most of its chunk ids, so the
  work list is the changed *paragraphs*.
- The **call for contribution** is the advertisement of `deltas/N+1`
  (§7): peers in contribute mode ask the coordinator for leases over the
  work list (one shard = one lease; amendment 37's TTL and eligibility),
  embed, and return signed bundles. The coordinator merges bundles into
  `vectors/<space>/…` for snapshot N+1, provenance per chunk retained,
  and publishes the member torrent. A peer that does not contribute
  downloads the vectors torrent when it is out.
- **Light peers** take records + index + vectors and never embed. **Full
  peers** take records (+ index if the pin matches) and contribute.

Sizing that follows from this: a shard of ~7,000 chunks is ≈ 1 GPU-hour
on the reference encoders (amendment 37) — 50 Gutenberg books, 2,000
Wikipedia articles, 7,000 PubMed abstracts (one chunk each since
amendment 39). PubMed's 1,334 origin containers become ~5,700 shards of
7,000 records; the monthly delta (~20k records) is 3 shards of work.

## 6. Freshness at search time (no re-embedding)

A result carries its snapshot id. On request ("check for a newer
version"), the node asks the origin for the current record (PubMed
efetch by PMID; the Wikipedia REST summary by title; Gutenberg is
immutable) and compares `content_hash`: a mismatch marks the result
"newer at the source" and shows the fresh text in the reader, pinned as
a fact beside the snapshot's — never re-embedded, never fed into the
index. Rate-limited, opt-in per query, pointer-only for yellow/red
sources (§8 of the architecture: the raw text of such sources is never
exchanged; showing the origin's page is the origin's business).

## 7. Cross-advertising without flooding

Two mechanisms, both existing-peer-to-peer over the node API, both
signed by the sender:

1. **Anti-entropy digests** (periodic, e.g. hourly): a node sends each
   known peer a digest of its catalogue — `(collection_id, latest
   snapshot, manifest hash)` per collection plus a hash of the whole
   list. A peer that sees a hash it lacks pulls that manifest. Bounded
   by the interval; no fan-out; converges in O(diameter) rounds.
2. **Events** (a new snapshot, a call for contribution): a node forwards
   an event to its peers once, keyed on the event id in a seen-set
   with a hop count (TTL 6); duplicates are dropped on receipt. A node
   never re-forwards what it has seen, so a mesh of N nodes moves each
   event O(N × degree) times, not endlessly.

Beyond direct peers: a node publishes its catalogue digest as a signed
mutable DHT item (BEP 44) under its key; a node that knows the key (or
learns it from a manifest's `publisher`) can find the latest snapshot
with no tracker. This is also what makes the torrent collection's
magnet stable across snapshots (BEP 46 mutable torrents — the follow-up
named in amendment 40).

The catalogue is what the Library's create dialog lists: "collections
available in the platform", with size, license class, spaces and the
next deadline, instead of a pasted magnet.

## 8. What exists already and maps onto this

| piece | today | in the pack |
|---|---|---|
| torrent collection (amendment 40) | index torrent over per-language members | the collection's `members`, unchanged |
| PubMed pack (5k) | per-container Parquet cache beside the archive | the **record shard**, generalised to every plugin and published |
| genesis archive (amendment 38) | remote npy shards fetched and applied with the same-function proof | the `vectors/<space>` member, plus the manifest's calibration block |
| signed bundles + ledger | peer contributions, provenance per chunk | unchanged: the coordinator's merge input |
| backend gate / approved-encoders | baseline reproduced at boot | the manifest's `calibration` + `tolerance` |
| leases + demand sets (coordinator) | 64-uid leases | one shard = one lease over `deltas/N+1` |
| dated snapshots (`snapshot_files`) | origin URLs pinned per date | the pack's `snapshot`/`previous` chain |
| verify-on-use (T4) | vectors spot-checked at query time | + the freshness check (§6) on content |
| staging (5i), per-language torrents (5h) | | shards land through staging; slices stay per language |

## 9. Compression: measured — zstd for records, nothing for vectors, OpenZL deferred

*Measured 2026-08-26 on real data, in the worker image: one PubMed
container (30,000 records, 39.6 MB as JSONL) and 100,000 real MedCPT
article vectors (768-d, f32). `zstandard` 0.25, pyarrow 25, and `openzl` 0.2.0
(PyPI, manylinux + macOS wheels, no Windows; `import openzl.ext as zl`,
generic `zl.graphs.Compress()` graph, no training from samples — a
pipeline is designed by hand per format). Script:
scratchpad `pmbench/pack_bench.py`; the earlier draft of this section
guessed, this one reports.*

**Records** (one shard's worth; single-record access = one row group or
one 1,000-record frame decompressed + one line parsed, mean of 300
random ids):

| form | size | one record |
|---|---|---|
| JSONL, raw | 39.6 MB | — |
| Parquet, zstd-3, row groups of 1,000 | 13.3 MB | 1.06 ms |
| Parquet, zstd-9 | 12.2 MB | |
| **JSONL, zstd-9, independent frames of 1,000 records** | **11.7 MB** | **0.98 ms** |
| JSONL, zstd-3 frames | 12.9 MB | 0.98 ms |
| JSONL, zstd-19 frames | 10.9 MB | |
| JSONL, zstd-3/9 frames + trained 110 KB dictionary | 13.1 / 12.0 MB | 0.96 ms |
| JSONL, OpenZL generic graph, frames of 1,000 | 12.0 MB | 0.98 ms |
| JSONL, zstd-19, one stream (no random access) | 9.5 MB | |

What it says: columnar layout buys nothing on prose — **framed JSONL is
4 % smaller than Parquet at the same level and equally fast**, so the
text-editor rule (§17) is free for records. A trained dictionary buys
*nothing* at 1,000-record frames (dictionaries pay on frames of a few
KB; ours are ~1.3 MB) — the first draft's 20–30 % estimate was wrong.
OpenZL's generic graph is zstd underneath for text and lands exactly
there; a gain would need a hand-designed JSON-aware pipeline, which its
Python API does not derive for us. Level 9 is the publisher's default
(−9 % vs level 3 at the same read cost; the initiator pays the CPU
once); level 19 is a further −7 % for anyone who wants it, and the
format does not care.

**Vectors** (100k × 768; f16 is the transport dtype — cosine to f32
**1.000000** on these vectors, both raw archive scale and
unit-normalised):

| form | size | note |
|---|---|---|
| f32 raw (NCBI's `.npy`) | 307.2 MB | |
| **f16 raw** | **153.6 MB** | mmap-able, zero-copy |
| f16 + zstd-3 / -9 | 141.0 MB (−8 %) | levels indistinguishable |
| f16 + OpenZL Numeric | 130.2 MB (−15 %) | 28 MB/s to compress, 500 MB/s to decompress, exact |
| f32 + OpenZL Numeric | 257.4 MB (−16 %) | |
| int8 + per-vector scale | 77.2 MB (−75 %) | **lossy**: cosine 0.9989 to f32 |
| int8 + zstd-3 / OpenZL | 42.1 / 43.0 MB | the two codecs tie once the data is integer |

What it says: a float mantissa is noise to any codec — zstd −8 %,
OpenZL −15 %, and OpenZL's advertised wins are on integer and
structured data (the int8 row confirms it: 56 % vs zstd's 55 %). The
exact wins are the ones already taken: f32 → f16 (−50 %) and one vector
per article (amendment 39). int8 would pass the 0.95 same-function gate
(`RE_ENCODE_TAU`) but sits below the ≥ 0.999 the same model reproduces
across hardware — a visible degradation, so at most a second, explicitly
different space for light peers, never the reference.

**Decisions.**

- **Records: JSONL in zstd-9 frames of 1,000 records + frame index**
  (§17). zstd is the whole dependency: stable format, `zstd -d` on every
  machine, already in the image via pyarrow and available directly as
  `zstandard` (to add when the pack is built — ask first, per the rules).
- **Vectors: raw f16 in safetensors, no codec.** −8 % (zstd) is not
  worth losing mmap/zero-copy and adding a decode step to the largest
  member; −15 % (OpenZL) on PubMed's ~55 GB of f16 is ~8 GB per copy,
  real but not worth a load-bearing dependency at version 0.2 whose
  format is still versioned (`MAX_FORMAT_VERSION`) and which has no
  Windows wheel. The manifest carries a per-member `codec` (`"zstd"`,
  `"none"`) so a codec can be added later without a format break; OpenZL
  is revisited at its 1.0, with 15 % as the known ceiling on f16.
- The measured numbers scale PubMed to ~14 GB of records (from 47 GB
  raw JSONL) and ~55 GB of f16 vectors for 36M articles; Wikipedia's
  pre-embedded vectors from 590 GB to ~295 GB.

## 10. Improvements this brings, beyond the brief

1. Peers need **no plugin parsers** and never hold origin files or
   markup: a `packed` source serves CanonicalDocs from record shards for every
   plugin. Discovery on a peer becomes reading shard metadata.
2. **Wikipedia pre-embedded: f32 → f16** in the record/vector split — the
   590 GB collection becomes ~300 GB with identical search results.
3. The **index travels** where pins match (5f), so a light peer does
   zero compute: download, search.
4. **Shard = lease = torrent piece-aligned unit**: fetching, embedding
   work and index segments all move in the same unit; a peer is busy on
   its first shard while the rest arrives (amendment 37).
5. **Deltas at record granularity** with CDC for mutable sources: a
   monthly Wikipedia update re-embeds changed paragraphs, not articles.
6. **Provenance stays per chunk** through merges (who embedded what, with
   which encoder version), so a later quarantine hits exactly the
   contributor's chunks.
7. The **catalogue** replaces pasted magnets in the create dialog; the
   collection id, not the magnet, is what a peer keeps across snapshots.

## 11. Open questions to decide before building

- **Who is a trusted source?** Proposal: any node may publish; a peer
  trusts publishers by key (the existing `bundle trust` list). The
  reference PubMed/Wikipedia packs would be published by this NAS's key
  first; nothing in the format assumes a single publisher.
- **Shard size**: 7,000 chunks (≈ 1 GPU-hour) vs the torrent piece size
  (16 MiB): both are fine; the lease unit wins.
- **Index sharing**: decide by measurement once PubMed's index is
  complete (5f) — the pack makes the measurement a one-liner.
- **Freshness check quotas**: the origins' API limits (NCBI 3 req/s
  without a key) bound how many results a query may verify; default the
  check to the top 5.
- **OpenZL**: decided by measurement (§9) — not now; the manifest's
  per-member `codec` slot keeps the door open.

## 12. Build order (each step useful on its own)

1. `packed` source + `pack build <collection>`: records shards from any
   plugin; PubMed's pack becomes this (its pack.py is the seed).
2. Manifest with shards, spaces, calibration, cadence; `torrent publish`
   emits records/index/vectors as members; peers consume through the
   torrent collection (already there).
3. Deltas: snapshot N+1 from `content_hash` diffs; the work list feeds
   the coordinator's leases; bundles merge into the vectors member.
4. Gossip: digests + events over the node API; DHT catalogue.
5. Freshness check in the Ask reader.
6. Wikipedia f16 repack; index members; OpenZL experiment.

---

# Part II — Governance: who makes a collection, and how anyone checks it

*Added 2026-08-26 after the operator's second brief: an initiator packs
the corpus in a compliant way and writes a self-consistent manifest; any
peer can verify it against the ORIGINAL material; no black boxes; trust
grows from independent checks; derived collections for better encoders;
PubMed's archive repacked into an inspectable form. Evaluation and the
resulting design.*

## 13. The initiator

Any node may initiate a collection. Initiating means producing, from the
origin, everything a peer needs to *use* the collection and everything a
peer needs to *check* it:

1. **Fetch** the origin exactly as a peer would (the plugin's
   AcquisitionSpec, a dated snapshot, checksums) — and record in the
   manifest WHERE it came from: `origin: {plugin, endpoints, snapshot,
   checksum source, parser: {id, version}}`. This is the AcquisitionSpec
   serialised, plus the parser version: a verifier must be able to
   re-fetch a sample of origin files by relative path + checksum and
   re-parse them with the same parser version. (Plugins gain an explicit
   `PARSER_VERSION`, like chunkers have: a parser change is a new
   snapshot, never a silent drift.)
2. **Pack** the records (§3), **hash** every record (`content_hash`) and
   every shard (torrent v2 roots), **chunk** — a pure function of the
   record, declared once per collection (`chunker: {id, version}`).
3. **Index** (optional member) and **embed** — none, a part, or all of
   the chunks, in the declared space; `coverage` in the manifest says
   how much. A pre-embedded origin (MedCPT, Upstash) is *imported* here:
   its vectors are admitted through the same-function proof against the
   declared reference encoder and become the vectors member, with
   `producer` naming the archive.
4. **Sign** the manifest with the node key.

The collection is then advertised (§7) at trust level *unverified*.

## 14. Verification: a declarative plan, never a script

The brief asks for a validation script any peer can run and inspect.
The principle is right — trust comes from independent reproduction, not
from the publisher's word — but a script is the wrong vehicle: code
shipped inside a collection and executed by peers is remote code
execution by construction, and "you can read it first" does not scale
to eleven thousand lines of a Wikipedia parser. It would make the black
box a trojan.

The design keeps the principle and drops the vehicle. A collection
ships `validation.json`: a **plan** made of check types the platform
implements — open source, the same code for everyone, inspectable in the
repository and versioned with it — parameterised by the collection:

| check | what it proves | cost | deterministic? |
|---|---|---|---|
| `roots` | every shard's bytes match the manifest's merkle roots (torrent v2 does this on download; the check re-hashes on demand) | one read of the collection | yes |
| `records-vs-origin` | a random sample of shards (seeded) re-fetched from `origin` and re-parsed with `parser` reproduces the records byte-for-byte (`content_hash`) | network + parse per sampled shard | yes |
| `chunks` | recomputing chunks from the records with the declared chunker reproduces every chunk id and text | CPU, ms per record; can be exhaustive | yes |
| `vectors-sample` | a seeded sample of chunks re-embedded with the reference encoder (or a calibrated compliant one) reaches cosine >= `tolerance` on the quorum | GPU, seconds | statistical |
| `calibration` | the reference vectors in the manifest reproduce on this node's encoder | GPU, one second | statistical |
| `index-sample` | re-indexing a sampled shard reproduces its postings under the same pin | CPU, seconds | yes |
| `license` | the terms text hashes to the manifest's `terms_sha256` | trivial | yes |

Anything a collection needs that is not in this table is not a script:
it is a plugin or parser version contributed to the platform, reviewed
like any code. That is the one place "inspect the validator" is real.

`python -m app.cli pack verify <collection> [--checks ...] [--sample 0.05]
[--seed N]` runs the plan and prints a report; the same report is what
the worker produces in the background at a configurable cadence.

## 15. Attestations and trust levels — reproduction, not votes

Each completed check yields an **attestation**: `{collection_id,
snapshot, check, seed, sampled shards, result, platform version, encoder
id/version (for statistical checks), verifier key, time, signature}`.
Attestations travel like events (§7) and are stored beside the catalogue.

A collection's level, as seen by a node, is computed **locally** from
the attestations it accepts — the keys on its trust list count fully,
unknown keys count with a small weight, its own checks count most. There
is no global consensus to reach and no ballot to stuff:

- **unverified** — only the publisher's word (the default for anything
  new; usable, marked as such in the Library and on results).
- **checked** — at least *k* independent verifiers (default 3) passed
  `roots`, `chunks` and `calibration`, and at least one passed
  `records-vs-origin` on a sample.
- **verified** — at least *K* verifiers (default 8) and the union of
  their samples covers >= 25 % of shards for `records-vs-origin` and
  >= 10 % for `vectors-sample`; later attestations keep raising the
  covered fraction, which is shown ("42 % of shards independently
  reproduced by 14 verifiers").
- **flagged** — a failing attestation on a *deterministic* check is a
  proof, not an opinion: one is enough to flag, and it names the shard.
  A failing *statistical* check (vectors below tolerance) asks for
  reproduction: the event invites two other verifiers to re-run the
  same sample; two independent failures flag the space (not the
  records). A flagged collection stays searchable but is marked, and
  its publisher's key loses standing on the flagging node.

"Critical mass" therefore applies only where variance exists (encoder
tolerance); everything else is reproducible by anyone from the origin,
and one honest reproduction outweighs any number of votes. Sybil
resistance comes from the trust list and from weighting a verifier by
the contributions of theirs that others have verified — not from
counting keys.

What the operator asked for is delivered by this: any inconsistency is
red-flagged reliably (deterministic checks), the level rises as random
checks accumulate (coverage is explicit), and nothing is a black box —
the checks are platform code, the plan is data, the report is readable.

## 16. Chunker per chunk — no: per collection, and derived collections

The brief asks where to record which chunker a chunk came from, for
cases like PubMed whose archive forced the abstract chunker. The answer
the invariants already give: **one collection = one chunker = one
space** (invariant 1, amendment 3), declared once in the manifest and
hashed into the space id. Every chunk of a collection was cut the same
way, or it does not belong to the collection; an archive is admitted
only when its vectors map 1:1 onto that chunker's chunks (the shape
gate). A per-chunk chunker field would be either always the same value
or a licence to mix spaces.

What the case really needs is **derivation**:

```json
"derived_from": {"collection_id": "urn:louise:pubmed:8f2c...", "snapshot": "2026-08-26",
                 "keeps": ["records", "index"], "changes": ["space"]}
```

A derived collection reuses the parent's record shards and index
*verbatim* — the same member torrents by info hash, zero duplication,
already verified — and declares a new space (a better encoder, a
different chunker, or both). Its vectors start empty (`coverage: 0`) and
fill through the call for contribution; it is searchable as it fills.
"PubMed re-embedded with encoder X" is a derived collection whose
`chunker` is still `abstract@1` and whose `spaces[0].encoder` is X; a
peer holding both searches both and fuses by RRF like any two legs.
When the parent updates, the child inherits the delta (its work list is
the parent's changed chunks) — one origin, one records line, many
spaces.

## 17. Inspectable formats — the text-editor test

The brief's rule: the uncompressed form must be readable with a text
editor. Parquet (the first draft of Part I) is open and tooled but fails
that test literally — it needs a reader. The rule is worth honouring
where it is free, and it is free for the records; for the vectors the
operator's second thought (2026-08-26: "maybe too extreme — for the
embeddings it might not be practical and even that useful") is right,
and the measurements of §9 say exactly where the line falls:

- **Records: JSON Lines**, one record per line, UTF-8, keys in a fixed
  order — `zstd -d shard.jsonl.zst | less` is the whole inspection.
  Compressed with **zstd's seekable format** (independent frames of
  1,000 records, an index of frame offsets at the end): a record is one
  frame decompressed plus one line parsed. Measured on PubMed's records
  (§9): **1.0 ms per record, the same as a Parquet row group, and 4 %
  smaller than Parquet at the same level** — the rule costs nothing
  here, so it holds. A per-shard `.idx` (uid -> frame, line) keeps the
  lookup O(1).
- **Vectors: safetensors, raw f16, uncompressed** — one file per shard:
  a JSON header (chunk ids, dtype, shape, producer) followed by the
  array. The rule is *relaxed* here on purpose: **the header is what a
  person can check, the array is not.** A vector printed as text is
  768 numbers nobody can read for meaning, 4x the bytes of f16, and
  still says nothing about whether it is *right* — the only inspection
  that means anything for an embedding is re-embedding the chunk and
  comparing (the `vectors-sample` check, §14). So "no black boxes" for
  vectors means: which chunk, whose run, which encoder, in a header
  anyone can `head -c 4096`; an openly specified one-page layout for the
  array (safetensors, mmap-able, zero-copy into NumPy/Lance); and a
  reproducible proof. NCBI's MedCPT `.npy` + `pmids.json` becomes this at
  half the size (f32 -> f16, cosine 1.000000 to f32 on 100k real vectors)
  with the proof recorded in the manifest.
- **Index**: tantivy segments as they are — a derived artefact that
  `index-sample` reproduces from the records; inspection is
  reproduction.
- **Everything about the collection** — manifest, deltas, validation
  plan, attestations — is JSON.

`python -m app.cli pack cat <collection> <uid>` prints a record and its
chunks as JSON; `pack dump <shard>` streams a shard as JSONL. No tool
in the chain is ours alone: zstd, JSON, safetensors, BitTorrent v2.

## 18. Evaluation of the brief, point by point

| brief | verdict | design |
|---|---|---|
| an initiator packs, writes the manifest, hashes, indexes, embeds partly or fully | adopted | §13; `coverage` makes partial embedding a first-class state |
| origin location in the manifest; any peer verifies against the original | adopted | `origin` = AcquisitionSpec + parser version; `records-vs-origin` re-fetches and re-parses a sample |
| a validation script shipped with the collection | **changed** | a declarative plan run by platform code (§14): same inspectability, no remote code execution |
| red-flag inconsistencies; unverified until a critical mass; levels as checks accumulate; voting | **refined** | deterministic checks flag on one reproduction, statistical ones on two; levels from coverage, not votes; local trust weighting (§15) |
| which chunker per chunk | **changed** | per collection, hashed into the space; different chunking = a derived collection (§16) |
| derived collections for a better embedder | adopted | `derived_from`, records and index reused by info hash |
| repack PubMed's pre-embedded archive into an inspectable form | adopted, generalised | JSONL + seekable zstd for records (measured: smaller than Parquet, same access), safetensors f16 for vectors (§17); the archive's `.npy` imported once through the proof |
| everything readable with a text editor | **kept for records, relaxed for vectors** | records: yes, at no cost (§9); vectors: the header is readable, the array is a raw open layout — a float printed as text is neither practical (4x bytes) nor useful (only re-embedding checks a vector), §17 |
| zstd / OpenZL as the packing format | **zstd adopted, OpenZL measured and deferred** | zstd-9 frames for records; vectors raw (zstd −8 %, OpenZL −15 % on f16, OpenZL nothing on text; §9) |
| no black boxes | adopted as the design rule | every artefact is JSON/JSONL/safetensors/BitTorrent; every check is open platform code |

Risks that remain: (a) `records-vs-origin` needs the origin — PubMed's
mirrors rate-limit, so sampling is small and slow, and a verifier
without the origin can only check internal consistency; (b) parser
drift across platform versions makes old attestations less meaningful —
attestations carry the platform and parser versions, and a level is
computed against the current ones; (c) trust lists are per node, so two
nodes can disagree about a collection's level — that is a feature, and
the UI shows *why* (whose attestations counted).

Build order addendum (after §12's step 2): `pack verify` with `roots`,
`chunks`, `calibration`, `license` first (all local, all deterministic
or already built as the genesis proof), attestations as events next,
`records-vs-origin` and `index-sample` with the gossip, derived
collections with the second space.
