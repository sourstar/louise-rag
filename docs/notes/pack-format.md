# The pack: how a collection is packaged, shared and checked

*Design, 2026-08-26. Status: PARTLY BUILT the same day — the record
shards, slicing, `pack build` / `verify`, the `packed` plugin, per-slice
torrents, chunker layers and the local vector index exist (amendment 43;
§18 maps what is built onto this document; §22 marks the build order).
Numbers are measured on this node's data unless marked "estimate".
Written from three operator briefs (a common open distribution format;
governance and verification; the vector index and versioning) and
restructured to read from the general picture down to the details.*

---

# Part 0 — The picture

## 0.1 Scope

A **collection** is a corpus (PubMed, a Wikipedia language, Gutenberg)
turned into what the platform actually searches: the parsed text, its
text index, its vectors in one embedding space, and the proofs that tie
those together. The **pack** is the way a collection is laid out on
disk so that it can be published as one torrent collection, fetched in
parts, verified by anyone against the original material, kept up to
date with monthly deltas, and embedded cooperatively by peers.

It solves five things at once, which today are solved separately or not
at all:

1. **Only useful data travels** — parsed records, never origin files or
   markup; a peer needs no plugin parser.
2. **A peer takes the part it wants** — one language, one period, with
   or without vectors and index — and knows from the manifest what it
   is getting before the first byte.
3. **Embedding is shared work** — the vectors member is filled by peers
   under leases, with provenance per chunk, and the vector index
   reassembles from their contributions.
4. **Updates are deltas** — a snapshot names exactly which records
   changed, so re-embedding is the changed chunks and nothing else.
5. **Trust is reproduction** — every artefact can be recomputed from the
   origin by any peer with open platform code; nothing executable is
   ever shipped inside a collection.

Out of scope: the encoders themselves (a space names one; see
`approved-encoders.md`), the query pipeline (RRF fusion, the reader), and
the coordinator's lease mechanics (amendment 37) — the pack feeds them.

## 0.2 The components of a collection

```
collection "pubmed"                  identified by collection_id (publisher key + name), signed
│
├── manifest.json        WHAT THIS IS: id, snapshot chain, slicing scheme, chunker layers, spaces
│                        (encoder, calibration), every shard with its hash, index versions,
│                        update cadence, torrent members                              [signed]
├── validation.json      HOW TO CHECK IT: the verification plan (which checks, sample sizes)
├── deltas/<snapshot>.json   WHAT CHANGED since the previous snapshot; the embedding work list
│
│   per shard — same numbering across the three, one shard = one lease = one torrent unit
├── records/<slice>/NNNNN.jsonl.zst          the corpus: parsed records, text only     (+ .idx)
├── index/<slice>/NNNNN/                     the text index (tantivy segment) of that shard
├── vectors/<space>/<slice>/NNNNN.safetensors  that shard's vectors (f16) + bucket labels
│
│   per space — the vector index
├── vectors/<space>/centres/v<n>.safetensors   the index's centre points, one file per version
└── vectors/<space>/buckets/v<n>/<group>.safetensors   the same vectors sorted by bucket
                                             (optional: light peers, topical downloads)

beside the collection, not inside it:
    attestations     signed check results from verifiers, travelling as events
    catalogue        each node's list of known collections, gossiped as digests
```

| component | what it is | produced by | format | needed by | PubMed, 36M articles |
|---|---|---|---|---|---|
| records | parsed text of every record, sliced and sharded (§2, §3) | initiator, from the origin | JSON Lines in zstd frames | everyone | ~14 GB (47 GB raw) |
| text index | one tantivy segment per record shard (§4) | initiator or any peer with the same index pin | tantivy | peers that do not want to index locally | est. 10 GB |
| vectors + labels | one f16 vector per chunk, plus the bucket number of each (§5) | whoever embeds: initiator, archive import, peers under lease | safetensors | searching peers; contributors for the delta | ~55 GB |
| centres | the vector index's centre points, per version (§5.2) | initiator (trained once on a corpus-wide sample) | safetensors | everyone who searches by vector | 8–16 MB |
| bucket-ordered vectors | the vectors re-sorted by bucket, in groups (§5.3) | any peer with full coverage | safetensors | light peers, topical slices, distributed search | ~55 GB (optional) |
| manifest | the description and the binding of all of the above (§7) | initiator, then the publisher of each snapshot | JSON, signed | everyone | KB–MB |
| deltas | what changed per snapshot; tombstones; work list (§6) | the snapshot's publisher | JSON | contributors, updaters | KB–MB per month |
| validation plan | which checks, how much to sample (§15) | initiator | JSON | verifiers | KB |
| attestations | signed check results (§16) | any verifier | JSON events | every node, to compute trust levels | KB each |

## 0.3 Who does what

| role | does | needs |
|---|---|---|
| **initiator** | fetches the origin, packs the records, hashes, chunks, builds the text index, trains the centres, embeds none/part/all, signs the manifest (§14) | the origin, CPU, optionally a GPU |
| **publisher** (of a snapshot) | re-runs the origin fetch at the deadline, writes the delta and the re-issued shards, signs (§6) | the origin; usually the initiator's key, not necessarily |
| **contributor** (full peer) | takes leases over the work list, embeds, labels, returns signed bundles (§11) | records; a calibrated encoder |
| **light peer** | takes records + index + vectors (+ bucket order), searches; never embeds (§10) | disk |
| **verifier** | runs the validation plan, or part of it, and publishes attestations (§15, §16) | records; for some checks the origin or an encoder |
| **coordinator** | hands out leases, merges bundles into the vectors member, republishes (amendment 37) | the manifest's `update.coordinator` key |

One node commonly plays several roles; any node may play any of them.

## 0.4 The lifecycle

1. **Initiate.** From the origin, produce records, text index, hashes,
   centres, some or all vectors; sign the manifest; publish the torrent
   collection at trust level *unverified*. (§14)
2. **Advertise.** Peers learn of the collection through catalogue
   digests and events; beyond direct peers, through a signed DHT item.
   (§13)
3. **Fetch a part.** A peer picks slices (a language, a period) and
   kinds (records only; + index; + vectors; + bucket order); the
   manifest says what each member is and what index version its shards
   need. (§10)
4. **Embed cooperatively.** Contributors take shard-sized leases over
   the work list, embed, label each vector with its bucket, return
   bundles; the coordinator merges them into the vectors member and
   raises `coverage`. (§11)
5. **Verify.** Any peer runs checks — bytes against hashes, records
   against the origin, chunks against the chunker, vectors against the
   encoder, centres and labels against the declared training — and
   publishes attestations; trust levels rise from *unverified* to
   *checked* to *verified* by coverage, or fall to *flagged* on one
   reproduced failure. (§15, §16)
6. **Update.** At the cadence's deadline the publisher re-fetches the
   origin, compares content hashes, re-issues only the shards with
   changed records, writes the delta; contributors embed the changed
   chunks; the index gets an additive version if a rule fires. (§6, §5.5)
7. **Derive.** A better encoder or another chunker is a *derived*
   collection that reuses the records and text index by hash and starts
   its own space from `coverage: 0`. (§17)

## 0.5 The rules, in one place

1. **The hash is the truth; a version number is a hint.** A record is
   "changed" when the hash of its canonical text changes, whatever the
   origin's revision fields say. (§6)
2. **One collection = one space = one encoder.** Chunking is declared
   per LAYER, every document in exactly one layer by a declared rule —
   never a per-chunk field; a different encoder is a derived collection.
   (§17, invariant 1 as amended)
3. **A shard is closed once published.** New records go into new
   shards; a changed record re-issues its shard under the next snapshot;
   a deleted record is a tombstone. (§2)
4. **Index versions are additive within an epoch.** Centres are added,
   never moved; a bucket label is correct at every version; older is
   slower, never wrong. A from-scratch retrain is a new epoch and is
   announced as such. (§5.5)
5. **Nothing executable travels.** Checks are platform code, the plan
   is data, the report is readable. (§15)
6. **Text is readable with a text editor; a vector's header is, the
   array is a raw open layout.** (§9)
7. **Everything is signed; trust is local.** One honest reproduction
   outweighs any number of votes. (§16)
