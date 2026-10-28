# Per-sequence encoder outputs in llama.cpp: the interface-conformant design (2026-10-27)

*Written by a design workflow (4 readers, 3 designs, 4 judges, 1 synthesis; 2026-10-27) for the operator's question "Considering the interface of llama.cpp, what would it be an interface-like compliant implementation?". The deployed patch is P3 (localai-bugs.md); this note is the design of the step after it, nothing here is built.*

*Research only. Nothing was compiled or run. All paths are in the llama.cpp checkout at `c0fd786` with our P3 applied (only `tools/server/server-context.cpp` differs from upstream). A claim marked "computed" was worked out from configs or code, not measured. Most file:line cites come from the four research maps. The ones the decision rests on were re-read for this note: llama-graph.cpp:37, 2910, 2923; llama-context.cpp:360, 379-380, 728, 1925-1953; ggml.c:4179-4181; t5.cpp:147, 170-173; vec.cpp:531-551; common.cpp:1583-1601; server-context.cpp:591-605, 2000-2026, 4041-4054; server-decision.cpp:1270-1290.*

## 1. The answer in five lines

1. The output belongs inside the context's one memory object. That means a T5-only composite, `llama_memory_enc_dec` (the decoder's `llama_kv_cache` plus a per-sequence encoder store), created by a new `case LLM_ARCH_T5:` in `llama_model::create_memory`. It adds no public symbol.
2. The existing `llama_memory_seq_*`, `llama_memory_clear` and `llama_state_seq_*` calls then carry the encoder output. An encode replaces only its own sequences' output. A decode reads only the rows of the sequences in its ubatch. A sequence with no encoder output gets `-1`.
3. Two fixes none of the three designs had complete. First, force `cparams.kv_unified` in the context constructor, not only inside the memory, or the first multi-slot graph aborts. Second, the server must encode after its whole-sequence `seq_rm` (server-context.cpp:4041), not before it.
4. Stage 2 is what upstream reviewers asked for: a persistent device buffer, written through `apply()` and `ggml_set_rows` from an optional `init_encode`.
5. No design makes a translation run among 7 concurrent ones byte-identical to the same translation run alone. No written rule requires it for query translation: invariant 12 binds encoders, and the 2026-10-20 amendment (02, "Methods whose output is not bit-exact") binds model-made claims stored in a collection, which it lets be checked by agreement. The unit of reproducibility for concurrent translation is the operator's choice (§8). *(Corrected by the session after the workflow, which had read the 2026-10-20 amendment as binding translation.)*

## 2. What llama.cpp's interface already offers, and where

**Already there:**
- **One entry point per kind of work, with a sequence id per token.**
  - `llama_process(ctx, LLAMA_PROCESS_TYPE_ENCODE|DECODE, llama_batch_ext*)`: include/llama.h:297-300, 1091-1095.
  - `llama_batch_ext_add_token` takes the seq id: llama.h:1038.
  - Per-sequence encoding needs no new API.
- **One memory per context, reached by every public memory call.**
  - The member is `memory` (src/llama-context.h:309).
  - `llama_memory_clear/seq_rm/seq_cp/seq_keep/seq_add/seq_div/seq_pos_*` take only a `llama_memory_t` (llama.h:755-821; llama-context.cpp:4385-4479).
  - The API promises "Removing a whole sequence never fails" (llama.h:760).
- **State goes through the memory only.** A context save is the arch string plus `memory->state_write` (llama-context.cpp:3608-3626). A per-sequence save is the memory alone (3654-3668). Flags: `PARTIAL_ONLY`, `ON_DEVICE` (llama.h:922-934).
- **The memory interface, `llama_memory_i`** (src/llama-memory.h:73-128).
  - Methods: `init_batch`, `init_full`, `init_update`, `seq_*`, `state_write/read(io, seq_id, flags)`, `memory_breakdown`.
  - "The only method that should mutate the memory … is `apply()`" (llama-memory.h:50).
  - `process_ubatch` applies any memory context it is given, for any graph type (llama-context.cpp:1507-1512).
- **Memory type by architecture.** `create_memory` dispatches on the arch (src/llama-model.cpp:2771-3315). T5 has no case today, so it gets a plain `llama_kv_cache` from the default branch (3292-3310).
- **Precedents for composing memories:**
  - `llama_memory_hybrid` composes by delegation (llama-memory-hybrid.h:19-91; .cpp:138-206).
  - `llama_memory_hybrid_idx_context` subclasses a base context, so builders that `static_cast` to the base keep working (llama-memory-hybrid-idx.h:133). Its state section is appended as a suffix with no version bump (.cpp:256-263).
  - dsv4 keeps per-sequence device side state with its own state header (llama-kv-cache-dsv4.h:11-76; .cpp:991-996, 1598-1640).
  - The recurrent memory defines "whole removal" as `rm_all` (llama-memory-recurrent.cpp:171-184).
- **Graph builders take their data from the memory context.**
  - `build_attn_inp_kv` and `build_inp_pos_bucket_dec` cast `mctx` to `llama_kv_cache_context` (llama-graph.cpp:3153, 2864).
  - The KV cache writes K and V with `ggml_set_rows` (`cpy_k`, llama-kv-cache.h:416).
