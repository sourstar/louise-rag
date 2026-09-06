# Implementation Prompt Pack

Companion to `p2p-rag-architecture-v2.md`. Repo assumptions: the existing `gutenberg-rag`
tree at the root, the architecture doc at `docs/architecture-v2.md`, this file's CLAUDE.md
section saved as `CLAUDE.md` at the repo root.

**How to use**
- One prompt = one Claude Code session = one PR. Don't chain tasks in a session.
- Each prompt header states **Model · Effort · Mode**. Start Opus/Fable tasks (and P0.1)
  in plan mode; approve the plan before any edit.
- Run order: P0.1 → P0.2 → P0.3 → P0.4 → P0.5 → P0.6 → P0.7 → P1.1 → P1.2 → P1.3 → F1 → F2.
  P0.2–P0.6 can interleave once P0.1 is merged.
- After any Sonnet task that touches core semantics, optionally run the **Advisor prompt**
  (bottom) against its plan or diff with Fable 5 before merging.
- Global invariants live in CLAUDE.md so the prompts stay short. Task prompts only add
  task-specific constraints.

---

## CLAUDE.md

The canonical text lives at the repo root: [`CLAUDE.md`](../../CLAUDE.md). It used
to be copied here verbatim; two copies of a rules file drift (the invariants
were amended on 2026-08-23 — enforcement tags, engineering rules, corrected
wording for 1/3/8/9/10), so this section is now a pointer. Task prompts
below assume the root file is loaded, as it always is in a Claude Code session.

---


## P0.1 — Extract the plugin SPI and collections
**Model: Sonnet 5 · Effort: high · Mode: plan-first**

```
Read CLAUDE.md, then docs/architecture-v2.md §1–3 and §16.1.

Goal: refactor the single-source app into the plugin/collection architecture with zero
behavior change for the existing Gutenberg pipeline.

Deliverables:
1. app/plugins/base.py — CorpusSource Protocol + CanonicalDoc exactly as in §2.
2. app/plugins/gutenberg/ — current discovery.py, pg_text.py, rdf.py moved behind
   CorpusSource. Pure mechanical move; no logic changes.
3. app/collections.py — Collection = (plugin, snapshot, versions, space); per-collection
   paths for manifest.db, fts/, lancedb/. Config: collections/*.toml (§14 Tier 2).
4. Manifest migration: books → documents(uid, source, native_id, …existing flags…),
   uid = f"{source}:{native_id}". Write the migration as an idempotent script with a
   dry-run flag.
5. space_id computation (§3) + boot validation: on startup, verify every collection's
   vector table dim and pinned space; mismatch = refuse to boot with a clear message.

Acceptance:
- Golden test: on the fixture corpus under tests/fixtures/gutenberg-mini (create it:
  3 small public-domain books), the refactored pipeline produces byte-identical chunk
  ids, FTS hits, and manifest state vs. current main. This test is the contract.
- All existing tests pass unmodified or with mechanical import fixes only.

Non-goals: PubMed, entities, network, UI. Do not rename user-facing env vars.
Stop and ask if: the golden test cannot be byte-identical for a reason you can name.
```

## P0.2 — PubMed plugin
**Model: Sonnet 5 · Effort: high**

```
Read CLAUDE.md, then docs/architecture-v2.md §2, §5.1, §8 (PubMed rows).

Goal: second CorpusSource implementation, proving the SPI on container files.

Deliverables:
1. app/plugins/pubmed/: discover() over baseline + updatefiles listings (container
   files → per-record extraction; DocRef supports container membership); fetch() with
   .md5 sidecar verification; parse() MEDLINE XML → CanonicalDoc (title+abstract
   sections, MeSH → subjects, PMID native_id, journal/date → extra).
2. Chunking profile "abstract": one chunk per record.
3. Delta handling: updatefiles are the same code path as baseline; DeleteCitation
   entries mark documents deleted (delete-then-insert semantics; the manifest keeps a
   tombstone).
4. Fixture: tests/fixtures/pubmed-mini with 3 hand-crafted XML files (~20 records
   total) covering: normal record, revised record (same PMID twice across files),
   DeleteCitation, missing abstract, non-ASCII text.

Acceptance: ingest fixture → correct document count, revision overwrites, tombstone
honored, re-ingest is a no-op (idempotence test).
Non-goals: real downloads (mock endpoints), embeddings, MedCPT import.
```