8. **Only useful data travels.** Parsed text, vectors, derived indexes;
   never origin files, markup or "local extras". (§1)

## 0.6 How to read this document

Part I describes each artefact and its format, in the order a collection
is built: slicing, records, text index, vectors and the vector index,
hashes and deltas, the manifest, then the compression and
inspectability measurements that decided the formats. Part II is
operation: distribution, contributions, freshness, advertising. Part III
is governance: the initiator, verification, trust, derived collections.
Part IV maps the design onto what exists, evaluates the briefs, lists
the open questions and the build order; Appendix A holds the query-side
measurements.

---

# Part I — The artefacts

## 1. Why parsed records, not origin files

| origin file | what the platform uses | waste |
|---|---|---|
| PubMed gz XML (51 GB, 380 GB unpacked) | title + abstract + 4 fields (~47 GB raw text, 14 GB packed) | 90 % markup; no random access |
| Gutenberg epub + txt | txt | epub is a "local extra", never p2p (spec.extras) |
| Wikipedia bz2 multistream (26 GB) | article text after wikitext cleaning | markup, templates; a whole dump per change |
| Upstash wikipedia-2024-06 (590 GB) | text + vectors | vectors stored **f32**: f16 loses nothing for cosine search and halves it (−295 GB) |

The PubMed pack (STATUS 5k) is the prototype: 30,000 records → 45 MB
with the XML kept, **12 MB with the six fields only**; one record in a
millisecond (vs 8–20 s to parse the container); discovery reads three
columns in 30 ms. Generalised to every plugin, the pack means: the
publisher runs the plugin's `discover/fetch/parse` once; peers never see
origin files, never run a parser, and never store markup. Invariant 3 is
untouched: chunking and cleaning stay pure functions of the record text,
versioned in the manifest and recomputed by each peer (a few ms per
record).

## 2. Slicing: how a corpus is split and tracked

A peer should be able to take "PubMed before 1980" or "German Wikipedia"
and nothing else. That needs a split that is meaningful to a person,
known from the record *before* any embedding (so that it can never
depend on a vector), and stable for the life of the record (so that
updates do not move records between shards and churn the torrents).
Publication year, language and the origin's own ids qualify; "last
modified" does not — every edit would move the article.

Two levels keep the meaningful unit and the transfer unit apart:

**Slice** — the human-facing part, declared by the collection's plugin
as a path of stable record fields, recorded in the manifest as a
scheme, not a convention:

| collection | slice keys | example slice | why |
|---|---|---|---|
| PubMed | `year` (5-year bins before 2000, yearly after) | `year=1975-1979`, `year=2023` | publication year never changes; one language |
| Wikipedia (text) | `lang` / `pages` (page-id range) | `lang=de/pages=0-2M` | page ids are stable and monotonic in creation time — the honest proxy for "created before ~2010"; creation dates are not in the dumps |
| wikivec (pre-embedded Wikipedia) | `lang` / `pages` | `lang=tr/pages=0-1M` | same; today's per-language torrents (5h) are the `lang` level alone |
| Gutenberg | `lang` | `lang=it` | 70k books; language is the only split anyone wants |

```json
"slicing": {"keys": ["year"], "year": {"field": "date", "bins": ["<1950", "5y<2000", "1y"]}}
```

**Shard** — the unit inside a slice: ~7,000 chunks (≈ 1 GPU-hour, so
that a shard is also one lease — amendment 37's unit; ~30,000 PubMed
abstracts, ~2,000 Wikipedia articles, ~50 Gutenberg books), records in
id order, numbered `records/<slice>/00017.jsonl.zst`, and the same
number for the shard's text index and vectors. Rules:

- A shard is **closed once published**. New records for a slice go into
  the next shard number (append-only), regardless of their id.
- A **changed** record re-issues its shard under the next snapshot with
  a new hash; the delta names the shard and the records. A shard is
  ~10 MB, so a re-issue is cheap for peers (torrent v2 shares every
  unchanged file).
- A **deleted** record is a tombstone in the delta; its shard is
  rewritten at the next snapshot.
- The manifest lists every shard with its slice, id range, record and
  chunk counts and merkle root; a small per-slice index maps uid → shard
  (sorted ids). That is the whole tracking: what a peer holds is a set
  of (slice, shard) pairs, and its catalogue says so.

PubMed's 1,334 origin containers become ~5,700 shards over ~40 slices;
the monthly delta (~20k records) is three new shards plus the re-issued
ones. "PubMed before 1980" is the members of slices `year<1980`:
records, index and vectors of those slices only (§10).

## 3. Record shards