- **The decoder already selects by sequence.** The cross mask zeroes entry (i,j) only when token i's sequence owns encoder row j (llama-graph.cpp:1127-1160). The encoder mask separates sequences within one ubatch (llama-graph.cpp:496-499).

**Not there:**
- **The encoder output lives outside the memory.**
  - `llama_cross cross; // TODO: tmp` is a member of the context (llama-context.h:307; struct at llama-graph.h:84-99).
  - Every T5 encode overwrites it wholesale (llama-context.cpp:1740-1764).
  - It is never saved, never freed by `seq_rm` or `clear`, and never shared by `seq_cp`.
- **Encode passes no memory context** (`mctx = nullptr`, llama-context.cpp:1649). `llama_memory_i` has no encoder hook.
- **No status means "invalid input."** `llama_memory_status` has only FAILED_PREPARE and FAILED_COMPUTE (llama-memory.h:30-35). FAILED_PREPARE makes decode return `1`, "no KV slot" (llama-context.cpp:1940-1953; llama.h:1002), and the server answers that by halving its batch and retrying (server-context.cpp:4423-4430).
- **Several decoder streams are not supported for T5.**
  - `set_input_pos_bucket` asserts `n_stream == 1` and `!equal_seqs` (llama-kv-cache.cpp:1808, 1812). The cross mask asserts `!equal_seqs` (llama-graph.cpp:1134).
  - `kv_unified` defaults to false (common/common.h:580). The server turns it on only for `-np auto` (tools/server/server.cpp:169-172).
- **No public predicate for "encoder-decoder with cross-attention."** `has_encoder` is true for T5, T5ENCODER, EAGLE3 and DFLASH (llama-model.cpp:3756-3771). That is why P3 matches the arch string.

## 3. The recommended design

It is Design 2 (`llama_memory_enc_dec`) with parts taken from the other designs, which this section names. How the four reviews disagreed, and how that was settled:

| Review | Picked | Settled as |
|---|---|---|
| Conformance | D2 | Taken: D2's shape is the one that uses llama.cpp's own abstractions |
| Isolation | D1 | D1's smaller surface is not taken. Its store stays a context member that the memory reaches through a back-pointer, valid only because of member order (llama-context.h:307/309). Its behaviour also depends on `llama_kv_cache`'s internal virtual calls reaching the overrides (llama-kv-cache.cpp:1198, 2184, 2368, 2707-2740), which a rebase can change without a conflict. |
| Correctness | D1 ("the only one that builds") | Taken as a defect in D2: fixed by D1's `cparams` forcing (below) |
| Cost | D1 to carry, D2 to submit | Carry D2 directly, so there is one implementation, not two. The cost review's rule is adopted: the carried patch only adds lines, and deletions wait for the upstream PR. |

**Blocking defects found by the reviews, and their fixes:**
- **D2 and D3 abort at context creation for `-np N` without `-kvu`** (verified).
  - They force one stream inside the memory only. The self-attention mask still takes `n_stream = cparams.kv_unified ? 1 : ubatch.n_seqs_unq` (llama-graph.cpp:37), while attention splits Q by `k->ne[3] = 1` (llama-graph.cpp:2910).
  - Graph reserve uses `n_seqs = n_seq_max` (llama-context.cpp:728). The mask is then `[n_kv, n_tokens/7, 1, 7]` against kq `[…, 1]`, and `GGML_ASSERT(a->ne[3] % mask->ne[3] == 0)` fails (ggml/src/ggml.c:4181).
  - Fix: force `cparams.kv_unified` at llama-context.cpp:360.
- **D3 dereferences a null pointer for every other encoder.** It checks `mctx_enc`'s status without a null guard. Fix: `if (mctx_enc && …)`.
- **D3 returns 1 for a missing encoder output**, which triggers the server's batch-halving loop. Fix: the new status maps to `-1`.
- **D2's `llama_encode` doc line is wrong for stage 1.** It says the output is dropped on error, but stage 1 writes only after a successful compute (llama-context.cpp:1740), so the old output survives. Corrected below.
- **No design meets "alone == among 7" by construction.** This cannot be fixed by where the store lives. It is the deploy gate (§6 T2) and an operator decision (§8).

### 3.1 Public API

None: no symbol, no struct, no enum value, no version macro. Only doc comments change:
- **`llama_encode` (llama.h:984-989).** Replace "does not use KV cache" with: "For encoder-decoder models, stores each batch sequence's encoder output in the context's memory, replacing that sequence's previous output and keeping all others. `llama_memory_clear`, a whole-sequence `llama_memory_seq_rm` and `llama_memory_seq_keep` of another sequence release it; `llama_memory_seq_cp` shares it; `llama_state_seq_*` saves it. On error, the previous output is kept."
- **`llama_decode` (llama.h:1001-1005).** `-1` also when a sequence of an encoder-decoder context has no encoder output. A failed first decoder ubatch also loses that sequence's encoder output (see the rollback row in §3.4).

