# Model profile: cold start, warm latency, memory (2026-10-09)

*Operator, 2026-10-09: "Timeouts shall not include cold start; we must have
a profiling of the cold/warm start and of the real memory occupancy for
future improvements." Tool: `tools/profile_models.py`. Raw result:
`deploy/vega/profile/2026-10-09.json` (08:46–09:01 UTC). The load bounds
derived from it: `app/model_loads.json`, read by `app/model_load.py`.*

## How it was measured

- **Where.** On the node, in a throwaway container from the live
  `louise-rag:local` image. It ran with the host PID namespace and host
  network, `SYS_PTRACE`, `PERFMON` and `/dev/dri`. No Louise data was
  mounted. LocalAI v4.11.0 was asked on its loopback port (30286).
- **One model at a time.** The unpinned models that were loaded
  (`louise-chat-qwen3-8b`, `louise-rerank-bge`, `vega-bge-m3`) were shut
  down first. The pinned `vega-qwen3-embedding-4b` and `vega-granite` stayed
  loaded. Each model was shut down again after its profile. LocalAI's own
  memory peaked at 9.7 GB pinned plus the one model being profiled, under
  the reclaimer's 21,096 MiB trigger, so nothing was evicted. At the end the
  five models that had been loaded were loaded again, and the card read
  19,254 MiB used (19,478 before).
- **Beside the live worker.** Gutenberg kept embedding through
  `vega-qwen3-embedding-4b` the whole time, and the GPU was shared with it.
  So the warm figures are what a search meets on the node as it runs, not
  what an idle card gives. `vega-qwen3-embedding-4b`'s own cold starts
  raced the worker's requests. The worker deferred no document.
- **Cold.** Three times per model: `POST /backend/shutdown`, then one
  minimal request, timed from send to answer. A poller ran every 0.2 s and
  read the phases:
  - `process_spawned`: a new process in LocalAI's cgroup;
  - `vram_first`: the backend tree's first `vram0` byte in DRM fdinfo;
  - `vram_95`: 95 % of the memory it held at the answer;
  - `listed`: the model appears in `/system`.
- **Warm.** Five requests per size, after one unmeasured request.
- **Memory.** Read once after the load and again after the largest warm
  request:
  - from the backend tree's DRM fdinfo, one record per `drm-client-id`. The
    tree is followed, because LocalAI's pid for a `vega-` model is the
    wrapper, and its child holds the card;
  - LocalAI's `size_vram`;
  - the tree's host RSS.

## Results

Cold is the time from request to first answer, in seconds. The earliest
first load on record is the first load from disk (vega.md §11–§12). VRAM
and gtt are read after the largest warm request, in MiB. RSS is the
backend tree's host RSS, wrapper included, in MiB.

| model | cold min / median / max | earliest first load | warm | VRAM (after load → after warm) | gtt (host GPU buffers) | RSS | load bound |
|---|---|---|---|---|---|---|---|
| `vega-qwen3-embedding-4b` | 4.0 / 4.1 / 6.9 | 33.7 | 1×300: 0.25 · 8×300: 1.6 · 32×300: 6.9 · 1×1k: 0.27 · 8×1k: 2.4 · 32×1k: 9.3 | 8,787 → 9,449 | 1,320 | 850 | 105 |
| `vega-bge-m3` | 6.7 / 6.9 / 7.1 | 11.9 | 0.06 · 0.41 · 1.6 · 0.14 · 0.82 · 3.4 | 1,097 → 1,132 | 724 | 1,069 | 40 |
| `vega-granite` | 6.6 / 6.8 / 6.8 | 9.2 | 0.03 · 0.11 · 0.44 · 0.05 · 0.16 · 0.59 | 196 → 229 | 1,012 | 723 | 30 |
| `vega-medcpt-query` | 5.9 / 5.9 / 6.4 | 6.3 | 0.04 · 0.30 · 0.90 · 0.06 · 0.38* · 1.5* | 356 → 391 | 282 | 476 | 30 |
| `vega-medcpt-article` | 6.0 / 6.1 / 6.3 | 6.3 | 0.04 · 0.23 · 0.85 · 0.07 · 0.36* · 1.5* | 356 → 391 | 280 | 476 | 30 |
| `vega-medcpt-article-ov` (pairs, OpenVINO) | 3.5 / 3.6 / **21.1** | 7.3 | 0.03 · 0.16 · 0.57 · 0.03 · 0.21 · 0.67 | 289 → 349 | 34 | 406 | 65 |
| `louise-rerank-bge` | 7.2 / 7.5 / 7.5 | 8.1 | 10×300: 0.44 · 50×300: **1.7** · 10×3k: 1.2 · 50×3k: **5.5** | 1,107 → 1,162 | **4,552** | 557 | 30 |
| `louise-rerank-medcpt` (OpenVINO) | 2.4 / 2.4 / 5.6 | 4.7 | 0.04 · 0.13 · 0.05 · 0.20 | 296 → 651 | 42 | 256 | 30 |
| `louise-translate-madlad` (torch XPU) | 16.7 / 16.8 / **38.0** | 25.0 | 1 item: **7.1** · 10 items: 12.4 (max 22.3) | 6,394 → 7,160 | 97 | **3,561** | 115 |
| `louise-chat-qwen3-8b` | 6.2 / 6.3 / 8.2 | 9.4 | 1 token: 0.13 · 100 tokens: 10.1 (≈10 tokens/s) | 7,417 → 7,435 | 394 | 500 | 30 |
| LocalAGI's `qwen3.8-2b-q4` | 44.6 / 47.0 / 47.1 | 46 | 1 token: 0.17 · 100 tokens: 5.8 | 7,032 → 7,034 | 604 | 1,384 | (145) |

