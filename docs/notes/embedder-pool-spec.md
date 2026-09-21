# Runtime serving: the embedder pool (llama-swap)

Status: specification. Scope: the external container that serves embeddings
to Louise for both query and creation, on one machine. This document fixes
the design decisions; it is not yet an implementation.

*2026-09-19: the node now runs PyTorch, OpenVINO and Ollama models beside
llama.cpp. [`rage.md`](rage.md) extends this design to every
runtime and every model kind, in one image.*

## 1. Purpose

Louise does not re-embed shared corpora — vectors travel between peers ready
made (§4). A running engine is therefore needed for only two workloads on a
peer's own machine:

- **query** — embedding the user's question at ask time. Small, latency
  sensitive, many collections may need it at once.
- **creation** — embedding when a peer creates or extends a collection.
  Batch, heavy, throughput sensitive, one encoder at a time, rotated as
  batches change.

These two workloads are served by one external container running
**llama-swap** in front of **llama.cpp** (`llama-server`) instances.
llama-swap speaks the OpenAI-compatible API; Louise always calls one
endpoint and names the encoder in the `model` field. llama-swap decides
what to load, keep hot, or unload.

## 2. Core principle: one engine per distinct encoder, not per collection

The costly resource is the model in memory, not the collection. The
encoder's hash is fixed in the Codex (§2.4), and many collections share one
encoder. Ten collections on the same encoder are served by one
`llama-server` process. So the question is never "how many collections" but
"how many *distinct encoders* are active, and do they fit in memory".

A container-per-collection design is explicitly rejected: it pins memory for
dormant collections and wastes VRAM on idle models.

## 3. Query embedders: kept hot, evicted only on demand

- All query embedders live in one llama-swap **group** with `swap: false`,
  so they coexist and never evict one another.
- Each has **TTL = 0** (never auto-unload on idle).
- Rationale: the memory is allocated to Louise by the peer. If no other
  tenant reclaims it, unloading an idle embedder is self-harm — it frees
  memory nobody wanted and forces a reload (~6–8 s) on the next query. Idle
  is not a reason to unload when the space is ours.
- Eviction is therefore **explicit and event-driven**, never on a timer: a
  query embedder is unloaded only when a new model must be loaded and there
  is genuinely no headroom left. No pre-emptive eviction. While there is
  headroom, nothing is touched.
- One query embedder per distinct encoder among the collections the peer has
  activated. If memory holds them all, every active collection answers with
  no load latency.

TTL-based eviction has a place only when memory is contended by another
tenant — not this case.

## 4. Creation embedder: pinned during a batch, rotated explicitly

- The encoder running a creation batch is marked **persistent** (TTL 0 /
  persist) so it is never evicted mid-batch. Evicting a batch model
  interrupts real work; a query, by contrast, tolerates a short load wait.
- Batches rotate roughly hourly. Two acceptable modes, chosen by available
  memory:
  - **rotate**: unload the finished encoder, load the next. The ~6–8 s load
    cost is negligible over an hour-long cycle.
  - **keep all hot**: if the GPU has room, leave creation encoders resident
    and just use whichever the next batch needs, paying no load cost at all.
- Rotation / unload of a creation encoder is **explicit, at batch boundary**,
  driven by Louise's orchestrator — not by a TTL timer.

## 5. Query priority over creation

llama-swap orchestrates *which* models are loaded; it does **not** schedule
GPU priority between two already-running jobs. A running batch will not be
pre-empted just because a query is "priority" — without care, the query
queues behind the heavy job.

Priority is built above llama-swap, in two layers:

- **Primary (adopt now): separate hot query embedder.** Query and batch run
  as *distinct processes* (§3 vs §4). They no longer contend for one
  process; they contend only for the GPU, where a query occupies the card
  for a fraction of a second and slips between the batch's kernels with
  negligible perceived latency. This suffices in the large majority of
  cases.
- **Secondary (hold in reserve): explicit batch throttle/pause.** If
  measurement shows the primary is not enough, Louise's orchestrator pauses
  or slows the batch while a query is pending. This is Louise's logic, not a
  llama-swap feature. Do not build it until measured to be needed.

## 6. Memory budget: the peer decides

The peer declares a memory ceiling dedicated to models. The pool lives
within it. The peer sets one number; nothing is configured per collection.
(Low-ceiling / single-model-at-a-time behaviour is deliberately out of
scope for this document.)

## 7. What llama-swap gives us (verified)

- Default: one model at a time; requesting another swaps it in.
- **TTL** per-model or global: seconds idle before unload; 0 = never.
- **persist** (or ttl 0): keep a model always loaded, ignore eviction — used
  to pin a creation batch.
- **groups** with `swap: false`: run several models concurrently without
  mutual eviction — used for the query-embedder set.
- OpenAI-compatible endpoint; manages any such server, including in Docker.
  Louise calls one endpoint, names the encoder in `model`.
- `${PORT}` macro is mandatory in every `cmd`.

## 8. Determinism note (carried from the verification design)

On a single machine the embedder is bit-exact: the vendor's deterministic
mode is enabled so identical input yields an identical vector, and Louise's
internal self-check compares bit-for-bit. Cross-peer comparison, on
different hardware, is not bit-exact and uses a cosine threshold declared in
the Codex. The pool config must enable the deterministic mode in each
`llama-server cmd`. (Detail of exact flags: to be fixed at implementation.)

## 9. Open items for implementation

- Exact `llama-server` flags for deterministic mode per backend (SYCL/CUDA/
  Metal/CPU).
- Concrete llama-swap `config.yaml`: group definition for query embedders,
  per-model persist for creation, `${PORT}`, ctx sizes, `--embedding`.
- How Louise maps a collection's encoder hash → a llama-swap model alias.
- ~~Headroom check that triggers explicit eviction (§3)~~ — **built
  2026-09-21, in RAGE rather than Louise** (rage.md §7, amended): a load that
  does not fit gives up idle models ranked strictly below it, lowest
  `priority` first, and only if that is enough. "Idle" — nothing in flight,
  nothing asked for `idle_s` — stands in for "whose batch is in flight".
  The batch-boundary unload hook (§4) is still open.
- Health/readiness wiring so Louise waits for load before first query.
