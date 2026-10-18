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
| NVIDIA cuBLAS | yes | same architecture **and same SM count**, single stream, same toolkit version | `cublasSetAtomicsMode` |
| AMD rocBLAS | yes | identical GFX target ISA, single HIP stream, same ROCm version | `ROCBLAS_DEFAULT_ATOMICS_MODE=0` |
| Intel oneDNN | yes | "on a given platform" | `set_deterministic` — API only, no env var |
| Intel oneMKL | CNR | fixed thread count; GPU since 2024.1, BLAS-3 under OpenMP offload | `MKL_CBWR` |
| CPU (llama.cpp) | none offered | — | — |

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

## Adding a device class

New peers with other hardware (AMD ROCm/Vulkan, NVIDIA CUDA, Apple Metal)
extend this file with their own section after passing the same two-step
procedure against a node whose space they intend to share. The consistency
bar is absolute: a build that cannot reproduce the space's vectors at
cos ≥ 0.999 is not a slower encoder, it is a different function.