## P0.3 — ReferenceSource, EntityResolver, doc_keys
**Model: Sonnet 5 · Effort: high · Mode: plan-first**

```
Read CLAUDE.md (invariant 4 especially), then docs/architecture-v2.md §2 and §4.

Goal: the entity/reference layer with graceful degeneration.

Deliverables:
1. app/plugins/base.py: ReferenceSource + EntityResolver Protocols (§2).
2. app/refs/: SQLite-backed reference store; RefSchema declares tables, keys, and
   typed links (e.g. variant→gene, gene→region). sync() idempotent by primary key
   with ref_version.
3. Two ReferenceSources: hgnc (symbols↔ids↔coordinates, small TSV) and clinvar-mini
   (subset schema: variant id, gene, significance). Fixtures included.
4. EntityResolver: regex + dictionary pass producing species-qualified EntityIds
   (§4 formats). expand() traverses declared links only.
5. doc_keys(uid, entity_id) table + population hook in parse(); Lance prefilter and
   Tantivy field filter wired to entity constraints.
6. search() gains resolve → expand → plan (§4): lookups → pinned fact blocks;
   corpus legs → RRF, with entity filters/boosts.

Acceptance:
- Degeneration test: for 20 queries with no resolvable entities, results are
  IDENTICAL (ids and order) to pre-change search. This is the contract.
- Entity test: "BRCA2" query returns pinned HGNC/ClinVar facts above ranked results,
  and ranked results are filtered/boosted by the entity.
- Type test: assert no ReferenceSource output ever appears inside the RRF input.
Non-goals: LLM fallback extraction (leave a clearly named hook), gnomAD scale.
```

## P0.4 — Acquisition layer
**Model: Sonnet 5 · Effort: high**

```
Read CLAUDE.md (invariants 5, 9), then docs/architecture-v2.md §5.1.

Goal: declarative acquisition with profiles, as a first-class manifest stage.

Deliverables:
1. app/acquire/: AcquisitionSpec dataclass (§5.1) consumed by a generic engine;
   transports rsync (subprocess), https/ftp (httpx, Range resume), hf_hub. Torrent =
   stub interface only (Phase 2).
2. fetch as manifest stage (fetch_done, fetch_version); delta sync = same code path,
   driven by the existing mtime/size→hash gate.
3. Profiles (demo/starter/recent/full) as filters; per-source specs for gutenberg and
   pubmed with demo profiles that complete in <2 min against mocked endpoints.
4. Checksum verification: origin sidecar when declared, blake2 always.
5. Guards: disk preflight vs estimated_bytes (refuse, don't half-fill); licence-ack
   gate writing timestamp+sha256 into the manifest; --dry-run printing files, bytes,
   ETA, licence state.
6. CLI: app corpus add|status|grow (§14 verbs), each with --dry-run.

Acceptance: kill -TERM mid-acquisition → restart resumes without re-downloading
completed files (test with a mocked slow endpoint). Dry-run output matches the doc's
UX sketch fields.
Non-goals: bandwidth scheduling (P0.7 will wrap these workers), real origin traffic
in tests.
```

## P0.5 — Query federation
**Model: Sonnet 5 · Effort: high**

```
Read CLAUDE.md, then docs/architecture-v2.md §3 (topology) and §4 (plan).

Goal: multi-collection search with unchanged per-collection behavior.

Deliverables:
1. Federation loop: parallel per-collection hybrid search → RRF across collections;
   collections param on /api/search and the OpenAI-compatible endpoint (default: all
   enabled).
2. Source-qualified citations: [gutenberg:1342 §12], [pubmed:38112345].
3. Per-collection candidate budgets (config) ahead of rerank; rerank stays optional
   and unchanged.
4. Lazy-embed enqueue becomes per-collection.

Acceptance: with one collection enabled, results identical to single-collection
baseline. With two, an integration test proves interleaving by rank (construct a
fixture where the same query hits both).
Non-goals: cross-space anything; scores never compared across collections.
```

