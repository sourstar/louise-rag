# Project: Louise — implementation rules

## RULE 1 — READ THIS BEFORE ANYTHING ELSE, NO MATTER WHAT

*(Operator, 2026-09-21. It stands until the operator changes this statement.)*

**This is a DEVELOPMENT system.** When the specifications, the code or the
artifacts need to change, take whatever shortcut minimises recomputing
time, as long as the final artifact is indistinguishable from one made by
the book. For example:
- reuse bytes that are provably identical;
- carry a space whose vectors already exist;
- resume a build instead of redoing it;
- re-create a collection nothing official was published from, instead of
  continuing it with a daughter (operator, 2026-10-24: "No official
  daughters so far, rule 1") — carrying what is identical.

Tell the operator about each such intervention, but it is the default
choice, not an exception to ask about. A spec rule that forces a
recomputation where an identical result can be kept is changed, not
obeyed. Name the amendment.

## RULE 2 — THE INTERFACES HIDE THE COMPLEXITY. EVERYTHING IS PLUG AND PLAY

*(Operator, 2026-10-20, verbatim: "Claude is too stupid to understand that
hiding complexity to the regular user is the main goal of the user
interfaces." It stands until the operator changes this statement.)*

A regular user chooses what only they can know — a local name — and
nothing else. Everything the system can determine, it determines: its
model server, the model names for a collection's encoder, what to take,
how to embed, which version. Where it cannot determine something, it does
not ask a form; it proceeds with what it has and says in plain words what
is missing and what would supply it. Choices for operators live in the
command line, not in the Library's dialogs. A dialog that asks for a
server address, a protocol, a model name or a Codex field is a defect.

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
- **The migration to the documented architecture is COMPLETE (2026-10-08).**
  Its plan was deleted, as it said it would be; the last version is
  `git show 7f84d44:docs/notes/migration-plan.md`. What it left open is
  in docs/notes/STATUS.md ("Open after the migration"). Changing code
  still means: wrap, do not rewrite; keep every pre-existing test green.
- **Touching WHAT RUNS WHEN — the round loop, a stage's selection or
  batching, archive passes, the pools, the maintainers — read
  docs/notes/scheduling.md FIRST and follow its checklist (§6).** It is
  the single reference: the model, the measured costs, and a catalogue
  of the pitfalls that have already cost six reworks. Measure with
  `louise dev schedule-report` before proposing anything; a scheduling
  change whose commit message has no numbers in it is a guess.
- **Deploying, configuring or measuring an ENCODER — on any card, any
  vendor — read invariant 12 and docs/notes/approved-encoders.md
  ("Bitwise reproducibility, by vendor") FIRST.** The vendor's
  deterministic mode was documented there in August and missed in
  October on a new AMD peer and on the node's own SYCL LocalAI.
- **MEASURING, BENCHMARKING OR TESTING ANYTHING — read
  docs/notes/measuring.md FIRST and use its checklist (§6).** Use the
  harness that already exists (`llama-intel-arc-b70`,
  `llama-amd-rx-9060-xt`, `tools/vega_repro_check.py`), measure the
  DEPLOYED flags and env, check that a doc's number was counted rather
  than inferred, and watch free space — measurement is a writer.
- Read the existing code in app/ for conventions before writing new modules.

## Invariants (violating any of these is a blocking bug)
Each names how it is enforced — *boot* (refuse to start), *test* (a test
fails), or *review* (a human must notice). The ones enforced by review alone
are the ones that have drifted; prefer to add a test when you touch them.

1. SPACES. Never mix embedding spaces. One collection = one space; a node
   runs several spaces side by side. `space_id` is computed as docs/02 §2.4
   states and docs/09 §9.3 fixes byte for byte: the encoder's WEIGHTS
   digest, the collection's chunker LAYERS with their ids,
   versions and parameters (one chunker per layer, every document in
   exactly one layer by a declared rule, the layer list fixed at creation;
   a collection with one layer is the common case and its layer IS its
   chunker) and each layer's INPUT form (text, or a tokenizer pair),
   `cleaning_version`, the space block's dim, dtype, ctx, pooling,
   normalisation, its query side (the query encoder's weights and its
   `instruction`), its `cut` where the space declares one (characters or
   tokens), and the epoch (09 §9.3's formula; a version-2 Codex still has
   its engine image inside). Not
   the ENGINE — the tested build, image, args, env and device are frozen
   in the Codex as `space.encoder.engine` (inside `collection_id`,
   outside `space_id`); any engine may contribute once its RUN passes
   the calibration gate, validated once, and a vector is written only from
   an answer whose run is a validated one — named by the answer's VeGa
   receipt for an `api = "vega"` encoder (bound to the input and vector
   digests), by its `Louise-Run` header otherwise — checked per answer,
   before the write (docs/03 §3.3, app/embeddings.py). Not the tolerance, not the calibration.
   A vector table whose
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
   never fused. Do not blend the types. Nothing boosts: an identifier the
   query names pins its facts and restricts nothing unless the reader asks
   for it as the `keys` filter (docs/05 §5.2, operator 2026-10-25). ONE
   EXCEPTION, by operator decision (2026-10-25: "The final goal is the
   quality of the information retrieval"): after the reranker, the record
   the catalogue leg ranks first takes position 1 and nothing else moves
   (docs/05 §5.6; titles MRR 0.782 against 0.720 fused by RRF and 0.578
   reranked alone). *Enforced: test + review.*
5. BANDWIDTH. No code path moves bytes without drawing from the central
   token-bucket budget (transfer manager). No per-tool rate limits as a
   substitute. *Enforced: test (tests/test_invariant_5_bandwidth.py: every
   app/ module doing network I/O draws from the budget or is on its named
   exemption list — model-server calls, each with its reason).*
6. STORAGE. /data is a local filesystem: SQLite WAL + LanceDB, no SMB/NFS
   assumptions. SQLite holds state (never travels); Lance holds vectors
   locally — a bundle travels as Parquet (docs/09 §9.7) and rests in the
   peer's LanceDB table (docs/09 §9.6; no per-part vectors file). Implicit rowids
   are load-bearing (the catalog index, `app/catalog.py`, is
   keyed on `documents.rowid`): nothing VACUUMs a manifest, and anything
   that renumbers rowids must rebuild it. DERIVED, re-creatable,
   random-access data — a plugin's pack, a label table — goes under
   `LOUISE_CACHE_DIR` (the fast drive) when set, keyed on the origin's
   physical identity (`app/derived.py`); the archive and the authoritative
   state never do (operator, 2026-10-20; docs/07 §7.8). *Enforced: test
   (tests/test_invariant_6_storage.py: no VACUUM in app/, tools/, deploy/;
   every write-open of a manifest sets WAL; only app/derived.py resolves
   LOUISE_CACHE_DIR, for named derived data) + review (SMB/NFS).*
7. GPU BOUNDARY. The app never imports torch/CUDA/SYCL. All model access is
   HTTP (CHAT_URL / TRANSLATE_URL / RERANK_URL, and each collection's own
   [encoder] — there is no default encoder). Embedding calls go through the
   existing client. *Enforced: test (tests/test_invariant_guards.py).*
8. SECRETS. Secrets — key material AND tokens — are referenced by filesystem
   path (`NODE_KEY_PATH`, `ADMIN_TOKEN_FILE`); never in logs, config values
   or error messages, and never in an env VALUE where a path is possible.
   *Enforced: test (tests/test_invariant_8_secrets.py: sentinel token and
   key files, real config loading in a fresh process, never in log, stderr,
   stdout or an HTTP answer over startup, bad tokens, an admitted request,
   bad key files and an unreadable token file; tests/test_invariant_guards.py)
   + review.*
9. LICENSE GATES. A yellow- or red-class source requires a recorded
   acknowledgement (timestamp + sha256 of terms text) before its data is
   USED — however the data arrived (docs/04 §4.4). Acquisition: before
   the first byte moves from the origin. Ingestion/exchange: a yellow- or
   red-class collection without an ack is not discovered, not
   imported, not exchanged — hand-populated folders and peer copies are
   not a bypass (observed live: a peer-copied part was indexed and
   imported ack-free). Terms text is byte-exact and never converted (a
   CRLF checkout would silently invalidate every recorded ack).
   SHARDs carry the origin's terms byte-exact (`LICENSE-TERMS`, docs/09
   §9.13), so one ack covers corpus, pre-embeddings and SHARD
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
    ONE EXCEPTION, by operator decision (2026-09-13): the node's MODE
    (`worker_state` — paused / acquire-only / seed-only / running, in
    `state/node.sqlite`, app/node_state.py) SURVIVES a restart until it is
    changed. No file sets it, so there is nothing to reset it to, and an
    expiry would turn acquire-only back into running — starting the
    encoder — on every deploy. A paused COLLECTION (`node pause
    <collection>`) survives a restart for the same reason
    (app/node_state.py). docs/07 §7.8 says the same. Do not "fix" it.
    *Enforced: test.*
11. CORPUS FIDELITY. A plugin's corpus may be PRUNED but never altered in
    a way that prevents verification against the original form, and
    **WE CANNOT END UP HOLDING LESS INFORMATION THAN THE ORIGIN GAVE US
    ABOUT AN ENTITY** — docs/01 promise 9, docs/06 §6.4, and 09 §9.5's
    `extra` row, non-negotiable for ANY collection. The only content that
    may be dropped is content that is not about the entity:
      (a) wrapping and markup — XML tags, wikitext syntax;
      (b) common headers and boilerplate — the Project Gutenberg licence
          header, a dump's page chrome, a provider's banner;
      (c) the date the entity entered OUR corpus (a date ABOUT the entity
          is entity information and stays);
      (d) what the PROVIDER wrapped around the entity and is not the
          entity — the file formats it offers, download counts, mirror
          bookkeeping.
    Everything else the origin asserts about the entity is KEPT: its ids,
    its dates, its authors and their identifiers, its subjects,
    categories and keywords, its classification, its publication
    details, its language, its structure. A §9.5 top-level key where
    there is a natural home, `extra.<name>` otherwise. "We do not use it
    yet" is NOT a reason to drop it — `extra`'s keys are fixed for a
    collection's life, so a field left out now costs a whole new
    collection to add later. If you cannot decide whether something is
    (d) or entity information, ASK; do not guess it away.
    Losing the ability to re-fetch the origin and reproduce what we kept
    is equally forbidden. EVERYTHING HELD IS VERIFIABLE (operator,
    2026-10-25): a superseded part version is kept where the origin keeps
    its history and dropped where it replaces in place, since it can no
    longer be checked against the source (01 promise 7, 06 §6.2
    `origin_versions`, app/hold/shard.py).
    NOTHING IS PRUNED UNLESS WRITTEN (operator, 2026-10-24): "If not
    written explicitly in the guidelines a collection cannot prune any
    information from the corpus, in other words I cannot get from the raw
    corpus more information than those I can get from the parsed corpus
    and possibly from queries." A pruning rule (wikidata's scholarly
    articles, D1) is written in the plugin's guidelines and its Codex form
    before it applies; cost is answered by choosing what to EMBED, never by
    leaving entities out of the records.
    EVERYTHING THAT BELONGS TO A COLLECTION IS PART OF IT (operator, the
    same day): "everything belonging a collection must be part of the
    collection; if structurally independent it can be an optional download
    and/or embedding. As already happens, for instance, with languages in
    some collections." A structurally independent part — a language of
    Wikipedia, a family of Wikidata, Wikidata's lexemes — is a SLICE a node
    may hold or not and embed or not; it is never a separate collection and
    never left out.
    Concretely: every record carries `origin_hash` — the hash of the origin
    unit it was parsed from — and `origin` locates that unit, while the
    Codex names the acquisition spec and parser version that produced it
    (docs/02 §2.3, docs/06 §6.4), so `records-vs-origin` can re-fetch a
    sample and reproduce it. A record with no `origin_hash` is REFUSED at
    build (`hold records build`; `pack build` is retired),
    never warned about: a SHARD whose records all verify
    internally while being permanently unverifiable against the source is
    the worst artefact we could ship, and it is published once and adopted
    by peers who cannot repair it.
    WHERE SEVERAL ORIGIN FORMS EXIST (epub vs plain text, XML baseline vs
    API, dump vs live), choose the one that best fits our goals and RECORD
    the choice with its reason beside the plugin — the form is part of what
    a verifier needs in order to reproduce our records. *Enforced: build +
    test (tests/pack/test_origin_fidelity.py); the form choice recorded by
    test (tests/test_invariant_11_origin_form.py: every registered plugin's
    source module carries "THE ORIGIN FORM, and why"), its content by
    review.*
12. ENCODER DETERMINISM. *(Operator, 2026-10-18: a fundamental pillar.)*
    The same input on the same card, the same build and the same flags
    gives the SAME BYTES, every time — not "within tolerance". Tolerance
    (03 §3.1) is for what no vendor can promise, agreement ACROSS
    hardware; it is never a cover for a card that does not reproduce its
    own output, because a vector its own encoder cannot reproduce cannot
    be checked by anyone. So every encoder run on a card uses its stack's
    bitwise-reproducibility mode, set in the MODEL's YAML `env` so the
    stated run carries it (docs/notes/approved-encoders.md, "Bitwise
    reproducibility, by vendor"):
      - AMD ROCm: `ROCBLAS_DEFAULT_ATOMICS_MODE=0` (rocBLAS reduces with
        atomics by default; llama.cpp's F16 models go through its gemm_ex);
      - Intel SYCL: `GGML_SYCL_ENABLE_DNN=0` (oneDNN chooses kernels by
        heuristic and drifts run to run; llama-intel-arc-b70);
      - NVIDIA CUDA: none to set — cuBLAS disallows atomics by default;
        a single stream (`parallel:1`) keeps it so;
      - Vulkan, llama.cpp CPU: no mode offered — measure, do not assume.
    And it is MEASURED, never assumed: before a new engine (card, build,
    flags, env) embeds for a collection, the same texts are embedded over
    several fresh loads and in several orders, and every vector must come
    back bit-identical (llama-intel-arc-b70 `capture.py` + a byte compare;
    the prompt cache and overlapping requests are what break it — `-np 1`).
    *Enforced: the VeGa wrapper states no card run whose model env lacks
    its vendor's mode (deploy/vega/vega_wrapper.py `DETERMINISM`,
    tests/vega/test_wrapper_logic.py); the gate fails a run whose sixteen
    fingerprint texts, embedded one at a time again in reverse order, come
    back different, and admits batched answers only when they equal the
    single-text bytes (app/hold/embedder.py, operator 2026-10-25,
    tests/hold/test_embedder.py); the fresh-load half by the measurement
    harness and review.* Open: torch (louise-python) and
    OpenVINO have their own controls (`torch.use_deterministic_algorithms`,
    CUBLAS_WORKSPACE_CONFIG) and are not yet checked by the wrapper.

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
- **Do not reinvent the wheel** (operator, 2026-10-19; restated
  2026-10-23). Before building a mechanism, use what already does it:
  an available solution PROVEN effective (the library's own call, a tool
  in use, the algorithm with a name), and preferably an existing STANDARD
  — and name it in the code and the commit. Within reason: a whole
  standard is not integrated for a single case. Then solve the case
  simply, and WRITE DOWN the standardized alternative (which standard,
  what adopting it would take, when it would pay) in the code's comment
  or the note beside it, so the choice can be revisited. Build our own
  only where nothing fits, and say why. Paid for: CPU weighting left
  to cgroups v2 `cpu.weight`, scheduling to DRR/AIMD/CFS
  (docs/notes/architecture-v2.md); bundles and attestations on the
  digest's existing messages, BitTorrent v2 and the mainline DHT
  (docs/notes/p2p-review-open-issues.md); a vector index refresh as
  Lance's own delta index, not a rebuild of ours.
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
- **A question to the operator has ONE shape** (operator, 2026-10-20,
  after "Confirm or change work.tokens, 1.7 billion a month" went out
  bare): (1) one line saying what the thing IS, in plain words, no
  field name alone; (2) the choices as a short table with their effects;
  (3) the recommendation, and what happens if the operator says nothing.
  A question that does not fit that shape is not asked. Never re-ask a
  decision already presented; decide and proceed.
- **Always give the pros AND the cons** (operator, 2026-10-24: "Always
  give a pros cons"): every proposal, option and recommendation carries
  both — in a choices table, a pros column and a cons column per row,
  the status quo included. A benefit stated without its cost, or a
  recommendation without the cons of what it keeps, is not given.
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
