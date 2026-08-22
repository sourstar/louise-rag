# Project status

**Last updated: 2026-08-22.** What exists, what is designed but unbuilt, and
what is deliberately not being built. Read this before planning work; read
[`architecture-v2.md`](architecture-v2.md) for the design it implements, and
its §17 amendments (1–29) for every place the design was corrected in flight.

Node-specific facts — addresses, paths, ports, the live collection inventory —
are deliberately **not** here: they live in `deploy/reference/DEPLOYMENT.md`,
which is gitignored, because this file is public to anyone who clones.

---

## Where the roadmap stands

| phase | state |
|---|---|
| **Phase 0** — generalize, no network | **done.** CorpusSource + collections + `space_id`, five plugins, ReferenceSource/EntityResolver/`doc_keys`, query federation, acquisition layer with profiles, transfer manager. |
| **Phase 1** — bundles + community | **built, never exercised.** Signed bundle export/import, verification ladder T0–T4, coordinator with leases and demand-sets, `contribute` mode, genesis import. Integration-tested two-node. The *deliverable* — publishing a revived PubMed corpus — has never been done, and §12 argues that artifact is the entire go-to-market. |
| **Phase 2** — swarm | **transport done, discovery designed.** BitTorrent v2 via libtorrent works and is tested against a real two-session swarm; peer/collection discovery is specified in [`p2p-discovery.md`](p2p-discovery.md) and not built. |
| **Phase 3** — decentralize | not started, correctly. §12: "don't build the DHT until someone begs." |

## Built and running

- **Five plugins**: gutenberg, pubmed, wikipedia, embedded (pre-embedded
  corpora), genome. Each declares `license_class`, `privacy_class`,
  `chunking_profile` and `mutability`.
- **Per-collection embedding spaces.** `collections.recipe_for()` is THE
  resolution point: collection `[embed]` block → `platform.toml`
  `[embed_defaults.<plugin>]` → Tier-1 env, each a fallback and never an
  override. A node runs several encoders at once.
- **`space_id` immutability.** Pinned per collection; refuse-to-boot on
  mismatch, never auto-migration. Includes the chunker (amendment 28).
- **Acquisition**: declarative `AcquisitionSpec` per source, profiles
  (incremental / exclusive / additive), delta gate, checksum sidecars,
  license gates, disk preflight, resumable at byte granularity, **adoption**
  of files already on disk.
- **Transfer manager**: central token bucket, priority classes, schedule
  windows, per-endpoint etiquette, multi-connection single-file downloads
  (off by default).
- **Verification ladder** T0–T4 with quarantine and verify-on-use; genesis
  import for reusing published vectors.
- **Web UI**: Ask / Library / Stats / Network / System, server-rendered.
- **Snapshots and deltas**: manifests, parent-chained deltas with
  tombstones, vector deltas as ordinary bundles.
- **BitTorrent v2** transport and corpus publishing.

## Pipeline performance (2026-08-21/22 review — measured, not estimated)

A profiling pass over the ingestion pipeline on a 33M-document manifest
removed four independent per-document costs. All numbers were measured live
before and after; the commit messages carry the full evidence.

| defect | cost before | after |
|---|---|---|
| tantivy writer+commit per document (`fts.index_books` now batches) | 136.9 ms/doc | 0.6 ms |
| HGNC symbol dict rebuilt per document (resolver now memoised on `ref_meta` version) | 75.6 ms/doc | 0.03 ms |
| `DELETE FROM catalog_fts WHERE uid=?` scans the FTS5 shadow table (`uid UNINDEXED`) — **extraction was O(N²)** | 14.02 ms/doc at 36k rows, growing | INSERT OR REPLACE by `documents.rowid`, size-independent |
| `ORDER BY CAST(native_id AS INTEGER)` sorting the whole set per browse page | ~10 s/page | 1 ms (expression index, worker-built) |

