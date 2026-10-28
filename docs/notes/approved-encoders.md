# Approved encoder builds, by device class

The backend gate (app/verify/backend_gate.py) protects each collection
behaviorally — an endpoint that stops reproducing its recorded baseline is
refused writes. This file is the complementary *procurement* list: which
encoder builds have been **proven same-function and not-slower** on which
hardware, so an operator (or a future peer) picks a known-good build
instead of trusting a moving tag. A moving tag once silently corrupted
714 books on the reference deployment; everything here is pinned by digest.

## Approval procedure (what "approved" means)

A candidate build is run beside the production one on the same hardware,
same model file, same flags, and must pass BOTH:

1. **Consistency** — cosine ≥ 0.999 between candidate and current vectors
   on ≥16 production-shaped texts (the same-function bar genesis import
   uses). This is what protects the pinned embedding spaces.
2. **Throughput** — an interleaved A/B benchmark (alternating batches, so
   shared-GPU drift cancels); the candidate must not be >10% slower.

`scratchpad: encoder_trial.py` (kept with the deployment reference) runs
both and prints a verdict. On adoption the app is re-pinned to the new
DIGEST and the row below is added; on rejection the build is listed under
Rejected with the reason. The worker's backend gate then independently
confirms baseline reproduction on the first embed after the swap.

## Intel GPU (SYCL) — Arc Pro B70 (Battlemage), llama.cpp `server-intel`

| build | digest | verdict | evidence |
|---|---|---|---|
| b10603 | `sha256:33927f8a9af7657469e5b3a65027a1b2d0419f185385cc82f7be59790590a74d` | **approved (in production, 2026-08-24)** | trial vs b10450 on the Arc, Qwen3-Embedding-8B-Q4_K_M: cosine min/mean **1.000000** over 16 texts; interleaved A/B **+1.5% faster** (1.95 vs 1.92 texts/s under a live embed workload) |
| b10450 | `sha256:5a47f527e1b06b0351bf43e9b2f3baf14a06b097526cc37d2cdd9c77de9a3ec2` | approved (superseded by b10603) | served gutenberg's space until 2026-08-24; baseline reproduced across restarts |
| **b10630** | `sha256:37dd060b3616c35a7f1b9ff8684a3ebcb59b57cf5e40b0d3f578d209279f3fc7` | **UNAPPROVED — found running in production 2026-08-28** | never trialled. Tag `server-intel-b10630`, started 2026-08-27T21:46:45Z, `restarts=0`. Note the transposition: the approved build is b106**03**, this is b106**30** |

> **The drift this file exists to prevent happened again, and this time it
> is measured.** On 2026-08-28 the production Arc encoder was serving
> `server-intel-b10630` — a build that appears in no row above. Against
> gutenberg's stored vectors, `tools/vector_compliance gutenberg` found
> **13 of 163 sampled chunks (8.0%) below TAU**, worst cosine **0.338**,
> median 1.000000. Every failure was a 4,000-char chunk, and they cluster
> by book (4643 and 4639 at 21% bad; 4646, 4642, 4640 at 0%).
>
> The corrupt side is the STORED vector, not the comparison: for each
> failing chunk, six fresh draws agreed with each other at cosine
> 1.000000 and disagreed with the stored one at an identical cosine every
> time. So these vectors were written wrong at some earlier point and have
> been sitting in the table since.
>
> **What could NOT be reproduced on demand**: the endpoint tested clean
> afterwards at every concurrency (1/4/8), with identical and with
> distinct long texts, serially and in parallel — all 1.000000. Short
> tests do not catch this, which is precisely why the approval trial (16
> texts) and the backend gate (a handful of probes) both pass while the
> corpus takes damage. Treat "the endpoint tests clean" as necessary and
> NOT sufficient; the corpus-side check is
> `python -m tools.vector_compliance <collection>`.

