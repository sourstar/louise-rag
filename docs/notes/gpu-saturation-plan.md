# The card kept busy, shared and interruptible: the plan (2026-11-01)

*Operator, 2026-11-01:*
> "When the switch to near exactness has been verified on all the collections I want you to:
> 1. Optimize each collection so that the embedding uses the GPU close to 100% in stand alone mode (only that collection).
> 2. These optimizations are only part of the common collections backend (that is not the localai backend, but louise's engine to handle quering, reranking, embedding in the most efficient way).
> 3. Test concurrently the embeddings of multiple collections ensuring that the GPU is properly shared without relevant idle times.
> 4. Test that queries have an higher priority without breaking embeddings in the middle and without breaking the several steps needed to complete a query already in process. […] Do every step in the order I gave you. Step 2 actually is a base rule that applies to all the other steps."

The steps run in the operator's order: precondition, then 1, then 3, then 4. Step 2 is a rule that applies to all of them. Every change follows scheduling.md §6 and measuring.md §6: BEFORE numbers, one deploy, AFTER numbers, a row in scheduling-log.md.

## The base rule (step 2): where the optimizations live

**Louise's engine** is the model-work layer that every collection uses:
- the encoder gates and request levels (`app/dispatch.py`: the gates, the levels CORPUS < TRANSLATE < QUERY < RERANK < CHAT, the interactive claim);
- the embedding client (`app/embeddings.py`);
- the worker's feeders (`_embed_producer`, `_embed_parallel`);
- the query steps (`app/retrieval.py`, `app/llm.py`, `app/translate.py`).

The rule has three parts:
- **One mechanism for every collection.** No collection gets its own code path. A collection's numbers (batch, requests in flight) are chosen by the engine from measurement (RULE 2), never hand-set per collection as the way to tune.
- **LocalAI stays a plain model server.** Its backends and model YAMLs are not where tuning happens. A model-server setting changes only when Louise's engine needs a capability it lacks, and then as a run setting that the gate validates (invariant 12).
- **The VeGa wrapper is ours.** It may publish what the engine needs to see, such as the card's busy time.

Today's per-collection `[encoder] batch` and `concurrency` keys become what the engine chose, recorded, rather than what a person typed. Every setting the engine moves to is gated first: a new batching is a new run to the gate, which takes about 8 minutes.

## What exists, and what does not (survey of 2026-11-01)

**What exists:**
- **One gate per URL**, strict by level, least-served plugin first within a level (dispatch.py:206).
- **Corpus stands aside card-wide** while a query step holds the claim (dispatch.py:378).
- **The claim is per step**: a whole-query claim was reverted because it idled the card 3–4 s per query (scheduling-log 1174–1189).
- **The feeders:** one producer per collection, 2 requests submitted and 1 prepared (`EMBED_PLUGIN_SLOTS`), streams of `EMBED_STREAM_S` = 120 s.
- **Measured today:**

  | window | card busy | rate |
  |---|---|---|
  | wikidata alone (batch 16, 4 in flight) | 36–40 % | 57 chunks/s |
  | wikipedia alone | 8–21 % | — |
  | both | 25–36 % | — |

  The bound is latency per request, not the card and not the CPU.

**What does not exist:**
- (a) **Nothing chooses batch or requests in flight.** They are static, hand-set values.
- (b) **No in-app measure of the card's busy time or idle gaps.** Today's figures come from `/root/measure/gpu_busy.py` on the node; `app/gpu_probe.py` reads memory only. The gate's per-collection served time (`Gate._served`) is never published.
- (c) **No end-to-end test of query priority under load.** The tests patch the claim or run one process.

**Two defects found by the survey:**
- **One gate for every model on a URL.** All models behind one LocalAI address share one gate, and `gate()` resets that gate's capacity to whatever the latest caller passes (dispatch.py:281–284). Two collections with different concurrency flip it back and forth. **The flip is FIXED (2026-11-01):** each caller declares its collection's concurrency, and the gate admits the largest declared by the collections running or waiting there now. A query in the web has no collection name, so for it the gate keeps the largest ever given. An arrival that raises the limit, or an admission that leaves room, wakes those waiting. The fix is inert while one collection runs alone. One gate per (URL, model) and the card budget remain step 3's.
- **Stale text in scheduling.md:** lines 1083 and 1099–1103, and line 873 naming `db.claim_query_priority`.

## Step 0 — the precondition: near-exact verified on every collection

For each collection with an encoder, all three must hold:
1. #160, #161 and #162 are deployed.
2. Its encoder's document run, and its query run where the space has one, has passed the gate again under the new rule. The statement records `exact` or `near`, with its `load_variation` and its batching.
3. Where a run is near-exact, it has a stored CPU reference. A run that stays exact needs none.

| collection | encoder | known blocker |
|---|---|---|
| wikidata, wikipedia | bge-m3 | the CPU twin must run on our engine (patch 0002, token ids); München is on it |
| gutenberg | qwen3-embedding-4b | the query-side CPU reference to finish |
| pubmed | MedCPT article + query | the CPU twins' 503s; rerun on the #157 image |
| normattiva | DAR-legal-it | to check |
| deposit collections | as declared | to check |

München produces the table for every collection on the node, and step 1 starts when every row passes. Meanwhile I build the instruments, which change nothing on the node.

## Instruments first (needed by 1, 3 and 4), all in Louise

- **I1 — the card's busy time series. BUILT (2026-11-01) in the `gpu-probe` service, not the VeGa wrapper.** The wrapper's code is hashed into every run's engine (`wrapper_manifest`; `app/vega/procfs.py` is staged into its package too), so changing it would have been a new run for every model and a re-gate each, in the middle of step 0. The probe already sees the host's processes and reads their DRM fdinfo for the System page; it now also samples each client's engine counters once a second and keeps an hour per card (`app/gpu_busy.py`, GET `/busy?since=`).
  - The counters are the kernel's standard (Documentation/gpu/drm-usage-stats.rst): xe's `drm-cycles-<engine>` over `drm-total-cycles-<engine>`, each client against its own time base; amdgpu's `drm-engine-<engine>` ns against the wall clock; both divided by `drm-engine-capacity-<engine>`. The same counters that gputop, nvtop and qmassa read; none of them publishes a series a program can poll.
  - A card is as busy as its busiest engine class. Impossible readings (a counter that went back, a share above 1.02) are dropped and counted, never averaged in (the two lessons in STATUS.md).
  - Pros: no run changes; per process with the panel's labels; read-only. Cons: a LocalAI backend's command line names no model, so per model is per process, and which model a process serves is read from the gates (I2), not from the card.
  - **AMENDED (2026-11-01, after the first deploy read 0 % of a 34–37 % card):** reading another process's fdinfo is a ptrace-read check, and the probe (CAP_PERFMON, the host's PID namespace) was refused every model server's. CAP_SYS_PTRACE was not taken: with the host's PIDs it reads and writes every process's memory, the node key included (invariant 8). The card's figure now comes from its DRIVER (`app/gpu_pmu.py`): xe's perf PMU (`engine-active-ticks` / `engine-total-ticks` per engine instance, what intel_gpu_top reads; the node's B70: `xe_0000_03_00.0`, ccs0 its only compute engine), allowed by the CAP_PERFMON the probe already holds; amdgpu's `gpu_busy_percent`. fdinfo stays for WHO, and gives a card's figure only when no process was unreadable. A card with neither reports `busy: null` with `blind: <why>`, never 0.
  - **SETTLED (München, 2026-11-01 18:2x): the PMU is the truth; fdinfo's client cycles are HALVED on this card and kernel** (B70, 6.18.52). The test: ccs0 kept full by 4 loops of one ~8 k-token text on bge-m3, everything else paused. perf ccs0 read 91.8 % and 92.1 %, the probe's /busy averaged 90.4 % over 131 samples, and fdinfo (`gpu_busy.py`) read 45 % and 46 %. Idle: 0 on both. So every fdinfo figure on this card is ×2, the afternoon's included: wikidata alone at batch 16 was ~75–90 % busy, not 37–45 %, and both collections together ~50–70 %. Step 1's ≥ 90 % is measured on the PMU, and with chunks/s beside it, because busy that does not raise throughput is not counted.
  - **The card's figure is its busiest COMPUTE engine** (`gpu_busy.COMPUTE_ENGINES`: ccs, rcs, gfx, compute). perf confirmed the copy engine reads as busy as ccs under load: 78.4 against 78.3, 92.7 against 91.9, and 0 at idle (München, 18:29–18:47). The Level Zero runtime keeps a copy context resident while compute runs, and the driver counts it as active.
