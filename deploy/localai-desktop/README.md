# LocalAI + VeGa on an AMD card under Docker Desktop (Windows)

The dev PC's RX 9060 XT (gfx1200), 2026-10-18. The Louise node itself is
`deploy/docker/` with an env file pointing here (see `compose.yaml`).

1. **Image** — LocalAI's ROCm build plus librocdxg and the dxg amd-smi:
   `docker build -t louise-localai:rocm deploy/localai-desktop`
2. **Weights** — byte-identical to the node's (`/mnt/ssd5/apps/models`:
   `llama-embed/`, `granite/`, `bge-m3/`, `chat/`), checked with `sha256sum`,
   in a folder named by `LOUISE_WEIGHTS`.
3. **The real backend** — `rocm-llama-cpp`, into the backends volume once:
   start the stack, then `docker exec louise-localai-localai-1 ./local-ai
   backends install llama-cpp` (the capability is forced to `amd`) and
   restart the container.
4. **VeGa's wrapper** in front of it, built from a COMMIT
   (`deploy/vega/stage/`, as on the node):
   ```
   sh deploy/vega/stage/stage_src.sh /tmp/vega-src.tgz HEAD     # unpack as src/
   docker run --rm -v <cache>:/cache -v <src>:/src:ro python@sha256:54c85f3c… \
     sh /src/deploy/vega/stage/fetch.sh
   docker run --rm -v localai_backends:/backends busybox mkdir -p /backends/vega-llama-cpp
   docker run --rm -e PKG=vega-llama-cpp -e BACKEND=rocm-llama-cpp \
     -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
     -e CARDS='{"0000:03:00.0": "AMD Radeon RX 9060 XT"}' \
     -e RUNTIME_PACKAGES='["hip-runtime-amd", "hsa-rocr", "rocm-core", "rocdxg-roct"]' \
     -e AMD_SMI=/opt/rocm-wsl/bin/amd-smi -e OWNER=0:0 \
     -v localai_backends:/backends -v <src>:/src:ro -v <cache>:/cache:ro \
     python@sha256:54c85f3c… sh /src/deploy/vega/stage/build_vega_package.sh
   ```
   The card's address comes from `amd-smi static --bus` inside the
   LocalAI container (`source /opt/rocm-wsl/.env.sh` first).
5. `LOUISE_WEIGHTS=<folder> docker compose -f deploy/localai-desktop/compose.yaml up -d`

**Determinism (CLAUDE.md invariant 12).** Every model here carries
`ROCBLAS_DEFAULT_ATOMICS_MODE=0` in its `env`; the wrapper refuses a card
run without it. Measured on this card: bit-identical run to run, across
fresh loads, for bge-m3 F16 and Qwen3-Embedding Q4_K_M
(docs/notes/approved-encoders.md, 2026-10-18).

**What it does not have yet:** louise-python (MADLAD, MedCPT on torch
ROCm, 23.8 GB image, not pushed), so the chat model translates and pubmed's
MedCPT models are absent. LocalAI's GPU panel names the card "AMD GPU" (a
string LocalAI hardcodes); its VRAM figures are real (the `rocm-smi` shim).
