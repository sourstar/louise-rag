# LocalAI + VeGa on an NVIDIA card

Ready before any peer has one (2026-10-21); nothing here has run on a card
yet — step 6 is what makes it usable. Design: docs/notes/nvidia-support.md.
The Louise node itself is `deploy/docker/` with an env file pointing here
(see `compose.yaml`).

**Host.** Driver R580 or newer (CUDA 13), a Turing (compute capability 7.5)
or newer card, and either Linux with the NVIDIA Container Toolkit or Docker
Desktop on Windows with WSL2 GPU support. `docker run --rm --gpus all
ubuntu nvidia-smi` must list the card first.

1. **Weights** — byte-identical to the node's (`/mnt/ssd5/apps/models`),
   checked with `sha256sum`, in a folder named by `LOUISE_WEIGHTS`.
2. **The real backend** — start the stack
   (`LOUISE_WEIGHTS=<folder> docker compose -f deploy/localai-nvidia/compose.yaml up -d`),
   then `docker exec louise-localai-localai-1 ./local-ai backends install
   llama-cpp` (the capability is forced to `nvidia-cuda-13`: it installs
   `cuda13-llama-cpp`).
3. **The card's address and nvidia-smi**, inside the LocalAI container:
   `nvidia-smi --query-gpu=pci.bus_id,name --format=csv,noheader` (write the
   address as `0000:01:00.0`, lower case) and `command -v nvidia-smi`.
4. **VeGa's wrapper** in front of it, built from a COMMIT
   (`deploy/vega/stage/`, as on the node):
   ```
   sh deploy/vega/stage/stage_src.sh /tmp/vega-src.tgz HEAD     # unpack as src/
   docker run --rm -v <cache>:/cache -v <src>:/src:ro python@sha256:54c85f3c… \
     sh /src/deploy/vega/stage/fetch.sh
   docker run --rm -v localai_backends:/backends busybox mkdir -p /backends/vega-llama-cpp
   docker run --rm -e PKG=vega-llama-cpp -e BACKEND=cuda13-llama-cpp \
     -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
     -e CARDS='{"0000:01:00.0": "NVIDIA GeForce RTX 4090"}' \
     -e NVIDIA_SMI=/usr/bin/nvidia-smi -e OWNER=0:0 \
     -v localai_backends:/backends -v <src>:/src:ro -v <cache>:/cache:ro \
     python@sha256:54c85f3c… sh /src/deploy/vega/stage/build_vega_package.sh
   ```
   `RUNTIME_PACKAGES` stays unset: on NVIDIA the run states the CUDA
   libraries the backend actually mapped (libcuda, cuBLAS, cudart…), hashed.
5. **louise-python** (jina-v3, DAR-legal-it, MADLAD), optional:
   `docker build -f deploy/localai/backends/louise-python/Dockerfile
   --build-arg VARIANT=cuda -t louise-python:cuda .`, install it as
   `louise-python` (GUI "From URL", `ocifile://`), and wrap it as step 4 with
   `PKG=vega-louise-python BACKEND=louise-python CUTS=context_size` and the
   SAME `SOCKET` (one receipts socket for every package).
6. **Measure, never assume (CLAUDE.md invariant 12)** — before any
   collection embeds here, for every model: `tools/vega_repro_check.py`,
   3 fresh loads × shuffled orders, every vector byte-identical; then
   `hold embedder run --encoder http://<host>:30286 --api vega --model <m>`
   states the run, and the space's calibration gate passes it once.
   Record the result in docs/notes/approved-encoders.md.

**Determinism.** llama.cpp on CUDA: `parallel:1`, no vendor variable (cuBLAS
takes no atomics by default). torch on CUDA: `LOUISE_TORCH_DETERMINISTIC=1`
and `CUBLAS_WORKSPACE_CONFIG=:4096:8` in the model's env. The wrapper refuses
a run that lacks either. A CPU twin shows CUDA no device
(`CUDA_VISIBLE_DEVICES=-1`): a CUDA context alone moves a few hundred MiB.

**Several cards.** Give each model one: `CUDA_VISIBLE_DEVICES: "<index>"` in
its `env`, and every card in `CARDS`. A load split across cards states no run.

**The reranker** (`louise-rerank-bge`) is not behind VeGa and is not
bit-exact on the node's B70; measure it here before trusting it.