- **I2 — who used the card. BUILT.** Each gate's snapshot gives, per collection, its requests running and waiting and the seconds it really held the server (`held_s`, running requests included). The existing `_served` could not serve: it is the fair-share account, raised when a collection joins.
- **I3 — idle gaps. BUILT.** The embed feeders say whether their collection has work it could send now (`stats.Recorder.set_backlog`): yes while candidates remain, including between two streams at the 120 s boundary; no when the selection runs dry, the encoder defers or the node pauses. The worker's card recorder (`app/card_record.py`) joins I1–I3 one line a second into `<root>/state/card/<date>.jsonl` (3 days kept). A GAP is a second the card was idle (under 5 %) while work waited and no query held the claim; LOST is Σ (1 − busy) over the seconds work waited. `louise dev schedule-report --gpu --minutes 15` prints them per minute with each collection's request-seconds held.
  - The standard not used: Prometheus (a scrape a second, PromQL windows). The node runs none; adopting it is a server and its storage for one instrument. Exposing these figures at `/metrics` would let one scrape them unchanged.
- **I4 — query timing under load.** A harness in `tools/measure/`: real web-process queries (embed → search → rerank → answer) against a worker embedding at full rate. It records each step's wait and service time, its errors, and the corpus throughput lost.

## Step 1 — each collection alone, card close to 100 %