> **Still pinned to b10630 on 2026-09-10.** Read from the node while
> starting step 6: `midclt call app.config embed-gpu-qwen3-4b` gives
> `/services/llama-embed/image =
> ghcr.io/ggml-org/llama.cpp:server-intel-b10630`, on port `30437:8080`,
> which is the endpoint `config/collections/gutenberg.toml` names for
> `qwen3-embedding-4b`. The app is STOPPED, so nothing is being written
> through it — but step 6 cannot create a collection against it, and
> [`step6-preflight.md`](step6-preflight.md) item 0 is that gate.
>
> The approved b10603 image is already present on the node and its digest
> matches the row above exactly, so the fix is a re-pin and a restart —
> no pull, no trial. It needs the operator: `app.update` is blocked for
> the assistant. The Arc is held by a raw `dnabert2-xpu` container and
> fits one model server at a time, so that stops first.

<!-- encoder_trial verdicts are appended here -->

## AMD GPU (Vulkan) — RX 9060 XT (RDNA4), llama.cpp Windows binary

Second instance (Windows machine). Native Windows process — ROCm-in-WSL2
does not cover RDNA4; containers reach it via `host.docker.internal`.
Flags replicate the production app exactly (`--embeddings --pooling last
-ngl 999 -c 65536 -np 4 -b 8192 -ub 8192`).

| build | artifact | verdict | evidence |
|---|---|---|---|
| b10603 | `llama-b10603-bin-win-vulkan-x64.zip` (same tag as the production SYCL build) | **approved (2026-08-24)** | cross-backend consistency vs the production Arc encoder, Qwen3-Embedding-8B-Q4_K_M (GGUF byte-copied from the node): cosine min **0.999599** / mean 0.999681 over 16 long texts, dim 4096; local throughput 0.39 s/text idle (~5x the Arc under load). **Capacity flags sized for the 16 GB card** (`-c 36864 -np 4 -ub 2048`, function unchanged — re-verified min **0.999607** / mean 0.999684): the production `-c 65536` copied verbatim oversubscribed VRAM (18.2 GB committed of 16) and the driver's paging froze every slot ~9 s per ~30 s. Rule of thumb: per-slot context ≥ the recipe's EMBED_CTX is all the function needs; size `-c` = slots × EMBED_CTX (+margin) to the card, never to another card. |

## CPU (TEI) — text-embeddings-inference

| model | image | verdict |
|---|---|---|
| BAAI/bge-m3 | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8` | approved (wikivec/wikipedia; same-function proven against the Upstash archive by genesis import) |
| ncbi/MedCPT-Article + Query | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8` | approved (pubmed) |

## CPU (custom) — DNABERT-2 FastAPI server

| model | image | verdict |
|---|---|---|
| DNABERT-2-117M | `dnabert2-embed:local` | approved for parse/index era; embedding parked pending dna-search-axes review |

## Bitwise reproducibility, by vendor

Context for the consistency bar above, which is cosine and stays cosine. Each
vendor scopes its bitwise guarantee to its own hardware, so bitwise agreement
cannot be a cross-peer property.