The warm sizes are inputs × characters. A `*` marks a size where one input
of about 1,000 characters exceeded MedCPT's 512 tokens (606 tokens, "too
large to process"). Louise's client cuts and retries such an input; the
median shown is over the requests that passed. The load bound is
`app/model_loads.json`: three times the slowest load on record, never
under 30 s, rounded up to 5 s.

## What the numbers say

1. **The 8 s rerank budget failed on the load, never on the rerank.**
   - `louise-rerank-bge` takes 7.2–7.5 s to load, or 8.1 s from disk.
   - Warm, 50 candidates take 1.7 s, which matches the 1.7 s of the warm
     wikipedia search. Gutenberg's long chunks (50 × 3,000 characters) take
     5.5 s.
   - So with the load inside the budget, a cold rerank had at most 0.8 s
     left for the work itself. With the load outside it, the budget has
     2.5 s of slack at gutenberg's worst size.
2. **A load has three phases, and the upload of the weights is not the
   longest.**
   - Every llama.cpp backend spends 2.15–2.2 s from spawn to its first
     VRAM byte. This is process start and SYCL/Level Zero init, and it is
     the same for a 200 MiB model as for a 9 GB one.
   - Reaching 95 % of the VRAM held at the answer takes 0.9–2.6 s from the
     first byte (3.7 s in qwen3-4b's first cold start, which raced the
     worker).
   - The model then answers 0.6–3.5 s later. This third phase does not
     follow the model's size: granite 3.0 s, bge-m3 2.8 s,
     `louise-rerank-bge` 3.5 s, qwen3-4b 1.1 s, chat 0.6 s. Two causes are
     plausible: LocalAI polling the backend's health, or the backend's
     warm-up run.
   - So 4–6 s of every load is fixed cost whatever the model. That is the
     obvious place to look next (`docs/notes/localai-bugs.md` if it is
     LocalAI's).
3. **A load from a cold page cache is several times slower for the Python
   backends.**
   - The first of the three cold starts: `vega-medcpt-article-ov` 21.1 s
     against 3.5 s, `louise-translate-madlad` 38.0 s against 16.7 s,
     `louise-rerank-medcpt` 5.6 s against 2.4 s.
   - The llama.cpp ones barely move, because their weights were in the page
     cache from the morning's loads.
   - Earlier first loads from disk were 33.7 s for qwen3-4b and 11.9 s for
     bge-m3.
   - The bounds take the slowest of all of these.
4. **LocalAGI's 2B model loads in 45–47 s, almost all of it upload**
   (VRAM grows from 2.2 s to 46 s). That is 17 times slower per byte than
   `louise-chat-qwen3-8b`, which uploads 7.4 GB in 2.6 s. Its weights are
   read some other way, perhaps unmapped or from a slower disk. That is
   unexplained, and worth a look (LocalAGI's YAML), since 46 s is every
   first agent turn after an eviction.
5. **`/system` lists a model only once its load has finished.** No model
   was listed before 95 % of its VRAM was there. Every `listed` time falls
   between 1.2 s before the answer and 0.15 s after it, with a poll every
   0.2 s. So a model someone else
   is loading reads as not loaded. `app/model_load.py` relies on this: its
   warm then simply joins that load.
6. **LocalAI's `size_vram` equals the tree's fdinfo `vram0`** in all 22
   readings, child included. vega.md §10 finding 5 holds for the pid that
   `/system` names, not for this figure.
7. **Host memory is not small.**
   - MADLAD holds 3.3–3.6 GB of RSS (torch) beside its 6.4–7.2 GB on the
     card.
   - `louise-rerank-bge` grows 4.4 GB of `gtt` (host-side GPU buffers)
     after 50 × 3,000-character requests, almost four times its VRAM.
   - The SYCL encoders grow 0.3–1.3 GB of `gtt` with batch size.
   - None of this counts toward LocalAI's reclaimer, which sums only
     `drm-total-vram*`. On a 63 GB host shared with the worker, it belongs
     in the balance (balance-on-shared-node).
8. **Warm translation is slow beside the worker.** One 200-character item
   takes 7.1 s and ten take 12.4 s. The 3B model generates token by token
   while qwen3-4b holds the card's compute, so this is contention, not
   load. Measure it again with the worker paused before concluding
   anything.

## Pinning: a recommendation, not applied

The arithmetic uses the values from vega.md §13:
- card C = 32,656 MiB, of which LocalAI sees T = 31,023;
- reclaimer threshold 0.68, so the trigger is X = 21,096 MiB of LocalAI's
  own `vram`;
- margin M = 2,048 MiB, so LocalAI may peak at A = C − M = 30,608 MiB;
- the peak is at most X + L, where L is the largest unpinned model.

Sizes are VRAM after warm, in MiB.

**Pinned today:** `vega-qwen3-embedding-4b` 9,449 and `vega-granite` 229,
P = 9,678.

**Recommended: also pin the four search-path models.** None of them is used
by corpus embedding, since wikipedia and pubmed have `embed = "off"`. Each
is on the path of a person's search:
- `vega-bge-m3`, 1,132 (wikipedia's query encoder);
- `louise-rerank-bge`, 1,162 (wikipedia's and gutenberg's reranker);
- `vega-medcpt-query`, 391 (pubmed's query encoder);
- `louise-rerank-medcpt`, 651 (pubmed's reranker).

Together they add 3,336, so P = 13,014. The arithmetic:
- X − P = 8,082 MiB is left for unpinned models. It holds any one of the
  chat model (7,435), MADLAD (7,160) or LocalAGI's model (7,034). A second
  one triggers the reclaimer, which evicts the least recently used one.
  This is what happens today too: chat was evicted for MADLAD at 08:11.
- The peak is X + L = 21,096 + 7,435 = 28,531 ≤ A = 30,608. The threshold
  stays where it is.
- The cost is 3.3 GB of VRAM held while idle, and about 2.4 GB of host RSS
  plus `gtt` that grows with use (finding 7).
- The gain: the first search after idle on wikipedia or pubmed meets no
  load. Today that is 7–12 s for the encoder plus 7–8 s for the reranker,
  sequential, now outside the budgets but still waited for.

**Not recommended: pinning MADLAD**, although every translated search needs
it.
- P would be 20,174. That leaves 922 MiB under X, so any chat or LocalAGI
  load would trigger the reclaimer, and the only unpinned model it can
  evict would be the one just loaded.
- Avoiding that needs X ≥ 20,174 + 7,435 = 27,609, a threshold of 0.89.
  Then X + L = 35,044 > C: the card would be overcommitted.
- MADLAD's cold start (17–38 s) is now outside the 120 s translation
  timeout and the language list's 10 s. It costs a cold search that time,
  but no longer its translations.

**Not recommended either: pinning the pubmed document encoders**
(`vega-medcpt-article`, `-ov`). Nothing embeds pubmed until its
`embed` mode changes.

## What changed in the code (same day)

`app/model_load.py`:
- Before a request to a model on a runner that answers `GET /system` (the
  embedding client, both the `openai` and the `vega` paths, the reranker,
  chat, translation and the language list), a model the runner does not
  list is warmed first. The warm is a minimal request bounded by the
  model's load bound: `app/model_loads.json`, else `[models]
  load_timeout_s` = 120 (07 §7.8).
- There is one warm per (server, model). `/system` is cached for 2 s.
  **Amended 2026-10-25:** the 2 s holds for corpus work only. A request a
  person waits on trusts an answer 0.25 s old at most, so a query's variants
  share one read, and a single request (a rerank, a chat answer, a
  translation) asks fresh. The old cache let a search's rerank believe the
  query embedding's answer of a moment before, while another model's load
  had evicted the reranker meanwhile, so the load ran inside the budget
  (STATUS.md, 2026-10-25). A rerank that times out with its model gone or
  re-spawned (a new backend pid) says so, and the cold start is logged.
- A load past its bound is a deferral for embedding and a stated fallback
  for search: `legs_withheld`, or `rerank_withheld` beside `reranked:
  false` (05 §5.6), with the reason "model loading took longer than N s".
- Each cold start goes to the node's event log (`node info:models`) from
  a publisher thread.
- A runner without `/system` is asked exactly as before.
