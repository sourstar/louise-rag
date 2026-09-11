# Project: Louise — implementation rules

## Before any work
- Read docs/README.md and the numbered documents it lists (01–09), in
  order. They are the specification; the vocabulary they fix (docs/01,
  docs/07) is the vocabulary of the code, and docs/09 fixes every byte
  that is hashed, signed or moved. If a task conflicts with them, stop
  and ask — the documents win until changed.
- docs/notes/ is the engineering record: measurements, postmortems,
  the design history. Read docs/notes/STATUS.md for where things stand
  and what cost time, and the note a task names, before changing the
  code that note is about.
- **On the `louise` branch, read docs/notes/migration-plan.md before any
  step.** It fixes the order, the scope of each step, what existing code
  it wraps, and the tests that prove it done. Wrap, do not rewrite; add
  the new command beside the old one with a deprecation alias; keep every
  pre-existing test green.
- **Touching WHAT RUNS WHEN — the round loop, a stage's selection or
  batching, archive passes, the pools, the maintainers — read
  docs/notes/scheduling.md FIRST and follow its checklist (§6).** It is
  the single reference: the model, the measured costs, and a catalogue
  of the pitfalls that have already cost six reworks. Measure with
  `louise dev schedule-report` before proposing anything; a scheduling
  change whose commit message has no numbers in it is a guess.
- Read the existing code in app/ for conventions before writing new modules.

## Invariants (violating any of these is a blocking bug)
Each names how it is enforced — *boot* (refuse to start), *test* (a test
fails), or *review* (a human must notice). The ones enforced by review alone
are the ones that have drifted; prefer to add a test when you touch them.

1. SPACES. Never mix embedding spaces. One collection = one space; a node
   runs several spaces side by side. `space_id` is computed as docs/02 §2.4
   states and docs/09 §9.3 fixes byte for byte: the encoder (image AND
   weights digests), the collection's chunker LAYERS with their ids,
   versions and parameters (one chunker per layer, every document in
   exactly one layer by a declared rule, the layer list fixed at creation;
   a collection with one layer is the common case and its layer IS its
   chunker), `cleaning_version`, the space block's dim, dtype, ctx,
   pooling, normalisation and query encoder, and the epoch. Not the
   tolerance, not the calibration, not the device. A vector table whose
   dim/space mismatches the Codex is a
   REFUSE-TO-START error FOR THAT COLLECTION, never a warning or
   auto-migration: the node keeps running its others, that one is not
   served, shared or searched, and `hold collection show` names the
   reason (docs/02 §2.4, docs/07 §7.8). *Enforced: per-collection refusal
   at start; layers by test (app/layers.py, tests/test_layers.py: one layer =
   the pre-layers space id byte for byte; every stage cuts a document
   through `layers.chunks_for_document`, never a plugin's chunker directly).*
2. IDEMPOTENCE. Every write is delete-then-insert keyed on uid / chunk id.
   Every stage is resumable and safe to re-run. A stage marks done only
   AFTER its durable commit, inside the same transaction — marking first is
   the one ordering that loses data. SIGTERM or SIGINT = finish the
   current unit, commit, exit 130 (docs/07 §7.7); `louise node stop`
   finishes its units and exits 0. The unit is a document where that is
   possible and smaller where it is not — acquisition checkpoints mid-file
   at byte granularity, bundle import stops between bundles; docs/07 §7.7
   fixes the unit per stage — lossless and truthful, always, and bounded
   in time. *Enforced: test + review.*
3. DETERMINISM. Chunking is a pure function of (cleaned text, chunker id,
   chunker version). Any change to chunking or cleaning bumps the version
   of the thing that changed, explicitly — never silently, and never via a
   global knob that re-pins collections whose chunker never changed.
   *Enforced: test (golden) + boot (space pin).*
4. FUSION TYPES. ReferenceSource lookups are facts-by-key: they go into the
   LLM context as pinned blocks and NEVER enter RRF. Ranked lists from corpus
   legs ALWAYS go through RRF — when they answer the same question. Two
   ranked lists that answer different questions (docs/05 §5.1; homology vs
   function in the genome collection is the case) are presented separately,
   never fused. Do not blend the types. *Enforced: test + review.*
5. BANDWIDTH. No code path moves bytes without drawing from the central
   token-bucket budget (transfer manager). No per-tool rate limits as a
   substitute. *Enforced: review.*
6. STORAGE. /data is a local filesystem: SQLite WAL + LanceDB, no SMB/NFS
   assumptions. SQLite holds state (never travels); Lance holds vectors
   locally — a bundle travels as Parquet (docs/09 §9.7) and a pack's
   vectors member rests as safetensors (docs/09 §9.6). Implicit rowids
   are load-bearing (`catalog_fts` is
   keyed on `documents.rowid`): nothing VACUUMs a manifest, and anything
   that renumbers rowids must realign. *Enforced: review.*
7. GPU BOUNDARY. The app never imports torch/CUDA/SYCL. All model access is
   HTTP (CHAT_URL / EMBED_URL / RERANK_URL). Embedding calls go through the
   existing client. *Enforced: test (tests/test_invariant_guards.py).*
8. SECRETS. Secrets — key material AND tokens — are referenced by filesystem
   path (`NODE_KEY_PATH`, `ADMIN_TOKEN_FILE`); never in logs, config values
   or error messages, and never in an env VALUE where a path is possible.
   *Enforced: test + review.*
9. LICENSE GATES. A yellow- or red-class source requires a recorded
   acknowledgement (timestamp + sha256 of terms text) before its data is
   USED — however the data arrived (docs/04 §4.4). Acquisition: before
   the first byte moves from the origin. Ingestion/exchange: a yellow- or
   red-class collection without an ack is not discovered, not
   imported, not exchanged — hand-populated folders and peer copies are
   not a bypass (observed live: a peer-copied shard was indexed and
   imported ack-free). Terms text is byte-exact and never converted (a
   CRLF checkout would silently invalidate every recorded ack).
   Packs carry the origin's terms byte-exact (`LICENSE-TERMS`, docs/09
   §9.13), so one ack covers corpus, pre-embeddings and pack
   (`app/licensing.py` is THE resolution — never re-implement the check).
   *Enforced: acquisition by
   test; ingestion by test (the worker withholds scan and archive, `pack
   build` and the create dialog refuse — tests/test_license_gate.py).*
