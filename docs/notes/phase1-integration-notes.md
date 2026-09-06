# Phase 1 integration notes (F1)

Findings from the end-to-end integration and adversarial pass over
P0.7 / P1.1 / P1.2 / P1.3. Six cross-module defects were found and fixed, all
of the shape module suites cannot see: **each half of the broken behavior was
individually correct**. Every fix landed with a failing-first regression test;
no prior acceptance test was weakened (full suite: 560 passing before and
after, with the new tests added).

This repository is not under git, so the task's "fix PR reference" is given as
the regression test that failed before the fix and passes after — which is the
durable artifact a PR link would point at anyway.

Definition of done, verified: `docker compose -f
tests/integration/two_node/docker-compose.yml run --rm driver` runs the
producer → coordinator → consumer → query flow green from a clean checkout,
**twice in a row against the same volumes** (second pass: bundle delivery is a
no-op, vector counts stable, badges stay `verified`).

---

## Defects found and fixed

### F1-D1 — Quarantine burned the key but kept serving the poison
**Seam:** `app/bundles/verify_on_use.py` × `app/bundles/trust.py` × the worker.
**Symptom:** verify-on-use caught a poisoned chunk, quarantined the producer —
and every *other* vector that producer had written, including whole documents
the failed sample never touched, stayed live in Lance and kept surfacing in
retrieval. Trust said "never again"; the index said "still serving".
**Root cause:** `trust.quarantine_key` *returns* the affected uid→chunk map
precisely so a caller can act on it, and `verify_chunks` ignored the return
value. Each module correct; the handoff missing.
**Fix:** on quarantine, every affected document gets `embed_done=0` plus a
priority `embed_queue` entry, so the worker re-embeds locally (§6:
"quarantine the key, re-embed locally"). Stale vectors serve only until the
worker's delete-then-insert replaces them — a bounded window, deliberately
traded against making those documents unsearchable immediately.
**Test:** `tests/integration/test_defects.py::test_quarantine_requeues_every_document_the_producer_wrote`
(plus `..._restores_local_provenance`).

### F1-D2 — Every node reported demand as the same consumer
**Seam:** `app/contribute.py` × `app/coordinator/store.py`.
**Symptom:** the coordinator counts *distinct reporters* so one noisy consumer
cannot dominate demand ordering — and `report_demand` defaulted the reporter
id to the **collection name**, so every node in the network reported as
`"gutenberg"` and N consumers collapsed into one. The anti-gaming mechanism
was silently defeated by its own caller's default.
**Fix:** `db.node_id(con)` — a random identity generated once and persisted in
the control table (random rather than hostname-derived, which would leak
deployment topology onto a shared service).
**Test:** `test_demand_reports_from_two_nodes_count_as_two_consumers`,
`test_node_id_is_generated_once_and_persisted`.

### F1-D3 — No fulfilled lease was ever completed
**Seam:** `app/coordinator/service.py` × `app/contribute.py` × the wire format.
**Symptom:** every lease sat `open` until its 24 h TTL expired, however fast
the work finished — lease listings lied for a day about minutes-long work.
**Root cause:** the upload handler keyed completion on a `lease_id` field the
wire format (correctly — a bundle is content, not workflow) does not carry, so
the completion call was dead code; and `contribute.run_round` never called
back either. Three modules, each defensible alone.
**Fix:** server-side coverage matching, no wire change: an accepted upload
completes the uploader's open leases whose uid sets it covers
(`leases.complete_covered`). Naturally idempotent.
**Test:** `test_upload_completes_the_lease_it_fulfils`.

### F1-D4 — A weak first boot bricked the node when the endpoint came up
**Seam:** `app/spaces.py` boot validation × deployment startup ordering.
**Symptom:** a node whose *first* boot ran while the embed endpoint was down
pinned a weak (name-derived) model hash. When the endpoint came up, the
confirmed hash differed from the pin and `validate_boot` refused to start —
permanently, with a message telling the operator to wipe their vectors. An
innocent `depends_on` ordering slip became an unbootable node.
**Root cause:** the mismatch branch considered only whether the *current* hash
was confirmed, never whether the *pinned* one had been.
**Fix:** a weak pin upgrades to a confirmed hash (same config recipe) with a
logged event. Sound because a weak hash under the ollama API means the
endpoint was unreachable — any vector embedded since came from whatever
server answers `EMBED_URL`, which is exactly the model now being confirmed.
A later *confirmed* change still refuses, unchanged.
**Test:** `test_weak_pin_upgrades_when_the_endpoint_comes_up` (including the
still-refuses case).

### F1-D5 (minor) — Import left satisfied demand in the lazy queue
**Seam:** `app/bundles/importer.py` × `embed_queue` × the demand feed.
Import is "an alternative implementation of the embed stage", and the real
embed stage clears its document's queue row; the import path didn't — so an
imported document's demand entry lingered and kept being re-published to the
coordinator as need the network had already met. (Self-healing at the
coordinator, which excludes covered uids — but the node kept reporting it.)
**Fix:** one `DELETE FROM embed_queue` per imported uid.
**Test:** `test_import_clears_the_lazy_queue_entries_it_satisfies`.

### F1-D6 — The consumer role could not search its own imported vectors
**Seam:** `app/retrieval.py` × §14's role model × the import path.
**Symptom:** found by the two-node flow itself — the consumer imported
everything and then answered dense queries with nothing.
**Root cause:** retrieval gated its dense leg on `embed_mode != "off"`,
conflating "don't **spend** on embedding" with "don't **use** embeddings".
§14's consumer role is import-only: `embed_mode=off` *because* vectors arrive
from the network — making the one node type built entirely around imported
vectors the one node type that refused to search them.
**Fix:** the dense leg runs whenever vectors exist (no table → `[]`, endpoint
down → caught and degraded, both pre-existing behavior). `embed_mode` keeps
governing the worker's spending — lazy enqueue and backfill are untouched,
asserted in the same test.
**Test:** `test_imported_vectors_are_searchable_with_embed_mode_off`.

