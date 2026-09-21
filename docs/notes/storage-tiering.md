# What belongs on the fast disk, and what does not

*Proposal, 2026-08-28. Measured on the reference node, which had run out of
room by getting this wrong.*

The node has two pools: `ssd4`, 1.4 TB and fast, and `tank`, 8 TB of
mechanical disk. It reached **94% full on the SSD with 7.7 TB free on the
HDD**, which is not a capacity problem — it is a placement problem.

## 1. The rule

> **SSD** if the access is RANDOM and a query waits for it.
> **HDD** if the access is SEQUENTIAL and in bulk, or the bytes are written
> once and rarely read at all.

And the corollary this node proved the hard way:

> **Size follows from mutability.** A mutable store is 3–5× its logical
> content, because it carries versions, tombstones and merge debris. An
> immutable artefact is exactly its content.

So mutable stores are the ones that *belong* on the SSD and are also the
ones that *must be actively bounded* — pruning is not housekeeping, it is
the price of the tier. Immutable artefacts need no maintenance at all, which
is precisely why they can sit on slow disk and be forgotten.

## 2. Measured placement

| artefact | this node | access | tier |
|---|---|---|---|
| origin corpus (epub, XML.gz, dumps) | gutenberg **261 GB**, wikivec 525 GB, pubmed 49 GB | sequential, during parse; written once | **HDD** |
| pack record shards (`.jsonl.zst`) | gutenberg 6.5 GB | one zstd frame at a time; immutable | **HDD** |
| pack vectors (safetensors) | — | bulk read once on import | **HDD** |
| genesis archive shards (parquet) | pubmed 23 GB | 437 MB per shard, sequential (6.3 s) | **HDD** |
| derived parse caches (`PUBMED_PACK_DIR`) | 61 GB | sequential during parse; rebuildable | **HDD** |
| **SQLite manifests** | pubmed 19 GB, wikivec 5.6 GB | random reads + constant small WAL writes | **SSD** |
| **tantivy text index** | wikivec 71 GB, gutenberg 41 GB, pubmed 41 GB | random reads per query; merge-heavy | **SSD** |
| **Lance vector table** | wikivec 587 GB, pubmed 269 GB | random reads per query; delete+insert churn | **SSD** |
| acquisition staging | 303 MB | sequential write, then move | **SSD** |

## 3. The finding that pays for the whole exercise

Gutenberg's corpus sits on the SSD at **261 GB**. Its composition:

| | files | bytes | read by Louise? |
|---|---|---|---|
| `.epub` | 146,616 | **141.3 GB** | **no** |
| `.jpg` | 142,891 | | **no** |
| `.png` | 132,009 | | **no** |
| `.mobi` | 45,914 | | **no** |
| `.txt` | 77,756 | **28.3 GB** | **yes** — the only thing parsed |
| `.rdf` | 79,295 | | yes — metadata |

**About 230 GB of the fastest storage on the node holds files no code path
ever opens.** The Gutenberg plugin reads `.txt` and `.rdf`; epub, mobi and
the cover images arrive because the mirror ships whole book directories.

Moving that corpus to `tank` frees more than twice what the vector-version
leak did, costs nothing in query latency (it is only touched during parse,
sequentially), and needs no code change — `corpus_dir` is already a path in
the collection's TOML.

## 4. Why a pack cannot ship its Lance table

The same rule answers the question directly: **a pack is immutable and a
Lance dataset is a mutable database.** Concretely, measured here:

| collection | vectors (f16, raw) | Lance directory | ratio |
|---|---|---|---|
| wikivec (58.2M × 1024) | 119 GB | **587 GB** | 4.9× |
| pubmed (32.7M × 768) | 50 GB | **269 GB** | 5.4× |
| gutenberg (1.37M × 4096) | 11 GB | **33 GB** | 2.9× |

Where the excess goes: superseded versions (177 GB on wikivec, 522,650 of
them), index files (125 GB), transaction logs — and the `text` column, which
Lance stores beside every vector and which the pack **already carries in
`records/`**. Shipping the Lance table would ship the corpus text twice.

Four more reasons, each fatal on its own:

- **File count.** 115,528 `.lance` files on wikivec, 75,162 on pubmed,
  against 1,591 shards for the whole Gutenberg pack. A torrent of 100k+
  files is metadata-bound before it transfers a byte.
- **No cross-snapshot dedup.** Torrent v2 shares byte-identical files
  between snapshots; compaction rewrites data files, so a new snapshot
  would share almost nothing. Immutable per-shard safetensors dedup
  perfectly.
