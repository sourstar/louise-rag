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


---

# ggml-org/llama.cpp (SYCL) — F16×F32 batched matmul returns unrelated output when `a` is not contiguous across its batch dims (Arc Pro B70)

*Ready to file 2026-10-28; label `SYCL`. Checked against the tracker the
same day: no match. Nearest: #27771 (bf16 at `bs=[1,3]` with a permuted
operand on an Arc 140T, an oneDNN 2-byte alignment fault, f16 passed there;
closed stale 2026-10-16), #28384 (CONV_2D, CPY, ROLL, FLASH_ATTN_EXT failing
`test-backend-ops` on bmg-g31).*

**Setup.** llama.cpp `c0fd786` (plain, no patches of ours), the SYCL backend
built as `ggml-org/llama.cpp` CI builds `intel-sycl-f16` (oneAPI 2025.3.0,
`GGML_SYCL_F16=ON`), on an Intel Arc Pro B70 (Battlemage, 32 GB), Linux
6.18.52 with the `xe` kernel driver, `GGML_SYCL_ENABLE_DNN=0` (the fault is
the same with oneDNN on). Two user-space GPU runtimes, same binary (sha256
`20a1a019…364f98`), same result:

| runtime | libze_intel_gpu | IGC | Level Zero loader |
|---|---|---|---|
| 26.27 (bundled with the build) | 1.15.39122 | 2.38.5 | 1.27.0 |
| 26.35 (ppa:kobuk-team/intel-graphics, 26.35.39758.17) | 1.17.39758 | 2.41.15 | 1.32.0 |

**1. Upstream's own suite fails.**

```
GGML_SYCL_ENABLE_DNN=0 ./test-backend-ops -b SYCL0 -o MUL_MAT -p "type_a=f16,type_b=f32"
→ 191 OK, 32 FAIL, 60 not supported
```

Every failing case is `n = 1` with `a` a strided view (`k_v > 0`) batched as
`bs=[1,3]`, for example

```
MUL_MAT(type_a=f16,type_b=f32,m=128,n=1,k=1056,bs=[1,3],nr=[1,1],per=[0,1,2,3],k_v=2112): NMSE 1.38e+00 FAIL
```

and the same with `nr=[4,1]`, `m=129`, `k=1057`. The same `k_v` cases with
`bs=[1,1]` pass; the CPU backend passes all of them. `SET_ROWS` and
`GET_ROWS` pass 727/727 (the cache writes and reads are not involved).

**2. The attention shapes of a decoder.** Added cases (patch attached:
`test-backend-ops-kq-shapes.patch`, under `GGML_TEST_KQ_SHAPES=1`) at the
KQ product's shape — `a` = the F16 K cache view, permuted `{0, 2, 1, 3}`,
`b` = the F32 queries, 16 heads of 128, key counts 1…256, token counts
1…64:

```
test_mul_mat(GGML_TYPE_F16, GGML_TYPE_F32, m, n, 128, {16, 1}, {1, 1}, {0, 2, 1, 3})
→ 34 OK, 238 FAIL: every n ≥ 2 fails at every key count, every n = 1 passes
```

for example `m=1,n=2,k=128,bs=[16,1],per=[0,2,1,3]`: NMSE 2.41; `m=2,n=2`:
1.57; `n=64`: FAIL. The output is unrelated, not imprecise, and
deterministic (the same wrong bytes every run).

**Common factor, by the code.** Every failing case reaches
`ggml_sycl_mul_mat_batched_sycl` (ggml/src/ggml-sycl/ggml-sycl.cpp) with an
`a` that is not contiguous across dims 2–3 — the pointer-array
`oneapi::mkl::blas::column_major::gemm_batch` path (`to_fp16_nc_sycl` on `b`,
then `gemm_batch` with `ptrs_src`/`ptrs_dst`). Every passing `n > 1` case
takes the strided `gemm_batch` call. `n = 1` cases of the decoder shape take
the `mul_mat_vec_p021`/`nc` kernels and pass. Unchanged by
`GGML_SYCL_ENABLE_FUSION=0`, `GGML_SYCL_ENABLE_GRAPH=0`,
`GGML_SYCL_ENABLE_OPT=0`, `GGML_SYCL_DYNAMIC_PRECISION=F32` and by the
runtime step 26.27 → 26.35 above — so the suspect is oneMKL's pointer-array
`gemm_batch` under this device, or the pointer/stride setup feeding it, not
the GPU runtime or the compiler.

**What a user sees.** A T5 (MADLAD-400-3B, f16; 16 heads of 128) served by
`llama-server` with `-np 7`: one sequence alone decodes right; two or more
sequences decoding in one batch produce wrong text — the first tokens right,
then divergence, the wrong text following the slot, identical on every
repeat. The same build on the CPU backend is right. `--cache-type-k f32`
alone fixes it (the F32 K view takes the other path); `--cache-type-v f32`
alone does not.

**Open point.** The suite's KQV-shaped case (`per=[0,1,2,3]`, `k_v=512`)
fails, while the model's F16 V side decodes right (the f32 K cache alone
fixed the text). Both views are contiguous across dims 2–3 by the code; the
data differ (random against softmax probabilities). Not isolated.

**Workaround.** `--cache-type-k f32 --cache-type-v f32` (+79 % per decoded
token on this card); or `-np 1`.

**Attachments.** The two `test-backend-ops` outputs per runtime (six files),
`test-backend-ops-kq-shapes.patch`, the model-level reproduction
(`llama-server -np 7` with two concurrent completions against one alone).
