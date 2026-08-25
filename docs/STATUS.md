# Project status

**Last updated: 2026-08-24.** What exists, what is designed but unbuilt, and
what is deliberately not being built. Read this before planning work; read
[`architecture-v2.md`](architecture-v2.md) for the design it implements, and
its §17 amendments (1–37) for every place the design was corrected in flight.

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

**2026-08-24 — the queue-eligible join was the pipeline's ~20-40x brake.**
The lazy-queue selection at the top of every `_next_book` let the planner
invert its join: SEARCH over ALL in_scope rdf_done=1 documents (3.6M on
pubmed, growing with progress) probing the usually-empty queue per row,
plus a TEMP B-TREE — 4.9 s per step, measured live, in every collection
with embed_mode lazy/all. CROSS JOIN pins queue→documents; measured
after: pubmed rdf+fts at 894k docs/h (was ~18-54k/h), remaining corpus
~40 h, not weeks. The plan test asserts join order — behaviour alone
returns the same rows either way. Batch sizes bumped to 1000 in the
node's worker env. Diagnosis chain preserved in commit and
DEPLOYMENT.md; the container-cache cold-parse log and batch timings that
found it stay in.

**2026-08-24 — extra formats, and the p2p exchange policy** (amendment 35).
Peers exchange only the data that fulfils Louise's purpose. An origin's
alternative formats (gutenberg's epubs) are now declarable per spec
(`spec.extras`) and opt-in per collection (TOML `extras = ["epub"]`,
chosen at create or in grow) — fetched from the corpus source as a LOCAL
convenience, classified exclusively (`fetch_files.extra`, split
`acquire_listing` totals), shown as a separate `(+ e/E)` pair on the
Library card, and excluded unconditionally from published corpus torrents.
Explanation lives in Help's Acquisition tab; the GUI stays clean.

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

**2026-08-25 — remote genesis archives are fetched like a corpus and
applied as indexing goes (amendment 38).** Live pubmed after weeks:
8.25M of 40M indexed, **0 of 37 MedCPT shards fetched, 44 documents
embedded** — the post-settle gate (apply once, after the whole collection
had passed rdf/fts) held the free vectors back for the months the
indexing takes, and the embed thread would have downloaded each 3 GB
shard inline when it finally ran. Now `app/bundles/genesis_fetch.py`
lands shards on its own thread under the `archive` budget class (below
`backfill`: the corpus wins the link), bounded on disk
(`GENESIS_CACHE_BYTES`, default 24 GiB ≈ the active shard and a few
ahead) with a disk preflight; `drain_next_shard` applies whatever is on
disk for the documents indexed so far, revisits with a per-shard
geometric backoff (`GENESIS_MAX_WAIT_SEC` doubling to
`GENESIS_PASS_BACKOFF_MAX_SEC` after empty passes), and finalises — files
deleted, `coverage_epoch` bumped — once its id range has no document
waiting. MedCPT's coverage is declared (`ids_per_shard=1_000_000`,
verified exact against chunks 0/1/36), so the worker's "all" backfill
excludes covered ranges by predicate and walks the scope index from a
rowid cursor (`_backfill_rows`; plan-checked: no TEMP B-TREE — the old
`ORDER BY (lang='en')` sorted every candidate per round). Per-flush
manifest writes in `import_shard` are now one `BEGIN IMMEDIATE`
transaction (they were one commit per UPDATE). Observed on the NAS the
same evening: shards 0–2 landed at 7.5–10.8 MB/s; the first pass over
shard 0 ran at **~150 documents/s** on pubmed's own pipeline thread —
a container parse per 30k documents (0.6 ms/doc) plus one Lance
`delete` PER DOCUMENT on a growing table (the quadratic trap again) —
i.e. ~1.8 h per shard with rdf/fts stalled meanwhile. Two fixes in the
same change: the flush deletes ONCE per batch (`book_id IN (...)`), and
a pass is **budgeted** (`GENESIS_PASS_BUDGET_S`, 300 s): it commits what
it has, records the row as `applied` with unknown coverage, and resumes
after an equal share for rdf/fts; a stop (SIGTERM) resumes at once on
restart, and a row left `applying` by a crash resumes the same way.
`idx_documents_native_int` is what makes the pending range count cheap
— the worker builds it at startup. Then the restart's first pass went
to shard 1 and **its same-function proof failed at worst-of-8 cos
0.874** — while shard 0 had proved and imported, and a per-document
check of shard 1's single-chunk samples sat at ≥ 0.993: one sampled
document whose text changed since NCBI's 2023 snapshot. A snapshot of a
living corpus always has some. The proof is now a **quorum** (16
samples, median ≥ τ and 75 % ≥ τ; the samples below τ are stale
documents, evicted from the batch and left to the encoder, never
imported), and a rejected shard stops the archive ONLY while nothing has
proven it — under a proven archive the rejected shard alone is out and
its id range is released to the local encoder. (Shard 1 on the NAS,
rejected under the old rule, was reset to `fetched` by hand —
`UPDATE genesis_imports SET state='fetched', detail=NULL WHERE
shard='1'` — and re-proved; the recipe for any future case.)
Also measured: roughly half of the sampled 1970s-era shard-1 documents
chunk into TWO pieces locally, so the shape gate skips them (the
`chunk_abstract` follow-up in *Known gaps* is what would lift that).
Deployed and re-run: shard 1's quorum proof passed with exactly one
stale sample skipped; the cache bound held the fetcher at 8 shards
(22 GiB, "next fetch when one is final"). One more placement fix from
the same evening: a remote archive's pass runs on the collection's
PIPELINE thread (`_drain_remote_archive`), never on the GPU-serial main
loop — a collection whose encoder is the shared GPU would otherwise
block every other collection's embedding for each 300 s slice.

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
5k. **PubMed container access — measured 2026-08-25, pack BUILT the same
   night (`app/plugins/pubmed/pack.py`).** One row per direct child of
   the container root, in document order (the record index the locators
   carry): the child's exact serialised XML (tail included — emitted only
   once the next sibling has started, so `raw_hash` is byte-identical to
   the DOM path; discovery compares it to detect changed records) plus
   pmid/title/abstract/journal/date/authors for the reader. Row groups
   of 1,000, zstd-3; record i sits in row group i // 1000. Built lazily
   on first sight (discovery) or first fetch, one builder per container,
   atomic tmp+rename, rebuilt when the container's (size, mtime) or
   `PACK_VERSION` changes; freshness memoised per container (the
   footer read cost 3 ms per record, a stat costs microseconds). Lives in
   a hidden `.pack/` beside the containers (discovery lists files only;
   torrent publishing and `corpus_digest` skip hidden entries — cache,
   never p2p material) or in `PUBMED_PACK_DIR` for faster storage (5i).
   Measured on `pubmed26n0600` (30,000 records): build ≈ one DOM parse
   (10.8 s on the Windows box where the DOM parse takes 10.4 s; ~6 s on
   the NAS), **44.9 MB** with the XML kept (the gz is 35.5 MB — ~60 GB
   for all of PubMed), first record 31–48 ms cold, **0.6 ms** warm,
   discovery's three columns 30 ms, reader fields 8 ms, 0 fidelity
   mismatches on a spread sample; `tests/test_plugins_pubmed_pack.py`
   pins byte-identity on the fixture for plain and gzipped containers.
   The XML column (not just the six fields) is what makes it a cache
   with full fidelity — MeSH, references and grants stay available at
   the same cost when entity work wants them. **Live on the NAS
   (deployed 6926a81, 2026-08-26 00:39):** builds take 14–33 s per
   container under the full pipeline load (12–29 MiB each; older
   containers are smaller), paid once — the genesis pass that first
   touches five new containers spends ~125 s of its 300 s slice building
   them (shard 3: 29,706 documents imported in 309 s including the
   builds), rdf/fts continue at 1.4–15 s per 1,000-document transaction
   with the same one-off build on their container; 1,334 containers ≈
   9 CPU-hours spread over the pipeline, after which every record costs
   ~0.6 ms. Same pass, same lesson as 5g: 70,941 of ~100k eligible
   shard-3 documents were chunk-shape mismatches — the chunker change is
   what unlocks the archive.
   *Original measurements and proposal:*
   The corpus is 1,334 gzipped XML containers, 51 GB; one container
   (`pubmed26n0600`) is 35.5 MB gz → 283 MB XML (8×), 30,000 records, so
   the whole archive is ~380 GB uncompressed — never materialise it.
   Per container: gunzip 0.8 s; our `ET.parse` DOM 8.5 s idle (13–20 s
   live, under the pipeline's load) and hundreds of MB of tree, held in
   a ONE-slot cache; `ET.iterparse` streaming with `clear()` 4.1 s;
   extracting (pmid, title, abstract, authors, journal, year) and
   writing a Parquet pack (zstd-9, 1,000-row groups) 4.8 s → **12.2 MB**
   (34.5 MB of raw text; 25,331 of 30,000 records carry an abstract);
   one record by PMID from the pack, via row-group statistics: **16 ms**
   — against 8–20 s today (the container must be parsed whole). A
   gzip stream is not seekable, so "one record" always costs the
   gunzip of everything before it (0.4 s average) plus the parse.
   PROPOSAL — `plugins/pubmed` keeps the gz XML as the archive (what is
   fetched, checksummed and seeded) and builds, once per container, a
   DERIVED pack beside it (`<name>.pack.parquet`, PMID-sorted, rebuilt
   when the gz's checksum changes; ~16 GB for all of PubMed): rdf/fts/
   embed/genesis read records from the pack (16 ms, no cache thrash),
   the reader panel (5j) opens an article instantly, and year/journal
   become available for the bibliographic line without a manifest
   column. Deterministic from the gz, so invariant 3 is untouched; the
   pack is a cache, never exchanged. Build cost ≈ 1,334 × 5 s ≈ 2 h of
   one core, amortised into the first pass over each container.
5j. **Ask tab: translated results and a document reader (operator
   request 2026-08-25; BUILT 2026-08-26).** As specified below, with
   these concrete choices: `POST /ask/translate` (one `llm.translate_texts`
   call per page — numbered JSON in and out, a dropped passage comes back
   as its original; cache keyed on (chunk id, language), bounded; `failed`
   in the reply makes the page say "translation unavailable" instead of
   pretending), the answer language rides `ChatBody.answer_lang` into
   `build_answer_messages` (the model is told to answer in that ISO code —
   never post-translated, so [n] stays aligned), the drop-down offers
   the pre-embedded corpus's eleven languages plus the common European
   ones (`web.RESULT_LANGUAGES`). Reader: `app/reader.py` +
   `GET /read/<collection>/<book_id>?hl=<chunk ids>[&fragment=1]` —
   text from `fts.book_chunks` (a term query on the `book_id` fast field;
   the lexical index stores every chunk's body) or, for an embedded-but-
   not-indexed document, its Lance rows; never the corpus file; the
   in-page panel (`<aside id="reader">`, Esc closes) is the same
   template as the standalone page; hits carry `data-collection/book/
   cid`; [n] in the answer becomes a link into the reader; the reference
   line is authors — title — (PubMed: journal, date from the pack) —
   (lang) — source:id, plus the origin link per plugin. Help updated.
   Not done: PubMed's `date` column (the pack answers the reader instead).
   Verified on both nodes: Louise2's reader opened gutenberg 10218 with
   the hit highlighted (60 chunks, origin link) and translated a snippet
   ("ciao"); the NAS's PubMed hit 10329736 opened with the reference
   "… — J Biol Chem — 1999-May — (en) — pubmed:10329736" and an Italian
   snippet from the local model — but with NO text: after the re-pin
   every PubMed row has `fts_done=0` while the old entries stay
   searchable until rewritten, and the reader had gated its index read
   on the mark. Fixed the same hour: the reader asks the index
   regardless (`test_reader_serves_a_document_mid_reindex…`).
   *Original spec:*
   (1) *Results in another language, interface untouched.* A "show
   results in" drop-down beside the mode selector (default *original*;
   options = the languages indexed in scope plus the operator's default).
   Only CONTENT is translated — snippets, the drawer text and the RAG
   answer — never titles, authors or ids (a bibliographic reference must
   read as published) and never the page chrome. Snippets: one LLM call
   per result page (`POST /ask/translate {lang, items}` → JSON, batched
   like `llm.translate_query`), a small "translated from de" badge, the
   original kept in the provenance drawer, an in-memory LRU keyed on
   (chunk_id, lang) so toggling is free; failure degrades to the original
   with a note. The answer is not post-translated: `build_answer_messages`
   gets the target language and the model answers in it directly
   (cheaper, and citations [n] stay aligned).
   (2) *Open the document from a hit.* The operator asked for a popup;
   PROPOSAL (preferred): an in-page reader panel (slide-over) opened by
   clicking the snippet or the reference — the whole document, the hit
   chunk scrolled into view and highlighted, prev/next hit within the
   document, a bibliographic header and a link to the origin (PubMed
   `pubmed.ncbi.nlm.nih.gov/<pmid>/`, Gutenberg `gutenberg.org/ebooks/<id>`,
   Wikipedia's article URL); the search stays visible behind it, no
   popup blocker, and the same route (`/read/<collection>/<book_id>#c<seq>`)
   is a standalone page for middle-click / new tab. The text comes from
   the INDEX STORES — Lance chunk rows for embedded documents, the FTS
   store otherwise — assembled by book_id and seq, never from the corpus
   file on a click (a PubMed container costs 13–20 s to open; Gutenberg's
   plain files are the only acceptable fallback). Citations [n] in the
   answer become links into the same reader. Reference line per source
   plugin (authors, title, id + link); PubMed's year/journal are not
   persisted today (`documents` has no date column) — an additive `date`
   column is the one schema change this would want, to be asked for.
5i. **Storage tiers: main archive on HDD, working set on SSD (operator
   request 2026-08-25; evaluated, to implement 2026-08-26).** Today
   EVERYTHING sits on `ssd4` (NVMe, 1.81 TB, 569 GB free): corpora 923 GB
   (wikivec 590, gutenberg 256, pubmed 51, wikipedia 26) plus `/data`
   250 GB (Lance 23 + wikivec vectors 127, tantivy 34, pubmed 36). The
   HDD pool `tank` (4 × 6 TB Toshiba in raidz1, 8.9 TB free; `ssd4` is a
   2 TB Kingston NVMe) reads 304 MB/s sequential (direct), 121 MB/s in
   64 KiB direct random reads (~1,900 reads/s — small-file corpora are
   fine too), and 6.3 GB/s when the block is in ZFS's RAM cache (ARC:
   25 GB of 61 GB RAM, 99.8 % hit ratio). Measured stage costs are CPU/GPU, not
   disk: a 30 MB pubmed container takes 13–20 s to gunzip+parse and 0.1 s
   to read from HDD; wikipedia rdf 33–68 s per 1000 documents; a 438 MB
   wikivec shard ~10 s to decode vs 1.5 s to read from HDD (and the
   revisited shard comes back from ARC). Verdict: **no app-level SSD cache
   for reading** — ZFS ARC already is one, warm and free. Plan:
   (1) new datasets `tank/louise-corpora/{wikivec,pubmed,wikipedia}`
   (recordsize 1M, lz4), moved by rsync (~1 h at the measured rate), and
   the app's host mounts repointed — the in-container paths
   (`/corpora/<name>`) stay identical, so every locator stays valid and
   no discovery repair runs; (2) **gutenberg stays on SSD for now**: 256 GB
   of 2.8 M small files, and discovery stats every one of them each
   rescan — measure a scan from HDD before moving it; (3) `/data` (SQLite
   WAL, tantivy, Lance, the genesis cache's 3 GB mmap'd shards) stays on
   SSD, invariant 6; (4) the one place SSD staging DOES pay is
   ACQUISITION: torrent pieces land as random writes, which raidz1
   handles badly — download into `/data/staging` and move each file to
   its corpus directory on completion (the `.part` discipline already
   isolates the incomplete file; the engine's preflight bounds the
   staging area); (5) no L2ARC — it excludes streaming reads by default
   and the ARC hit ratio leaves nothing for it.
   **Built 2026-08-26 (operator: "datasets, not folders").** Datasets
   `tank/louise-corpora/{wikivec,pubmed,wikipedia}` (recordsize 1M, lz4,
   atime off, root:root 770 like the originals); bulk `rsync -aH` while
   the app kept running (wikipedia 26 GB in 64 s, pubmed 51 GB in
   2.6 min, wikivec 590 GB at 200–570 MB/s), then a stop → delta sync →
   mount switch → start window (`/root/switch_window.sh` +
   `/root/switch_mounts.py` on the NAS, which edits the custom app's
   compose config through `midclt call app.update` — only the three host
   paths change, the in-container paths and therefore every locator stay
   identical). Two knobs went in with the switch: `PUBMED_PACK_DIR=
   /data/pubmed-pack` (the packs on the NVMe; the ones already built were
   copied over) and `ACQUIRE_STAGING_DIR=/data/staging` (ff84884: the
   https/ftp/hf transports stream into staging on the NVMe, the verified
   file is moved into its corpus as one sequential copy; an interrupted
   `.part` resumes from staging; a file the area cannot hold lands in
   place; torrents unchanged until 5h). **Switched 2026-08-25 10:22
   CEST:** delta sync moved 0 bytes, the app came back on the datasets,
   discovery on all three reported 0 changed documents, PubMed's
   re-index went on from the 193 packs copied to `/data/pubmed-pack`
   (0 rebuilds), wikivec's archive reads its shards from tank. Lesson
   that cost a failed `up`: `midclt call app.stop/app.start/app.update`
   return a JOB and go on in the background — the window script's
   `start` raced its own `update` and left two *Created* containers with
   name conflicts; `midclt call -j …` waits, and is what the deploy
   recipe uses now. The ssd4 originals were datasets too
   (`ssd4/data/<name>`, which is why a plain `mv` said "resource busy");
   they were `zfs rename`d to `ssd4/data/<name>.moved-to-tank` (675 GB,
   reversible) — `zfs destroy` is the operator's call after a few days on
   tank; the `.pack/` copied into `tank/louise-corpora/pubmed` (5 GB) is
   unused under `PUBMED_PACK_DIR` and can go with them. Gutenberg
   (`ssd4/data/gutenberg`, 302 GB) stays on the NVMe.
5h. **wikivec per-language torrents (operator approved 2026-08-25; BUILT
   2026-08-26).** `torrent publish <collection> --per-language
   [--languages en,tr]` hashes one torrent per `data/<lang>` directory
   (en 192 GB, de 83, fr 75, ru 57, es 54, it 42, ja 33, pt 25, zh 14,
   fa 11, tr 8.4), ALL rooted at `data` with an include filter
   (`publish_corpus(include_patterns=…)` — libtorrent's add_files asks
   about directories too and a False prunes the subtree, so directories
   pass and files are judged), so a peer's files land in the plugin's
   own layout and the torrents combine. The seeding record is a list
   (`seeding.record_published` merges by info_hash; older single-dict
   records still read), the worker seeds every entry. On the peer,
   `peer_source` is one magnet or several — a TOML array, one per line,
   or comma-separated (`engine.peer_sources`); `effective_spec` makes
   one endpoint per torrent, the listing is their union, and
   `run_acquisition` fetches the torrents ONE AFTER THE OTHER in the
   listed order — the first language is complete, and the pipeline busy
   on it, while the rest arrives (amendment 37). The create dialog
   validates every entry. `tests/acquire/test_per_language_torrents.py`.
   **Published on the NAS 2026-08-26:** eleven torrents, 589 GiB, all
   seeding (the Network page's table shows them); hashing ran at
   ~500 MB/s from `tank` with the ARC still warm from the copy (de 82 GB
   in ~3 min, en 191 GB in ~5 min). Lesson: a `nohup docker exec … &`
   started over ssh dies with the session (the first pass lost its log
   and stopped after two languages); `docker exec -d` runs detached
   inside the container — use it for anything that outlives the ssh
   call. The worker's control manifest (seeding table, worker state) is
   the DEFAULT collection's (`/data/genome-dm6/manifest.db` on the NAS),
   not `/data/manifest.db` — read it through `db.connect()`.
5g. **Pre-embedded archives: fetched like a corpus, applied as indexing
   goes, one knob (amendment 38; operator policy, 2026-08-24).** Live
   pubmed: 8.25M/40M indexed, 0/37 MedCPT shards, 44 embedded — the
   post-settle gate withheld the archive for months. Built: the archive
   is a second acquisition stream (`app/bundles/genesis_fetch.py`, budget
   class `archive` below `backfill`, bounded on-disk cache with
   preflight); fetched shards apply at once for the indexed documents,
   are revisited with per-shard geometric backoff, and go FINAL (files
   deleted) when their id range has no document left waiting for fts;
   MedCPT declares its coverage (`shard_of_id`: exact PMID ranges); the
   single Embed-mode knob governs the local encoder only, and `all`
   selects the documents the archive cannot cover through a rowid
   cursor (no per-round re-walk of the covered prefix, no ORDER BY).
   Card: main `embedded (dense)` = total coverage; sub-box = `fetched
   (vectors)` + `pre-embedded (dense)`, no knob. Local archives
   (wikivec) unchanged.
5f. **Index sharing for LIGHT peers — decide by measurement (2026-08-24).**
   The lexical index stays recomputed by default (≈1 ms/doc, deterministic,
   zero trust surface; a received tantivy segment can only be fully
   verified by rebuilding it, and it is coupled to FTS_VERSION /
   ENTITY_VERSION / chunker / tokenizer). But a light peer "does only the
   work it cannot avoid", and pubmed's ~40 CPU-hours IS avoidable if the
   index travels. Option to build if the numbers favor it: index segments
   per lease (they merge like tantivy segments do) shipped beside the
   bundle, guarded like vectors are — an INDEX-SPACE PIN (the four versions
   above; refuse on mismatch) and SAMPLED verification (re-index a few
   received documents locally, compare postings). Decide once pubmed's
   index is complete: its size on disk vs the measured 40 h rebuild.
5e. **Two peer classes, and benchmark-based eligibility (operator design,
   2026-08-24).** *Light* peers do only the processing that cannot be
   shared (discovery, FTS — local by design) and still share corpus bytes
   and processed data (seed torrents, serve bundles). *Full* peers also
   take shared-workload leases (embedding batches) from the coordinator.
   For now a full peer gets a FIXED, generous lease TTL (the existing
   24 h `LEASE_TTL_SECONDS`); an expired lease is reissued to whoever
   asks next. TO BUILD: a benchmark a peer runs to become ELIGIBLE for
   shared workload (encoder consistency vs the space's baseline + a
   throughput measurement) that also sets a proper per-peer lease TTL
   instead of the fixed one; until then eligibility is the operator's
   choice (`contribute` mode on/off) and the TTL is the constant. See
   also the corpus chunking proposal recorded with amendment 37.
5d. **License gate on USE, not just acquisition (amendment 36; operator
   report, 2026-08-24).** A yellow-class shard copied from a peer into a
   hand-populated collection was indexed and imported with no ack — the
   gate only fired on origin fetches. To build: worker round loop +
   pipeline threads skip a yellow+ collection with no valid
   `license_ack` (discovery, genesis, bundles, onward exchange all
   excluded); Library card shows "license acknowledgement required" with
   the existing acknowledge flow; boot warning names gated collections.
   Until built, this is enforced by review only.
5c. ~~**Acquisition source choice at collection create.**~~ **Built
   2026-08-24:** the create panel's Acquisition drop-down offers *off* /
   *from the source* / *from peers* (magnet or .torrent). `peer_source`
   in the TOML redirects the whole acquisition to the torrent transport
   (`engine.effective_spec`); the worker seeds every torrent recorded by
   `torrent publish` and credits uploads to the UP bucket
   (`app/acquire/seeding.py`). What remains unbuilt is DISCOVERY (item
   1): today the magnet carries a direct-peer hint (`x.pe=host:port`)
   pasted by the operator.
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
  chunking (invariant 3). **Measured 2026-08-25, and it now matters:**
  MedCPT holds ONE vector per article (title + abstract, 512 tokens), the
  genesis shape gate binds vectors to chunks 1:1, and `chunk_book` splits
  every abstract-bearing article into two or more chunks — of shard 0's
  647,490 held documents 274,514 (42 %) are multi-chunk, of shard 1's
  938,691 609,182 (65 %), and the fraction rises with the year as
  abstracts become universal. So the archive can cover only the
  title-only records; every article with an abstract is skipped and
  falls to the local encoder once its shard is final. **Built
  2026-08-26 (operator approved 2026-08-25; amendment 39):**
  `chunker_name("abstract")` now returns the abstract chunker — one
  chunk per article, `title\n\nabstract`, no chapter label — under its
  own `ABSTRACT_CHUNK_VERSION` (PubMed-only, invariant 3), and
  "abstract" is no longer grandfathered in the space payload, so
  PubMed's space id moves and the boot check refuses the old pin;
  `python -m app.cli space repin <collection> --yes` (worker stopped) is
  the operator step: drops the pin, resets `embed_done` AND `fts_done`
  (the lexical index held chunk ids cut by the old chunker — same id,
  different text otherwise), clears the verification ledger, keeps
  fetched archive shards (`applied` → `fetched`, no re-download), drops
  finished rows, removes the Lance table. Measured before choosing the
  text format, 24 abstract-bearing articles against the archive: cosine
  median 0.988 / min 0.960 for `title\n\nabstract` (NCBI's `[SEP]` join
  0.991 / 0.974 — not worth a marker in displayed text; abstract alone
  0.976 / 0.945 — the title matters). The encoder is TEI (MedCPT
  tokenizer 3.8–6.2 chars/token, median 5.0; TEI refuses >512 tokens
  with 413 unless started with --auto-truncate), so the client's char
  budget of ctx·4 = 2,048 chars is 410–530 tokens and its over-length
  fallback now shrinks in 20 % steps instead of halving. Tests:
  `tests/test_pubmed_abstract_chunker.py` (one chunk per fixture
  article through the worker, PubMed's id moves and book's does not,
  the repin's every effect). **Re-pinned on the NAS 2026-08-26 07:39
  (5dd6250):** the old pin `b009849c…` held 1,587,743 archive vectors
  (title-only records) and 12,119,490 lexical entries — all reset; the
  worker pinned `41844ebb…` at boot, re-indexes at 1,000 documents /
  1,000 chunks per batch (one per article, ~8–10 h for the 12.1M from
  the packs), and the first archive pass over the re-indexed documents
  reported **0 chunk-shape mismatches** — every article the archive
  holds is now covered as it becomes indexed; the 8 fetched shards were
  kept (`applied` → `fetched`), nothing re-downloaded.
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
- **A rate from one window is not a rate.** The Library ETA divided one
  sample window's progress by its length, so a window that caught a
  commit burst said "~20min" and the next, idle one, said "~9h" — the
  operator saw a number swinging between optimistic and pessimistic and
  trusted neither. Now it is an EMA (alpha 0.3, ~3 windows to settle)
  over the windows that saw progress, it needs two windows before it
  shows anything, and while work remains but the rate is untrustworthy
  or zero it prints **TBD** rather than nothing — an honest "not yet"
  is a value; a blank is indistinguishable from "done". Same rule as
  `stats.eta_seconds`: never a number the page cannot stand behind.
- **A statistic the reader cannot act on is noise, not transparency.**
  The Network page listed the transfer manager's four priority classes
  per direction — "interactive 0 · delta 0 · backfill 0 · seeding 0" —
  which answered a scheduler's question, not an operator's. Replaced by
  two totals and the download split by collection; the classes stay in
  the API payload for tests.

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
