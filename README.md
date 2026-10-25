# Louise — knowledge, kept.

**Shared knowledge anyone can verify.** Large libraries of texts —
Project Gutenberg's books, PubMed, Wikipedia, Wikidata, Italian law — are
queried by an AI that runs on your own computer. It searches the real
texts and quotes them, so every answer can be opened and checked passage
by passage. No central server, no blind trust: *don't trust, verify.*

## In a nutshell

- **You hold the knowledge, you don't receive it.** The texts, their "map
  of meanings" (the embeddings) and the model that answers all live at
  home. No question leaves the machine, nobody filters the answers.
- **The work is shared.** Computing a collection's map of meanings alone
  takes weeks. Like BitTorrent, every peer keeps pieces and passes them
  on, and every peer computes a piece of the work: everyone processes a
  part, everyone uses the whole, and nobody redoes what someone already did.
- **Everything is checkable.** Each collection has a **Codex**, its
  immutable birth certificate: the sources, the method, the model.
  Anyone can rerun the process and get the same bytes; what a stranger
  sends is re-checked, never believed.
- **Knowledge that grows.** New blocks arrive every month; what is
  already published stays immutable and verifiable. The same method works
  on any body of text — scientific papers, laws, archives, or your own
  notes, kept as a private collection you never have to share.
- **Under construction — Gutenberg × Wikidata.** A knowledge graph over
  literature: the people, places and events every book mentions, linked
  to Wikidata; the relations the books assert; the communities they form.
  Where a book contradicts the encyclopedia the claim is kept and flagged,
  never corrected: the system shows where literature and facts diverge,
  each voice traceable to its passage. A source for historians — how an
  era told the world, not just how things went.

## How it is achieved

| promise | mechanism | spec |
|---|---|---|
| a collection is one well-defined thing | the **Codex**: a signed JSON file fixing origin, parser, chunking, the encoder's weights and engine, licence, cadence; `collection_id` is its hash, and `space_id` the hash of what decides the vectors — two spaces are never mixed | [02](docs/02-collections.md), [09 §9.3](docs/09-formats.md) |
| what you hold is what was published | records are cut into **parts** (zstd JSON Lines) that never move once closed; each **snapshot**, at the collection's cadence, lists every part's hash and is signed (Ed25519); each record carries the hash of the origin bytes it was parsed from, so it can be re-fetched and reproduced | [02 §2.2–2.3](docs/02-collections.md), [06 §6.4](docs/06-plugins.md) |
| vectors anyone can check | an encoder runs in its vendor's bitwise-reproducible mode; a **calibration gate** proves an engine reproduces the Codex's reference vectors before it may write one; **VeGa**, a wrapper between the model runner and each backend, issues a receipt for every answer, checked before the vector is stored (below) | [03](docs/03-checking.md) |
| no central server | peers find each other on the BitTorrent mainline DHT (keyed by collection) and move parts as BitTorrent v2 torrents; who embeds which part is a rendezvous function every peer computes the same way, so no node is in charge | [04](docs/04-sharing.md) |
| trust from reproduction, not votes | a bundle of vectors carries no text; importing it needs the same source bytes, passes a verification ladder and a sampled re-embed, and every vector is re-checked when first used; attestations say who reproduced what | [03 §3.5–3.6](docs/03-checking.md), [08](docs/08-adversarial.md) |
| answers that quote | a text index (tantivy, BM25, per language) and a vector index (LanceDB) per collection, fused by reciprocal rank — never by comparing scores — then reranked; reference lookups (genes, variants) are pinned facts, never ranked; every answer cites its passages | [05](docs/05-ask.md) |
| any source | a **plugin** declares acquisition, parsing, chunking and the licence class; nothing the origin says about an entity is dropped | [06](docs/06-plugins.md) |
| everything local | the app never touches a GPU: every model (encoder, chat, translation, reranker) is an HTTP endpoint on your own model server | [07 §7.8](docs/07-commands.md) |

One CLI drives it — `louise hold` (your own copy), `louise share` (with
peers), `louise ask` (questions), `louise node` (the daemon) — and the web
interface is the same operations behind a page.

**Read [`docs/README.md`](docs/README.md)** — nine documents, in order,
from what Louise is to the byte formats. `docs/notes/` is the engineering
record behind them (measurements, postmortems, design history);
[`docs/notes/STATUS.md`](docs/notes/STATUS.md) is where things stand, and
[`docs/HANDOVER.md`](docs/HANDOVER.md) is where a new maintainer starts.

## Models: LocalAI and VeGa

Louise runs no model. Every model it uses is an HTTP endpoint on the
node's model server — on the reference node **LocalAI**, which loads,
keeps and evicts the models and alone manages the card's memory. Chat,
translation and reranking go to it directly through its OpenAI-compatible
API. Embeddings do too, but an embedding becomes a stored vector, and a
stored vector must be provable. So every encoder is served through
**VeGa**.