| stack | bitwise mode | scope of the guarantee | control |
|---|---|---|---|
| NVIDIA cuBLAS | yes | same architecture **and same SM count**, single stream, same toolkit version | `cublasSetAtomicsMode` — atomics disallowed by default, nothing to set for llama.cpp (`parallel:1`); torch adds `CUBLAS_WORKSPACE_CONFIG=:4096:8`. The run states the card and the hashed CUDA libraries (docs/notes/nvidia-support.md); NOT YET MEASURED — no peer has a card |
| AMD rocBLAS | yes | identical GFX target ISA, single HIP stream, same ROCm version | `ROCBLAS_DEFAULT_ATOMICS_MODE=0` |
| Intel oneDNN | yes | "on a given platform" | `set_deterministic` — API only, no env var |
| Intel oneMKL | CNR | fixed thread count; GPU since 2024.1, BLAS-3 under OpenMP offload | `MKL_CBWR` |
| CPU (llama.cpp) | none offered | — | — |
| torch (`louise-python`: XPU today, CUDA elsewhere) | by calls, not a vendor env var | the same device and torch build. **MEASURED 2026-10-20, jina-v3 on the B70 (torch 2.14.0+xpu, tree -20261020b), torch's DEFAULTS: 204 of 512 calibration texts bit-identical over 2 fresh loads × 2 shuffled passes (2,048 vectors, 2,952 s); the 308 that differ form blocks by text (0–241, 448–511, 257, 273), so it is the input, not chance — NOT reproducible unset.** **With `LOUISE_TORCH_DETERMINISTIC=1` (tree -20261020c, window 1,024, node paused): the DOCUMENT model 512 of 512 bit-identical over 2 fresh loads × 2 shuffled passes (2,048 vectors, 974 s) — REPRODUCIBLE; the query model's run follows.** The texts that differed unset were the SHORT ones (0–241 average 48 tokens, 448–511 average 35; the long block 242–447, average 5,967 tokens, was identical but for two): small shapes reach kernels whose reduction order is not fixed by default. DAR-legal-it, the same stack: 1/4 bit-identical against itself on 2026-10-17 (max abs 8.2e-8), and its calibration fingerprint moved across three byte-identical trees (6d2c61d9…, 193d1cc8…, 6283455f…) while MedCPT's (OpenVINO) stayed 0eb32880… across four — the same switch is in its task now (deploy/dar-legal-it/server.py) and MEASURED next (operator, 2026-10-20). To notify upstream: upstream-bugs.md V1 | `torch.use_deterministic_algorithms(True)` + `torch.backends.mkldnn.deterministic` (oneDNN's `set_deterministic`), switched on by the MODEL's env `LOUISE_TORCH_DETERMINISTIC=1` (deploy/jina-embeddings-v3/server.py) so the stated run carries it; CUDA adds `CUBLAS_WORKSPACE_CONFIG=:4096:8`. Measured first with it unset, then set — louise-python-rebuild.md §4 |

**2026-10-20, night: ANY backend, fail-closed (operator).** "A backend that
does not produce bit exact results upon reprocessing the same data cannot be
used under any circumstances"; "VeGa wrapper MUST ensure determinism for ANY
backend". The audit of every model Louise uses found seven without their
stack's mode: the reranker, chat, index-translate, MADLAD, DAR-legal-it (whose
node tree predated its switch) and the two OpenVINO models. Measured on the
node with a fresh LocalAI load each time (det_check: the same input, three
fresh loads, two orders, compared as exact float reprs or text bytes):

| model | settings | result |
|---|---|---|
| louise-rerank-bge (bge-reranker-v2-m3 F16, stock `intel-sycl-f16-llama-cpp`) | `parallel:4`, no DNN switch | NOT bit-exact: the order moved 16 of 20 scores |
| the same | `GGML_SYCL_ENABLE_DNN=0`, `parallel:1`, `cache_ram:0`, applied by a LocalAI RESTART (the YAML is read at startup; an edit plus `/backend/shutdown` reloads the OLD config) | **BIT-EXACT**: 20 of 20 over 3 loads × 2 orders, batched and one candidate per request (56 ms a candidate) |
| louise-chat-qwen3-8b (stock SYCL) | DNN=0, `parallel:1`, `cache_ram:0`, greedy (`top_k:1`, temperature 0) | NOT bit-exact: the first answer after a load differs from the later ones (from token ~25), and the later ones agree. `cache_prompt:false` in the request changes nothing |
| index-translate-9b | DNN=0, `parallel:1`, `cache_ram:0` | bit-exact over 3 loads × 2 (an empty answer to a chat probe; the real path still to measure) |

**2026-10-21, behind VeGa's fail-closed wrapper** (determinism-switch.sh, then
one LocalAI restart per batch of YAMLs). Each row: 3 fresh loads, shuffled or
reversed orders, every score or token compared as bytes.