---

## Adversarial suite

`tests/integration/test_adversarial.py` — the eight prescribed scenarios, each
with its defined outcome encoded:

| Scenario | Defined outcome |
|---|---|
| Corrupted-space bundle | `BundleSpaceMismatch`; ledger `rejected`; document untouched, local embed available |
| Wrong-model bundle | Coordinator **accepts** (T0–T2 is all it can run; `tier_reached=0` records how little was checked); consumer rejects at sample re-embed; report → probation → next lease throttled 10× |
| 5 %-poisoned bundle | Import sampling is probabilistic per §6's own `(1−f)ⁿ`; with the sampler pinned to miss, the bundle is accepted, the poison sits inert until retrieval, verify-on-use fails exactly the poisoned chunk, quarantines the key, and (D1) re-queues every affected document |
| Replayed old bundle | Ledger no-op, no duplicate vectors, no double credit; replay against a *revised* document → possession refuses stale vectors |
| Expired-lease double completion | Same range re-issued, both upload, state converges: one bundle id, one credit, one `/have` entry |
| Import / local race | Both orders asserted at module level (`tests/bundles/test_import.py`) with the real cross-process lock; the crash-window artifact (`embed_done=0` → re-queue → retry succeeds) asserted here |
| Coordinator restart mid-lease | Leases persist in SQLite: no double-issue after restart, completion still accepted |
| Consumer SIGTERM mid-import | Stop mid-bundle: nothing corrupts, nothing half-applied; next drain completes; no duplicate ids |

## Two-node flow

`tests/integration/two_node/` — compose stack of producer, consumer,
coordinator and a mock embed endpoint (the only fake: deterministic
text→vector, serving `/api/embed` and `/api/show` so both nodes compute the
same **confirmed** space id with no monkeypatching anywhere). Flow proven:
consumer publishes demand → producer's lease returns the demanded uids first →
embeds through the mock → signs and uploads → coordinator verifies T0–T2 and
credits → bundle delivered to the consumer inbox (§7's local-dir distribution
channel) → worker drains and imports → dense query answers with hits carrying
`producer=<pubkey>` and `imported ◌` badges → `/api/verify` flips them to
`verified ✓` → replayed delivery is a no-op.

Harness note: the first compose run failed because the producer entry script
wrote the node key world-readable and `keys.py` **refused to load it** — the
invariant-8 guard catching the test harness itself. Fixed in the harness;
recorded here because it is the guard working, not a defect.

---

## Spec ambiguities resolved — flagged for backport into architecture-v2

1. **Bundle retrieval channel is undefined.** §7 specifies upload
   (`POST /bundles`) but no download; distribution is prose ("push to HF repo
   or local dir"). Phase 1 resolves this as the local-dir channel (coordinator
   artifact store → consumer inbox). Backport: name the consumer-side
   transport explicitly, or a `GET /bundles/{id}` becomes an accidental
   requirement on the coordinator ("holds no bulk data" says otherwise).
2. **`space_id` does not cover `dim`, `CHUNK_TOKENS` or `CHUNK_OVERLAP`.**
   §3's listing includes `dim`; the implementation (and §3's own formula
   footprint) does not, and chunk geometry was never included — so two honest
   nodes at the same `CHUNK_VERSION` can chunk differently. Import compensates
   (explicit four-way dim check; local chunk-id set equality), but the doc
   should either add these to the recipe or state the compensating checks.
3. **§6's "semantic triangles" cannot prove fabrication for this wire
   format.** The Gram/PSD condition holds automatically for any real vectors
   (Gram = XᵀX ⪰ 0); it discriminates only if the wire carried per-probe
   *cosines*. Measured: min det G over 200 k random real triples = +0.12.
   Kept as an integrity canary; the doc's "strongest test" framing should be
   corrected.
4. **The T2 beacon is unspecified.** Resolved as HMAC-epoch over a root
   published inside the signed space artifact — offline-verifiable, and
   honestly **not a security control** (the root is public, the bank
   cacheable): §6's own "proof of correct oven, not each loaf". Resist
   hardening it into generated challenge texts; that converts a free check
   into a GPU call (it becomes T3).
5. **§6's `n≈16` is a budget, not an acceptance boundary.** At n=16 against
   f=5 %, achievable β is 0.44 — and §6's network arithmetic *depends* on
   that (0.44¹⁰ ≈ 3×10⁻⁴). T3 records delivered (lattice-exact) error rates,
   never Wald's design targets. Worth one clarifying sentence in the doc.
6. **Invariant 2's "SIGTERM = finish current document" vs sub-document
   units.** Acquisition checkpoints mid-file (byte granularity + Range
   resume); import stops between documents mid-bundle. Both lossless, both
   deviations from the literal sentence; documented in the respective
   modules, worth acknowledging in the invariant.
7. **`embed_mode` semantics.** After F1-D6: `embed_mode` governs what the
   worker *spends*, never what retrieval *uses*. §14's role table implies
   this; one sentence would prevent the regression.
8. **Mock-model limitation.** A deterministic text→vector fake cannot
   reproduce the overlap invariant (adjacent-chunk similarity), so the
   two-node flow cannot exercise T1's overlap band. That is F2's territory:
   calibration against *recorded real* vectors. Noted so the gap is a known
   one, not a discovered one.
