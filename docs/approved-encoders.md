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

## Adding a device class

New peers with other hardware (AMD ROCm/Vulkan, NVIDIA CUDA, Apple Metal)
extend this file with their own section after passing the same two-step
procedure against a node whose space they intend to share. The consistency
bar is absolute: a build that cannot reproduce the space's vectors at
cos ≥ 0.999 is not a slower encoder, it is a different function.