| model | settings | result |
|---|---|---|
| louise-rerank-bge, `vega-llama-cpp` (tokenizer probe), ctx 16,384 | DNN=0, `parallel:1` | NOT bit-exact: 1 of 20 scores, first pass after 2 of 3 loads |
| the same, `vega-llama-cpp-gen` (no probe) | the same | NOT bit-exact |
| the same | + `GGML_SYCL_ENABLE_OPT/GRAPH/FUSION=0`, flash attention off | BIT-EXACT, 3 runs of 3 × 2; 13–17 s for 50 candidates, 5.3 GB at ctx 16,384 (kept as `louise-rerank-bge-strict`, ctx 2,560) |
| **louise-rerank-bge, ctx 2,560, batch/ubatch 2,560** | DNN=0, `parallel:1`, flash attention ON, OPT/GRAPH/FUSION default | **BIT-EXACT**: 20 × 3 × 2, and 50 real gutenberg candidates × 3 loads × 2 shuffled orders on two queries; 1.8–2.2 s for 50, 0.9–1.2 GB. The oversized context was what did not reproduce. In production since 2026-10-21 with the platform's window cut (05 §5.6, 2,048 tokens) |
| louise-chat-qwen3-8b, `vega-llama-cpp-gen` | DNN=0, `parallel:1`, `cache_ram:0`, greedy | NOT bit-exact (as before: the answers after the first differ) |
| the same | + `GGML_SYCL_ENABLE_OPT=0` | **BIT-EXACT**, 3 runs of 3 loads × 3 answers. Every switch off (OPT/GRAPH/FUSION=0, flash attention off) is NOT, 3 of 3 runs |
| index-translate-9b, `vega-llama-cpp-gen` | DNN=0, `parallel:1` | BIT-EXACT, 3 loads × 3 greedy answers (a real translation) |
| **louise-translate-madlad** (MADLAD-400-3B f16 GGUF, `vega-llama-cpp-t5` = series f8b4acdcdcb8, f32 KV cache, `parallel:7`, waves; live since 2026-10-28) | DNN=0, `cache_type_k/v: f32` | reproducible PER WAVE COMPOSITION: the same question group twice 625/626 (the one row differs when the group splits into different waves under load); a lone translation gives P3's F16-cache bytes 372/379 (the cache type moves 7 last bits); in-wave vs alone 614/626. Measured with the gates of tools/measure/t5waves.py (localai-bugs.md P4) |
| vega-dar-legal-it, louise-python-20261021a | `LOUISE_TORCH_DETERMINISTIC=1` | **BIT-EXACT**: 32 of normattiva's texts (24 spread + the 8 longest) × 3 fresh loads × 2 shuffled passes, 192 vectors; 20 of 20 re-embedded vectors equal to a fresh encode (float16) |
| vega-jina-v3 and vega-jina-v3-query, louise-python-20261021a (2026-10-27, before wikidata's re-creation; `tools/vega_repro_check.py --n 64 --loads 3 --repeats 2 --window 1024`) | `LOUISE_TORCH_DETERMINISTIC=1` | **BIT-EXACT**: 64 of the platform's calibration texts × 3 fresh loads × 2 shuffled passes, 384 vectors, each side |
| vega-medcpt-article-ov, louise-python-20261021b (OpenVINO) | `LOUISE_BITEXACT_MEASURED=louise-python-20261021b` | **BIT-EXACT**: 32 PubMed (title, abstract) pairs × 3 fresh loads × 2 shuffled passes, 192 vectors |
| louise-rerank-medcpt, the same tree | the same | **BIT-EXACT**, 20 × 3 × 2 |

Generative llama.cpp models (chat, translate) and rerankers need the
`vega-llama-cpp-gen` package (`cuts: context_size`): the tokenizer probe of
`vega-llama-cpp` embeds a text at load, and that killed a chat model's child
(exit 1, LocalAI "connection refused").

The rule is now code. `deploy/vega/vega_wrapper.py` `determinism_lacks`
applies on ANY device:
- llama.cpp needs `parallel:1`, and on a card its vendor's mode; Vulkan, or a
  card run naming no known vendor, is refused;
- louise-python's torch tasks need `LOUISE_TORCH_DETERMINISTIC=1` (on an
  NVIDIA card also `CUBLAS_WORKSPACE_CONFIG=:4096:8`, 2026-10-21); its
  OpenVINO tasks are refused;
