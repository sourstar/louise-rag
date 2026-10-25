# Louise — knowledge, kept.

Louise is a RAG platform you run yourself, online or off. It comes with
Wikipedia, Wikidata, Project Gutenberg, PubMed and Normattiva (Italian
legislation), with HGNC, ClinVar, gnomAD and HomoloGene as reference
lookups, and any other source can be added by implementing a plugin. Copies of a
collection — the documents and the embeddings both — pass directly
between the people who have them, so nobody re-does work that's already
been done, and the original source isn't asked for the same download
again and again.

Preservation and reliability are two different claims, and one mechanism
delivers both: the hash that lets peers avoid duplicate downloads is the
hash that proves a copy is identical to what the publisher released, so
a collection received from a stranger can be checked against the
original.

**Start with [`docs/README.md`](docs/README.md)** — nine documents, read
in order, from what Louise is to the byte formats. `docs/notes/` is the
engineering record behind them: measurements, postmortems, the design
history; needed to change the code, not to understand or use it.

The app never touches a GPU. All model access is HTTP to your model server
(on the reference node LocalAI, with the VeGa wrapper in front of every
encoder; an Ollama-style chat endpoint also works), so VRAM arbitration
stays where it already is.

## What a node does

```
<LOUISE_ROOT>/collections/<name>/   one collection (docs/07 §7.8):
               shard/   the copy (SHARD) — what is shared
               state/   manifest.sqlite (SQLite WAL), attestations — this
                        peer's alone, never travels
               index/   text index (tantivy) + facet values
               lance/   vectors (LanceDB)
               origin/  the origin files the records are parsed from
<LOUISE_ROOT>/state/node.sqlite     catalogue, coverage, peers, node mode
collections/*.toml + platform.toml  versioned desired state

ingest    acquire (rsync/https/ftp/hf/torrent, resumable, licence-gated,
          drawing from one shared bandwidth budget) -> discover -> rdf ->
          fts -> embed (off | on-demand | all local | all p2p) — rounds
          across all collections, per-document commits, SIGTERM-safe

retrieve  catalogue (tantivy) + per-language BM25 (query translated into
          the main languages) + dense LanceDB, federated across collections
          -> reciprocal-rank fusion (never cross-collection score compares)
          -> pinned reference facts (HGNC/ClinVar-style lookups:
             facts-by-key, never blended into ranking; nothing boosts;
             `--filter keys=<id>` keeps only the records tagged with one)
          -> optional rerank -> answer with [n] citations and per-chunk
             provenance badges (local / imported / verified)

share     export: pack vectors (never text) as Ed25519-signed bundles
          import: signature -> trust -> space match -> possession binding
          (you must hold the same raw bytes) -> verification ladder ->
          all-or-nothing apply, with verify-on-use catching targeted
          poisoning at first retrieval and quarantining the producer

network   peers found through the BitTorrent mainline DHT (by the
          collection's swarm key) or a hand-given address; parts move as
          BitTorrent v2 torrents; who embeds what is a function every peer
          computes, so no node is in charge (docs/04). An optional
          coordinator only keeps statistics and credit.
```

## The web interface

Ask, Library, Network and System, plus Connect (API and MCP clients),
Help and the Reader. Ask is open on the LAN until the first
`louise user add`; from then on it takes a user token. Library, Network
and System take the admin token (`ADMIN_TOKEN_FILE`, a path) or an admin
user (`X-Admin-Token` header or the login cookie).

- **Ask** — search/chat with citations, provenance drawer per hit (producer
  key, verification badge), pinned entity facts, collection scope chips.
- **Library** — per-collection shelf cards (fetch/parse/index/embed bars),
  Tier-3 actions with dry-run previews and their CLI equivalents.
- **Network** — peers, parts offered and fetched, live bandwidth by
  priority class (SSE), the Tier-3 "boost bandwidth tonight" override.
- **System** — effective config labelled by tier (env / TOML / DB-override),
  boot-validation report, worker log, endpoint health.