### 3.2 Internal changes, stage 1 (our P4, and the first upstream PR)

- **NEW `src/llama-memory-enc-dec.h/.cpp`** (added to src/CMakeLists.txt next to llama-memory-hybrid.cpp).
  - `llama_cross_store`, a host side state on the dsv4 pattern. It is not a `llama_memory_i`.
    - It holds `llama_cross`'s fields: `n_embd`, `v_embd`, one `std::set<llama_seq_id>` per row.
    - `replace(ubatch, embd, n_embd)` implements 6412d61's semantics: remove the batch's sequences from every row, compact rows left with no owner (order kept), append the new rows.
    - Also `seq_rm(s)` (s<0 means all), `seq_cp(src,dst)`, `seq_keep(s)`, `clear()`, `has(s)`, `rows_for(ubatch)` (the rows owned by the ubatch's sequences, in store order), `state_write/read`.
    - Rows are bounded by `n_seq_max × n_ubatch`. Seq ids are below `n_seq_max` (llama-batch.cpp:188-189), and an encode holds at most `n_ubatch` tokens (llama-context.cpp:1612).
  - `llama_memory_enc_dec : llama_memory_i`.
    - It owns `std::unique_ptr<llama_kv_cache> dec` and the store.
    - `init_batch` copies `llama_kv_cache::init_batch`'s split loop (llama-kv-cache.cpp:724-757; hybrid does the same) and calls `dec->prepare` (public, llama-kv-cache.h:209).
    - It then checks every ubatch's sequences against `has()`. On a miss it logs "sequence %d has no encoder output" and returns `LLAMA_MEMORY_STATUS_FAILED_INPUT`.
    - All other methods follow §3.4.
  - `llama_memory_enc_dec_context : llama_kv_cache_context` (the hybrid_idx pattern).
    - The existing casts at llama-graph.cpp:2864 and 3153 stay valid.
    - It keeps its own `i_cur`, because the base's is private (llama-kv-cache.h:460).
    - It exposes the store, the selected rows and `n_enc` for the current ubatch.
- **`src/llama-memory.h:30-35`, `llama-memory.cpp:9-63`.**
  - Add `LLAMA_MEMORY_STATUS_FAILED_INPUT`, with its case in `llama_memory_status_combine` and `llama_memory_status_is_fail`.
  - The header is internal, not public.
- **`src/llama-context.cpp`:**
  - **At :360, after `cparams.kv_unified = params.kv_unified;`:** `if (model.arch == LLM_ARCH_T5 && cparams.n_seq_max > 1 && !cparams.kv_unified) { warn; cparams.kv_unified = true; }`.
    - This runs before `n_ctx_seq` is computed (379-391). The mask, the KV view and `llama_n_ctx_seq` then agree.
  - **Decode switch (1931-1960) and the init/update switch (~957-970):** `FAILED_INPUT` → error log, `return -1`.
  - **Encode's T5 block (1740-1764):** keep the guard and `synchronize()`. Replace the overwrite with `static_cast<llama_memory_enc_dec *>(memory.get())->enc_store(ubatch, embd.data, t_embd->ne[0])`.
    - The cast is guarded by the same arch check that already guards the block. In stage 2, `init_encode` replaces it.
  - **Graph params (`&cross`, :2701):** unchanged in the carried patch, and removed in the upstream PR.
- **`src/llama-model.cpp`:** `case LLM_ARCH_T5:` before MINIMAX_M3 (~2801), ending in `break`.
  - The inner `llama_kv_cache` gets the default branch's arguments (3292-3310). `unified` is now consistent with `cparams`.
  - T5ENCODER keeps the default branch.
- **`src/llama-graph.h/.cpp`:**
  - `llm_graph_input_cross_embd` and `llm_graph_input_attn_cross` read `mctx`, cast as at :3153.
  - `cross_embd`'s set_input (432-440) uploads only the selected rows, one `ggml_backend_tensor_set` per contiguous run. A lone sequence is a single run, the same bytes as today.
  - The mask (1127-1161) reads the selected rows' sets. Its "encode first" assert becomes unreachable.
  - Reserve fallback (2837-2838, 3572): `n_ctx_train` when `n_seq_max == 1` (unchanged). Otherwise `max(n_ctx_train, n_seq_max × n_ubatch)`, so a VRAM shortfall shows at startup, not in the middle of a burst.
- **`src/models/t5.cpp`:** no change in stage 1.

### 3.3 Stage 2 (a separate PR): the device store

- `llama_memory_i` gains `virtual llama_memory_context_ptr init_encode(const llama_ubatch &) { return nullptr; }` after llama-memory.h:99.
- The store becomes F32 `[n_embd, (n_seq_max+1) × n_ubatch]` in its own backend buffer, zeroed at allocation (dsv4, llama-kv-cache-dsv4.cpp:991-996). Row metadata uses `llama_kv_cells` (sequence bitsets, `used_max_p1`).
- Encode passes `init_encode`'s context to `process_ubatch`. It stays nullptr for every other memory.
- `apply()` assigns the rows. The T5 encoder graph (graph<true>) writes them with `ggml_set_rows` under `if (mctx)`. The decoder reads `ggml_get_rows(store, rows)` (D2's selection, not D3's whole-store view).
- This removes the synchronize and memcpy (llama-context.cpp:1744-1749) and the per-step upload (llama-graph.cpp:438).
- State goes through `io.write_tensor`, so `llama_memory_get_buffers` (llama-memory.cpp:67-92) and `ON_DEVICE` see the buffer.
- **Stage 3, optional and gated on §6 T10:** store per-layer cross K/V instead, which removes the recompute at t5.cpp:170-173.

### 3.4 Sequence operations and state

"Whole" means `p0 <= 0 && (p1 < 0 || p1 == INT_MAX)`, the recurrent memory's `rm_all` rule (llama-memory-recurrent.cpp:171-184).

| Operation | Decoder KV | Encoder rows |
|---|---|---|
| `clear(data)` | as today | all dropped |
| `seq_rm`, whole | as today | that sequence's rows dropped; s<0 means all rows. Returns the KV's answer (always true, llama-kv-cache.cpp:404-471) |
| `seq_rm`, partial | as today | kept: context shift (server-context.cpp:3471), rollbacks (4668, 4715), cache reuse (3852) |
| `seq_cp(src,dst,…)` | the range is copied | dst leaves every row, then joins every row of src (shared, not duplicated). If src has no rows, dst ends with none |
| `seq_keep(s)` | as today | rows without s dropped; sets narrowed to {s} |
| `seq_add`, `seq_div` | as today | untouched: cross-attention has no position bias (t5.cpp passes `kq_b = nullptr` to the cross `build_attn`) |
| `seq_pos_min/max`, `get_can_shift` | KV only | none. Required by the position check (llama-batch.cpp:400-418) and position inference (:1427) |
| decode rollback (llama-context.cpp:2003-2024) | removes `[pos_min, ∞)` | when `pos_min == 0` that is a whole removal, so the rows go. Documented in llama.h |
| encode (not a memory op) | position check unchanged | only the batch's sequences replaced; on error the old output stays (stage 1) |

**State:**
- `state_write` writes the KV section first, unchanged. Then, unless the flags include `PARTIAL_ONLY` (a non-SWA KV writes nothing there either, llama-kv-cache.cpp:2077-2079), it appends a suffix:
  - `u32 magic 'T5XE'`, `u32 version 1`, `u32 n_embd`, `u32 n_rows`;
  - each row's sequence set (whole-context writes only);
  - then the F32 rows.
- `state_read` treats `PARTIAL_ONLY` the same way. It checks the magic, the version, that `n_embd` matches hparams, and that every id is below `n_seq_max`. A single-sequence restore tags its rows `{dest}`.
- On any failure it drops the sequence from both parts and rethrows (llama-memory-hybrid.cpp:190-206; hybrid_idx 265-313).
- No version bump.
  - Old T5 files fail to load ("end of buffer"). They never held the encoder output anyway (llama-context.cpp:3608-3668).
  - Other models' files are byte-identical.

### 3.5 Server changes on top of P3

All of these sit inside P3's `enc_dec` (arch "t5" with encoder and decoder, ~1244-1249).

1. **Remove P3's warning about more than one slot (~1252-1257).** The library now forces unified KV. Mirror it in `params_base.kv_unified` so the slot bookkeeping matches (1430, 1851, 2952).
2. **Remove the load-time probe encode (~1404-1418).** For enc_dec, skip `common_context_can_seq_rm` and set `ctx_tgt_seq_rm_type = NO`, warning that speculative decoding is unsupported.
   - Why: its `llama_memory_clear` (common/common.cpp:1583) would now wipe the probe's output, and its decode would log an error (1597-1601).
3. **Move the encode to just after `slot.mem.seq_rm(slot.id, p0, -1)` (:4041).**
   - For an enc_dec slot `p0 = 0`, because P3 cleared the prompt and `pos_next` returns the size (server-common.cpp:437-443). That call is therefore a whole removal: left where it is, the encode would be freed before the decode.
   - Keep the `n_enc > n_ubatch` refusal (3714-3725). Drop P3's own `seq_rm(slot.id,-1,-1)` (~3728).
   - Guard the encode with `!slot.enc_tokens.empty() && batch.size() < n_batch`. A slot whose start token misses this batch then encodes in the pass where it enters.
4. **Children.**
   - Give `enc_tokens` only to non-child tasks. `n_cmpl` children inherit the encoder rows through `seq_cp` in `copy_state_to/copy_prompt_to` (775-787).
   - Turn off shared-prefix grouping for enc_dec. server-decision.cpp:1270-1290 groups tasks by the token prefix of what is, for T5, the encoder input, before P3 rewrites the prompt (server-context.cpp:2007-2020). Its children (WAIT_OTHER, 2024-2026) would take the parent's output via `copy_prompt_to` (4053-4054).
5. **`release()` (591-605):** call `prompt_clear()` for enc_dec slots too. The KV cells and encoder rows are freed at task end, and `seq_rm` resets the head (llama-kv-cache.cpp:436-438). A lone request after a burst then gets cells `[0, n)`, as at `-np 1`. Nothing is lost, because `cache_prompt` is already off.
6. **`get_available_slot`:** `update_cache &= !enc_dec` (~1818-1821). The cache key is the encoder input's tokens, while the saved state is decoder state plus encoder rows.
7. **Only for choice B in §8:** for enc_dec, take the lowest free slot id instead of LRU (2540-2547). Batch order follows slot order (3252-3262).

### 3.6 Sizing

MADLAD-400-3B-MT, from its config.json (the GGUF was not checked): d_model 1024, 32 decoder layers, 16 heads × 128, n_positions 512.

- **Encoder store:** at most `n_seq_max × n_ubatch` rows × 4 KiB.
  - `-np 7 -ub 512`: 3,584 rows = 14 MiB on the host in stage 1; 16 MiB on the device in stage 2 (the extra region).
  - Louise's segments are at most 96 tokens (deploy/madlad-translate/server.py:107), so about 7 × 96 rows are live.
- **Decoder KV:** one shared pool. `--kv-unified-per-slot N` bounds each slot and sizes the pool as `n_parallel × N` (common/arg.cpp:1652-1659; server.cpp:178-185).
  - With `max_new = min(512, 2 × longest + 16)` (translator-choice.md), a 96-token segment needs ≤ 209 cells. So N = 256, pool = 1,792 cells.
- **Compute per decode step (computed):**
  - Re-projecting cross K/V costs 2 × 32 × 1024 × 2048 × 2 = 268 MFLOP per encoder row (t5.cpp:170-173). A 60-token segment is therefore 16 GFLOP per step, against about 2.9 GFLOP for the rest of the decoder per token.
  - Row selection keeps this equal to what seven sequential runs would do. Batching saves weight reads, not this.
- **Reserve when `n_seq_max > 1`** (computed, D3): K/V projections 2 × 28 MiB, and without flash attention a cross kq of 117 MiB at `-ub 512`. `-np 1` is unchanged.

## 4. Why no other model changes, by code path

| Change | Why only T5 reaches it |
|---|---|
| New memory class | Only `case LLM_ARCH_T5:` constructs it. Every other arch reaches the same branch and constructor as before. T5ENCODER (its own enum value), EAGLE3 and DFLASH keep their memories (llama-model.cpp:3756-3771). |
| Forced `kv_unified` | Guarded by `arch == T5 && n_seq_max > 1 && !kv_unified`. Every such configuration aborts at its first decode today (llama-kv-cache.cpp:1808) or at reserve. The one non-aborting case, a T5 context used only to encode, gets a larger `n_ctx_seq` and no change in math. |
| Encode edit | Inside the existing `model.arch == LLM_ARCH_T5` guard (llama-context.cpp:1741). Memory-less embedders (BERT family, llama-model.cpp:2782-2799) reach `encode()` through decode's fallback (llama-context.cpp:1822-1825) and never enter it. |
| `FAILED_INPUT` | Returned only by the new class. `combine`, `is_fail` and both switches give the same results for the four existing values, and none of those switches has a `default` (`-Wswitch` flags a missed case). |
| Graph inputs, reserve | Only src/models/t5.cpp calls `build_inp_cross_embd` and `build_attn_inp_cross` (t5.cpp:121, 127). |
| State | Only the new class writes the suffix, and no version macro moves. |
| Stage 2 virtual | Non-pure with a nullptr default. Every other memory still passes `mctx = nullptr` to `process_ubatch` (llama-context.cpp:1649), and failure cleanup sits under `if (mctx_enc)`. |
| Server | Everything is inside P3's `enc_dec`. `common/` is unchanged. |

A lone T5 sequence gets the same bytes by construction: the same `v_embd` bytes in one `[n_embd, n_enc]` tensor, the same mask, the same reserve at `-np 1`. Measure it anyway (§6 T4).

Outside llama.cpp's own logic, a rebuilt backend is a new engine identity for qwen3, bge-m3, granite and medcpt-query. Invariant 12 applies (§6 T11).

## 5. The alternatives

| Option | Pros | Cons |
|---|---|---|
| **Status quo: P3, one slot (`parallel:1`)** | Deployed shape. Each translation is alone, so it is reproducible per translation. No new carry. | ~5-6 s per question (7 languages in series). `llama_cross` is still never saved and never freed. |
| **One ENCODE carrying all 7 prompts, then batched decode** (no library change) | Correct today: rows are tagged per sequence (llama-context.cpp:1751-1763), and the mask pairs them. | Not slot-compatible: any later encode overwrites every in-flight sequence. Needs new server code anyway. Encoder shapes differ from a lone run. |
| **D1, P4-min:** `llama_cross` per sequence plus a `llama_kv_cache` subclass holding a pointer to it | Smallest diff (~520 lines). No graph change. A lone sequence's bytes are identical by construction. | A memory pointing into its context, valid only by member order. Its behaviour depends on the base's internal virtual calls. No row selection, so a sequence's bytes depend on idle slots' rows. Still a host copy. Would be rewritten as D2 for upstream. |
| **Recommended: D2 stage 1 plus the §3 grafts** | Uses llama.cpp's own abstractions: no public API, a memory that owns the store, a context subclass, arch dispatch. Seq ops, state and the prompt cache carry the output. Row selection isolates a sequence from idle rows. | ~900 lines. A new internal status touches shared switches (no-ops for others). A copied split loop can drift. Still a host copy. Hunks in hot graph files. |
| **D2 stage 2 / D3: device store** | Answers slaren's persistent-buffer requirement (PR #11213 r1969892524) and ggerganov's "no host copies" (#22728). Removes sync, memcpy and the per-step upload. | Widens `llama_memory_i`. `apply()` runs before compute, so keeping the old output on failure needs a post-compute commit (open). `set_rows`/`get_rows` into F32 not checked on SYCL or ROCm. ~1150 lines. |
| **Stage 3: cached cross K/V** | Removes the 268 MFLOP per row per step recompute. A sequence's K/V no longer depend on what else is in flight. | 0.9-1.75 GiB at `-np 7 -ub 512` (computed). Its bits must be measured again. Worth it only if T10 shows the recompute dominates. |
| **Upstream PR #17956** (public `llama_get/set/clear_cross_state`, server-side concatenation) | Exists. Handles several streams. | Exposes the encoder output to the user, against the 2024-06-14 guidance (#5763). Unreviewed since 2025-12, self-described as "vibe coded". |
| **Upstream PR #27113** (removes the `equal_seqs` assert) | Tiny change. 4 NLLB translations in 4.6 s against ~8 s serial, as reported. | Does not stop encodes from overwriting each other. Unreviewed. |
| **Separate encoder and decoder contexts** (ggerganov's 2025 plan) | The maintainer's stated ideal (#12181, 2025-03-18). | Never built, no design. The store would still need a home, and this memory would fit there unchanged. |
| **Keep MADLAD on torch (louise-python)** | Batches already (length-sorted batches of 32, server.py:107-135). ~3.2 s per query. | 17-38 s cold load and 7.2 GB on the card. A measured bit-exact result for it was not found in approved-encoders.md. |

## 6. Tests that prove it

- **T1, library regression** (tests/test-llama-archs.cpp, T5 random weights, CPU). T5 runs with encode there today (:1062), but the multi-sequence checks skip encode (:1117).
  - (a) Encode seq 0, then seq 1 in a separate call, then decode seq 0. Its logits must be bitwise equal to a run with seq 0 only. Today they are NaN or zero.
  - (b) Decode seq 0 and seq 1 in one batch: each within nmse 1e-4 of its solo run. Bitwise equality is reported, not required.
  - (c) After a whole `seq_rm(1)`, decoding seq 1 returns `-1` with no abort, and seq 0 is unaffected.
  - (d) A partial `seq_rm(0,2,-1)` keeps the encoder output: re-decoding equals the first run.
  - (e) After `seq_cp(0,2)`, seq 2's next logits equal seq 0's.
  - (f) State: `state_seq_get` from seq 0, clear, `set` into seq 3, decode equals the original. `PARTIAL_ONLY` size equals the KV-only size. A truncated suffix leaves seq 3 empty in both parts.
  - (g) After `llama_memory_clear`, decode returns `-1`.
  - (h) `n_seq_max=4` with `kv_unified=false` creates the context and decodes two sequences.
- **T2, alone equals among 7 (the byte-identity gate).**
  - Setup: on the card, with the deployed flags and env (docs/notes/measuring.md §6; harness `llama-intel-arc-b70`), over the 490-question set.
  - For each question: run each language alone on an idle `-np 7` server, then all 7 concurrently.
  - Pass only if every item matches byte for byte. Also compare a lone request at `-np 7` with `-np 1`.
  - **Expected to fail**, for three reasons in the code, independent of where the store lives:
    1. The decoder weight matmuls run with 7 columns instead of 1. Kernel choice depends on the column count (CUDA ggml-cuda.cu:1885; SYCL not checked).
    2. T5 self-attention never uses flash attention, because it carries a position bias (llama-graph.cpp:2923; t5.cpp:147). Its softmax sums in 8- or 16-wide chunks (ggml-cpu/vec.cpp:531-551). Interleaved cells in the shared pool move a sequence's scores across chunk boundaries.
    3. The cross softmax runs over the union of the batch's rows.
  - The result decides §8.
- **T3, the same group twice.** The same 7-language group in a fixed order, with lowest slot ids and nothing else on the server, over 3 fresh loads × 2 runs (det_check's shape). Must be byte-identical.
- **T4, one slot unchanged.** `-np 1` patched against the P3 build, 490 questions, byte-identical.
- **T5:** unit test of the store with no model. Covers replace with a row shared by several sequences (examples/batched/batched.cpp:128-136), `seq_cp` onto a destination that already has rows, `seq_keep`, compaction order, and the `n_seq_max × n_ubatch` bound.
- **T6:** status unit test. `FAILED_INPUT` in `combine` and `is_fail`; the four old values give unchanged results.
- **T7:** full test-llama-archs run over every arch. Pass/fail and nmse unchanged.
- **T8:** examples/batched with a T5 GGUF, `n_parallel 4`, `-kvu` on BOTH builds (the unpatched build asserts without it). Output byte-identical.
- **T9, server.**
  - `-np 7` with no `-kvu` loads, logs the forced unified KV and one "speculative decoding not supported", and logs no error.
  - A prompt longer than `n_ubatch` is refused.
  - `n_cmpl` children produce translations of the parent's input.
  - Over 1,000 requests memory stays flat, and after a burst the store's rows and the KV cells return to 0.
- **T10, before stage 3:** decode-step time against `n_enc` (8 against 120 rows, one slot) and with 7 slots, on the card.
- **T11, invariant 12:** the four bit-identity tests and the calibration gates for qwen3, bge-m3, granite and medcpt-query on the rebuilt backend, before it serves anything.

## 7. Size, rebase risk, upstreaming

**Size.**
- Stage 1 is about 900 lines including tests (D2's estimate, not counted from code). Most of it is in new files. The grafts add tens of lines.
- The server delta on top of P3 is the seven changes in §3.5.

**Rebase risk.** Upstream commits in the 90 days to 2026-10-27, counted by the cost review through the GitHub API: llama-model.cpp 90, server-context.cpp ~63, llama-graph.cpp 54, llama-context.cpp 53, llama-graph.h 28, llama-kv-cache.cpp ~25. To keep the carried patch cheap:
- Only add lines. Leave `llama_cross`, `llm_graph_params::cross` and the last term of `allow_reuse` (llama-graph.h:806, 908) in place. Delete them only in the upstream PR.
- The split loop copied into `init_batch` drifts silently, so T1 pins it.
- Carry it as `deploy/localai/patches/` P4 beside 0003, recorded in docs/notes/localai-bugs.md.

**Upstreaming: what to post, and where.**
1. **An issue on ggml-org/llama.cpp first** (CONTRIBUTING.md:34), written by the operator: AI-written issue and PR text is not accepted (CONTRIBUTING.md:19-25).
   - The facts: encodes overwrite each other (llama-context.cpp:1740-1764), and `-np N` without `-kvu` aborts.
   - Cite #5763 (2024-06-14 guidance), #11213 r1969892524, 6412d61, #26565 (closed stale), #17956 and #27113.
   - The proposal: the §3 shape, with no public API.
   - Four questions: memory or a separate encoder-decoder context; forced unified KV or a refusal; stage-2 failure semantics; a state suffix without a version bump.
2. **A comment on #17956 and #27113** pointing to the issue (CONTRIBUTING.md:21-22 asks contributors to work with an existing PR's author).
3. **PRs, CPU first, each with T1:**
   - PR 1: library stage 1.
   - PR 2: device store.
   - PR 3: tools/server encoder-decoder support (P3 plus §3.5). Upstream still lists this as a TODO (#5763, 2024-07-04).
   - PR 4 (optional): cached cross K/V, with T10's numbers.
   - Upstream commits carry `Assisted-by:`, not `Co-authored-by:` (AGENTS.md:96).

## 8. Open questions

**For the operator: the unit of reproducibility for concurrent translation.** This is whether one translation must come out byte-identical whatever else is translated beside it, or only when the same question is translated again the same way.

| Choice | Effect | Pros | Cons |
|---|---|---|---|
| A. Per translation: keep `parallel:1` (status quo) | ~5-6 s per question | Reproducible per translation. The library work still fixes state and children. | No speed-up for Louise. |
| B. Per question group: one question's languages run together in a fixed order, lowest slot ids, one group at a time on that server | expected 2-3 s, not measured | Reproducible when the same question is reprocessed (T3). Like the torch backend, which translates in batches. | A language's translation depends on which other languages were asked with it. A second user's question waits for the group. Needs §3.5 item 7 and a client-side order. |
| C. Fixed per-sequence shapes: several KV streams with per-stream pos_bucket and cross mask, fixed encoder regions | might give A's guarantee with concurrency | The strongest guarantee, if it holds. | Not designed and not measured. The weight matmuls still run 7 columns. The largest change, against open upstream TODOs (llama-kv-cache.cpp:1808, llama-graph.cpp:1134). |

Recommendation: B, measured first (T2 and T3). If T3 passes, a question's languages are reproducible as a group, as the torch backend's batches already are. If the operator says nothing, A stays, and no concurrent translation is deployed.

**Technical, not yet answered:**
- Whether the store belongs in `llama_memory` or in an encoder-decoder context. No maintainer has said; #26565 asked and was closed stale.
- ~~Stage-2 encode failure.~~ **Settled 2026-10-28 (P6):** write-beside plus a post-compute commit — the encode's context assigns a free region, `llama_context::encode` calls the memory's `enc_commit` after the compute; the commit is a memory-specific call under the T5 guard, which the upstream PR must either keep or turn into an interface method (a question for the issue).
- Whether upstream wants refusal rather than forcing for `-np > 1` without `-kvu`, and whether it accepts a T5-only state suffix with no version bump.
- ~~To check before P3 itself deploys: whether the shared-prefix grouping (server-decision.cpp:1270-1290) can make two same-language segments parent and child under P3.~~ **Answered 2026-10-27.** `server_decision_group_tasks` groups tasks in chunks of `n_slots`, and a chunk of one never has children (`end - i < 2`). P3 runs with `parallel:1`, so it never groups. A multi-slot stage must turn the grouping off for encoder-decoder targets: a child copies its parent's decoder state and never encodes its own prompt.
- Not checked: MADLAD GGUF `n_ctx_train`, the deployed `n_ubatch` and flash-attention setting; `ggml_set_rows`/`get_rows` into F32 on SYCL and ROCm; the column-count kernel choice on SYCL.
- ~~Whether the cross K/V recompute dominates step time on the card (T10).~~ Measured 2026-10-27: it does not. Time per output token goes ×1.10 from 7 to 192 input tokens (~170 ms per token), so stage 3 is low value and `-np 1` cannot reach the speed target. Batching can (localai-bugs.md P3, "On the card").
- No public predicate for "encoder-decoder with cross-attention" exists. P3 keeps matching the arch string until upstream adds one.

## 9. Implementation plan (saved for later, operator 2026-10-27)

*The operator asked to save this plan for a future implementation and to
propose a simpler MADLAD-only step (translator-choice.md, "Waves").
Then, the same day: keep things as they are; the llama.cpp improvement is
done in a separate session. MADLAD stays on torch. Nothing below is built.
Each stage ships only after its gate.*

*2026-10-27 night, that session: the server-only alternative to stage 1 —
translator-choice.md "Waves" — was built as P4 (localai-bugs.md), reviewed
against this note: it needs no library change, no public symbol, and gives
decision B of §8 (per question group) by construction, the batch following
the wave's content order. Stage 1 (the per-sequence memory) and stage 2
remain the upstream path; the measurements of T2–T4 run on the card through
tools/measure/t5waves.py.*

| Stage | What | Where | Gate before it ships |
|---|---|---|---|
| 0 | **Measure the cross recompute (T10).** One slot, P3 test backend: the same output length from a short input and a long input. Time per token against `n_enc` decides between stage 3 and concurrency. | read-only, the node | none: a measurement |
| 1 | **Per-sequence encoder memory**: `llama_memory_enc_dec` (T5 only, created in `create_memory`), plus the §3 grafts: `kv_unified` forced in cparams; encode after the server's `seq_rm`; shared-prefix grouping off for encoder-decoder targets. Only adds lines. **BUILT 2026-10-28** (operator: "once this stage is consolidated implement stage 1 and 2"): `deploy/localai/patches/staged/0005-llama-cpp-encoder-decoder-memory.patch`, localai-bugs.md P5 (staged), with the deviations listed there. T1 (a–h), T5, T6 and the P4 server checks pass on the CPU; T2 as predicted not bitwise, within nmse 1e-4. | staged beside the series until its card gates | T3, in-wave vs alone, a lone request against P4's bytes, seconds per question, the retrieval comparison; T11 does not apply (the T5 backend is its own, the embedders' is not rebuilt) |
| 2 | **Device store**: `init_encode`, a persistent buffer, `ggml_set_rows`/`get_rows`. Settle the failure semantics first (§8). **BUILT 2026-10-28** (operator's order with stage 1): `deploy/localai/patches/staged/0006-llama-cpp-encoder-decoder-store.patch`, localai-bugs.md P6 (staged). Failure semantics settled as write-beside plus a post-compute commit: the encode's context assigns the lowest free of `(n_seq_max + 1)` regions, `llama_context::encode` commits after the compute, so a failed encode keeps the old output. T1, the store unit test and the server checks pass on the CPU; a lone request stays byte-identical to P3. | staged beside the series with stage 1 | `set_rows`/`get_rows` F32 on SYCL (test-backend-ops on the node); the stage 1 gates, one rebuild |
| 3 | **Cached cross K/V** per layer. Removes ~268 MFLOP per encoder row per step. | P5, or upstream PR 4 | T10 shows the recompute dominates; 0.9–1.75 GiB fits beside the node's models; T2–T4 again |
| U | **Upstream.** An issue written by the operator (CONTRIBUTING.md:19-25: no AI-written issue or PR text), citing §7's references; then PRs, CPU first, each with T1. | ggml-org/llama.cpp | the operator writes the issue |

**What stays true whichever stage runs:**
- Every rebuild of the shared llama.cpp backend is a new engine for every SYCL embedder. It is measured bit-identical (invariant 12) and gated before it serves. Nothing is re-embedded when it passes.
- MADLAD replaces the torch path only after translator-choice.md's comparison on the 451 + 39 queries.
- The client change (one `/v1/completions` per segment, the `<2xx>` prompt, transformers' `clean_up_tokenization_spaces`) belongs to the architecture session.