Net: **rdf 16.0 → 0.36 ms/doc; fts ~130 → ~1 ms/doc warm.** Structural rules
that came with this, worth preserving:

- **rdf and fts interleave a batch at a time** (`RDF_BATCH_DOCS` /
  `FTS_BATCH_DOCS`, default 200): fts lags rdf by at most one batch, never
  by the corpus. Mark done only AFTER the commit, inside one transaction.
- **A locked manifest or unreachable encoder is a deferral, never a
  document error** (`manifest_contended` / `endpoint_unavailable`): recording
  an error removes the document from every selection until a human clears it.
- **Selection queries must be plan-checked, not just behaviour-checked** —
  tests assert no `TEMP B-TREE` / `LIST SUBQUERY`; a merely-slow query
  passes every behavioural test.
- **Scan cursors key on path relative to the corpus root**, with a legacy
  bare-name fallback on exact signature match.
- **Cold single-document probes measure the container, not the pipeline**
  (gzip cannot seek): pubmed rdf is 5.8 s cold and was 0.36 ms warm.
  Benchmark warm, over hundreds of documents.

Known follow-on: after a restart each collection's **startup discovery scan
holds its pipeline thread** until it completes (cursors make this one-time
per corpus change, but a first pass over a large corpus is hours). The
genome collections are parsing/indexing only by operator decision —
embedding awaits an architectural review ([`dna-search-axes.md`](dna-search-axes.md)).

## Designed, specified, not built

1. **Peer and collection discovery** — [`p2p-discovery.md`](p2p-discovery.md).
   A signed per-node catalogue whose first field is `space_id`; peers via a
   pinned list, then the coordinator, then the DHT. The load-bearing point:
   a DHT finds peers for an *infohash* and can say nothing about whether that
   infohash holds a corpus in your embedding space under a licence you may
   redistribute.
2. **Applying a delta.** Deltas can be computed and published but not
   consumed — `check_applicable` refuses a bad one and then nothing happens.
   Tombstones must delete documents and their vectors; added/changed must be
   marked for re-embed; the snapshot pin must advance. **The format is
   currently write-only**, and this is the gap that stops the whole
   reference-plus-chain model closing.
3. **Query-triggered freshness** — a query that lands on stale content
   enqueues a revision check and a re-embed, answering from what is held and
   improving on the next query. Zipf makes this dramatically cheaper than
   re-embedding a whole delta. Must never block the query, must respect
   `worker_state`, must be rate-limited, and must show which revision
   answered.
4. **Sharing the demand signal.** "N nodes found this stale" is a few bytes
   and lets one node do the work for everyone; the coordinator already counts
   distinct reporters for exactly this.
5. **LOCKSS-style possession audit** — [`lockss-p2p-evaluation.md`](lockss-p2p-evaluation.md).
   Poll the *content* plane, never the vector plane.
6. **DNA search axes** — [`dna-search-axes.md`](dna-search-axes.md).
   **Parked pending the Fable model.** DNABERT-2's space is anisotropic —
   unrelated sequences score 0.98 cosine — because MLM never asks them apart.
   DNABERT-S is *not* the fix: it contrasts on species-of-origin, which
   collapses within-genome distinctions and pushes apart exactly the
   cross-species pairs §9 wants together. Homology and composition need no
   model at all (k-mer sketches); only function does. The load-bearing point:
   **invariant 4 has no category for two ranked lists that must NOT be
   fused** — a homology ranking and a functional ranking answer different
   questions, and RRF-ing them yields neither. Measure before amending
   anything; there is currently no baseline to judge any model against.

## Known gaps and traps

- **Locators are absolute paths.** Moving a corpus invalidates every one.
  Discovery now detects and repairs this automatically, but storing locators
  relative to `corpus_dir` would remove the failure mode instead of healing
  it. Touches every plugin.
- **Discovery never prunes unseen documents** (`plugins/cursor.py`), so a
  document deleted upstream lives forever. Snapshot deltas carry tombstones;
  nothing else does.
