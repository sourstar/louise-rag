# louise-rag — a P2P RAG platform

A self-hosted, plugin-based RAG platform that grew out of a single-corpus
Gutenberg app. One node ingests corpora (Project Gutenberg, PubMed, your own
plugin), indexes them lexically and densely, and answers questions with `[n]`
citations — and nodes can **share the expensive part**: embedding vectors
travel between peers as signed, verifiable bundles, so a network of N
consumers pays for GPU work roughly once instead of N times.

The design is documented in [`docs/architecture-v2.md`](docs/architecture-v2.md);
the build order and per-task contracts in
[`docs/implementation-prompts.md`](docs/implementation-prompts.md); defects and
spec corrections found during integration in
[`docs/phase1-integration-notes.md`](docs/phase1-integration-notes.md).

The app never touches a GPU. All model access is HTTP to your existing
Ollama/llama.cpp/TEI containers, so VRAM arbitration stays where it already is.

## What a node does

```
/corpus (ro)   raw documents, one layout per plugin (gutenberg: dir-per-book)
/data  (ssd)   per-collection manifest.db (SQLite WAL) + fts/ (Tantivy)
               + lancedb/ (vectors) — local truth, never travels
/config        collections/*.toml + platform.toml + spaces/ artifacts
               — versioned desired state: a node restores from
               git clone + compose up + re-run of acquisition

ingest    acquire (rsync/https/ftp/hf, resumable, licence-gated, drawing from
          a shared bandwidth budget) -> discover -> rdf -> fts -> embed
          (lazy|all|off) — round-robin across all collections, per-document
          commits, SIGTERM-safe

retrieve  catalog FTS5 + per-language BM25 (LLM query fan-out translation)
          + dense LanceDB, federated across collections
          -> reciprocal-rank fusion (never cross-collection score compares)
          -> entity boosts + pinned reference facts (HGNC/ClinVar-style
             lookups: facts-by-key, never blended into ranking)
          -> optional rerank -> answer with [n] citations and per-chunk
             provenance badges (local / imported / verified)

share     export: pack vectors (never text) as Ed25519-signed bundles
          import: signature -> trust -> space match -> possession binding
          (you must hold the same raw bytes) -> verification ladder ->
          all-or-nothing apply, with verify-on-use catching targeted
          poisoning at first retrieval and quarantining the producer

network   optional coordinator (its own tiny FastAPI service): advisory work
          leases, demand aggregation from consumers' lazy queues, credit
          leaderboard, spot-check reports feeding a probation throttle
```

## The four-page UI

`/ask` is open on the LAN (read-only); Library/Network/System sit behind a
single `ADMIN_TOKEN` (`X-Admin-Token` header or the login cookie).

- **Ask** — search/chat with citations, provenance drawer per hit (producer
  key, verification badge), pinned entity facts, collection scope chips.
- **Library** — per-collection shelf cards (fetch/parse/index/embed bars),
  Tier-3 actions with dry-run previews and their CLI equivalents.
- **Network** — live bandwidth by priority class (SSE), Tier-3 "boost
  bandwidth tonight" override.
- **System** — effective config labelled by tier (env / TOML / DB-override),
  boot-validation report, worker log, endpoint health.

## Quick start (dev, Docker)

```bash
docker build -t louise-rag:dev .
docker run --rm -v "$PWD:/srv" -w /srv louise-rag:dev python -m pytest tests/ -q
```

572 tests, no live model needed anywhere in the suite.

The end-to-end two-node flow (producer → coordinator → consumer → verified
answer, mocked embed endpoint as the only fake):

```bash
docker compose -f tests/integration/two_node/docker-compose.yml run --rm driver
# run it twice: the second pass proves system-level idempotence
docker compose -f tests/integration/two_node/docker-compose.yml down -v
```

## Getting a corpus

**Declaratively** (preferred): enable acquisition in the collection's TOML and
use the CLI — profiles (`demo`/`starter`/`recent`/`full`) bound size, licence
acknowledgements are recorded before any byte moves, downloads resume, and
every transfer draws from the shared bandwidth budget:

```bash
python -m app.cli corpus add gutenberg --profile demo --dry-run
python -m app.cli corpus add gutenberg --profile demo
```

**Manually** (the original path, still fine): rsync the Gutenberg generated
collection yourself and mount it read-only as `/corpus`:

```bash
# dry run first — check size and file count
rsync -an --stats rsync.mirrorservice.org::gutenberg.org/cache/epub/ /mnt/tank/gutenberg/cache/epub/
# actual sync — skip Kindle builds (~half the volume)
rsync -avHS --timeout 600 --delete \
  --exclude '*/mbt-*' --exclude '*.mobi' --exclude '*.kf8*' \
  aleph.gutenberg.org::gutenberg-epub /mnt/tank/gutenberg/cache/epub/
```

New or changed documents are picked up automatically: the worker rescans at
startup and every `DISCOVER_INTERVAL_MIN` minutes (default 360). The corpus
root holds ~79k flat subdirectories — keep SMB browsers and media scanners
away from it.

## Configuration, in three tiers