## P0.6 — Web interface (four areas)
**Model: Sonnet 5 · Effort: medium**

```
Read CLAUDE.md (invariant 10), then docs/architecture-v2.md §15, and skim §14.

Goal: Ask / Library / Network / System as server-rendered pages over manifest tables.
FastAPI + Jinja + htmx + SSE. No SPA, no build step, no client state store.

Deliverables:
1. Ask: answer pane with [n] citations; provenance drawer per citation (chunk text,
   uid, collection, producer, verification badge — badge values stubbed as
   'local ✓' until P1.1); entity strip; pinned fact cards visually distinct from
   ranked results; collection scope chips.
2. Library: shelf card per collection (4 stage bars incl. fetched; profile + space
   badges; delta freshness line; lazy-queue depth); actions = Tier-3 verbs with
   dry-run preview modals; add-collection wizard per §15.
3. Network: placeholder panels bound to real (mostly empty) tables: bandwidth graph
   (SSE from transfer manager once P0.7 lands — render zeros now), bundles ledger,
   seeding table.
4. System: effective-config view labelling file/env/override per key; worker log
   tail; endpoint health pings; boot-validation report.
5. Every action modal shows its CLI equivalent string.
6. Auth: librarian areas behind a single admin token; Ask is LAN-open read-only.

Acceptance: pages render from fixture manifests with no JS errors; htmx actions call
the same handlers the CLI uses (assert shared code path in a test).
Non-goals: styling polish, websockets, user accounts.
```

## P0.7 — Transfer manager (bandwidth + parallelism)
**Model: Opus 5 · Effort: xhigh · Mode: plan-first**

```
Read CLAUDE.md (invariant 5), then docs/architecture-v2.md §5.2 in full.

Goal: the shared-budget scheduler every byte-mover draws from.

Deliverables:
1. app/transfer/: token-bucket pair (up/down) with priority classes
   interactive > delta > backfill > seeding; preemptive in the sense that higher
   classes starve lower ones under contention; seeding consumes leftover only.
2. Schedule windows (cron-like) composing with per-endpoint etiquette windows;
   effective limit = min(schedule, etiquette, global).
3. Worker pool for acquisition: per-endpoint concurrency = min(global, etiquette
   max_conns); per-endpoint EWMA throughput steering file→mirror assignment.
4. Wrappers so rsync/httpx workers cannot bypass the bucket (chunked reads gated on
   token acquisition; rsync via --bwlimit recomputed by the scheduler, documented as
   approximate).
5. Config surface exactly as §5.2; SSE endpoint emitting per-class rates for the UI.
6. Pipeline overlap: download ∥ checksum ∥ decompress ∥ parse via bounded queues.

Constraints: async where the codebase is async; no busy-waiting; clock injected for
tests.
Acceptance (all with fake clock + in-memory endpoints):
- Under saturation, class shares converge to policy within 2s simulated.
- Interactive burst preempts backfill within 200ms simulated.
- Sum of goodput never exceeds bucket rate ±5%.
- Etiquette cap respected per endpoint even when global slots are free.
- SIGTERM drains: in-flight files finish or checkpoint; nothing corrupts.
Stop and ask if: honoring rsync --bwlimit precisely proves impossible — propose the
approximation and its bound instead of silently accepting drift.
```

## P1.1 — Bundle format, signing, import path
**Model: Opus 5 · Effort: xhigh · Mode: plan-first · HUMAN REVIEW REQUIRED**

