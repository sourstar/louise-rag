# Gutenberg RAG — TrueNAS app

One app, two containers, one shared SQLite manifest: a **web** process serving the control dashboard, the stats, and an OpenAI-compatible RAG endpoint, and a **worker** process that ingests the gutenberg-epub corpus resumably. The container never touches the GPU — all embedding and generation goes through your existing Ollama, so VRAM arbitration stays exactly where it already is.

## Architecture

```
/corpus (ro)  = your gutenberg-epub rsync target, one dir per book id
/data  (ssd)  = manifest.db (SQLite WAL) + fts/ (Tantivy, one index per
                language group, snowball stemmers) + lancedb/ (vectors)

worker loop:  discover -> rdf -> fts -> embed(lazy|all|off), one book per
              iteration, per-book commits, SIGTERM-safe, control read each turn
retrieval:    catalog FTS5 (English LoC subjects = free cross-lingual)
              + per-language BM25 with LLM query fan-out translation
              + dense (LanceDB) over whatever is embedded
              -> reciprocal-rank fusion -> optional rerank -> answer w/ [n] cites
```

## Getting the corpus (gutenberg-epub)

The app reads the **generated collection** of Project Gutenberg (one directory
per book id containing `pg{id}.txt.utf8`, `pg{id}.rdf`, epubs, covers). Sync it
with rsync if you don't have it yet — the EU mirror is fastest from Germany:

```
# check total size/file count first (dry run, transfers nothing)
rsync -an --stats rsync.mirrorservice.org::gutenberg.org/cache/epub/ /mnt/tank/gutenberg/cache/epub/

# actual sync — skip Kindle builds (~half the volume) and mbt files
rsync -avHS --timeout 600 --delete \
  --exclude '*/mbt-*' --exclude '*.mobi' --exclude '*.kf8*' \
  aleph.gutenberg.org::gutenberg-epub /mnt/tank/gutenberg/cache/epub/
```

If the sync happens elsewhere, that's fine — just mount the resulting folder
as `/corpus` (read-only). **New or changed books are picked up automatically:**
the worker rescans the corpus every time it starts, and every
`DISCOVER_INTERVAL_MIN` minutes (default 360) while running. Restarting the app
after adding books is therefore enough; the **rescan corpus** button and
`POST /api/discover` remain available for an immediate trigger.

Note the target directory holds ~79k subdirectories in one flat root: avoid
pointing anything at it that auto-enumerates (SMB browsing, media scanners).

## Deploy on TrueNAS SCALE

1. Datasets: put the state dir on SSD, e.g. `ssdpool/ssd5/gutenberg-rag`.
2. Build the image on the NAS (shell):
   ```
   cd /mnt/ssdpool/ssd5/build/gutenberg-rag   # this repo
   docker build -t gutenberg-rag:local .
   ```
3. Apps → Discover → Custom App → Install via YAML → paste `docker-compose.yaml`.
   Adjust the two volume host paths and `OLLAMA_URL` (use the NAS LAN IP, not
   localhost — the container has its own network namespace).
4. Open `http://mynass-de:8742`.

Upgrades: rebuild the image with the same tag, restart the app. `pull_policy: never` keeps TrueNAS from ever trying to pull it.

**Do not expose this via the Cloudflare Tunnel without auth in front — there is none built in.** LAN/Tailscale only.

## First run — test on a small slice (recommended)

1. Dashboard → Scope → either press **Test preset: 30 books/lang**, or set e.g.
   `languages: it, la` + `limit: 200`, embed mode `all`. Apply.
2. **rescan corpus**, then **start** the worker.
3. Watch the shelves fill; use the Playground to search, inspect hits (click a
   title → metadata, reindex, generate summary), and check per-stage timings.
4. Happy? Clear the scope (empty languages, limit 0, embed mode `lazy`) and
   Apply. Nothing already built is discarded — the worker simply continues on
   the rest of the corpus.

Scope is non-destructive by design: it only marks which books the worker may
touch. Narrow it to reproduce a bug on a handful of ids (`book_ids: 1342`),
reset a single stage, and re-run.

## Resume / re-run semantics

- Every stage writes delete-then-insert keyed on `book_id`, so any book can be
  re-processed at any time without duplicates.
- `rsync` more books → **rescan corpus** (or cron `curl -X POST
  http://mynass-de:8742/api/discover` after your rsync) → worker picks up the
  delta. Changed files are detected by mtime/size, confirmed by blake2 hash,
  and only genuinely changed books get their flags reset.
- Kill/restart anytime: state is per-book in SQLite WAL. `stop_grace_period`
  gives a mid-book embed time to commit.
- Changed chunking or embedder? Bump `CHUNK_VERSION` / `FTS_VERSION` /
  `EMBED_VERSION` in the app env; the worker redoes exactly the stale stage on
  every book, nothing else.
- Embed modes: `off` (lexical+catalog only), `lazy` (books get embedded when
  queries surface them — the index grows around real usage), `all` (backfill,
  non-English first since that's the rarest knowledge).

## Hook into Open WebUI

Admin → Settings → Connections → add an OpenAI API connection:
`http://mynass-de:8742/v1`, any API key string. A model named
`gutenberg-rag` appears; every chat with it does full hybrid retrieval and
answers with inline `[n]` citations. The `/api/search` endpoint returns raw
hits + timings if you'd rather build a Filter/Tool around it.

## Optional reranker

Run a TEI container (e.g. `ghcr.io/huggingface/text-embeddings-inference` with
`BAAI/bge-reranker-v2-m3`, CPU is fine) and set `RERANK_URL` to its `/rerank`.
Unset = reranking skipped gracefully.

## Embedding throughput (dedicated instance)

For corpus-scale embedding, run a second Ollama instance dedicated to the
embedder (own port, own model dataset) so parallel slots never interfere with
your chat stack:

```
OLLAMA_NUM_PARALLEL: "16"      # slots = concurrent sequences per batch
OLLAMA_CONTEXT_LENGTH: "4096"
OLLAMA_MAX_LOADED_MODELS: "1"
OLLAMA_KEEP_ALIVE: "-1"
OLLAMA_FLASH_ATTENTION: "1"
```

Then point the app at it: `EMBED_URL: http://<nas>:11435`. The app keeps
`EMBED_CONCURRENCY` requests of `EMBED_BATCH` inputs in flight; keep
BATCH x CONCURRENCY at or above the instance's NUM_PARALLEL. Changing the
embedding model (different dimensions) or `EMBED_DTYPE` requires deleting
`/data/lancedb` and bumping `EMBED_VERSION`.

## Known limits of this skeleton

- Dense search over millions of vectors will eventually want an ANN index
  (`table.create_index()` in LanceDB) — irrelevant below ~100k vectors.
- The chapter splitter is heuristic; books that defeat it fall back to fixed
  sections (bounded, never broken).
- `catalog_fts` uses the default unicode61 tokenizer — fine for LoC subjects.
- One worker process. Plenty: the bottleneck is Ollama, not the loop.
