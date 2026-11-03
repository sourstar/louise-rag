# Staged patches — built and tested, not yet in `series`

A patch here has passed its CPU tests and waits for its CARD gates before it
joins `series` (CLAUDE.md: a stage ships only after its gate; a rebuilt
backend is a new engine, gated once). `tests/test_localai_patches.py` ignores
this folder: nothing here is applied by `build.sh`, and no release digest
names it. To build a test backend with one: append its path, relative to
`deploy/localai/patches` (`staged/0006-llama-cpp-encoder-decoder-memory.patch`, then `staged/0006-…`),
to a LOCAL, uncommitted copy of `series`, then `build.sh` as usual. When its
gates pass, `git mv` it up one level, add its line to `series`, and rename its
entry in docs/notes/localai-bugs.md from "P6 (staged)" to "P5" (and P7 alike).

**Not rebased yet (2026-11-02).** Both were written on `v4.12.1` (llama.cpp
`c0fd786`). The series moved to LocalAI `16acc89` (llama.cpp `ddf48f8`), where
0006's llama.cpp patch has 4 rejected hunks: each is rebased onto the series
and passes its CPU tests again before its card gates.

| patch | what | gates before it moves up |
|---|---|---|
| `0008-llama-cpp-chat-cohere-command3.patch` | Command R7B / Command A tool calls parsed by llama.cpp's own Cohere2 MoE parser (localai-bugs.md P8); on the series as it is (`16acc89`, P1–P5), no fuzz | engines rebuilt with it; every live encoder and reranker byte-identical to its stated run on the gate's fixed inputs (else the full re-gate); R7B walked and piloted |
| `0007-llama-cpp-encoder-decoder-store.patch` | stage 2 (on 0005): the store is one F32 tensor on the device, `(n_seq_max + 1)` regions of `n_ubatch` rows; the encoder graph writes its region (`ggml_set_rows`, `init_encode`), committed after the compute; the decoder gathers (`ggml_get_rows`); no host copies | the same run as 0005's; `test-backend-ops -o SET_ROWS,GET_ROWS` on the node: **727/727 OK (2026-10-28)** |
| `0006-llama-cpp-encoder-decoder-memory.patch` | stage 1 of docs/notes/llama-cpp-encoder-decoder.md: the encoder output kept per sequence (`llama_memory_enc_dec`), so a slot is encoded while others decode; on top of P3 and P4 | on the card, the f32-cache MADLAD backend rebuilt with it: T3 (same group twice), in-wave vs alone (bar 90 %), a lone request against P4's bytes, seconds per question; then the retrieval comparison |