- **`chunk_abstract` is dead code** — `chunker_for` never returns it, so
  pubmed uses `chunk_book`. Deliberate: wiring it would change pubmed's
  chunking (invariant 3).
- **Library/Stats counts are cached ~2 minutes** (stale-while-revalidate).
  `invalidate_counts` is wired only to the reset endpoint, so any change made
  outside the web process — including direct SQL — lingers until the TTL.
- **`test_documents_embed_concurrently_and_queue_drains` is flaky**: it
  asserts two threads genuinely overlap, which the scheduler need not do
  under load. A barrier would make it deterministic.
- **`_sync_pipeline_threads` reconciles by collection NAME only**, so a
  pipeline thread keeps the `Collection` object it started with: a TOML edit
  to `corpus_dir` or `[paths]` never reaches the rdf/fts path until restart.
  Harmless for recipe edits (those stages don't use the recipe).
- **No operator control retries a failed acquisition**: the rescan button
  only rescans the filesystem; a transient fetch failure waits for the
  6-hour discovery interval or a worker restart.
- **`EMBED_BATCH=1` is load-bearing.** llama-server corrupts long sequences
  when one `/v1/embeddings` request carries multiple texts, deterministically
  per batch shape, and its prompt cache can replay the corrupted result for
  later single-text requests. Use concurrency for throughput, never batching.
  This does **not** apply to encoders we host ourselves.

## Lessons that cost real time

Recorded because each was invisible until it wasn't, and each would be easy
to reintroduce.

- **A global knob that scopes per-thing.** `CHUNK_VERSION` entered every
  collection's `space_id` while chunking is per-plugin, so changing text
  chunking would have re-pinned collections whose chunker never changed —
  including pre-embedded ones whose vectors *cannot* be recomputed by
  re-chunking. Two chunkers at the same version number also produced
  byte-identical space payloads, so the identity has to name the chunker,
  not just its version.
- **Caching keyed on a name.** `fts.get_index` documents it and the counts
  cache repeated it: two Collection objects can share a name and point at
  different files. Key on the physical path. The scan cursor repeated it a
  third time (`path.name` as the key collided across language directories
  and silently disabled all skipping for the embedded plugin).
- **A linear cost per item is quadratic per corpus, and no test corpus is
  big enough to see it.** `DELETE FROM catalog_fts WHERE uid=?` scanned the
  shadow table (uid is UNINDEXED); at fixture scale that is nothing, at 36k
  rows it was already 98% of the stage, at corpus scale it never finishes.
  When a per-document cost depends on how much has been processed so far,
  treat it as a blocking bug regardless of how fast it currently looks.
- **Telemetry published by the thread doing the work** is invisible exactly
  when it matters — the loop is blocked. Publish from elsewhere.
- **Measure before optimising.** Page slowness was assumed to be connection
  setup; it was 12s of full-table scan. `db.connect()` was 1 ms.
- **An unreachable endpoint is not a broken document.** Recording it as an
  error removes the document from backfill until a human intervenes.
  Distinguish deferral from failure — the acquire path already did.
- **Contact with the real library finds what reasoning does not.** Every
  torrent bug (pad files, asynchronous renames, an always-truthy
  `error_code`) came from running a real swarm, not from reading docs.

## Conventions

- Python 3.12, full type hints, pytest. **Every task ships tests.**
- Tests run in Docker (`docker run --rm -v "$PWD:/srv" -w /srv louise-rag:dev
  python -m pytest tests/ -q`); there is no local interpreter assumption.
- New dependencies, schema changes beyond the doc, or wire-format changes:
  **ask first.**
- Config tiers: env = identity; TOML files = desired state; DB = runtime
  overrides that do not survive restart. Never write desired state to the DB.
- Secrets by filesystem path only — never in env values, config values, logs
  or error messages.