```
Read CLAUDE.md (invariants 1, 2, 8), then docs/architecture-v2.md §5 (derived plane)
and §6 (T0–T2 hooks).

Goal: signed vector bundles as an alternative implementation of the embed stage.

Deliverables:
1. app/bundles/format.py: header.json schema (space_id, collection, uid range/list,
   counts, producer pubkey, backend fingerprint, challenge embeddings slot),
   vectors.parquet (uid, chunk_seq, vector[dim]) — no text, ever; MANIFEST.sig =
   Ed25519 over canonical header bytes + parquet sha256. Use pynacl; no hand-rolled
   crypto; canonicalize JSON deterministically (sorted keys, no floats in header).
2. Export: pack from local Lance by uid range; sign with key at NODE_KEY_PATH.
3. Import as embed-stage implementation: verify sig → space match → possession
   binding (raw_hash present locally for every uid) → T0 checks (P1.2 provides the
   full ladder; call its interface) → columnar append to Lance → set embed_done with
   provenance (bundle id, producer) in the manifest bundles ledger.
4. Failure semantics: any verification failure imports NOTHING from the bundle
   (all-or-nothing per bundle), records the failure in the ledger, and never crashes
   the worker.
5. Race test: import racing local embedding of the same uid — delete-then-insert
   must leave exactly one consistent copy; assert both orders.

Acceptance: round-trip (export → tamper one byte in each of header/parquet/sig →
import) rejects with the right error class for each tampering; untampered round-trip
is bit-stable across two export runs (deterministic packing).
Non-goals: coordinator upload (P1.3), T1–T3 internals (P1.2).
NOTE FOR THE HUMAN: review the signing/canonicalization code personally before merge;
a subtle bug here is a security hole, not a crash.
```

## P1.2 — Verification ladder T0–T3
**Model: Opus 5 · Effort: xhigh · Mode: plan-first**

```
Read CLAUDE.md, then docs/architecture-v2.md §6 in full. Keep the tier table open
while you work; the acceptance criteria mirror it.

Goal: the ladder as a library with clean interfaces, callable from import (P1.1) and
later from the coordinator.

Deliverables:
1. app/verify/t0.py: dim/dtype/norm/NaN/dupe checks; per-dim moment envelope and
   sampled pairwise-cosine distribution vs. the space's published envelope
   (envelope format: JSON artifact shipped with the space definition).
2. app/verify/t1.py: probe-set cosine profile (probe set = signed artifact of the
   space); overlap-band check for consecutive chunks using locally re-derived text
   adjacency; thresholds from the space artifact, not hardcoded.
3. app/verify/t2.py: challenge-triple validation of the bundle header.
4. app/verify/t3.py: spot recompute — sample selection from OS entropy (os.urandom;
   NEVER seeded from bundle content — grindability, see §6), sequential test with
   configurable n and τ from the space calibration table; calls EMBED_URL through
   the existing client.
5. Space artifacts: writer that generates envelope+probes+goldens for a space given
   a corpus sample and the embed endpoint (this is how a space author publishes).
6. Verdict object: tier reached, pass/fail, evidence payload for the ledger.

Testing: property-based (hypothesis) — generators for honest bundles (real small
model via mocked embed endpoint returning recorded vectors), wrong-model bundles,
perturbed bundles (ε ladder), missing-normalization bundles. Assert detection
matrix: T0 catches norm/dtype, T1 catches wrong-model at ~100% on fixtures, T3
catches 5%-corruption with measured probability consistent with (1-f)^n ±
binomial CI. Statistical assertions use fixed recorded vectors, not live models.
Stop and ask if: any threshold wants to be hardcoded — thresholds live in space
artifacts.
```

## P1.3 — Coordinator service
**Model: Sonnet 5 · Effort: high · Advisor review before merge**

```
Read CLAUDE.md, then docs/architecture-v2.md §7.

Goal: the four-endpoint coordinator, deliberately boring.

Deliverables:
1. Separate FastAPI app (deployable standalone): GET /work (lease uid range, TTL,
   advisory), POST /bundles (auth by signature; runs T0–T2 via app/verify; stores
   accepted bundle metadata; pushes artifact to configured HF repo or local dir),
   GET /have, GET /demand.
2. SQLite state: leases, bundles, credit ledger, producer stats (spot-check pass
   rate fed by importer reports endpoint POST /reports).
3. Lease semantics per §7: expiry re-issue, collisions harmless by design — write
   the test that proves double-completion of one range converges.
4. contribute mode in the worker: pull lease → fetch/chunk/embed → pack (P1.1) →
   upload; demand-priority ordering per §7.
5. Rate limiting and body-size caps on all endpoints.

Acceptance: two simulated contributors + one consumer against the coordinator in a
pytest integration: work partitions, a bundle flows end-to-end, credit accrues,
demand ordering observed.
Non-goals: DHT, reputation decay curves (flat probation flag is enough), web UI.
```