- any other family is refused.

louise-python's dispatcher refuses the same for a model the wrapper is not
in front of. Every model Louise uses goes behind the wrapper
(deploy/16f/determinism-switch.sh). OpenVINO stays refused until it is
measured bit-exact over loads and orders. MedCPT's fingerprint did stay
0eb32880… across four trees, which says it may pass.

Notes:

- No vendor claims agreement across different hardware. NVIDIA's lapses even
  between its own models, since SM count differs.
- IEEE 754-2019 requires correct rounding for `+ − × ÷ √` but only
  *recommends* it for `exp`, `log`, `tanh`. Softmax and the activation use
  those, so cross-vendor bitwise identity is unreachable without emulating the
  special functions.
- Measured on the reference deployment: the same model file and the same
  llama.cpp build produce different vectors on its GPU backend and its CPU
  backend, and the CPU backend does not reproduce its own output run to run.
  That last clause is about **llama.cpp's** CPU backend and has not been
  re-measured. A DIFFERENT CPU stack was measured on 2026-09-09 and does
  not behave that way: `embed-cpu-bge-m3` (TEI, BAAI/bge-m3, dim 1024),
  seven texts from 61 to 16,920 characters, five rounds — self-cosine
  **1.000000000** on every text, three of the seven bit-identical and the
  rest differing only at float64 machine epsilon, which is the cosine's
  own rounding rather than the encoder's. So "CPU is unstable" is not a
  property of CPU inference; it is a property of a particular build, and
  the llama.cpp case is the one still to be re-measured.
- **Re-measured 2026-10-18, the dev PC's RX 9060 XT** (LocalAI v4.11.0,
  `rocm-llama-cpp` = ROCm 7.2.1, Docker Desktop over /dev/dxg;
  llama-intel-arc-b70's gate pool, `capture.py` + a byte compare of f32):
  - bge-m3 F16 (its matmuls are rocBLAS `gemm_ex`, f16 in, f32 out —
    traced with `ROCBLAS_LAYER`): **15/15 chunks bit-identical over 120
    vectors** (2 fresh loads x 4 shuffled repeats), with atomics allowed AND
    with `ROCBLAS_DEFAULT_ATOMICS_MODE=0`, and the two settings give the SAME
    bytes. The mode costs nothing here and makes the guarantee the vendor's
    rather than this shape's luck; invariant 12 requires it.
  - Qwen3-Embedding 4B and 8B Q4_K_M: llama.cpp's own quantised kernels,
    no rocBLAS call at all; 16/16 bit-identical.
  - **llama.cpp's CPU backend** (the same build, `gpu_layers: 0`): 16/16
    bit-identical. The "does not reproduce its own output" clause above
    does not hold for this build.
  - The native Windows Vulkan build (b10603, `-np 4`, the PC's encoder
    until today): 14/16, two chunks at 0.99983. Vulkan offers no mode.
  - GPU against CPU, same build and options (Qwen3-4B): worst 0.9964, a
    uniform precision gap at every length — the cross-device question,
    which a deterministic mode does not and cannot close.
- **The node's SYCL LocalAI runs WITHOUT `GGML_SYCL_ENABLE_DNN=0`**
  (2026-10-18: not in the container's env, any model's YAML, or any
  backend process). The deployed llama-server config had it
  (llama-intel-arc-b70 `embed-xpu-qwen3-8b.yaml`); the move to LocalAI
  dropped it. Invariant 12 makes that a refused run. Fixed by the P2
  rollout the same evening (every llama.cpp `vega-` model sets it).

