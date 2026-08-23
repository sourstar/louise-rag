# Project: P2P RAG Platform — implementation rules

## Before any work
- Read docs/STATUS.md first (where things stand, what cost time), then
  docs/architecture-v2.md for the sections named in the task prompt — §17's
  amendments are where the design was corrected in flight. If the task
  conflicts with the doc, stop and ask — the doc wins until amended.
- Read the existing code in app/ for conventions before writing new modules.

## Invariants (violating any of these is a blocking bug)
Each names how it is enforced — *boot* (refuse to start), *test* (a test
fails), or *review* (a human must notice). The ones enforced by review alone
are the ones that have drifted; prefer to add a test when you touch them.

1. SPACES. Never mix embedding spaces. One collection = one space; a node
   runs several spaces side by side. `space_id` is computed by §3 as amended
   (it names the encoder function AND the chunker — not `dim`, not chunk
   geometry, not the device; compensating checks cover those at import).
   A vector table whose dim/space mismatches config is a REFUSE-TO-BOOT
   error, never a warning or auto-migration. *Enforced: boot.*
2. IDEMPOTENCE. Every write is delete-then-insert keyed on uid / chunk id.
   Every stage is resumable and safe to re-run. A stage marks done only
   AFTER its durable commit, inside the same transaction — marking first is
   the one ordering that loses data. SIGTERM = finish the current unit,
   commit, exit 0; the unit is a document where that is possible, smaller
   where it is not (acquisition checkpoints mid-file at byte granularity,
   bundle import stops between documents) — lossless and truthful, always,
   and bounded in time. *Enforced: test + review.*
3. DETERMINISM. Chunking is a pure function of (cleaned text, chunker id,
   chunker version). Any change to chunking or cleaning bumps the version
   of the thing that changed, explicitly — never silently, and never via a
   global knob that re-pins collections whose chunker never changed.
   *Enforced: test (golden) + boot (space pin).*
4. FUSION TYPES. ReferenceSource lookups are facts-by-key: they go into the
   LLM context as pinned blocks and NEVER enter RRF. Ranked lists from corpus
   legs ALWAYS go through RRF — when they answer the same question. Two
   ranked lists that answer different questions (pending amendment from the
   DNA search-axes work: homology vs function) are presented separately,
   never fused. Do not blend the types. *Enforced: test + review.*
5. BANDWIDTH. No code path moves bytes without drawing from the central
   token-bucket budget (transfer manager). No per-tool rate limits as a
   substitute. *Enforced: review.*
6. STORAGE. /data is a local filesystem: SQLite WAL + LanceDB, no SMB/NFS
   assumptions. SQLite holds state (never travels); Lance holds vectors
   (travels as Parquet). Implicit rowids are load-bearing (`catalog_fts` is
   keyed on `documents.rowid`): nothing VACUUMs a manifest, and anything
   that renumbers rowids must realign. *Enforced: review.*
7. GPU BOUNDARY. The app never imports torch/CUDA/SYCL. All model access is
   HTTP (CHAT_URL / EMBED_URL / RERANK_URL). Embedding calls go through the
   existing client. *Enforced: test (tests/test_invariant_guards.py).*
8. SECRETS. Secrets — key material AND tokens — are referenced by filesystem
   path (`NODE_KEY_PATH`, `ADMIN_TOKEN_FILE`); never in logs, config values
   or error messages, and never in an env VALUE where a path is possible.
   *Enforced: test + review.*
9. LICENSE GATES. Acquisition of yellow-class sources requires a recorded
   acknowledgement (timestamp + sha256 of terms text) before the first byte
   moves. Terms text is byte-exact and never converted (a CRLF checkout
   would silently invalidate every recorded ack). *Enforced: test.*
10. CONFIG TIERS. Env = identity; TOML files = desired state; DB settings =
    runtime overrides that do NOT survive restart. Never write desired state
    into the DB. Concretely: every Tier-3 override either expires or is
    reset when the worker applies the files (boot, add, TOML edit), and the
    effective value shown anywhere comes from the same resolution function
    the worker uses (`collections.recipe_for`, `db.apply_desired_embed_mode`).
    *Enforced: test.*

## Engineering rules (blocking in review; each one was paid for)
- A per-round cost scales with the work available, never with the size of
  a list or the corpus. If a per-item cost depends on how much has been
  processed so far, it is a bug however fast it looks at fixture scale.
- "Did work" is not "made progress": a stage that can succeed at doing
  nothing must not win the round from the stages behind it.
- Selection queries are plan-checked in tests — no `TEMP B-TREE`, no
  `LIST SUBQUERY`. A merely slow query passes every behavioural test.
- Deferral is not failure: an unreachable endpoint or a locked manifest
  never writes an error to a document. An error removes the document from
  every selection until a human intervenes.
- Caches, cursors and bookkeeping key on physical identity (a path), never
  on a name. Two objects can share a name and point at different files.
- Telemetry is published by someone other than the thread doing the work.
- A writer that reads first takes `BEGIN IMMEDIATE`. Under WAL a deferred
  transaction that has read fails its upgrade to write INSTANTLY if anyone
  committed meanwhile — the busy handler never runs. Long non-DB work
  (an encoder call, a fetch) stays outside the transaction entirely.
- Every log line names its collection (db.ManifestConnection does it).
- Measure before optimising; the worker log read as a time series is the
  first benchmark. Read it before reading the code.

## Style
- Python 3.12, full type hints, dataclasses/Protocols as in the doc, pytest.
- Small modules; follow naming and layout of existing app/ code.
- Every task ships tests. New behavior without a test is unfinished.
- New dependencies, schema changes beyond the doc, or wire-format changes: ask first.

## Thinking guidance
- Think longer on: transaction boundaries, concurrency, crypto, statistics, migration.
- Don't overthink: templates, CRUD, config plumbing — implement and test.