- **Tier 1 — env** (identity/infrastructure, restart to change): paths, ports,
  `OLLAMA_URL`/`EMBED_URL`/`RERANK_URL`, `EMBED_MODEL`, version bumps
  (`CHUNK_VERSION`…), `ADMIN_TOKEN`, `NODE_KEY_PATH` (path to the Ed25519
  node key — the key *location*, never key material).
- **Tier 2 — files** (desired state, versioned):
  `collections/*.toml` (plugin, profile, embed mode, licence ack) and
  [`platform.toml`](platform.toml.example) (`[bandwidth]` caps + schedule,
  `[transfer]` worker counts). No file = today's defaults, unlimited bandwidth.
- **Tier 3 — DB** (runtime overrides, do **not** survive restart):
  pause/resume, scope, bandwidth boost, quarantines. On restart the files win.

Boot validation refuses to start on inconsistency — a vector table whose
dim/space mismatches config is an error, never a warning, because silently
mixed embedding spaces are the one unrecoverable corruption.

## Sharing vectors between nodes

```bash
python -m app.cli bundle keygen /config/node.key        # once; set NODE_KEY_PATH
python -m app.cli bundle export gutenberg /tmp/out      # sign & pack
python -m app.cli bundle import gutenberg /path/bundle  # verify & apply
python -m app.cli bundle status gutenberg               # producers, ledger
python -m app.cli bundle trust  gutenberg <pubkey> [--quarantine]
python -m app.cli contribute gutenberg http://coordinator:8750
```

Bundles carry **no text, ever** — importing requires holding the same raw
document bytes (possession binding), and imported vectors are re-checked
against your own embed endpoint (sample re-embed at import, verify-on-use at
retrieval; `verify: true` on a query, or `POST /api/verify` afterwards).
Trust is on-first-use per collection and quarantinable; calibrated tolerances
come from per-space artifacts (`tools/build_space_artifact.py`,
`tools/calibrate_space.py` — see [`docs/calibration.md`](docs/calibration.md)).

The coordinator is optional and standalone:
`python -m app.coordinator.service` (its own SQLite, holds no bulk data).

## Deploy on TrueNAS SCALE

1. Datasets: state on SSD (e.g. `ssdpool/ssd5/louise-rag`); corpus on the pool.
2. Build on the NAS: `docker build -t gutenberg-rag:local .` (the compose file
   uses that tag with `pull_policy: never`).
3. Apps → Discover → Custom App → Install via YAML → paste
   [`docker-compose.yaml`](docker-compose.yaml). Adjust the volume host paths,
   `OLLAMA_URL` (LAN IP, not localhost) and set `ADMIN_TOKEN`.
4. Open `http://<nas>:8742` → `/ask`.

`/data` must be a **local** filesystem (SQLite WAL + LanceDB do not tolerate
SMB/NFS); `/corpus` read-only over NFS is fine. Ask is unauthenticated by
design — keep the app on LAN/Tailscale; don't tunnel it to the internet.

## Model endpoints

- **Chat + embeddings via Ollama** (or anything OpenAI-compatible with
  `EMBED_API=openai`). For corpus-scale embedding, run a dedicated instance:

  ```
  OLLAMA_NUM_PARALLEL: "16"
  OLLAMA_CONTEXT_LENGTH: "4096"
  OLLAMA_MAX_LOADED_MODELS: "1"
  OLLAMA_KEEP_ALIVE: "-1"
  OLLAMA_FLASH_ATTENTION: "1"
  ```

  and point `EMBED_URL` at it, keeping `EMBED_BATCH × EMBED_CONCURRENCY ≥`
  its `NUM_PARALLEL`. Changing the embedding model or `EMBED_DTYPE` changes
  the embedding *space*: wipe `lancedb/` and bump `EMBED_VERSION`.
- **Optional reranker**: a TEI container (`BAAI/bge-reranker-v2-m3`, CPU is
  fine); set `RERANK_URL` to its `/rerank`. Unset = skipped gracefully.
- **Open WebUI**: add an OpenAI connection to `http://<nas>:8742/v1`; the
  `gutenberg-rag` model does full federated retrieval per chat message.

## Resume / re-run semantics

Every write is delete-then-insert keyed on uid/chunk id, so anything can be
re-processed without duplicates; kill/restart anytime (per-document commits,
`stop_grace_period` lets a mid-book embed finish). Interrupted downloads
resume from byte offsets; interrupted bundle imports are swept on restart and
re-queued. Bump `CHUNK_VERSION`/`FTS_VERSION`/`EMBED_VERSION`/`ENTITY_VERSION`
to selectively redo a stage corpus-wide.

## Known limits

- Dense search is brute-force; add a LanceDB ANN index past ~100k vectors.
- The chapter splitter is heuristic (bounded fallback, never broken).
- One worker process per node — the bottleneck is the embed endpoint.
- Torrent/S3 transports are stubs; multi-connection single-file downloads are
  declared but refused (`segments_per_file=1` only).
- The committed calibration numbers are synthetic stand-ins; run
  `tools/calibrate_space.py --live` against two real backends for real ones.

## License

AGPL-3.0 — see [LICENSE](LICENSE).
