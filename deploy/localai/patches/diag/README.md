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
| `0005-llama-cpp-sycl-cpu-ops-diag.patch` | `GGML_SYCL_CPU_OPS=OP[:cond]...,...` makes the SYCL device decline those ops, so the scheduler runs them on the CPU: a wrong answer is bisected to an op and a shape class, one model load per class, no rebuild. Conditions: `n1`/`nN` (src1 one / several columns), `f16`/`f32` (src0 type), `c`/`nc` (src0 contiguous or not), `p`/`np` (src1 permuted or not), `k<N>`, `m<N>` (src0 ne[0], ne[1] at most N). Never `SET_ROWS` (writes the device cache). | localai-bugs.md P4, "On the card" (2026-10-28) |