**Target:** sustained card busy ≥ 90 % (I1) with the collection alone. "Close to 100 %": the last few percent are the request boundaries. Throughput must rise with it; busy that does not raise chunks/s is not counted.

**The engine's levers, in order of cost** (each measured BEFORE/AFTER, one deploy each):
0. **wikidata's BEFORE row (2026-11-01 18:28–18:43, alone, batch 16 / concurrency 4, the PMU):** ccs 77–84 % a minute (82.1 % over 15 min); **gaps 1 s in 885 s** (0.1 s/min); lost 10.8 s/min; **78.8 chunks/s** (71,316 chunks / 68,322 documents); worker 0.76 cores. Two consequences:
   - **Lever 1 has nothing to take here:** not one idle second, even across the 120 s stream boundaries. It is reverted on main (#169), and it comes back only for a collection whose BEFORE row shows boundary gaps (wikipedia's is next).
   - **The loss is inside the seconds, between requests, and the requests are too small.** wikidata averages 1.04 chunks a document, and a request carries one DOCUMENT's chunks (`embed_chunks` per `upsert_book`). So the gated batch of 16 almost never fills: about 76 requests a second, ~1 text each, each paying the server's whole per-request turnaround. The lever is CROSS-DOCUMENT BATCHING (below, lever 2).
1. **No drain at stream boundaries.** A feeder hands over to the next stream with its requests still in flight, instead of draining to zero every 120 s. Engine code, no run change.
2. **Requests filled across documents (dynamic batching).** The texts of many documents in flight join one request of up to the gated `[encoder] batch` texts, with the collection's concurrency of requests in flight. This is the standard of inference servers: Triton's dynamic batcher, TEI's token-budget batching. Each document still writes only when all its chunks are back (invariant 2). A request that fails is sent again per document, so an input's error stays its own document's. It is inert for a batch of 1. It needs no new gate: the gate's "contribute" pass already sends batches of `batch` texts at the collection's concurrency, which is exactly what is validated. **Then, by tokens rather than texts:** A request carries up to a token budget, filling the encoder's window (bge-m3: ubatch 8192). A request of wikidata's ~120-token chunks then holds ~64 texts, and wikipedia's longer ones fewer. One run setting per budget, gated (the near-exact rule holds batches to the bound).
3. **Requests in flight chosen by measurement.** AIMD on throughput (the wheel already used for the re-index window): add one while chunks/s rises, back off on a fall or a deferral. It moves only between settings already gated: a ladder such as {2, 4, 6, 8}, each gated once and recorded. Persisted as the engine's last good value, re-confirmed after a restart (invariant 10: runtime, not desired state).
4. **The producer, where it becomes the bound** (wikipedia's chunk preparation, the write path #162 eased). Prepared batches ahead in the parse pool, and commits per batch.

**Order:** wikidata, wikipedia, gutenberg, pubmed, normattiva, deposit. Each gets its standalone row in scheduling-log.md:
- busy, chunks/s, tokens/s;
- the chosen batch and requests in flight;
- the gate's verdict;
- a byte or bound check on 2,000 written rows.

**Done for a collection:** ≥ 90 % busy sustained over 15 min, or the measured reason it cannot. For example, a model whose server cannot take a batch: then step 2 decides whether a server capability is needed.

## Step 3 — several collections at once, the card shared without idle gaps

1. **Fix the gate defect first.** One gate per (URL, model) with its own capacity. Above the gates, one CARD resource: the in-flight budget the card can keep busy, measured in step 1, shared by DRR across the collections with work (`app/sched.py`, the existing wheel). It is work-conserving: a collection with no work leaves its share to the others.
2. **Measure** with 2, 3, then every collection embedding:
   - card busy (I1) at least the best standalone figure;
   - idle gaps (I3) under ~1 s a minute;
   - each collection's served share (I2) within ±10 % of the DRR rule;
   - no collection starved over any 5 min.
3. **Tests:**
   - unit: the card budget, DRR shares, a collection arriving and leaving;
   - a scenario with a mock encoder: three feeders and the gaps counted;
   - the live rows on the node.

## Step 4 — queries first, nothing broken in the middle

**The rules to hold:**
- **A query step waits behind at most the corpus requests already sent.** None is pre-empted (dispatch.py:27). So the engine bounds a corpus request's duration while queries are possible: a token budget whose service time is ≤ ~200 ms, measured. With requests in flight ≤ N, a query step waits at most N × 200 ms.

  Steps 1 and 4 pull against each other here: large batches fill the card and lengthen that wait. The token budget and N are chosen together, and recorded with the measurement.
- **The model server's own queue.** Corpus requests queued at a one-sequence server sit ahead of a query. The engine keeps at most the in-flight depth step 1 needs queued there, never more.
- **No embedding broken in the middle.** A stand-aside is a WAIT, bounded and never an error:
  - the document is neither deferred nor errored, and never half-written (invariant 2: marked only after its commit);
  - its batch is finished, not abandoned;
  - only a wait past the request's deadline is a deferral, as today.
- **No query broken between its steps.** Today the claim drops between steps, so corpus requests sent in that gap delay the next step. A short hold-over keeps the claim for a step's expected successor, for example 0.5 s after a query step if that query has more steps. It is measured against the idle cost that made the whole-query claim be reverted.
- **Across processes** (web and worker share no gate): the claim stays the coupling. Its read period (0.1 s) is part of the bound and is measured.

**The test (I4)**, run with the worker at the full rate from steps 1 and 3 on every collection:
- a scripted query every 10 s for 30 min, and bursts of 5;
- for each step (translate, query embedding, rerank, answer), p50 and p95 waits within budget against the same query with the card idle;
- zero query step failures;
- corpus side: zero errors and zero half-written documents (`chunks_drift` over the documents embedded in the window); throughput lost during queries reported.

Plus the unit and scenario tests that are missing:
- a real web process and a worker against a mock encoder;
- the cross-process claim under load;
- a hold-over that does not idle the card.

## What this asks of München, in order

1. Step 0's table, and the CPU twins.
2. Deploying I1–I3, then each lever of step 1 alone, with its row.
3. The live windows of steps 3 and 4.

I write the code, the tests and the notes. München deploys and measures. Nothing moves on the node without its BEFORE numbers.
