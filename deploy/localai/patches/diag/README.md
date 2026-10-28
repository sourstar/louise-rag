# Diagnostic patches — never in `series`, never on a live backend

Instruments for a build that MEASURES, kept beside the series so a result
can be reproduced. `tests/test_localai_patches.py` deliberately ignores this
folder: a patch here has no `### Pn.` entry and makes no release digest.

To build with one: append its path, relative to `deploy/localai/patches`
(for example `diag/0005-llama-cpp-sycl-cpu-ops-diag.patch`), as the last
line of a LOCAL, uncommitted copy of `series`, then
`sh deploy/localai/llama-cpp/build.sh sycl_f16 <out>` as usual; the series
digest in the tar's name differs from the release's, which is the point.

| patch | what it measures | note |
|---|---|---|
| `0005-llama-cpp-sycl-cpu-ops-diag.patch` | `GGML_SYCL_CPU_OPS=OP[:cond]...,...` makes the SYCL device decline those ops, so the scheduler runs them on the CPU: a wrong answer is bisected to an op and a shape class, one model load per class, no rebuild. Conditions: `n1`/`nN` (src1 one / several columns), `f16`/`f32` (src0 type), `c`/`nc` (src0 contiguous or not), `p`/`np` (src1 permuted or not), `k<N>`, `m<N>` (src0 ne[0], ne[1] at most N). Never `SET_ROWS` (writes the device cache). The declining entry is logged once as a WARNING (the 03c771b88d03 build logged INFO, which the server's log drops; the bytes were the proof). | localai-bugs.md P4, "Measured on the card" (2026-10-28): named the KQ product over the F16 K cache at N ≥ 2 |
| `test-backend-ops-kq-shapes.patch` | A llama.cpp patch (not a LocalAI one): `GGML_TEST_KQ_SHAPES=1 test-backend-ops test -b SYCL0 -o MUL_MAT` runs only the two attention products over an F16 KV cache at the shapes a small batch of sequences reaches (keys 1..256, tokens 1..64, 16 heads of 128). Build a plain checkout of the series' llama.cpp commit with `-DLLAMA_BUILD_TESTS=ON` in the same oneAPI image as build.sh. | the upstream reproducer for that fault; upstream-bugs.md S1 |
