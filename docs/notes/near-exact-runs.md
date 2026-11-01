# Near-exact runs, the stored CPU reference, the multi-load gate (2026-10-31)

*Built: S1 in #119, S2 in the change after it.*

*Operator, 2026-10-31, choice C:*
- Exact stays the default.
- A run may be admitted as **near-exact**, but only on a gate of several fresh loads, so that its tolerance is "not just a lucky run".
- Flash attention is used "only if there is a performance improvement".
- "The reference is the CPU, as usual." Every GPU driver or backend update redoes the validation, and the CPU reference is stored so that half is not repeated.

Invariant 12 is amended by this note. Until the operator's text replaces it in CLAUDE.md, this note is the record.

**Amended 2026-11-01, operator's choice B.** The operator: "considering that collections embedding is ideally distributed, that different hw produce slightly different vectors, and the acceptance policy is anyway a tolerance threshold, it makes not much sense to enforce local bit exactness in this scenario" — then "B, go ahead". So:
- **Near-exact is the rule for card runs.** `exactness` defaults to `"near"`: no opt-in, no "only where measured faster". A run that gives back its own bytes is still recorded **exact**; one that reproduces itself within NEAR_VARIATION (0.99999) across orders, three fresh loads AND batches, and reaches the stored CPU reference at 0.999, is admitted as **near-exact**. Anything further apart FAILS, as before: the NaN and the 0.0865 vector of oneDNN's SDPA (below) are what this catches, and a bound catches them as well as byte equality does.
- **Batches are held to the same bound** (§3's "validated for one text a request only" is lifted): a near-exact run's batched answers within NEAR_VARIATION of its single-text ones are admitted, and their distance counts toward the run's `load_variation`.
- **`exactness = "exact"`** stays as a collection's stricter choice: bytes or nothing, batches by bytes.
- **Unchanged:** the CPU reference and its review line (0.995), the multi-load gate, NaN refusal, the space's tolerance between peers, the VeGa wrapper's vendor reproducibility modes (each a setting to measure, one at a time, before any is relaxed), and every vector already stored.
- A run that moves from exact to near-exact (a driver or engine update) shows it in its statement's `exactness`; it is reported, not refused.
- **A collection may require bytes for every peer** (operator, 2026-11-01: "if it useful add a parameter in the codex … we don't know yet what the futures will bring"): `space.exactness = "exact"` in its Codex, version 10 (09 §9.3), outside `space_id`, inside `collection_id`. Absent, the rule above. Then every run contributing to it must be exact (`runs.passing` refuses a near-exact statement for it), and a node's own setting can only be stricter. A recipe states it as `[space] exactness = "exact"`.

## 1. Why

On the SYCL card, flash attention gives a vector of a long input that differs across fresh loads now and then (approved-encoders.md):
- vega-medcpt-query: 3 of 8 runs;
- gutenberg's qwen3-4b: 4 of 8 runs.

Each flip stays within cosine ≥ 0.999999 of the other answers. Turning it off costs gutenberg's encoder ~2.2× (P5 engine: 436 s a run with FA on against ~970 s off, München). medcpt-query is faster with FA off, so it stays off there.

## 2. The stored CPU reference (S1)

- **What it is.** For one space and role, the vectors of the calibration set's 512 items, computed by the encoder's **CPU twin**: the same weights, engine and options, `gpu_layers: 0`, `no_op_offload: true`, the device hidden (approved-encoders.md "Cross-device bar").
- **Where it lives.** `<collection>/calibration/cpu-reference/<role>/<run>.json`, where `<run>` is the CPU twin's run id. That run id names the driver ("cpu"), the engine image and the weights, so a new CPU build or new weights is a new file and nothing is silently reused.
- **Its format.** The run object, the set id, and the vectors as base64 float32, plus a digest of the whole.
- **How it is made, once.** `louise hold embedder cpu-reference <collection> --model <cpu twin> [--query-model <cpu twin>]`, an operator verb. On the CPU it takes hours for qwen3-4b, which is why it is stored. The verb embeds the sixteen fingerprint texts twice, in reverse order, and refuses if the CPU twin does not give back its own bytes. It asks ONE TEXT A REQUEST, each bounded at 300 s per 1,024 tokens of the space's window times the requests queued at the twin (`cpu_reference.request_timeout`): the card's flat 300 s and the collection's `[encoder] batch` made wikidata's (ctx 8,191, batch 16) time out three times on 2026-11-01.
- **How the gate uses it.** Every gate pass already embeds all 512 items. With a CPU reference present, it also reports each item's cosine to it:
  - worst **< 0.995** (the review line, operator 2026-10-18): the run FAILS. Review means it writes nothing until a human has looked.
  - **Quantised weights: a line per quantization level, measured** (operator, 2026-11-01: "calculate a reasonable threshold per quantization level, do not just assume it is the same as the full precision"; `embedder.cpu_review_line`).
    - **Why every quantised level differs, not only 4-bit.** For every quantised weight type, llama.cpp's CPU path rounds the activations to 8 bits before the dot product (`vec_dot_type`: Q8_K for the k-quants, Q8_0/Q8_1 for the others), and the card's path does not round them the same way. F16 and BF16 weights take F16/BF16 activations on both. So Q8_0 is not expected to sit near full precision here, and is measured like the rest.
    - **The rule, fixed before the data** (`cpu_line_from`): line = 1 − 1.5 × (1 − worst), where worst is the lowest card-vs-CPU-twin cosine measured for the level over the calibration texts, the shortest included. That leaves 50 % headroom over the largest deviation seen, for other cards, drivers and texts. The result is rounded down to 0.001 and never set above the operator's 0.995.
    - **An unmeasured level has no line.** Its run is not judged against a CPU reference on another level's line: with a reference stored, it fails with "measure this level". Without one, nothing is compared, as for any run.
    - **Measured so far:**

      | level | model, measurement | worst | p1 | median | line |
      |---|---|---|---|---|---|
      | F16 / BF16 / F32 | bge-m3 F16, wikipedia's gate against c19ca7b8 (2026-11-01) | 0.999930 | 0.999955 | 0.999999 | 0.995 (operator; the rule gives 0.9998) |
      | Q4_K_M | Qwen3-Embedding-4B, gutenberg run 0a80405f against our CPU engine's twin (München, m6) | 0.989292 | 0.996898 | 0.998358 | **0.983** |

      The worst sits on the shortest texts (36–39 tokens), with a near-constant offset at every length. Two CPU engines agreed 1.000000 on 19 of 19.
    - **How a level is measured:** `tools/cross_vendor_check measure <space> --model <card model at the level> --twin <its CPU twin> --twin-texts 128 --twin-shortest 32`. It prints worst, p1, median and the rule's line. The worst over the model families measured goes into `MEASURED_CPU_WORST`. Both families in production are to be measured at every level llama.cpp offers in use (Q8_0, Q6_K, Q5_K_M, Q5_0, Q4_0, IQ4_XS, Q3_K_M, Q2_K): bge-m3 (XLM-R encoder) and Qwen3-Embedding (decoder).
    - **The level's source:** the type tag of the weights file LocalAI loaded (`options.Model`, in the stated run and its statement). The standard source is GGUF's own `general.file_type`, which the VeGa wrapper would have to state; that is a new run for every model, so it is taken at the wrapper's next change.
    - **This supersedes the first 4-bit rule** (choice A, 0.985 for every 4-bit tag): Q4_K_M's line is 0.983, computed from its measurement, and other 4-bit levels are measured on their own.
  - worst **< 0.999**: the run may not be near-exact (§3); an exact run passes as today, with the number in its statement.
  - The statement carries `cpu_reference: {run, worst, median}`, or `null` when none is stored.
- **Validation is redone on every update for free.** A driver, engine-image or wrapper change is a new run (09 §9.12; the wrapper since #116/#117), so the gate runs again before any write, against the stored CPU reference, in minutes of the card.

## 3. Near-exact runs (S2)

- **Opt-in, per collection and role**, in desired state: the collection's TOML `[embed] exactness = "near"`, default `"exact"`.
  - The opt-in exists only for a model where a near-exact setting is measured to be FASTER.
  - The gate never downgrades a run to near-exact by itself. A run believed exact that shows a flip FAILS and is investigated: an AMD or NVIDIA driver update must not slide into "near" unnoticed.
- **The multi-load gate.** Every gate pass now runs the fresh-load half of invariant 12 itself, which only the measurement harness ran before:
  - LOADS = 3 fresh loads (LocalAI `/backend/shutdown`, waited for, retried while "loading", as `tools/vega_repro_check` does since #113);
  - each load embeds the sixteen fingerprint texts plus SWEEP = 32 prefixes of the set's longest item, one word up to the window, in a shuffled order.
- **Verdicts:**
  - every text one byte string across all loads: **exact**;
  - otherwise, with `exactness = "near"`, the worst cosine between any two answers of one text ≥ **0.99999**, and the CPU reference present with a worst ≥ 0.999: **near-exact**;
  - otherwise: FAIL.
- **What changes for a near-exact run:**
  - its in-load reverse pass is held to the same 0.99999, not to byte equality;
  - it is validated for one text a request only (`batch_invariant: false`), since a byte comparison of batched answers means nothing for it;
  - the fingerprint stays in the statement as evidence, with `exactness: "near"` beside it, so no peer expects to reproduce it bit for bit.
- **The cost.** Three model reloads and 3 × 48 texts each time a run is gated: about 1–3 minutes on the card, gated runs only.

## 4. The statement (09 §9.12, §9.18): version 5

Version 5 adds, to version 4's body:
- `exactness`: `"exact"` or `"near"`;
- `loads`: an integer, the fresh loads measured;
- `load_variation`: the worst cosine between two answers of one text, a decimal string; `"1"` for an exact run;
- `cpu_reference`: `{run, worst, median}` or `null`.

A version-4 statement keeps meaning "exact, one load". `read_statement` reads 4 and 5.

## 5. What it does not change

- **Vectors already stored.** They are kept, gutenberg's included (operator, 2026-10-31).
- **The space's `tolerance` and the calibration gate's quorum.** The CPU bar and the load bar are additional.
- **Peers.** They verify at tolerance as before. A near-exact statement tells them its fingerprint is evidence, not something they can reproduce.

## 6. Order of work

1. S1: the CPU reference store, its verb, and the gate's comparison with `cpu_reference` in the statement. The statement goes to version 5 here, with `exactness: "exact"` and `loads: 1` until S2.
2. S2: the multi-load gate, the opt-in, and the near-exact verdict in `runs.check`.
3. On the node: the CPU references, made once (gutenberg, wikipedia, pubmed's two sides), then each collection gated again.

**Flash attention's NaN: the cause, by measurement (2026-10-31, München).**
oneDNN's fused SDPA, which llama.cpp SYCL takes for flash attention unless
`GGML_SYCL_FA_ONEDNN=0` (`GGML_SYCL_ENABLE_DNN=0` never covered it):
- qwen3-4b on the P5 engine, FA on with oneDNN's SDPA: 4 of 8 runs varied,
  with a NaN and a 0.0865 wrong vector. With `FA_ONEDNN=0`: 8 of 8
  bit-exact, no NaN, 23–45 % faster than FA off.
- louise-rerank-bge, FA on + `FA_ONEDNN=0`, on gutenberg's real candidates
  at the batch sizes that NaN'd on 10-21: 0 NaN in 135 requests over 5
  fresh loads × 3 passes, every score identical across loads and orders,
  16–36 % faster than the strict copy.

`FA_ONEDNN=0` is the wrapper's Intel contract (#124). FA on is live where it
is EXACT and faster, so no near-exact opt-in is in use:
- gutenberg, run 0a80405f (exact over the multi-load gate, the 2,048 sweep
  clean);
- louise-rerank-bge, since 14:06 (scores within 0.031 logits of the strict
  copy, the same top 10 on 3 queries);
- wikipedia's bge-m3 (already FA on).

medcpt-query stays FA off: exact either way, not faster (43.3 ms against
47.6 ms a query, median). Why oneDNN's SDPA gives a NaN is not studied
further: it is switched off.