**VeGa, the Vector Gate, makes the provenance of every vector provable.**
It is two pieces beside LocalAI:

```
Louise ── POST /v1/embeddings (model vega-<name>, each input tagged) ──► LocalAI
  │                                                                         │ gRPC
  │                                                     VeGa wrapper (a LocalAI backend, vega-<backend>)
  │                                                       │ strips the tag; the real backend is its child
  │                                                       ▼
  │                                                     llama.cpp SYCL / ROCm / CUDA, OpenVINO, …
  │                                     receipt ◄─────────┘ held BEFORE the vector goes back
  │                                        │
  └── GET /vega/receipts?ids=… ──────────► VeGa service (receipts and run descriptions)
```

- **The wrapper** is installed in LocalAI as a backend (`vega-<backend>`)
  and launches the real backend as its own child. It therefore knows the
  run from what it launched, never from what a configuration declares:
  the digest of the backend's installed files, the weights (hashed while
  they load), the exact arguments, load options and environment, the
  device and its driver. That is the **run identity**. For every answer
  it issues a **receipt** binding that run to the digest of the input the
  backend received and of the vector it returned. The service must hold
  the receipt before the vector is released.
- **Louise** sends two requests: the embedding to LocalAI, then the
  receipts to VeGa. It writes a vector only when every input has its
  receipt, the digests match what was sent and what came back, and the
  run has passed the collection's calibration gate. A missing receipt
  defers the work. A mismatched one refuses the answer. A query vector at
  search time needs its receipt too.
- **LocalAI stays outside the trust path.** It decides when an answer
  comes, never what it says. A runner that altered an input or a vector,
  or reached a backend around the wrapper, is caught by the writer.
- **Determinism is part of the run.** The wrapper states no run whose
  model environment lacks its vendor's bitwise-reproducible mode
  (`GGML_SYCL_ENABLE_DNN=0` on Intel, `ROCBLAS_DEFAULT_ATOMICS_MODE=0` on
  AMD; one request at a time). Reloading with another flag, upgrading the
  backend or updating the driver is a new run, and says so on its next
  receipt.

A collection's file names its encoder (`[encoder]`, `api = "vega"`) and
there is no default. The Library finds the model itself: it asks the
server's candidate models for their stated run and takes the one whose
weights are the Codex's. Specification: [03 §3.3](docs/03-checking.md),
[09 §9.12](docs/09-formats.md); code: `app/vega/`, `deploy/vega/`,
`deploy/localai/`.

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
          drawing from one shared bandwidth budget) -> records build
          (parse, slice, parts, snapshot) -> text index -> embed
          (off | on-demand | all local | all p2p) — rounds across all
          collections, per-document commits, SIGTERM-safe

retrieve  catalogue + per-language BM25 (the query translated into the
          main languages) + dense LanceDB, federated across collections
          -> reciprocal-rank fusion (never cross-collection score compares)
          -> pinned reference facts (facts-by-key, never ranked; nothing
             boosts; `--filter keys=<id>` keeps the records tagged with one)
          -> rerank -> answer with [n] citations and per-chunk provenance

share     parts and signed vector bundles (never text) as BitTorrent v2;
          import: signature -> space match -> possession binding (you
          must hold the same raw bytes) -> verification ladder ->
          all-or-nothing apply, verify-on-use at first retrieval

network   peers through the mainline DHT or a hand-given address; who
          embeds what is a function every peer computes (docs/04). An
          optional coordinator only keeps statistics and credit.
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

5,300+ tests, no live model needed anywhere in the suite. `louise node
start` runs the worker and the web interface (`--only worker|web` for one
container each); `louise node stop` finishes the current units and exits 0.

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

**Manually**: rsync the Gutenberg generated
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
  `CHAT_URL` (the chat model), `CHAT_MODEL`,
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

Installs use `deploy/truenas-app.yaml` or `deploy/docker/`; the root
`docker-compose.yaml` is not a node install.

`/data` must be a **local** filesystem (SQLite WAL + LanceDB do not tolerate
SMB/NFS); `/corpus` read-only over NFS is fine. Ask is open until the first user
exists — keep the app on LAN/Tailscale; don't tunnel it to the internet.

## Model endpoints

- **Chat**: `CHAT_URL` + `CHAT_MODEL` (OpenAI-compatible or Ollama-style).
- **Embeddings**: each collection's own encoder through VeGa (above) — the
  collection file's `[encoder]` names it, and there is no default one;
  `VEGA_URL` is where the receipts are read. Each encoder runs in
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
- A space's tolerance is calibrated against real backends by `louise hold
  embedder calibrate` (docs/03 §3.3); the committed fixture numbers under
  `tests/fixtures/calibration` are synthetic stand-ins for the tests.

## License

AGPL-3.0 — see [LICENSE](LICENSE).
