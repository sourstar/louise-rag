# Project: P2P RAG Platform — implementation rules

## Before any work
- Read docs/architecture-v2.md for the sections named in the task prompt. If the task
  conflicts with the doc, stop and ask — the doc wins until amended.
- Read the existing code in app/ for conventions before writing new modules.

## Invariants (violating any of these is a blocking bug)
1. SPACES. Never mix embedding spaces. `space_id` pins model-file hash, dim, dtype,
   normalization, pooling, CHUNK_VERSION, cleaning version, truncation policy, and
   query-encoder hash. A vector table whose dim/space mismatches config is a
   REFUSE-TO-BOOT error, never a warning or auto-migration.
2. IDEMPOTENCE. Every write is delete-then-insert keyed on uid / chunk id. Every stage
   is resumable and safe to re-run. SIGTERM = finish current document, commit, exit 0.
3. DETERMINISM. Chunking is a pure function of cleaned text. Any change to chunking or
   cleaning bumps CHUNK_VERSION / cleaning version explicitly. Never silently.
4. FUSION TYPES. ReferenceSource lookups are facts-by-key: they go into the LLM context
   as pinned blocks and NEVER enter RRF. Ranked lists from corpus legs ALWAYS go
   through RRF. Do not blend the two.
5. BANDWIDTH. No code path moves bytes without drawing from the central token-bucket
   budget (transfer manager). No per-tool rate limits as a substitute.
6. STORAGE. /data is a local filesystem: SQLite WAL + LanceDB, no SMB/NFS assumptions.
   SQLite holds state (never travels); Lance holds vectors (travels as Parquet).
7. GPU BOUNDARY. The app never imports torch/CUDA/SYCL. All model access is HTTP
   (CHAT_URL / EMBED_URL / RERANK_URL). Embedding calls go through the existing client.
8. SECRETS. Key material by filesystem path only; never in env values, config values,
   logs, or error messages.
9. LICENSE GATES. Acquisition of yellow-class sources requires a recorded
   acknowledgement (timestamp + sha256 of terms text) before the first byte moves.
10. CONFIG TIERS. Env = identity; TOML files = desired state; DB settings = runtime
    overrides that do NOT survive restart. Never write desired state into the DB.

## Style
- Python 3.12, full type hints, dataclasses/Protocols as in the doc, pytest.
- Small modules; follow naming and layout of existing app/ code.
- Every task ships tests. New behavior without a test is unfinished.
- New dependencies, schema changes beyond the doc, or wire-format changes: ask first.

## Thinking guidance
- Think longer on: transaction boundaries, concurrency, crypto, statistics, migration.
- Don't overthink: templates, CRUD, config plumbing — implement and test.
