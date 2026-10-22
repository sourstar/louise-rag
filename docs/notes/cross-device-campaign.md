# Cross-device agreement — a test campaign, and what might be published

*Opened 2026-10-22 (operator: "Write it down for a future test campaign. We
might have material to write some papers."). Planning note, nothing decided.
The measured numbers live in [`approved-encoders.md`](approved-encoders.md)
("Bitwise reproducibility, by vendor", "Cross-device bar", "Cross-vendor
deviation reference"); this note does not copy them, it says what to measure
next and why.*

## 1. Why

Louise lets peers on different hardware embed parts of one collection into
ONE space and accept each other's vectors (03 §3.1, 04 §4.5). That rests on
two claims nobody has measured systematically across vendors:

1. **Run to run, one engine is bit-exact** — invariant 12. Measured, not
   assumed: Intel SYCL needs `GGML_SYCL_ENABLE_DNN=0`, AMD ROCm needs
   `ROCBLAS_DEFAULT_ATOMICS_MODE=0`, and `-np > 1` with overlapping requests
   corrupts vectors on any vendor ([`encoder-slot-corruption`] in the
   operator's memory; llama-intel-arc-b70).
2. **Across engines, the vectors agree closely enough that retrieval does
   not change** — today judged by cosine against a reference (the space's
   `tolerance`) and against the card's own CPU twin (the 0.995 review line).

Claim 2 is judged by a proxy. What matters is whether a user's search
returns the same documents, and cosine alone cannot say that for near-ties.

## 2. What we already have (2026-10-22)

- Bit-exact runs over fresh loads and shuffled orders: Intel B70 (SYCL),
  AMD RX 9060 XT (ROCm 7.2 through WSL2 /dev/dxg), both for
  Qwen3-Embedding-4B Q4_K_M and bge-m3 F16 (approved-encoders.md).
- Whole-set cosines, 512 calibration texts, AMD card vs the node's Intel
  reference: document worst 0.996898, query worst 0.997380, 0 of 512 below
  0.995.
- Card vs its own CPU twin (same build, `gpu_layers 0`, HIP shown no
  device), query side: worst 0.995076, median 0.997106 at 100 of 512 texts;
  the full 512 is being measured in idle time (D:\louise-work\qcc3.py on the
  Perugia peer, results in /data/qcc/). Observation worth a paper sentence:
  the ROCm card agrees MORE with the Intel reference (median 0.9990) than
  with its own CPU build (0.9971).
- Defects these measurements exposed, each a lesson for the method:
  - the gate prefixed the query instruction twice (81ff189): the sampled
    worst read 0.9932 where the truth was 0.9974. A gate's number was
    trusted until a direct computation over the whole set contradicted it.
  - a model silently fell back to the CPU after the card's /dev/dxg path
    failed, and stated a NEW run (fixed: e77c255 refuses it). A cross-device
    measurement must check the device of every answer, not of the setup.
  - a 16-text random sample hides the tail; the whole set (512) is the unit
    (86bfbfb prints it).

## 3. Literature context (to verify with citations before writing)

From general knowledge, not yet a literature search:
- quantization studies compare embeddings with an fp32 reference by cosine;
  around 0.99 is treated as near-lossless for retrieval, above 0.995 as
  indistinguishable; Q4 GGUF embedding models often land at 0.98–0.995;
- retrieval benchmarks (MTEB and similar) show third-decimal cosine
  differences do not move benchmark scores;
- GPU non-determinism work treats cross-device differences as floating-point
  noise and measures ranking agreement (top-k overlap, NDCG change) rather
  than fixing a cosine cut-off.
No standard bar exists for "the same model, different hardware". Louise's
0.995 is conservative by that practice. **Task for the paper: a proper
search and citations** (quantization, reproducible inference, embedding
interchangeability).

## 4. The campaign — what to measure

**Matrix.** Engines: Intel B70 SYCL, AMD 9060 XT ROCm, NVIDIA CUDA (when the
NVIDIA peer exists), each with its CPU twin; Vulkan only if a peer runs it.
Models: Qwen3-Embedding-4B Q4_K_M (asymmetric, instruction on queries),
bge-m3 F16 (symmetric), granite. Optional axis: quantization (Q4_K_M, Q8_0,
F16) on one engine, to separate quantization error from device error.

**Optional row: Qualcomm (mobile / Snapdragon X).** Candidates, from general
knowledge (verify before planning): llama.cpp's OpenCL backend on the Adreno
GPU (Qualcomm-contributed upstream; same GGUF weights and the VeGa wrapper
approach — the realistic path), llama.cpp's Hexagon NPU backend (newer,
embedding coverage partial), ONNX Runtime's QNN provider / Qualcomm AI
Engine Direct (ONNX weights, a different engine family). No vendor
reproducibility mode is known (invariant 12): measure bit-exactness first,
and expect a larger gap on the NPU (int8/fp16 compute). The operator's
Motorola Edge 50 Ultra (Snapdragon 8s Gen 3, Adreno + Hexagon) is on the
tailnet: a phone-GPU data point for the paper, not a production encoder.

**Precondition per cell:** the engine is bit-exact run to run
(`tools/vega_repro_check.py`, 3 fresh loads × shuffled orders), and every
answer's device is recorded. A cell that is not bit-exact is reported as
such and excluded from agreement statistics.

**Metrics, per pair of engines:**
1. cosine per text over the whole set: worst, p1, median, count below
   0.995 / 0.999 (what we do today);
2. **neighbour agreement**: for N queries, the top-10 documents retrieved
   from a fixed corpus sample with each engine's vectors; Jaccard@10 and
   rank-biased overlap; Kendall τ on the top-100;
3. **task effect**: recall@k / NDCG@10 against the query campaign's graded
   expectations (tests_campaign/, query-campaign.md) — does the engine pair
   change which tests pass;
4. mixed spaces: a corpus whose vectors come half from engine A and half
   from B, queried from each — the real Louise case (04 §4.5), and the one
   the literature probably does not cover.

**Data.** The 512 calibration texts (both roles); the query campaign's
questions; a fixed seeded sample of gutenberg chunks as the retrieval corpus
(enough for stable top-10s, e.g. 100k chunks).

**Harness to build:** a rank-agreement tool beside
`tools/cross_device_check.py` (embeds a query set and a corpus sample on two
engines, writes the four metrics); everything else exists.

## 5. Possible papers

- **"Bit-exact embedding inference on consumer GPUs"** — the vendor modes,
  what breaks determinism (heuristic kernels, atomics, slots), how to
  measure it, and the cost of the deterministic modes.
- **"Is a vector a vector? Cross-vendor agreement of LLM embeddings for
  shared retrieval"** — the matrix above, cosine vs ranking agreement, the
  mixed-space result, and a principled bar to replace a fixed 0.995.
- **"Verifying a peer-produced vector space"** — the calibration gate,
  challenge rows, statistical vs deterministic checks (03), and the
  measurement pitfalls in §2 (gate numbers trusted over the whole set,
  silent CPU fallback).
