# Project status

**Last updated: 2026-08-23.** What exists, what is designed but unbuilt, and
what is deliberately not being built. Read this before planning work; read
[`architecture-v2.md`](architecture-v2.md) for the design it implements, and
its §17 amendments (1–34) for every place the design was corrected in flight.

Node-specific facts — addresses, paths, ports, the live collection inventory —
are deliberately **not** here: they live in `deploy/reference/DEPLOYMENT.md`,
which is gitignored, because this file is public to anyone who clones.
(Working from a machine without that file: a current copy is kept on the
node itself, beside the deployment, readable over the same SSH access used
to deploy.)

## Bootstrapping a new development machine

Everything except two items travels through this repository.

1. `git clone` — code, tests, docs, CLAUDE.md. Read this file first.
2. Fetch the node reference from the node into the local clone (the app
   root is the directory the deploy procedure tars into; the file sits one
   level above `src/`):
   `scp <node>:<app-root>/DEPLOYMENT.md deploy/reference/DEPLOYMENT.md`
   After editing it locally, copy it back the same way — the node-side copy
   is what the *next* machine bootstraps from.
3. **The SSH key does not travel** — not through git, chat, or any log
   (invariant 8). Move it between machines yourself, or authorize a fresh
   key on the node.
4. Reaching the node from outside its LAN: substitute your remote
   host/tunnel for the LAN address in every DEPLOYMENT.md command; nothing
   else changes.
5. No local Python is assumed anywhere: `docker build -t louise-rag:dev .`
   recreates the test image, and tests run as in Conventions below.

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

**2026-08-23 — discovery is batched, resumable and off-thread** (amendment
32). The startup scan used to hold the collection's pipeline thread (wikivec:
1,083 s per restart, no rdf/fts/genesis meanwhile), ran in autocommit (one
transaction per row — that WAS the 1,083 s), and saved its cursor only at
the end, so a restart mid-scan threw the whole pass away. Now:
`sync_collection` commits every `DISCOVER_BATCH_REFS` (2,000) refs and
signals the plugin (`DiscoverState.received/committed/on_commit`);
`cursor.Progress` promotes a container into the saved cursor only once its
last ref is committed, so an interrupted scan resumes exactly where its
rows are durable; the worker runs each collection's scan on its own thread
(`_start_scan`, one at a time, stop-aware on SIGTERM and removal, joined at
shutdown). The embedded plugin's boundary straddler made this subtle and
exposed two latent locator bugs, both fixed: a rescanned shard whose
neighbour is skipped now completes the carry from that neighbour's tail or
head (it used to mint or "relocate" the straddler with one segment), and
an embedded ref's `size` is the article's row count, not its shard's byte
size (a straddler gaining a segment is now a change, never a relocation).
`tests/test_discovery_resumable.py` pins it, including every interruption
point of a straddling fixture. The genome collections are parsing/indexing
only by operator decision — embedding awaits an architectural review
([`dna-search-axes.md`](dna-search-axes.md)).

**2026-08-23 — local genesis archives no longer sweep the shard list.**
Measured live on wikivec (959 shards, CPU encoder, so the pipeline thread
owns rdf + fts + genesis): with 200 embed-eligible documents present,
genesis claimed every round re-reading the next shard oldest-first (~437 MB,
~10 s, "imported 0 vectors across 0 documents") and rdf/fts got one turn
per full sweep — **200 documents per 3.2 hours, 13.5 M pending.** Now the
candidates' own locators name the shards worth reading (an index range over
`idx_documents_scope`, plan-checked), a shard nobody waits for is never
opened, documents a read cannot cover (boundary straddlers) are remembered
as residual per shard, and while rdf/fts still have work a read waits for a
batch (`GENESIS_MIN_CANDIDATES`) or an age cap (`GENESIS_MAX_WAIT_SEC`).
`tests/bundles/test_genesis_starvation.py` pins all of it, through the
worker — keep it passing across any rework of this path. Open follow-on:
the read still loads a whole shard's `embedding` column for the ~200–1000
documents it wants; the locators carry row ranges, so reading only those
row groups would cut it to a fraction (and would make straddlers
importable by reading both segments).

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
5b. **The enumeration travels with the corpus.** A peer that downloads a
   corpus snapshot still re-derives its document list by scanning (hours
   on a large corpus, even resumable). The snapshot manifest already names
   every uid with its raw hash (§5, parent-chained deltas); extended with
   each document's locator-within-the-snapshot, mtime and size it becomes a
   *signed discovery input*: import it, then verify by stat and hash
   spot-check instead of enumerating. This is the part of "indexing" that
   SHOULD be shared. The FTS index itself should not: it is cheap to
   compute (≈1 ms/doc warm, against hours to download), expensive to verify
   (a binary blob; poisoning is invisible), coupled to local config
   (`FTS_VERSION`, analyzers, entity version, chunker) and to the tantivy
   version — and invariant 6 says SQLite state never travels. The rdf
   output (catalog metadata) is small and derivable and could ride along
   for "browse before you index"; a possible later addition to the same
   manifest.
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

## Invariants review (2026-08-23 — amendments 30, 31)

The ten CLAUDE.md invariants were re-read against what was built. They
hold, with one violation and several stale sentences: **#10 was inverted
for `embed_mode`** (TOML parsed and only displayed; the Library's override
permanent) — fixed, the worker now applies the TOML on restart/add/edit and
the reference node's TOMLs were written with their effective modes first so
the deploy changed nothing. Each invariant now says how it is enforced
(*boot* / *test* / *review*); #7, #8, #9 gained mechanical tests
(`tests/test_invariant_guards.py`); #8 gained `ADMIN_TOKEN_FILE`; the stale
`space_id` component list in #1 is a pointer to §3. A short list of
**engineering rules** joined CLAUDE.md, each a recorded incident. Every
stdout log line now names its collection. The golden test no longer fails
on a fresh clone (fixture mtimes are pinned to the golden's values).

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
- **"Did work" is not "made progress".** A stage that returns True for a
  round in which it imported nothing still wins the round from every stage
  behind it. Genesis's 0-document shard reads were each legitimate
  bookkeeping and together were the whole behaviour of the collection. When
  a stage can succeed at doing nothing, the selection that hands it the
  round must know the difference — and a per-round cost that scales with
  the size of a LIST (shards) rather than with the work available
  (candidates) is the quadratic trap again, in a different coat.
- **A log line per round is a benchmark.** "imported 0 vectors across 0
  documents" every ~10 s, for hours, was the measurement; nobody had to
  profile anything. Read the worker log as a time series before reading
  the code.

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
