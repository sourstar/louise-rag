# LocalAI + VeGa on an AMD card under Docker Desktop (Windows)

The dev PC's RX 9060 XT (gfx1200), 2026-10-18. The Louise node itself is
`deploy/docker/` with an env file pointing here (see `compose.yaml`).

1. **Image** — LocalAI's ROCm build plus librocdxg and the dxg amd-smi:
   `docker build -f deploy/localai-desktop/Dockerfile.localai -t louise-localai:rocm deploy/localai-desktop`
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

**After every deploy** (once München's full suite has passed the commit —
docs/notes/STATUS.md, "the deploy gate"): `sh deploy/localai-desktop/smoke.sh
<env file> <sha>`. It checks only what this machine adds: one image in
worker, web and VeGa; the web at localhost and at the Tailscale address
peers dial; the worker's first round, run check and tracebacks; every
collection's copy state; LocalAI's backends; the card through /dev/dxg and
librocdxg; invariant 12 in every card model's YAML AND live (three
identical requests and a reversed order, bit-identical, through Louise's
own client); the pinned peers from the worker; the socket count
(dht-flood.md); free space on D:. Exit 1 on any FAIL.

**When the card stops answering** (Perugia, 2026-10-21: the encoder hung
at 06:15Z; LocalAI's restarts of the backend did not help; after a LocalAI
restart `amd-smi` failed with AMDSMI_STATUS_UNKNOWN_ERROR and the model fell
back to the CPU — a run the wrapper now refuses, e77c255). Windows' System
log showed nothing: the failure was inside WSL's /dev/dxg path. Pause the
collections that embed here first (`louise node pause <collection>`), then:
restarting Docker Desktop alone leaves the VM up and the card failed;
`wsl --shutdown`, then start Docker Desktop. Check `amd-smi static` sees
the card. If the web gives empty replies afterwards (wslrelay holding its
port from the old VM), restart the web container. Then rerun the smoke.

**What it does not have yet:** louise-python (MADLAD, MedCPT on torch
ROCm, 23.8 GB image, not pushed), so the chat model translates and pubmed's
MedCPT models are absent. LocalAI's GPU panel names the card "AMD GPU" (a
string LocalAI hardcodes); its VRAM figures are real (the `rocm-smi` shim).