10. CONFIG TIERS. Env = identity; TOML files = desired state; DB settings =
    runtime overrides that do NOT survive restart. Never write desired state
    into the DB. Concretely: every Tier-3 override either expires or is
    reset when the worker applies the files (boot, add, TOML edit), and the
    effective value shown anywhere comes from the same resolution function
    the worker uses (`collections.recipe_for`, `db.apply_desired_embed_mode`).
    *Enforced: test.*
11. CORPUS FIDELITY. A plugin's corpus may be PRUNED but never altered in
    a way that prevents verification against the original form. Dropping
    what we do not use (epubs, XML markup, dump indexes) is fine; losing
    the ability to re-fetch the origin and reproduce what we kept is not.
    Concretely: every record carries `origin_hash` — the hash of the origin
    unit it was parsed from — and `origin` locates that unit, while the
    Codex names the acquisition spec and parser version that produced it
    (docs/02 §2.3, docs/06 §6.4), so `records-vs-origin` can re-fetch a
    sample and reproduce it. A record with no `origin_hash` is REFUSED at
    build (`hold records build`; `pack build` until the migration lands),
    never warned about: a pack whose records all verify
    internally while being permanently unverifiable against the source is
    the worst artefact we could ship, and it is published once and adopted
    by peers who cannot repair it.
    WHERE SEVERAL ORIGIN FORMS EXIST (epub vs plain text, XML baseline vs
    API, dump vs live), choose the one that best fits our goals and RECORD
    the choice with its reason beside the plugin — the form is part of what
    a verifier needs in order to reproduce our records. *Enforced: build +
    test (tests/pack/test_origin_fidelity.py); the form choice by review.*

## Engineering rules (blocking in review; each one was paid for)
- **"Fix it after" is NEVER an option** (docs/01, promise 8). A known
  defect is fixed before anything else proceeds — before the feature it
  blocks, before the run already queued, before the deploy that is
  otherwise ready. Nothing published is deleted, so a wrong artefact is
  never recalled, only superseded, and every peer that adopted it
  meanwhile keeps it. The dangerous artefact is not the one that
  crashes; it is the one that passes every check and is wrong, whose
  halves are each internally consistent and honestly computed.
  **Offering the choice is itself the error**: do not present "proceed
  now, repair afterwards" as an option to the operator, and do not take
  it alone. Stop, fix, verify, then continue.
- A per-round cost scales with the work available, never with the size of
  a list or the corpus. If a per-item cost depends on how much has been
  processed so far, it is a bug however fast it looks at fixture scale.
- "Did work" is not "made progress": a stage that can succeed at doing
  nothing must not win the round from the stages behind it.
- Selection queries are plan-checked in tests — no `TEMP B-TREE`, no
  `LIST SUBQUERY`. A merely slow query passes every behavioural test.
  And a plan check is not enough: `SEARCH ... USING INDEX
  idx_documents_scope (in_scope=?)` reads as indexed while SQLite filters
  the rest of the corpus. TIME every new hot-path query against a real
  manifest (docs/notes/architecture-v2.md amendment 49 found three costing
  16–128 s PER ROUND that way);
  if no index can answer it, walk it in windows from a cursor and stop.
- Deferral is not failure: an unreachable endpoint or a locked manifest
  never writes an error to a document. An error removes the document from
  every selection until a human intervenes.
- A pipeline thread NEVER waits without a bound on an optional subsystem.
  The parse pool hung with its children gone and pubmed stopped dead for
  hours (docs/notes/architecture-v2.md amendment 51): every such wait takes
  a timeout and falls back to
  doing the work in-process, because a worker that cannot finish also
  cannot honour SIGTERM.
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
- **Answer at one fifth the length that feels right.** MANDATORY, and it
  binds explanations hardest — the finding, not the retelling. State the
  outcome, anything that needs a decision, and stop. No recap of work the
  user just watched, no restating a point in a second register, no
  summary of a summary. Length that is genuinely owed goes in
  docs/notes/ with a link, never in the reply.
- Python 3.12, full type hints, dataclasses/Protocols as in the doc, pytest.
- Small modules; follow naming and layout of existing app/ code.
- Every task ships tests. New behavior without a test is unfinished.
- New dependencies, schema changes beyond the doc, or wire-format changes: ask first.

## Thinking guidance
- Think longer on: transaction boundaries, concurrency, crypto, statistics, migration.
- Don't overthink: templates, CRUD, config plumbing — implement and test.