**Cross-device bar: the CPU is the reference, 0.995 the review line**
*(operator, 2026-10-18).* An engine on a card is compared, text by text,
with a CPU twin of the same build and options (`gpu_layers: 0`,
`no_op_offload:true`, the device hidden — `gpu_layers: 0` alone still
offloads matmuls). A cosine **below 0.995 sends that engine to further
review**; at or above it the precision gap between devices is accepted.
This is a review line, not the space's `tolerance` (which calibration
sets, 03 §3.1), and it says nothing about run-to-run identity, which is
invariant 12 and must be exact.

*Measured 2026-10-18, request 1* (bge-m3 F16, all 512 calibration texts,
VeGa window 1024, LocalAI v4.11.0 + P1/P2 `louise-rocm-llama-cpp`, RX 9060
XT; captures in `D:\louise-captures` on the dev PC):

| | result |
|---|---|
| card, run to run | 512/512 bit-identical, 2 fresh loads x 2 shuffled orders |
| CPU twin, run to run | 512/512 bit-identical, 1 load x 2 orders |
| **card vs CPU twin** | worst **0.998362**, p1 0.999702, median 0.999999; below 0.999: 1 text; below 0.995: **none** — passes |
| `tools/vega_cut_scan` at 1024 | 0 of 512 refused (3 before P2) |
| granite, Qwen3-4B, Qwen3-8B, bge-m3 (`tools/vega_repro_check`) | 16/16 bit-identical each |

