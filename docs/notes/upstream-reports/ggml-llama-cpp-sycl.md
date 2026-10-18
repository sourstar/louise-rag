# ggml-org/llama.cpp (SYCL) — embeddings are not reproducible while oneDNN runs the matmuls

Measured 2026-08-27 on an Intel Arc Pro B70, image
`ghcr.io/ggml-org/llama.cpp:server-intel-b10630`, Qwen3-Embedding-8B-Q4_K_M,
`--embeddings --pooling last`.

**Root cause:**
- With `GGML_SYCL_ENABLE_DNN=1` (the default), matmuls go to oneDNN's
  `jit:gemm:any`. For some prompt lengths, its accumulation order varies
  between launches.
- oneDNN's deterministic mode is off by default and can only be set through
  its API (`dnnl_primitive_attr_set_deterministic`); llama.cpp never sets it.
- `GGML_SYCL_ENABLE_DNN=0` makes every vector bit-identical, at 1.7 % less
  throughput.

**Example:** the same text, 10 requests, serial, cache evicted between them:

| tokens | 9 | 65 | 257 | 512 | 513 | 1024+ |
|---|---|---|---|---|---|---|
| distinct vectors | 1 | 10 | 10 | 1 | 10 | 1 |

The drift is small (worst cosine 0.9999974), but it survives L2 + f16
storage, so stored embeddings cannot be verified by hash.

**Suggested fix:** a flag (or the default for `--embeddings`) that sets
oneDNN's deterministic attribute on the matmul primitives. And mention in
`docs/backend/SYCL.md` what `GGML_SYCL_ENABLE_DNN` costs in
reproducibility.