## F1 — Phase 1 end-to-end integration + adversarial pass
**Model: Fable 5 · Effort: default · Mode: plan-first**

```
Read CLAUDE.md, then docs/architecture-v2.md §5–§7 and the merged code of
P0.7/P1.1/P1.2/P1.3.

Goal: make the whole Phase 1 loop true, not just its parts. You own cross-module
correctness; expect to open fixes in several modules.

Deliverables:
1. tests/integration/two_node/: docker-compose bringing up producer node, consumer
   node, coordinator, mocked embed endpoint with recorded vectors; scripted flow:
   producer leases+embeds+uploads → consumer demands+imports+verifies → consumer
   answers a query citing imported vectors with correct provenance badges.
2. Adversarial suite: corrupted-space bundle, wrong-model bundle, 5%-poisoned
   bundle, replayed old bundle, expired-lease double completion, import/local race,
   coordinator restart mid-lease, consumer SIGTERM mid-import. Each has a defined
   correct outcome; encode it.
3. A findings report (docs/phase1-integration-notes.md): every cross-module defect
   found, root cause, fix PR reference — plus any spec ambiguity you had to resolve,
   flagged for backport into architecture-v2.
4. Fix the defects (separate small PRs per module, referencing the failing test).

Constraint: no weakening of any acceptance test from prior tasks to make
integration pass. If a prior contract is wrong, say so explicitly in the report and
propose the amendment; don't silently change it.
Definition of done: the two-node flow runs green from a clean checkout with one
command, twice in a row (idempotence at the system level).
```

## F2 — Tolerance calibration harness
**Model: Fable 5 · Effort: default**

```
Read CLAUDE.md, then docs/architecture-v2.md §6 (T3, backend fingerprint, open
limits).

Goal: turn τ from a number we asserted into a number we measured.

Deliverables:
1. tools/calibrate_space.py: given a space + corpus sample + N embed endpoints
   (e.g. CPU llama.cpp, SYCL llama.cpp, CUDA via a peer/API), embed the same
   chunks everywhere; produce the calibration table: same-model cross-backend
   cosine distribution (min/p01/p50), per-backend fingerprint stats, and the
   recommended τ with its false-reject estimate at the T3 sample size.
2. Adversarial margin analysis: measured gap between cross-backend variation and
   (a) different quantization of the same model, (b) a different model, (c) ε-
   perturbation at the τ boundary — i.e., quantify the perturbation budget §6
   flags as the adversary's window, and state whether the default τ should move.
3. Short writeup (docs/calibration.md) with plots, reproducible from the script.
4. Wire the output format into app/verify/t3.py's expectations (P1.2 defined it).

Constraint: everything reproducible from recorded vectors checked into fixtures;
live-endpoint runs are a script flag, never a test dependency.
```

## Advisor prompt (paste into a Fable 5 session with the plan or diff)

```
You are reviewing, not implementing. Context: CLAUDE.md and
docs/architecture-v2.md §<relevant>. Below is a <plan|diff> produced for task <id>.

Evaluate strictly against: the ten invariants in CLAUDE.md; the task's acceptance
criteria; failure modes the author didn't test (concurrency, partial failure,
restart, adversarial input); and any place the implementation quietly diverges from
architecture-v2. 

Output: (1) blocking issues with the exact invariant or criterion violated,
(2) risks worth a test but not a block, (3) what's solid — one line each. No
rewrites unless a fix is under 5 lines; otherwise describe the fix. If the plan is
sound, say so plainly and stop.
```

## Debugging prompt (heisenbugs, escalate here before changing models)

```
Model: Opus 5 · Effort: xhigh. Read CLAUDE.md.

Bug: <symptom, frequency, first-seen commit if known>.
Reproduce: <command / test>, fails ~<rate>.

Rules of engagement: form hypotheses before touching code; instrument, don't guess —
add targeted logging/asserts, run the reproduction, read the evidence, then fix.
The fix must include a test that failed before and passes after, run 50× if timing-
dependent. If the root cause is a spec gap, name the architecture-v2 section that
needs amending. Do not suppress, retry-wrap, or widen tolerances to make the
symptom disappear.
```