**Cross-vendor deviation reference** *(operator, 2026-10-21: "once we have
the numbers for Nvidia we can set a deviation threshold as a reference").*
The threshold is SET from this table once its NVIDIA row is measured, not
before: the worst honest cross-vendor deviation per model and role, with its
margin stated. Each row costs nothing extra: `hold embedder check
<collection>` embeds all 512 calibration texts and prints the `whole set`
worst and median against the space's reference (the calibration member,
made on the node's B70); the card-vs-own-CPU-twin column is the 0.995 review
line above. Until then the space's `tolerance` is calibration's and 0.995
stays the review line.

| engine | model | role | vs the reference, 512 texts: worst / median | vs its own CPU twin: worst / below 0.995 | run to run |
|---|---|---|---|---|---|
| Intel SYCL, Arc Pro B70 (München) | — | — | the reference | — | bit-identical |
| AMD ROCm 7.2, RX 9060 XT (Perugia, 2026-10-18) | bge-m3 F16 | document | not recorded | 0.998362 / none | 512/512 |
| AMD ROCm 7.2, RX 9060 XT (Perugia, 2026-10-21) | Qwen3-Embedding-4B Q4_K_M (gutenberg) | document | 0.996898 / 0.999151, 0 of 512 below 0.995 | — | 16/16 over 96 vectors |
| the same | the same | query | 0.997380 / 0.998991, 0 of 512 below 0.995 | 242 of 512 texts (2026-10-22): worst 0.991561, median 0.997205, **1 below 0.995** (#233, 98 chars, Hebrew), 7 below 0.996 — reviewed: the card stays (see below) | the same |

The query row's first reading, the gate's 0.993243 / 0.996924, measured the
instruction sent TWICE (fixed 2026-10-21, "the gate and the re-check send the
query instruction once"): gate numbers on a query side with an instruction
from before that commit are not this table's. The card is closer to the
Intel reference (median 0.99899) than to its own CPU build (0.99699).

*The review of 2026-10-22 (the twin's one reading below 0.995).* The four
lowest twin readings are short texts in scripts other than Latin or with
compounding: #233 Hebrew, 98 characters, 0.991561; #39 Hebrew, 79, 0.995076;
#126 German, 198, 0.995494; #223 Korean, 53, 0.995542. Script-dependent,
not length-dependent: in a few tokens, last-token pooling carries the
whole text, so a small kernel difference shows. Against the B70 reference
the same card is within 0.995 on all 512 query texts (worst 0.997380). The
odd one out is the CPU build, not the card, so **the card's query engine
stays validated**. The twin column stays a review line, not a gate: one
reading below it sends a human to look, as it did here.
| NVIDIA CUDA 13 | — | — | no card yet (deploy/localai-nvidia, README step 6) | — | — |

*Published numbers, searched 2026-10-21 (operator: "check online"):* no
published measurement of an NVIDIA card against the CPU for an embedding
model, on llama.cpp or torch, was found. The nearest:
- llama.cpp's own backend test (tests/test-backend-ops.cpp) holds every
  backend, CUDA included, to the CPU backend PER OPERATION: normalised MSE
  ≤ 1e-7 by default, ≤ 5e-4 for matrix multiplication (with a comment that
  1e-7 is "too small for Vulkan"). For unit vectors NMSE 5e-4 is cosine ≥
  0.99975 per op, which is our arithmetic and not a measurement. It is a
  per-op pass bar, not a model-level number: errors compound over layers.
- Verifiable-inference work measures hidden states, not embeddings, and
  between GPUs: VeriLLM (arXiv 2509.24257) reports Apple M4 against an RTX
  5090 for Qwen2.5-7B, mean ε about 0.002–0.009 (read from a search
  summary; arXiv is unreachable from this environment). Across A100, L40S
  and H100, IEEE FP32 GEMM was bitwise identical and TF32 broke it
  (arXiv 2609.25624).
- The large embedding gaps reported in issues come from versions,
  pooling and tokens, not hardware: Ollama nomic-embed-text across versions
  cosine 0.57–0.84, and 0.996 against a reference (ollama#14449);
  Qwen3-Embedding with the wrong end token, cosine 0.55 (mlx-serve#909).
So the NVIDIA row is measured here, or not at all.

**What this means for `tolerance`, which is the number it feeds.** The
loosest HONEST deviation measured anywhere in this project is 4x10^-4 —
Intel SYCL against AMD Vulkan, two different ISAs and two different
kernel sets. Same-backend, same-stack is zero to machine epsilon. Against
that, `tolerance` at 0.95 leaves 125 times more room than the worst
honest case has ever needed, and 03 §3.1 is explicit about what that
room costs: "the tolerance window is also the widest perturbation an
adversary can hide in, which is why it is kept as tight as calibration
allows". 0.95 is not as tight as calibration allows; it is a round number
in the middle of the empty gap between 0.9996 and the 0.7 where a
different model lands. Tightening it sharpens T3 and T4, the tiers that
actually carry detection — T0's envelope is unaffected either way,
because it measures distribution and not deviation. The measurement that
should set it is the llama.cpp CPU case above, since it is the only
honest backend anyone has claimed is noisy.

Sources: [cuBLAS](https://docs.nvidia.com/cuda/cublas/) ·
[rocBLAS](https://rocm.docs.amd.com/projects/rocBLAS/en/docs-6.4.0/how-to/what-is-rocblas.html) ·
[oneDNN deterministic mode](https://uxlfoundation.github.io/oneDNN/dev_guide_attributes_deterministic.html) ·
[oneMKL CNR](https://www.intel.com/content/www/us/en/docs/onemkl/developer-guide-linux/2023-0/set-env-var-for-cond-numerical-reproducibility.html) ·
[Defeating Nondeterminism in LLM Inference](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/) ·
[Bit-Exact AI Inference Verification](https://arxiv.org/abs/2606.00279)

## Known limits of the Arc Pro B70 (2026-10-28)

What the card, its driver (`libze-intel-gpu1 26.05.37020.3` in the LocalAI
image, 26.18.38308.1 in RAGE's) and our stacks are known NOT to do right:
our own measurements first, then upstream's reports (checked 2026-10-28;
the curated list is github.com/riceharvest/awesome-b70). Each row: what
fails, what it touches here, the workaround, where it is recorded.

| limit | symptom | exposure here | workaround | record |
|---|---|---|---|---|
| oneDNN gemm heuristic (llama.cpp SYCL) | the same input gives different bytes run to run | every llama.cpp model | `GGML_SYCL_ENABLE_DNN=0` in the model's env, measured | invariant 12; "Bitwise reproducibility" above |
| torch fp32 softmax at row lengths 513–768, 1025–1280, 1537–1792 | finite, normalised, UNRELATED vectors, the same every run | jina-v3's eager attention (replaced by SDPA); any torch model with an eager fp32 softmax over such lengths | SDPA, or a half softmax | gpu-silent-wrong-vectors.md; upstream-bugs.md V2; intel/compute-runtime #966 names the same bands (torch-xpu-ops #4772) |
| torch's defaults on short inputs | 308 of 512 calibration texts not reproducible across loads | louise-python models | `LOUISE_TORCH_DETERMINISTIC=1` | upstream-bugs.md V1 |
| **F16 KV cache with two or more sequences decoding in one batch (llama.cpp SYCL)** | the decoded text is WRONG (first tokens right, then divergence, following the slot); alone right; the CPU right; deterministic | MADLAD waves (P4). No live model decodes several slots on this card: chat and translate run `parallel:1`, the embedder and reranker only process prompts | `cache_type_k`/`cache_type_v: f32` in the model YAML (R0, correct; its price measured paused: 29.2 against 16.3 ms per token, +79 %; still 1.12 s per question in waves against torch's 3.2 s; 2× cache memory); the op is NAMED: the KQ product (upstream-bugs.md S1) | localai-bugs.md P4 "Measured on the card"; nothing upstream yet |
| an oversized context (reranker ctx 16,384) | 1 of 20 scores differs on the first pass after a load | the reranker | ctx 2,560, the platform's window | the "by vendor" table above |
| the chat model's first answer after a load | differs from the later ones from token ~25 | louise-chat-qwen3-8b | none; the use is unaffected, the run is not bit-exact | the same table |
| a non-finite vector, occasionally | NaN | embeddings | `embeddings._numeric_failure` retries | gpu-silent-wrong-vectors.md §1 |
| LocalAI's memory reclaimer | memory held by another process evicts every model, one per 500 ms | the shared card | pinned off | localai-bugs.md B4 |
| **upstream, not seen here** | reorder/OPT corruption of quantized models (llama.cpp #21893; `GGML_SYCL_ENABLE_OPT=0`); fused XMX dequant-GEMM NaNs since #29107 for K- and IQ-quants (#30892, fixed by #30604; else `GGML_SYCL_XMX_GATHER_TYPES=0`) — F16 weights never enter that path; flash-attention corruption (#19276 on an Xe2 iGPU; #30624 sparse FA at long context) — T5 cannot use FA; device-lost and reset storms under sustained load (compute-runtime #966; `SYCL_UR_USE_LEVEL_ZERO_V2=0`, PCIe Gen4, a 1,000 W PSU); compute-runtime 26.27 with a level-zero loader before 1.32 segfaults llama.cpp (#972); hybrid models' gibberish (#24168); no scaling with parallel slots (a speed limit: Jonathan Mann's B70 benchmarks) | our models are F16 and never quantized; our driver is 26.05 | pin the driver to a measured version; keep `parallel:1` where bit-exactness is required | this row |

## Adding a device class

New peers with other hardware (AMD ROCm/Vulkan, NVIDIA CUDA, Apple Metal)
extend this file with their own section after passing the same two-step
procedure against a node whose space they intend to share. The consistency
bar is absolute: a build that cannot reproduce the space's vectors at
cos ≥ 0.999 is not a slower encoder, it is a different function.