## Quick start (dev, Docker)

```bash
docker build --target dev -t louise-rag:dev .
docker run --rm -v "$PWD:/srv" -w /srv louise-rag:dev python -m pytest tests/ -q
```

5,300+ tests, no live model needed anywhere in the suite.

The end-to-end two-node flow (producer → coordinator → consumer → verified
answer, mocked embed endpoint as the only fake):

```bash
docker compose -f tests/integration/two_node/docker-compose.yml run --rm driver
# run it twice: the second pass proves system-level idempotence
docker compose -f tests/integration/two_node/docker-compose.yml down -v
```

## Getting a corpus

**Declaratively** (preferred): enable acquisition in the collection's TOML and
use the CLI (`louise` is `python -m app.louise`; the tree is
[docs/07](docs/07-commands.md)) — profiles (`demo`/`starter`/`recent`/`full`) bound size, licence
acknowledgements are recorded before any byte moves, downloads resume, and
every transfer draws from the shared bandwidth budget:

```bash
louise hold records fetch gutenberg --profile demo --dry-run
louise hold records fetch gutenberg --profile demo
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
  `CHAT_URL` (the chat model; `OLLAMA_URL` is its old name, still read), `CHAT_MODEL`,
  `TRANSLATE_URL` (the translation model; unset, the chat model translates),
  `RERANK_URL` as the fallback behind each collection's own reranker, stage
  version bumps (`FTS_VERSION`…), `ADMIN_TOKEN_FILE` (a path; secrets are
  referenced by path, never given as values), `NODE_KEY_PATH` (path to the Ed25519 node key — the key
  *location*, never key material; the worker's), `NODE_PUBLIC_KEY_PATH` (its
  public half — the web process's, which signs nothing).
- **Tier 2 — files** (desired state, versioned):
  `collections/*.toml` (plugin, profile, embed mode, licence ack) and
  [`platform.toml`](platform.toml.example) (`[bandwidth]` caps + schedule,
  `[transfer]` worker counts). No file = today's defaults, unlimited bandwidth.
- **Tier 3 — DB** (runtime overrides, do **not** survive restart):
  scope, embed mode, bandwidth boost, quarantines. On restart the files
  win. The one exception is the node's mode (paused / acquire-only /
  seed-only / running), and a paused collection, which last until changed:
  no file sets them, so there is nothing to reset them to.

Boot validation refuses to start on inconsistency — a vector table whose
dim/space mismatches config is an error, never a warning, because silently
mixed embedding spaces are the one unrecoverable corruption.

## Sharing vectors between nodes

```bash
louise hold key create                                  # once, at NODE_KEY_PATH (+ NODE_PUBLIC_KEY_PATH)
louise hold vectors export gutenberg --shard <slice>/<n> --out /tmp/out   # sign & pack
louise hold vectors import gutenberg --bundle /path/bundle                # verify & apply
louise hold vectors show gutenberg                      # coverage, producers
louise hold key trust <key> [--deny]                    # the trust list
louise share contribute gutenberg                       # the shards this peer leads, shared

# reuse published embeddings (same-function-verified; see /help in the UI):
louise hold vectors import pubmed --archive medcpt-pubmed
```

Bundles carry **no text, ever** — importing requires holding the same raw
document bytes (possession binding), and imported vectors are re-checked
against your own embed endpoint (sample re-embed at import, verify-on-use at
retrieval; `verify: true` on a query, or `POST /api/verify` afterwards).
Each collection's Codex fixes its encoder, its tolerance and its
calibration set (docs/02 §2.4); trust comes from re-checking (docs/03), and
`hold key trust` filters whose attestations count. An encoder run on a card
must reproduce its own output bit for bit (CLAUDE.md invariant 12;
[`docs/notes/approved-encoders.md`](docs/notes/approved-encoders.md)).

The coordinator is optional and standalone, and only keeps statistics and
credit: `python -m app.coordinator.service` (its own SQLite, holds no bulk
data).

## Run a node on any Docker host

Linux, or Docker Desktop on Windows and macOS: [`deploy/docker/`](deploy/docker/compose.yaml).

```sh
cp deploy/docker/louise.env.example deploy/docker/.env   # ports, this node's address, model endpoints
sh deploy/docker/up.sh                                   # build, first boot, start
```

The one-shot `init` service makes the node key, its public half and the
admin token once, each in a volume of its own, and seeds `platform.toml`.
Data lives in named volumes by default, because SQLite WAL and LanceDB need
a local Linux filesystem. A second node on the same host is a second env
file with another `LOUISE_PROJECT` and other ports. The header of
`compose.yaml` explains storage, keys and endpoints.

## Deploy on TrueNAS SCALE

1. Datasets: state on SSD (e.g. `ssdpool/ssd5/louise-rag`); corpus on the pool.
2. Build on the NAS: `docker build -t louise-rag:local .` (the app file uses
   that tag with `pull_policy: never`).
3. Apps → Discover → Custom App → Install via YAML → paste
   [`deploy/truenas-app.yaml`](deploy/truenas-app.yaml). Adjust the volume
   host paths and `CHAT_URL` (LAN IP, not localhost), and give the admin
   token as a file (`ADMIN_TOKEN_FILE`).
4. Open `http://<nas>:30042` → `/ask`.

(The root `docker-compose.yaml` is the earlier single-collection app, kept
for its volumes; new installs use `deploy/truenas-app.yaml` or
`deploy/docker/`.)

`/data` must be a **local** filesystem (SQLite WAL + LanceDB do not tolerate
SMB/NFS); `/corpus` read-only over NFS is fine. Ask is open until the first user
exists — keep the app on LAN/Tailscale; don't tunnel it to the internet.

## Model endpoints

- **Chat**: `CHAT_URL` + `CHAT_MODEL` (OpenAI-compatible or Ollama-style).
- **Embeddings**: each collection's own encoder — the collection file's
  `[encoder]` names it, and there is no default one. Each encoder runs in
  its vendor's bitwise-reproducible mode, one request at a time
  (`ROCBLAS_DEFAULT_ATOMICS_MODE=0` on AMD, `GGML_SYCL_ENABLE_DNN=0` on
  Intel; `-np 1`), set in the model's YAML (invariant 12,
  [`approved-encoders.md`](docs/notes/approved-encoders.md)). Changing a
  collection's encoder or dtype changes its embedding *space* (invariant 1).
- **Translation**: MADLAD-400 (`deploy/madlad-translate`, or under LocalAI);
  set `TRANSLATE_URL` and `TRANSLATE_MODEL`. Unset, the chat model
  translates, much slower.
- **Reranker**: each collection's own; `RERANK_URL` is the fallback. Unset
  = skipped, and the answer says so.
- **Open WebUI**: add an OpenAI connection to `http://<host>:<port>/v1`; the
  `louise` model (every collection) or `louise/<collection>` does full
  retrieval per chat message.

## Resume / re-run semantics

Every write is delete-then-insert keyed on uid/chunk id, so anything can be
re-processed without duplicates; kill/restart anytime (per-document commits,
`stop_grace_period` lets a mid-book embed finish). Interrupted downloads
resume from byte offsets; interrupted bundle imports are swept on restart and
re-queued. Bump `FTS_VERSION`/`EMBED_VERSION`/`ENTITY_VERSION` to redo a
stage. A chunking change bumps that chunker's own version in the Codex
(invariant 3), never a global knob.

## Known limits

- The chapter splitter is heuristic (bounded fallback, never broken).
- One worker process per node — the bottleneck is the embed endpoint.
- The S3 transport is declared but not built; splitting one download over
  several connections is off by default (`segments_per_file`).
- The committed calibration numbers are synthetic stand-ins; run
  `tools/calibrate_space.py --live` against two real backends for real ones.

## License

AGPL-3.0 — see [LICENSE](LICENSE).