JSON Lines: one record per line, UTF-8, keys in the order below;
compressed with zstd level 9 in **independent frames of 1,000 records**
with a frame index at the end (zstd's seekable layout), so one record
costs one frame decompressed plus one line parsed — measured 1.0 ms,
the same as a Parquet row group (§8). A per-shard `.idx` (uid → frame,
line) keeps the lookup O(1). `zstd -d shard.jsonl.zst | less` is the
whole inspection (§9).

| key | type | note |
|---|---|---|
| uid | string | `<plugin>:<native id>` (the platform's key) |
| native_id | string | |
| lang | string | |
| title, creators, date, subjects, keys | string / list | as CanonicalDoc |
| sections | list of {name, text} | the text the chunker sees |
| extra | object | plugin-specific (journal, url…) |
| content_hash | hex(32) | BLAKE2b-128 of the canonical text — change detection (§6) |
| origin_hash | hex(32) | hash of the origin bytes the record came from (today's raw_hash) |

## 4. Text index shards

One tantivy segment per record shard, same numbering, plus the index
pin (FTS_VERSION, ENTITY_VERSION, chunker id/version, tokenizer/stemmer
ids). A peer with a different pin rebuilds instead of importing; a peer
with the same pin imports and spot-checks (`index-sample`, §15). Locally
a peer merges the segments it holds into one per slice for query speed;
the travelling unit stays per shard so that record and index are always
checked as a pair. Whether sharing the index pays over rebuilding is
decided by measurement once PubMed's index is complete (STATUS 5f) —
the pack makes that measurement a one-liner.

## 5. Vectors and the vector index

### 5.1 What an embedder produces: vectors and labels, per shard

`vectors/<space>/<slice>/NNNNN.safetensors` holds two tensors —
`vector` [n, dim] f16 and `bucket` [n] uint16 — and a JSON header with
the chunk ids, the producer of each run (node key, encoder id/version),
and the index version the labels were computed against. Rows align with
the record shard's chunks. One directory per **space id**; a collection
has one space, a derived collection (§17) another.

f16 is the transport and storage dtype: on 100k real MedCPT vectors the
cosine to f32 is 1.000000 and not one top-10 result moves outside the
near-tie band (Appendix A). Nothing here is compressed: a float
mantissa is noise to any codec (§8).

### 5.2 The centre points, and how they are chosen

The vector index is of the bucket (IVF) kind: a few thousand **centre
points**, every vector filed under its nearest centre, and a search that
looks only inside the 20–30 buckets closest to the query instead of
scanning everything. Graph indexes (HNSW) are faster at small scale but
cannot be assembled from pieces and need RAM proportional to the
collection; the bucket kind can be assembled by concatenation and lives
on disk — which is what a cooperative, Wikipedia-sized collection needs.

The centres come from k-means, which is simpler than its name:

1. Take a **random sample** of the vectors — random across the *whole*
   corpus, every slice, never one shard (centres trained on one year or
   one topic serve everything else badly). At least 50 sample vectors
   per centre: 200k–1M for 4,096 centres.
2. Start from 4,096 sample vectors picked at random as centres. Repeat
   10–20 times: assign each sample vector to its nearest centre; move
   each centre to the mean of its members. The centres settle where the
   data is dense.
3. **Label** every vector of the collection with its nearest centre:
   4,096 dot products per vector, a 2-byte number, computed by whoever
   embedded it (minutes of CPU for millions of vectors, negligible next
   to the embedding itself).

How many centres: about the square root of the collection — 1,024 for a
million vectors, 4,096–8,192 for PubMed's 36 million; a query probes
~1 % of them. PubMed's average bucket is ~9,000 vectors ≈ 13 MB of
f16; a tenfold spread between big and small topics is normal.

When they are trained: PubMed and Wikipedia have pre-embedded vectors,
so the initiator trains immediately. For a corpus embedded from scratch
the initiator embeds a random 1 % first (this is also the calibration
sample), trains, publishes the centres, and only then opens leases — so
every contributed vector arrives labelled.

The training is reproducible: the manifest records the sample (snapshot,
shard list or seed, size), the seed and the iteration count, and the
`centres` check (§15) recomputes them and compares.

```
vectors/<space>/centres/v1.safetensors     tensor "centres" [4096, 1024] f16 (8 MB); header: training record
```

### 5.3 Bucket-ordered vectors: the index as files

Sorting the vectors by bucket turns every bucket into one contiguous
piece of file, and then the index *is* the vector files — nothing
separate to build or share. `vectors/<space>/buckets/v<n>/<group>.safetensors`
holds the vectors of a **bucket group** (consecutive bucket ids, grouped
to ~1 GB because single buckets are too uneven), with their chunk ids
and labels. This member is optional and derived: any peer with full
coverage produces it in minutes by one sort, and publishes it. It gives:

- **light peers** an index with zero compute;
- **topical downloads** — "the medical buckets of Wikipedia" instead of
  an id range or a language;
- **distributed search** — a query touches ~30 buckets, so the network
  knows exactly which peers can answer it; the bucket → peers map is a
  routing table that id- or language-based slices cannot provide.

Records stay in id order (text is read by id); vectors exist in two
orders — per shard while the collection is being embedded, per bucket
once coverage is complete.

### 5.4 Searching, locally

A peer holding shard vectors + labels sorts them into bucket order
locally (or keeps a bucket column and filters on it — to be measured on
Lance before building). A query: embed, compute the distance to the K
centres, probe the closest `nprobes` buckets, scan their f16 vectors,
rank. Measured on 100k real vectors (Appendix A): the flat scan the
platform runs today costs 90 ms per 100k rows and scales linearly —
**13 s per query on the live 15.8M-row wikivec table**; a bucket index
answers in ~6 ms at 98 % of results within 0.001 cosine of the exact
top-10. Product-quantised codes (96 B per vector) are a *local* RAM
trade for machines where the f16 vectors do not fit (PubMed: 3.5 GB in
memory, final ranking re-read from f16 on disk); they are derived in
minutes and never travel. PQ without that re-ranking drops to 66 % on
MedCPT's tightly clustered space — the re-rank is not optional.

### 5.5 Index versions: additive, with a minimum version per shard

Retraining is needed when a bucket has grown too big (COVID-19 put
millions of abstracts into a handful of PubMed buckets — slow to probe,
never wrong), when the collection has outgrown its centre count (trained
at 1M vectors, now 30M), or when a genuinely new region of meaning
arrives (a monolingual encoder receiving another language; a new kind
of content). In a cross-lingual space such as BGE-M3 a new language
barely moves anything — Armenian photosynthesis sits next to English
photosynthesis by design. None of these cases requires touching the
existing centres, hence the rule:

**Version n+1 = version n's centres, unchanged, plus new ones** (from
splitting big buckets or covering a new region). Centres are never
moved or removed within an epoch. Consequences:

- A vector's label changes between versions only if one of the *new*
  centres is closer than its old one: the vectors of a split bucket, the
  vectors of the new region, and nothing else.
- **A shard's minimum index version is computed, not declared**: at
  publish time compare the shard's labels under each version; the
  minimum is the earliest version whose labels equal the newest. It is
  in the manifest's per-shard entry (`index_min`), known before any
  download. An English-only peer sees `index_min: 1` on everything it
  holds and never fetches version 2; the first shard with `index_min: 2`
  triggers it. Newest is the default.
- The bucket-ordered member carries over: a new version re-issues only
  the bucket groups it touched (a split bucket's sub-buckets stay in
  the parent's group); untouched groups are the *same files* — same
  hash, same torrent pieces, already seeded by everyone on the previous
  version. A fresh version is nearly as available as the old one from
  day one.
- **A label is correct at every version** — a version-1 label is a true
  nearest-centre-among-version-1 assignment. A peer on an older version
  is never wrong, only slower (bigger buckets).
- A **from-scratch retrain is a new epoch**: every shard's `index_min`
  jumps, every group is re-issued. Rare, deliberate, announced; a peer
  may stay on the old epoch as long as it wants.

The triggers are rules in the manifest, not decisions, so any peer can
predict when a version is due and verify that one was warranted:

```json
"index": {"epoch": 1, "version": 2,
          "centres": [{"version": 1, "k": 4096, "path": "vectors/41844ebb…/centres/v1.safetensors", "root": "…",
                       "trained_on": {"snapshot": "2026-08-26", "sample": 500000, "seed": 7, "iterations": 20}},
                      {"version": 2, "k": 4608, "path": "…/v2.safetensors", "root": "…", "additive_to": 1,
                       "reason": "split 512 buckets > 8x mean"}],
          "rules": {"split_when": "bucket > 8x mean", "grow_when": "vectors > 4x trained", "probe_default": 30},
          "bucket_groups": 64, "buckets": "vectors/41844ebb…/buckets/v2"}
```

### 5.6 Why there is nothing to compress here

The index is the centres plus the vectors sorted by bucket; the vectors
are f16 numbers that do not compress (zstd −8 %, OpenZL −15 %, §8); the
exact savings — f32 → f16, one vector per article — are already taken;
the lossy ones (int8, −75 %) visibly move results (Appendix A) and stay
a local option for light hardware, never the reference form.

## 6. Hashes, snapshots and deltas

**Hashes.** The rule: *the hash is the truth; a version number is a
hint.* Origins get versions wrong in both directions — PubMed bumps
`DateRevised` for metadata-only edits and corrects text silently;
Wikipedia revision ids tell nothing about whether the text the chunker
sees changed.

- `content_hash` — BLAKE2b-128 of the **canonical text**: the fields in
  fixed order, normalised newlines, UTF-8 — exactly what the chunker
  sees. Same hash → same chunks → same vectors, whatever the origin's
  version says. A different hash under an unchanged origin version is
  recorded in the delta as a *silent change*: information about the
  origin, not an error.
- `origin_hash` — the hash of the raw origin bytes the record was parsed
  from: provenance, for `records-vs-origin` (§15). **Mandatory.** A
  record without one is refused at build (`pack_slices`) and reported by
  `pack verify --checks records`, because a pack whose records all verify
  internally while being unverifiable against their source is a worse
  artefact than a build that failed — and it is adopted by peers who
  cannot repair it. See CLAUDE.md invariant 11 (corpus fidelity).

#### Which origin form, and why

A corpus may be pruned — nothing we do not use is packed — but never in a
way that costs us the ability to reproduce what we kept from the origin.
Where an origin publishes SEVERAL forms, the choice is part of what a
verifier needs, so it is recorded here with its reason:

| plugin | form taken | why, and what is left behind |
|---|---|---|
| gutenberg | the per-book **`.txt`** (`pg_text.scan_dir`), hashed as it is read | plain text is the form the chunker actually consumes; epub/mobi/html are re-renderings of it. A title published *solely* as epub/mobi/html is recorded `no txt file` — declared unindexable, never silently dropped. **What that costs, counted on the node 2026-09-11**: of 1,454 such titles, 255 do have an `.epub` (159 a `.mobi`, 8 an `.html`) and the remaining ~1,199 carry no text in any form — an `.rdf` and a cover `.jpg`, some only a converter log. So the rule forgoes 255 readable books out of 79,320, and the other 1,199 are unreadable however they are approached. Those 255 are not lost: the epub is the origin form of the Gutenberg-with-plates collection (decision 2026-09-08), which is where they belong, together with the plates that are only in the epub |
| pubmed | the **baseline `*.xml.gz`** containers + `updatefiles` deltas | the archival form the origin itself versions and checksums (`.md5` sidecars), and the only one carrying `DeleteCitation` tombstones. The E-utilities API returns the same records without the versioning |
| wikipedia | the **multistream `pages-articles.xml.bz2`** + its index | the index makes the dump seekable per page, so a record can be re-derived without re-reading the dump. Cirrus/HTML dumps are pre-rendered and lossy for our purposes |
| wikipedia — **figures** | Commons' **pre-rendered PNG rendition** at a fixed width, not the `.svg` | operator decision 2026-09-11. The dump carries `[[File:…]]` references, not bytes, so figures are a separate acquisition against Commons either way. 08 §8.7 refuses SVG — "an SVG is a document, not an image: it carries script, external references and its own fetches" — and that refusal was costed against epubs, where SVG is 0.34 % of images. On Wikipedia it is neither rare nor incidental: the diagrams, maps, charts, chemical structures and coats of arms are overwhelmingly SVG, which is the load-bearing share of an encyclopaedia's illustration. MediaWiki already rasterises every SVG and serves it, so the PNG rendition is the ORIGIN's own bytes fetched from the origin, not a local derivation — it stays inside "a figure travels as the origin's own bytes" (01, promise 1) without touching the closed type list. What is left behind is resolution independence; the record's `origin` still locates the `.svg`, so the original is always one fetch away. The width must be named in the acquisition spec and fixed, or two peers fetch different bytes for one figure |

The rule for a new plugin: prefer the form the ORIGIN versions and
checksums, at the lowest level of processing we can consume, because that
is the form a verifier can re-fetch and reproduce byte-for-byte.

That rule and the figures row above pull in opposite directions, and the
tension is worth naming rather than smoothing over: the `.svg` IS the form
Commons versions, and the PNG is a rendering of it. The rule yields here
because 08 §8.7's refusal is not about fidelity but about what a viewer
executes, and no choice of origin form can make an SVG safe to render.
Taking the rendition is the only way to carry those figures at all.
- **Chunk ids** include the chunker and its version; **chunk hashes**
  come from the chunk text. A changed record re-embeds only the chunks
  whose hash changed — with content-defined chunking (Wikipedia's
  `chunk_cdc`) a corrected sentence costs one paragraph, not the
  article.
- **Shard roots** — torrent v2 gives every file a merkle root; the
  manifest lists them, so a signed manifest binds every byte, a shard
  that fails its root is refused whole, and the delta can say exactly
  "shard 00017 of `year=2019` re-issued, records X and Y changed".
- The only version numbers that exist are the collection's
  **snapshots** (`snapshot` + `previous`, a chain). Records have hashes,
  not versions.

**Snapshots and deltas.** A snapshot is immutable. Snapshot N+1 is built
by the publisher at the manifest's deadline: re-run discover/fetch/parse
against the origin, compare `content_hash` per record with snapshot N,
re-issue only the shards holding a changed or new record, and write
`deltas/<N+1>.json`: added records, removed records (tombstones —
PubMed's DeleteCitation, a deleted Wikipedia page), changed records
(with the silent-change mark), the re-issued shards, and — because
chunk ids are deterministic per record — the **work list**: the chunk
ids that need vectors in each space. Unchanged records in a re-issued
shard keep their chunk ids and their vectors are copied forward. The
monthly PubMed delta (~20k records) is three shards of work.

### 6.1 Closed packs and updates: who fetches, who publishes (2026-08-26)

**A pack is closed.** The operator's procedure, decided 2026-08-26: the
origin collection finishes fetching, parsing and indexing everything
the origin held up to a cutoff date; the pack is built *from that
completed origin* with `pack build --until <date>` (records that
existed at the origin on that date and nothing later — gutenberg by
release date, pubmed by DateRevised, wikipedia by the dump's date;
`closed: true` and the rule in the manifest); the origin collection is
then turned off and its content moves to the packed twin with `pack
adopt <origin> <packed>` — the vector table and marks, optionally the
text index, without re-embedding, because the twin yields the same uids
and chunk ids in the same space (§18). The first closed pack is
Gutenberg to 2026-06-30; PubMed and Wikipedia follow the same steps.
A closed pack never changes: later content is an UPDATE.

**An update is a pure function of (previous manifest, origin, cutoff).**
This is the architectural choice that makes "anyone may publish an
update" safe before any policy exists. An updater — a peer holding the
pack and able to reach the origin — runs the origin plugin's
acquisition for the window (PubMed's daily updatefiles, Gutenberg's new
ids, the next Wikipedia dump), parses, and derives the update by rules
the previous manifest fixes completely:

- new records go into NEW shards appended per slice, in native-id
  order, numbered after the last shard the previous manifest lists;
- a changed record (a different `content_hash` for a uid the previous
  snapshot holds) RE-ISSUES its shard under the new snapshot with the
  same shard number — the manifest names it, a peer refetches it;
- a deleted record is a tombstone in `deltas/<snapshot>.json`, applied
  at the next re-issue of its shard;
- the update's own cutoff (`until`) is the new `snapshot`; `previous` is
  the pack it extends; `collection_id` is unchanged.

Two honest updaters with the same origin window therefore produce
BYTE-IDENTICAL shards and the same delta — the same torrent info hashes
— so there is nothing to reconcile: the update is verifiable by anyone
who re-derives it (`records-vs-origin`, §15), and the first valid one to
reach a peer is the same as any other. Divergence can only come from a
different origin window (a peer that fetched later sees more): the
cutoff in the update's manifest says which, and a peer keeps the chain
whose cutoffs match its needs.

What travels: the new/re-issued record shards as members of the torrent
collection (slice × kind, §10), the delta file, and the new manifest;
vectors for the update's chunks follow through the call for
contribution (§11) — the update's work list is the delta's chunk ids —
so an updater needs no GPU.

**Policy comes later, deliberately.** Until then: any peer may publish an
update, every update is *unverified* on arrival (§16), a consumer applies
an update only when its `previous` is the manifest it holds (a chain, no
gaps), and `pack verify` reproduces its records from the origin on a
sample. The slot for a lead exists in the manifest already —
`update.coordinator` — and the intended policy is a maintainer key (or
keys) whose signature promotes an update from *unverified* to
*published*, with the cadence and deadline fields governing when the
window closes. Nothing in the format changes when that policy lands:
the derivation rules above are what make a lead's job checkable.

Not built yet: the updater (`pack update <packed> --until <date>`: run
acquisition for the window, derive, emit shards + delta + manifest),
the consumer's chain check, and the coordinator promotion. Built: the
closed pack (`--until`), the hand-over (`pack adopt`).

## 7. The manifest

```json
{
  "kind": "louise-pack", "version": 1,
  "collection_id": "urn:louise:pubmed:8f2c…",       stable across snapshots (publisher key + name)
  "plugin": "pubmed", "license": {"class": "yellow", "terms_sha256": "…"},
  "origin": {"plugin": "pubmed", "endpoints": [...], "snapshot": "2026-08-26", "checksums": "…", "parser": {"id": "pubmed", "version": 3}},
  "snapshot": "2026-08-26", "previous": "2026-07-26",
  "publisher": "<node public key>", "signature": "…",
  "layers": [{"chunker": {"id": "abstract", "version": 1}, "rule": "default"}],   one layer: the common case (§17.1)
  "cleaning_version": 1,
  "slicing": {"keys": ["year"], "year": {"field": "date", "bins": ["<1950", "5y<2000", "1y"]}},
  "slices": {"year=1975-1979": {"shards": 41, "records": 1230000, "chunks": 1230000}, …},
  "shards": [{"slice": "year=1975-1979", "n": 17, "path": "records/year=1975-1979/00017.jsonl.zst",
              "root": "<v2 merkle root>", "ids": ["pubmed:1180001", "pubmed:1210000"], "records": 30000, "chunks": 30000,
              "index_min": 1}, …],
  "spaces": [{
     "space_id": "41844ebb…", "encoder": "medcpt-article", "reference_version": "tei-1.8/medcpt-2023",
     "dim": 768, "dtype": "f16", "ctx": 512, "tolerance": 0.95,
     "calibration": {"chunk_ids": ["pubmed:10329736:0:1", …16], "vectors_root": "…"},
     "vectors": "vectors/41844ebb…", "coverage": 0.62,
     "index": { …§5.5… }
  }],
  "text_index": {"pin": {"fts_version": 3, "entity_version": 2, "tokenizer": "…"}, "path": "index"},
  "update": {"cadence": "monthly", "deadline": "2026-09-26T00:00Z", "grace_days": 7,
             "coordinator": "<node key or URL>"},
  "members": [ {"name": "records-year=1975-1979", "kind": "records", "slice": "year=1975-1979", "codec": "zstd", "info_hash": "…"},
               {"name": "vectors-41844ebb-year=1975-1979", "kind": "vectors", "codec": "none", …},
               {"name": "buckets-41844ebb-v2-g07", "kind": "buckets", "group": 7, "index_version": 2, …}, … ],
  "derived_from": null
}
```

The **calibration** block is the compatibility contract: a peer whose
encoder differs from the reference (a newer TEI, a Vulkan build, a
quantised GGUF) embeds the calibration chunks and must reach cosine ≥
`tolerance` on the quorum (the genesis proof, amendment 38's rule)
before it may *contribute* vectors to that space; a peer that fails may
still *search* with what it holds. This is today's backend gate and
`approved-encoders.md`, moved into the artefact instead of a page
someone has to read. `origin` is the AcquisitionSpec serialised plus the
parser version (plugins gain an explicit `PARSER_VERSION`, like
chunkers: a parser change is a new snapshot, never a silent drift).

## 8. Compression: measured — zstd for records, nothing for vectors, OpenZL deferred

*Measured 2026-08-26 on real data, in the worker image: one PubMed
container (30,000 records, 39.6 MB as JSONL) and 100,000 real MedCPT
article vectors (768-d, f32). `zstandard` 0.25, pyarrow 25, and `openzl`
0.2.0 (PyPI, manylinux + macOS wheels, no Windows; `import openzl.ext as
zl`, generic `zl.graphs.Compress()` graph, no training from samples — a
pipeline is designed by hand per format). Script: scratchpad
`pmbench/pack_bench.py`; the first draft of this section guessed, this
one reports.*

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

Columnar layout buys nothing on prose — **framed JSONL is 4 % smaller
than Parquet at the same level and equally fast**, so the text-editor
rule (§9) is free for records. A trained dictionary buys *nothing* at
1,000-record frames (dictionaries pay on frames of a few KB; ours are
~1.3 MB) — the first draft's 20–30 % estimate was wrong. OpenZL's
generic graph is zstd underneath for text and lands exactly there; a
gain would need a hand-designed JSON-aware pipeline, which its Python
API does not derive for us. Level 9 is the publisher's default (−9 % vs
level 3 at the same read cost; the initiator pays the CPU once); level
19 is a further −7 % for anyone who wants it, and the format does not
care.

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

A float mantissa is noise to any codec — zstd −8 %, OpenZL −15 %, and
OpenZL's advertised wins are on integer and structured data (the int8
row confirms it: 56 % vs zstd's 55 %). The exact wins are the ones
already taken: f32 → f16 (−50 %) and one vector per article (amendment
39). int8 would pass the 0.95 same-function gate (`RE_ENCODE_TAU`) but
sits below the ≥ 0.999 the same model reproduces across hardware — a
visible degradation, so at most a second, explicitly different space
for light peers, never the reference.

**Decisions.**

- **Records: JSONL in zstd-9 frames of 1,000 records + frame index**
  (§3). zstd is the whole dependency: stable format, `zstd -d` on every
  machine, already in the image via pyarrow and available directly as
  `zstandard` (to add when the pack is built — ask first, per the rules).
- **Vectors: raw f16 in safetensors, no codec.** −8 % (zstd) is not
  worth losing mmap/zero-copy and adding a decode step to the largest
  member; −15 % (OpenZL) on PubMed's ~55 GB of f16 is ~8 GB per copy,
  real but not worth a load-bearing dependency at version 0.2 whose
  format is still versioned (`MAX_FORMAT_VERSION`) and which has no
  Windows wheel. The manifest carries a per-member `codec` (`"zstd"`,
  `"none"`) so a codec can be added later without a format break;
  OpenZL is revisited at its 1.0, with 15 % as the known ceiling on f16.
- The measured numbers scale PubMed to ~14 GB of records (from 47 GB
  raw JSONL) and ~55 GB of f16 vectors for 36M articles; Wikipedia's
  pre-embedded vectors from 590 GB to ~295 GB.

## 9. Inspectable formats — the text-editor test

The brief's rule: the uncompressed form must be readable with a text
editor. Parquet (the first draft) is open and tooled but fails that
test literally — it needs a reader. The rule is worth honouring where
it is free, and it is free for the records; for the vectors the
operator's second thought ("maybe too extreme — for the embeddings it
might not be practical and even that useful") is right, and the
measurements of §8 say exactly where the line falls:

- **Records: JSON Lines** in zstd frames (§3) — `zstd -d shard.jsonl.zst
  | less` is the whole inspection, at 1.0 ms per record and 4 % smaller
  than Parquet: the rule costs nothing here, so it holds.
- **Vectors: safetensors, raw f16, uncompressed.** The rule is *relaxed*
  here on purpose: **the header is what a person can check, the array
  is not.** A vector printed as text is 768 numbers nobody can read for
  meaning, 4x the bytes of f16, and still says nothing about whether it
  is *right* — the only inspection that means anything for an embedding
  is re-embedding the chunk and comparing (the `vectors-sample` check,
  §15). So "no black boxes" for vectors means: which chunk, whose run,
  which encoder, which index version, in a header anyone can `head -c
  4096`; an openly specified one-page layout for the array (safetensors,
  mmap-able, zero-copy into NumPy/Lance); and a reproducible proof.
  NCBI's MedCPT `.npy` + `pmids.json` becomes this at half the size with
  the proof recorded in the manifest. The centres and the bucket labels
  are the same kind of thing: small open arrays, reproducible by the
  `centres` and `labels` checks.
- **Text index**: tantivy segments as they are — a derived artefact that
  `index-sample` reproduces from the records; inspection is
  reproduction.
- **Everything about the collection** — manifest, deltas, validation
  plan, attestations — is JSON.

`python -m app.cli pack cat <collection> <uid>` prints a record and its
chunks as JSON; `pack dump <shard>` streams a shard as JSONL. No tool in
the chain is ours alone: zstd, JSON, safetensors, BitTorrent v2.

---

# Part II — Operation

## 10. Distribution: the torrent collection and its members

A collection is published as one **torrent collection** (amendment 40:
one magnet, an index torrent listing member torrents). The pack fixes
what a member is: **member = slice × kind** — `records-<slice>`,
`index-<slice>`, `vectors-<space>-<slice>` — plus, per space and index
version, the bucket groups `buckets-<space>-v<n>-g<k>`, and the tiny
`centres` member. Today's per-language torrents (5h) are the `lang`
slices of this scheme.

Fetching a part is therefore choosing members: "PubMed before 1980,
records + vectors" is a list of slices and two kinds; the manifest says
their sizes, hashes, `coverage`, and the index version each shard
needs, before the first byte. A peer that wants the index order takes
the bucket groups for its version; with the per-shard `index_min` rule
(§5.5) it takes the newest version it actually needs. Shards land
through staging (5i) and are promoted into the corpus in one sequential
copy.

Two peer profiles fall out of this: a **light peer** takes records +
index + vectors (+ bucket order) and never embeds; a **full peer** takes
records (+ index if the pin matches) and contributes vectors.

## 11. Contributions: leases, work lists and the call for contribution

The **call for contribution** is the advertisement of a snapshot's
delta (§13). Peers in contribute mode ask the coordinator for leases
over the work list — one shard = one lease, with amendment 37's TTL and
eligibility — embed, **label each vector against the current centres**,
and return signed bundles. The coordinator merges bundles into the
vectors member for that snapshot, provenance per chunk retained, raises
`coverage`, and republishes the member; a peer that does not contribute
downloads the vectors member when it is out. Because labels travel with
the vectors, the vector index of the whole collection is the
concatenation of what the contributors returned (§5.3): no one has to
hold everything to have contributed to the index.

Sizing: a shard of ~7,000 chunks is ≈ 1 GPU-hour on the reference
encoders — 50 Gutenberg books, 2,000 Wikipedia articles, 7,000 PubMed
abstracts (one chunk each since amendment 39).

## 12. Freshness at search time (no re-embedding)

A result carries its snapshot id. On request ("check for a newer
version"), the node asks the origin for the current record (PubMed
efetch by PMID; the Wikipedia REST summary by title; Gutenberg is
immutable) and compares `content_hash`: a mismatch marks the result
"newer at the source" and shows the fresh text in the reader, pinned as
a fact beside the snapshot's — never re-embedded, never fed into the
index. Rate-limited (the origins' API limits bound it: NCBI 3 req/s
without a key — default the check to the top 5), opt-in per query,
pointer-only for yellow/red sources (§8 of the architecture: the raw
text of such sources is never exchanged; showing the origin's page is
the origin's business).

## 13. Cross-advertising without flooding

Two mechanisms, both peer-to-peer over the node API, both signed by the
sender:

1. **Anti-entropy digests** (periodic, e.g. hourly): a node sends each
   known peer a digest of its catalogue — `(collection_id, latest
   snapshot, manifest hash)` per collection plus a hash of the whole
   list. A peer that sees a hash it lacks pulls that manifest. Bounded
   by the interval; no fan-out; converges in O(diameter) rounds.
2. **Events** (a new snapshot, a call for contribution, an attestation):
   a node forwards an event to its peers once, keyed on the event id in
   a seen-set with a hop count (TTL 6); duplicates are dropped on
   receipt. A node never re-forwards what it has seen, so a mesh of N
   nodes moves each event O(N × degree) times, not endlessly.

Beyond direct peers: a node publishes its catalogue digest as a signed
mutable DHT item (BEP 44) under its key; a node that knows the key (or
learns it from a manifest's `publisher`) can find the latest snapshot
with no tracker. This is also what makes the torrent collection's
magnet stable across snapshots (BEP 46 mutable torrents — the follow-up
named in amendment 40).

The catalogue is what the Library's create dialog lists: "collections
available in the platform", with size, license class, spaces, slices,
trust level and the next deadline, instead of a pasted magnet.

---

# Part III — Governance

## 14. The initiator

Any node may initiate a collection. Initiating means producing, from the
origin, everything a peer needs to *use* the collection and everything a
peer needs to *check* it:

1. **Fetch** the origin exactly as a peer would (the plugin's
   AcquisitionSpec, a dated snapshot, checksums) and record WHERE it
   came from: the manifest's `origin` (§7). A verifier must be able to
   re-fetch a sample of origin files by relative path + checksum and
   re-parse them with the same parser version.
2. **Slice and pack** the records (§2, §3), **hash** every record and
   every shard (§6), **chunk** — a pure function of the record and its
   layer's chunker, declared at creation (§17.1).
3. **Index** the text (§4), **train the centres** on a corpus-wide
   sample (§5.2), and **embed** — none, a part, or all of the chunks, in
   the declared space, labelling as it goes; `coverage` says how much. A
   pre-embedded origin (MedCPT, Upstash) is *imported* here: its vectors
   are admitted through the same-function proof against the declared
   reference encoder and become the vectors member, with `producer`
   naming the archive.
4. **Write the validation plan** and **sign** the manifest with the
   node key.

The collection is then advertised (§13) at trust level *unverified*.

## 15. Verification: a declarative plan, never a script

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
| `chunks` | recomputing chunks from the records with the chunker of each record's layer (§17.1) reproduces every chunk id and text | CPU, ms per record; can be exhaustive | yes |
| `vectors-sample` | a seeded sample of chunks re-embedded with the reference encoder (or a calibrated compliant one) reaches cosine ≥ `tolerance` on the quorum | GPU, seconds | statistical |
| `calibration` | the reference vectors in the manifest reproduce on this node's encoder | GPU, one second | statistical |
| `centres` | k-means from the declared sample, seed and iterations reproduces the published centres (within float tolerance) | CPU, minutes | yes, given the platform version |
| `labels` | for a sampled shard, the nearest centre of every vector equals its stored label, at the shard's `index_min` and at the newest version | CPU, seconds | yes |
| `index-sample` | re-indexing a sampled shard reproduces its postings under the same pin | CPU, seconds | yes |
| `license` | the terms text hashes to the manifest's `terms_sha256` | trivial | yes |

Anything a collection needs that is not in this table is not a script:
it is a plugin or parser version contributed to the platform, reviewed
like any code. That is the one place "inspect the validator" is real.

`python -m app.cli pack verify <collection> [--checks ...] [--sample 0.05]
[--seed N]` runs the plan and prints a report; the same report is what
the worker produces in the background at a configurable cadence.

## 16. Attestations and trust levels — reproduction, not votes

Each completed check yields an **attestation**: `{collection_id,
snapshot, check, seed, sampled shards, result, platform version, encoder
id/version (for statistical checks), verifier key, time, signature}`.
Attestations travel like events (§13) and are stored beside the
catalogue.

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
  their samples covers ≥ 25 % of shards for `records-vs-origin` and
  ≥ 10 % for `vectors-sample`; later attestations keep raising the
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

## 17. Chunkers: one per layer; derived collections

The brief asks where to record which chunker a chunk came from, for
cases like PubMed whose archive forced the abstract chunker. Not per
chunk: a per-chunk field would be either always the same value or a
licence to mix. The chunker is a property of a **layer** (§17.1), a
collection declares its layers once, and every document belongs to
exactly one of them by a declared rule; the encoder is one per
collection, so all vectors are comparable and are searched as one list.
An archive is admitted only when its vectors map 1:1 onto the chunks of
the document's layer (the shape gate). The common case is one layer,
and then the layer simply *is* the collection's chunker — exactly the
rule the invariants had before.

### 17.1 Layers — the MedCPT case

A pre-embedded archive fixes the chunking of the documents it covers
(MedCPT: one vector per article, title + abstract; Upstash: its own
paragraphs, `verbatim`), and that chunking is not always the one we
would choose for the documents the archive does *not* cover — new and
changed articles, for years to come. Under "one chunker per collection"
the only options were to imitate the archive forever or to re-embed
everything (PubMed: ~10,000 GPU-hours). The operator's rule, adopted
2026-08-26 by amending invariant 1: allow a bounded patchwork in v1 and
let v2 clean it up when the community decides to.

```json
"layers": [
  {"chunker": {"id": "verbatim",  "version": 1}, "rule": "archive:upstash-wikipedia-2024-06"},
  {"chunker": {"id": "chunk_cdc", "version": 2}, "rule": "default"}
]
```

- **One layer per document**, decided by a rule anyone can compute from
  the record and the manifest alone: "covered by the archive's id set →
  layer 0, otherwise layer 1". The record carries its layer in the pack;
  the `chunks` check verifies it (§15).
- **One encoder per collection.** The space id hashes the encoder and
  the ordered layer chunkers, fixed at creation; a vector's header names
  its layer beside its producer.
- **Nothing else changes.** Upsert is per document, so idempotence and
  bundle merging are untouched; the shape gate applies within the
  document's layer; leases and the index (§5) do not care which layer a
  chunk came from.
- **Known cost:** short chunks outscore whole abstracts on specific
  queries, so the finely chunked layer tends to dominate the top of a
  list; a per-document best-chunk step before fusion removes most of it
  — to be measured on the pubmed table before building.
- **Where it pays:** not PubMed itself (abstracts are short; the only
  real loss of `abstract@1` is the tail of long structured abstracts) —
  pubmed v1 stays one layer. It pays where the archive's chunking is
  *foreign*: wikivec keeps `verbatim` for the archive's articles and
  gets `chunk_cdc` for new and changed ones, so monthly Wikipedia deltas
  need not imitate Upstash.
- **v2 = a derived collection** (below) with "changes: layers → one":
  it reuses records and text index by hash, inherits every document
  already embedded with the surviving chunker (same encoder, same
  chunker, identical chunk ids → vectors copied forward), and its work
  list is the other layer. The community pays only for the portion that
  changes, when it decides to.

What a *different encoder* needs is **derivation**:

```json
"derived_from": {"collection_id": "urn:louise:pubmed:8f2c...", "snapshot": "2026-08-26",
                 "keeps": ["records", "text_index"], "changes": ["space"]}
```

A derived collection reuses the parent's record shards and text index
*verbatim* — the same member torrents by info hash, zero duplication,
already verified — and declares a new space (a better encoder, a
different chunker, or both) with its own centres. Its vectors start
empty (`coverage: 0`) and fill through the call for contribution; it is
searchable as it fills. "PubMed re-embedded with encoder X" is a
derived collection whose `chunker` is still `abstract@1` and whose
`spaces[0].encoder` is X; a peer holding both searches both and fuses by
RRF like any two legs. When the parent updates, the child inherits the
delta (its work list is the parent's changed chunks) — one origin, one
records line, many spaces.

---

# Part IV — Status, evaluation and plan

## 18. What exists already and maps onto this

| piece | today | in the pack |
|---|---|---|
| torrent collection (amendment 40) | index torrent over per-language members | the collection's `members`; member = slice × kind (§10) |
| per-language torrents (5h) | `lang` slices | the `lang` level of the slicing scheme (§2) |
| PubMed pack (5k) | per-container Parquet cache beside the archive | the **record shard**, generalised to every plugin and published (§3) |
| genesis archive (amendment 38) | remote npy shards fetched and applied with the same-function proof | the `vectors/<space>` member, plus the manifest's calibration block |
| signed bundles + ledger | peer contributions, provenance per chunk | unchanged: the coordinator's merge input, now carrying labels |
| backend gate / approved-encoders | baseline reproduced at boot | the manifest's `calibration` + `tolerance` |
| leases + demand sets (coordinator) | 64-uid leases | one shard = one lease over the delta's work list (§11) |
| dated snapshots (`snapshot_files`) | origin URLs pinned per date | the pack's `snapshot`/`previous` chain (§6) |
| verify-on-use (T4) | vectors spot-checked at query time | + the freshness check (§12) on content |
| staging (5i) | streaming transports land on fast storage | shards land through staging (§10) |
| vector search (`embeddings.search`) | **BUILT 2026-08-26:** `app/vector_index.py` — Lance IVF-PQ (or IVF-FLAT) built by the worker off the GPU loop, refreshed as rows arrive, probed by search with nprobes + refine | the bucket index of §5 with published centres/labels is the next step; today's index is Lance's own, local |
| record shards, slicing, manifest | **BUILT:** `app/pack/{records,slicing,manifest,build}.py`, `pack build`, `tools/pack_convert.py` | §2, §3, §7 as designed; JSONL + zstd frames + skippable-frame index |
| the pack as a corpus | **BUILT:** the `packed` plugin — same uids, chunks and possession hashes as the origin | §10's light/full peers start here |
| `pack verify` | **BUILT:** roots, records, chunks, license, signature (deterministic, local) | §15; attestations, `records-vs-origin`, `vectors-sample` not yet |
| chunker layers | **BUILT:** `app/layers.py`, `[[layers]]` in the TOML | §17.1 |
| per-slice members | **BUILT:** `torrent publish --per-slice` | §10 |

## 19. Improvements this brings, beyond the briefs

1. Peers need **no plugin parsers** and never hold origin files or
   markup: a `packed` source serves CanonicalDocs from record shards for
   every plugin. Discovery on a peer becomes reading shard metadata.
2. **Wikipedia pre-embedded: f32 → f16** in the record/vector split — the
   590 GB collection becomes ~300 GB with identical search results.
3. The **text index travels** where pins match (5f), so a light peer
   does zero compute: download, search.
4. **Shard = lease = torrent unit**: fetching, embedding work, index
   segments and vector files all move in the same unit; a peer is busy
   on its first shard while the rest arrives (amendment 37).
5. **Deltas at record granularity** with CDC for mutable sources: a
   monthly Wikipedia update re-embeds changed paragraphs, not articles.
6. **Provenance stays per chunk** through merges (who embedded what,
   with which encoder version), so a later quarantine hits exactly the
   contributor's chunks.
7. The **catalogue** replaces pasted magnets in the create dialog; the
   collection id, not the magnet, is what a peer keeps across snapshots.
8. The **vector index reassembles** from contributions and can be
   fetched by topic; a query names the peers that can answer it.

## 20. Evaluation of the operator's briefs, point by point

| brief | verdict | design |
|---|---|---|
| a common, open, efficient distribution format inside the torrent collection | adopted | the pack: members = slice × kind, JSONL/safetensors/JSON, signed manifest (§7, §10) |
| a manifest with a unique id, the embedder and its reference version, compatibility, update deadlines | adopted | `collection_id`, `spaces[].encoder/reference_version/calibration/tolerance`, `update.cadence/deadline` |
| re-embed only what changed | adopted | `content_hash` deltas → work list per chunk (§6) |
| verify freshness at search time, no re-embedding | adopted | §12 |
| calls for contribution propagated without flooding | adopted | digests + TTL-bounded events, DHT beyond direct peers (§13) |
| a corpus holds only useful data | adopted | parsed records only; extras stay local (§1) |
| embeddings, indexes as separate torrents | adopted | vectors, text index, bucket groups are separate members |
| an initiator packs, writes the manifest, hashes, indexes, embeds partly or fully | adopted | §14; `coverage` makes partial embedding a first-class state |
| origin location in the manifest; any peer verifies against the original | adopted | `origin` = AcquisitionSpec + parser version; `records-vs-origin` re-fetches and re-parses a sample |
| a validation script shipped with the collection | **changed** | a declarative plan run by platform code (§15): same inspectability, no remote code execution |
| red-flag inconsistencies; unverified until a critical mass; levels as checks accumulate; voting | **refined** | deterministic checks flag on one reproduction, statistical ones on two; levels from coverage, not votes; local trust weighting (§16) |
| which chunker per chunk | **refined** | per LAYER, never per chunk: every document in one declared layer, the layers hashed into the space (§17.1) |
| two chunkers in v1 (the archive's and ours), v2 derived by re-embedding the old-chunker portion | adopted — invariant 1 amended | layers (§17.1); v2 = a derived collection inheriting the surviving layer's vectors |
| derived collections for a better embedder | adopted | `derived_from`, records and text index reused by info hash |
| repack PubMed's pre-embedded archive into an inspectable form | adopted, generalised | JSONL + zstd frames for records (measured smaller than Parquet, same access), safetensors f16 for vectors (§9); the archive's `.npy` imported once through the proof |
| everything readable with a text editor | **kept for records, relaxed for vectors** | records: yes, at no cost (§8); vectors: the header is readable, the array is a raw open layout — a printed float is neither practical (4x bytes) nor useful (only re-embedding checks a vector), §9 |
| zstd / OpenZL as the packing format | **zstd adopted, OpenZL measured and deferred** | zstd-9 frames for records; vectors raw (zstd −8 %, OpenZL −15 % on f16, OpenZL nothing on text; §8) |
| split the corpus by year / language, differently per collection; download only the part of interest | adopted | the slicing scheme (§2): plugin-declared stable keys, shards closed once published, member = slice × kind |
| a text index per corpus chunk | adopted (already designed) | one segment per shard, same numbering (§4) |
| the vector index reassembled from the peers that embed | adopted | published centres + per-vector labels computed by the embedder; concatenation is the index (§5.2–5.3) |
| index buckets as the peers' work batch | **changed** | a bucket is known only after embedding — the lease stays the shard; buckets become the *download and search* unit once vectors exist (§5.3) |
| per-shard minimum index version; newest by default; no forced download of a version one does not need | adopted, made computable | additive versions within an epoch; `index_min` computed from labels; untouched bucket groups shared by hash across versions (§5.5) |
| hashes to detect changed text despite matching version numbers | adopted | `content_hash` of the canonical text is the truth; silent changes recorded in the delta (§6) |
| no black boxes | adopted as the design rule | every artefact is JSON/JSONL/safetensors/BitTorrent; every check is open platform code |

Risks that remain: (a) `records-vs-origin` needs the origin — PubMed's
mirrors rate-limit, so sampling is small and slow, and a verifier
without the origin can only check internal consistency; (b) parser
drift across platform versions makes old attestations less meaningful —
attestations carry the platform and parser versions, and a level is
computed against the current ones; (c) trust lists are per node, so two
nodes can disagree about a collection's level — that is a feature, and
the UI shows *why* (whose attestations counted); (d) the bucket index's
query cost on HDD-resident vectors: 30 probed buckets of PubMed are
~400 MB of sequential reads — fine on NVMe, seconds on the HDD pool —
so the local layout (which tier holds the bucket-ordered vectors, or PQ
codes in RAM with re-ranking) is a measurement to make before building
§5.4.

## 21. Open questions to decide before building

- **Who is a trusted source?** Proposal: any node may publish; a peer
  trusts publishers by key (the existing `bundle trust` list). The
  reference PubMed/Wikipedia packs would be published by this NAS's key
  first; nothing in the format assumes a single publisher.
- **Shard size**: 7,000 chunks (≈ 1 GPU-hour) vs the torrent piece size
  (16 MiB): both are fine; the lease unit wins.
- **Text index sharing**: decide by measurement once PubMed's index is
  complete (5f).
- **Local bucket search on Lance**: a bucket column + `prefilter`, or a
  re-sorted copy — measure recall, latency and the HDD/NVMe question
  (risk d) on wikivec before choosing.
- **Centre count and probe defaults** per collection size; the rules'
  thresholds (8x mean, 4x growth) are first guesses.
- **Freshness check quotas**: bounded by the origins' API limits;
  default the check to the top 5.
- **OpenZL**: decided by measurement (§8) — not now; the manifest's
  per-member `codec` slot keeps the door open.
- **Should a collection declare a content class, and should the manifest
  carry it?** Proposal in [`collection-classes.md`](collection-classes.md):
  not one bundled "type" (the reason `license_class` and `privacy_class` are
  separate applies again) but two orthogonal axes — **`modality`** (what the
  bytes are, making whole encoder/reranker families *wrong* rather than
  worse; the incident behind `[embed_defaults.<plugin>]` is the argument)
  and **`erasure`** (append-only / retractable / erasable). One consequence
  lands directly on this format: **erasure and exchange are incompatible**,
  because a seeded shard cannot be un-published — so `erasable` implies
  never-share, and **a pack can only ever carry an append-only or a
  retractable collection**, checkable at `pack build`.
- **Should the manifest declare an ELI profile for legal corpora?**
  Proposal in [`eli-legal-plugins.md`](eli-legal-plugins.md): adopting the
  European Legislation Identifier as the template for EU-country legal
  plugins costs two additive manifest fields (`eli.jurisdiction` + the URI
  template / ontology version / pinned expression) and buys a standard
  identity and relation vocabulary across jurisdictions. The clash analysis
  found no incompatibility and one strong FIT: **`snapshot` is already an
  ELI point-in-time coordinate** — putting it in the fetch URL makes a
  consolidated legal corpus reproducible, which it otherwise is not. The
  one structural friction is `native_id`'s integer requirement against
  ELI's URI-addressability premise; `uid` + `keys` absorb it.
- **Should a space publish its evaluation score, and how is a bad choice
  remedied?** Proposal in
  [`embedder-choice-and-supersession.md`](embedder-choice-and-supersession.md):
  the embedder is the collection creator's choice and pinning freezes it, so
  each space carries an `evaluation` block (suite + hash, metric, conditions,
  a coverage check that the suite's targets resolve in *this* pack, and a
  reproducible/asserted tier) that any peer can re-run. A review that carries
  produces a DERIVED collection over the same record shards — `derived_from`
  already exists in §7; `superseded_by` would need adding. The re-fetch bill
  scales with `coverage × dim`, which is why supersession has to be cheap
  early or not at all.
- **May a pack require a reranker, or only recommend one?** Proposal in
  [`reranker-scope.md`](reranker-scope.md). The discriminator is whether
  rerank output reaches common content: at query time it cannot, so every
  plugin today is *optional* and a peer chooses freely; at BUILD time
  (chunk selection, index supervision, precomputed scores) it determines
  shard bytes and would have to be pinned. If it is ever compulsory, the
  admissible set should be a **predicate** — a `rank_calibration` block
  in the shape of §7's encoder `calibration`/`tolerance` — never one name
  and never a list of names (the moving-tag incident in
  `approved-encoders.md` is the argument). A pack may name a model
  identity and digest; never a URL the node contacts. Unresolved: whether
  a Kendall-τ tolerance is an equivalence test or a quality threshold in
  disguise, and whether to specify the compulsory arm before a use for it
  exists.

## 22. Build order (each step useful on its own)

1. ✅ `packed` source + `pack build <collection>`: sliced record shards
   from any plugin (2026-08-26; PubMed's per-container pack.py stays as
   the origin reader).
2. ◐ Manifest with slicing, shards, layers, license (✅); spaces /
   calibration / cadence in the manifest (not yet); `torrent publish
   --per-slice` emits records members = slice + a manifest member (✅);
   vectors and text-index members (not yet); peers consume through the
   torrent collection (already there), but a `packed` collection cannot
   yet ACQUIRE a pack from a peer (no acquisition spec) — it is
   hand-populated over a folder the torrent filled.
3. ◐ **The local vector index** — built as Lance's own IVF (PQ by
   default) with worker maintenance and probing at search (✅); published
   centres, labels, bucket-ordered members (§5.2–5.5) not yet.
4. ◐ `pack verify` with `roots`, `records`, `chunks`, `license`,
   `signature` (✅); `calibration`, `labels`, attestations as events (not
   yet).
5. Deltas: snapshot N+1 from `content_hash` diffs; the work list feeds
   the coordinator's leases; bundles carry labels and merge into the
   vectors member; additive index versions with `index_min`.
6. Gossip: digests + events over the node API; DHT catalogue;
   `records-vs-origin`, `centres` and `index-sample` checks.
7. Freshness check in the Ask reader; bucket-ordered member; derived
   collections with the second space; Wikipedia f16 repack.

---

# Appendix A — Query-side measurements (2026-08-26)

*100,000 real MedCPT article vectors (768-d), 300 held-out queries,
exact f32 cosine top-10 as truth; Lance 0.37 in the worker image.
Scripts: scratchpad `pmbench/query_bench.py`, `query_bench2.py`,
`probe_query.py`.*

**Geometry.** The median cosine gap between rank 10 and rank 11 is
0.00065; between rank 1 and rank 10, 0.0235. The top-10 boundary is a
near-tie, so raw recall@10 overstates every loss; the honest column is
"the candidate's true score is within 0.001 of the 10th".

| stored as | recall@10 | within 0.001 of 10th | within 0.01 | bytes/vector |
|---|---|---|---|---|
| f16 | 0.992 | **1.000** | 1.000 | 1,536 |
| int8, per-vector scale | 0.941 | 0.981 | 1.000 | 769 |
| int8, global scale (SQ8) | 0.906 | — | — | 768 |
| int4 | 0.05 | — | — | useless |

**Flat scan, the platform's path today** (`tbl.search(q).limit(k)`, no
index): 100k rows f32 199 ms/query, f16 90 ms — memory-bandwidth-bound,
bytes = time. Live tables, random unit vector, reads only:

| table | rows | dtype | one query |
|---|---|---|---|
| Louise2 wikivec (tr) | 100k × 1024 | f16 | 0.05 s |
| NAS pubmed | 2.2M × 768 | f16 | 0.75 s |
| NAS wikivec | 15.8M × 1024 | f16 | **13 s** |

**Index types** (same 100k, same truth):

| index | ms/query | recall@10 | within 0.001 | extra disk | build |
|---|---|---|---|---|---|
| flat f16 (today) | 90 | 0.997 | 1.000 | 0 | 0 |
| IVF-FLAT, 256 partitions, nprobes 30 | 6.3 | 0.967 | 0.980 | +154 MB (a copy of the vectors) | 5 s |
| HNSW-SQ (int8 inside the graph), ef 100 | 4.9 | 0.956 | **0.992** | +90 MB | 6 s |
| IVF-PQ 96 B/vector, nprobes 30, rerank ×50 | 11.3 | 0.966 | 0.979 | +11 MB | 73 s |
| IVF-PQ, no rerank | 5.5 | **0.66** | — | +11 MB | 73 s |

Conclusions carried into the design: f16 is free; int8 is a visible
loss and stays local; quantisation is fine *inside* an index as long as
the final ranking re-scores against the stored f16; the bucket (IVF)
family is the one that reassembles from pieces and lives on disk; the
performance item is the missing index, not compression.