- **Format lock-in.** A pack should be readable in ten years by anyone; the
  Lance on-disk format is a moving target and would pin the whole community
  to one library version. Safetensors is a frozen, trivially parsed layout —
  the reason §9 chose it.
- **It breaks partial download.** The format's whole shape is slice × kind,
  so a peer can take German Wikipedia's vectors alone. A Lance dataset is
  one object whose manifest references every fragment; a subset is a broken
  dataset.

**The format already has the better version of what the question is after.**
§5.2 publishes the index's **centre points**, and §5.3 optionally publishes
the vectors re-sorted into buckets. The centres are the expensive part of an
index — the k-means training — so a peer that downloads them skips straight
to assignment. That is a portable index, without shipping a database.

So: **the Lance table is a local derived cache, exactly like the tantivy
index.** Both are rebuilt from published, immutable inputs; neither travels.
That is already doctrine for the ANN index ("indexes are local, built over
imported vectors, never shared"), and it generalises.

## 5. What to put in the architecture

Today every collection's TOML names absolute paths (`[paths] manifest`,
`fts`, `lancedb`) and a `corpus_dir`, and there are one-off knobs
(`ACQUIRE_STAGING_DIR`, `PUBMED_PACK_DIR`, `PACK_MOUNTS_DIR`). The mechanism
exists; the *policy* does not, so placement is decided per collection by
whoever creates it, and gets it wrong silently.

Proposed:

1. **Two named tiers in Tier-1 config**: `FAST_DIR` and `BULK_DIR`.
2. **Placement by artefact class, not by collection.** A collection that
   declares no explicit paths gets `FAST_DIR/<name>/{manifest.db,fts,lancedb}`
   and `BULK_DIR/<name>/` for corpus, packs and derived caches. Explicit
   paths keep overriding, for the cases that need it.
3. **The create flow shows which tier each artefact will land on**, with the
   free space on each — the moment an operator can still choose cheaply.
4. **A boot check**, not a boot refusal: warn when a bulk artefact is on the
   fast tier or the fast tier is under `VECTOR_INDEX_MIN_FREE_GB`. Placement
   is an operator's decision; a silent one is the problem.

The tiers are the honest generalisation of what this deployment already did
by hand (STATUS 5i moved pubmed/wikipedia/wikivec corpora to `tank` and left
gutenberg behind — which is exactly the 261 GB above).

## 6. Open questions

1. **Does the Gutenberg mirror have to bring epub/mobi/images at all?**
   `extras` exists for opt-in formats, but the corpus is fetched by rsync
   from the *epub* mirror, so they arrive regardless. A filtered rsync would
   avoid moving 230 GB that is never read — at the cost of no longer holding
   a faithful copy of the origin, which invariant 11 may care about.
2. **Should the vector table's payload and index be split across tiers?**
   The `text` column is bulk and cold; the ids and the IVF index are hot and
   small. Lance keeps them in one directory, so this is not available today —
   but it is the reason the vector store is simultaneously the biggest and
   the most latency-sensitive artefact, and worth revisiting.
3. **wikivec's `_indices` is 125 GB** for 58M vectors. IVF-PQ at 96 B/vector
   would be ~6 GB, so either the index is IVF-FLAT (storing the vectors a
   second time) or stale index versions are retained. Worth measuring before
   sizing any tier.

## 7. Decided, 2026-09-21

The operator: "export bundle should be done in tank only", and the DRAG
goes to tank too "unless the snapshot can be used in place of the existing
collection data". It cannot. Search reads the manifest, the text index and
the Lance table, and nothing reads `drag/` per query. The DRAG is what
REBUILDS those stores on a peer, not a substitute for them. (The one partial
exception is open question 2 in §6: records could supply chunk text
instead of the copies kept in Lance and tantivy, at the price of reading
from the HDD per query.)

- **Exports**: `EXPORT_DIR`, a Tier-1 path to bulk storage. The Library's
  download, `snapshot vectors`' default output and a contribution are
  built under `EXPORT_DIR/<name>/`. When `EXPORT_DIR` is set but not
  mounted, the export is refused, never created, because inside a container
  an unmounted path is the container's own storage. Unset means beside the
  manifest, as before (a single-disk node).
- **The DRAG**: a bulk dataset bind-mounted at `collections/<name>/drag/`,
  exactly as `origin/` is. The records build still stages under `state/`,
  on the fast pool, and `drag.promote` crosses the mount by copying to a
  hidden name next to the target, syncing it, and renaming. That is still
  all-or-nothing. The bare `os.replace` it replaces would have failed on
  the first shard with EXDEV.
- **Not automatic**: a new collection's `drag/` is on the fast pool until
  its mount is added. Add it before the collection's first `records build`.
