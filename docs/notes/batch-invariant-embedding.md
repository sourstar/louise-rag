# Identical batching for the encoders: what exists (2026-11-01)

*Operator, 2026-11-01: "I agree with 1 now and 2 as an option if 1 fails.
Check about 2 what's available and under discussion; do not reinvent the
wheel."*

1. **Now:** batches on today's llama.cpp SYCL engine, admitted where the
   gate finds them byte-equal to one text at a time, then checked on the
   rows actually written. Wikipedia has run at batch 16 since 2026-10-31,
   with 4,000 written rows compared and 0 differing (scheduling-log 5e34cc0).
   Wikidata follows.
2. **If 1 ever fails:** batching that is identical *by construction*.
   Below is what exists for that, checked on 2026-11-01.

## What exists

| where | what | status | fits us? |
|---|---|---|---|
| **llama.cpp** (our engine) | PR #16016, "Deterministic inference mode (CUDA)": RMSNorm with a fixed reduction, matmul with fixed tiling and no split-K, attention with a fixed KV split, `--deterministic` | **Draft since 2025-09-15, not merged, CUDA only.** A maintainer (JohannesGaessler): "I don't want to maintain guarantees for bit-for-bit identical results as the batch size is varied"; one slot without prompt caching is the supported way to get identical bytes, which is how we run today | No SYCL code. Its design would be ours to port to ggml-sycl and to carry as a patch; an upstream PR would go against the stated position |
| llama.cpp server README | "logits are not guaranteed to be bit-for-bit identical for different batch sizes" | documented | the reason the gate checks it |
| llama.cpp issue #28656 (CUDA) | text embeddings identical *within* one batch, but a batched answer and a solo answer differ | closed as stale, 2026-10-25 | the same property our gate measures |
| **vLLM** | batch invariance mode, `VLLM_BATCH_INVARIANT=1`, beta (tracking issue #27433). Intel XPU support: #41934, then #49209 (Triton matmul and linear kernels, merged 2026-09-02), with `TRITON_ATTN` as XPU's attention under it. Intel CI on **Arc Pro B60** (Battlemage, the B70's family): the batch-invariance tests pass (#64396, draft, active 2026-11-01) | maintained, upstream, on our card family | **No pooling or embedding test under batch invariance**: bge-m3 (encoder-only, non-causal attention) is unverified. vLLM serves embedding models on Arc Pro B-series (Intel, 2025-11); bge-m3 appears in Intel's llm-scaler fork, not in upstream's XPU list |
| Thinking Machines `batch_invariant_ops` | PyTorch library: batch-invariant Triton kernels for `mm`, `addmm`, `log_softmax`, `mean` | the origin of vLLM's and SGLang's modes | LayerNorm and attention are not covered as listed; a torch encoder would still need them checked |
| SGLang | deterministic inference on these kernels | CUDA backends (FlashInfer, FA3, Triton) | no XPU found |

**Cost seen elsewhere:** Thinking Machines measured 26 s for the default path
against 55 s for its unoptimised batch-invariant one, and 42 s once the
attention kernel was improved. SGLang reports a ~34 % throughput loss on
average. Even so, these modes stay far faster than one text at a time.

## The order, if 1 fails

1. **vLLM's batch-invariant mode on the B70 for bge-m3.** It is the
   existing, maintained route, with Intel CI on the same GPU family. To
   measure:
   - (a) it serves bge-m3 on XPU with `VLLM_BATCH_INVARIANT=1`;
   - (b) the gate's batch check over many random compositions (sizes 1–64,
     mixed lengths), not one;
   - (c) the rate against today's.

   If it passes, it is a new ENGINE: the VeGa wrapper learns the backend,
   and the run is gated like any (invariant 1: the engine is outside
   `space_id`, and a validated run contributes).
2. **Only then: PR #16016's design ported to ggml-sycl**, as our own
   patch. Fixed tiling, no split-K, fixed KV split. Measured the same way;
   carried across llama.cpp updates.

Sources:
- [llama.cpp PR #16016](https://github.com/ggml-org/llama.cpp/pull/16016)
- [llama.cpp issue #28656](https://github.com/ggml-org/llama.cpp/issues/28656)
- [vLLM PR #49209](https://github.com/vllm-project/vllm/pull/49209)
- [vLLM PR #64396](https://github.com/vllm-project/vllm/pull/64396)
- [vLLM batch invariance](https://docs.vllm.ai/en/latest/features/batch_invariance/)
- [Intel: vLLM on Arc Pro B-series](https://vllm.ai/blog/2025-11-11-intel-arc-pro-b)
- [intel/llm-scaler vLLM README](https://github.com/intel/llm-scaler/blob/main/vllm/README.md/)
- [SGLang deterministic inference](https://docs.sglang.io/docs/advanced_features/deterministic_inference)
