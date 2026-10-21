# NVIDIA support (2026-10-21)

Built before any peer has an NVIDIA card (operator: "sooner or later it will
be"). Nothing here has run on one; the first card's measurement (step 6 of
deploy/localai-nvidia/README.md) is what admits it, as for any engine.

## The defect it closes

VeGa's wrapper read a run's card from DRM fdinfo (/proc/<pid>/fdinfo,
`drm-pdev`). A CUDA process is no DRM client: it holds /dev/nvidia<N>,
/dev/nvidiactl and /dev/nvidia-uvm. A backend computing on an NVIDIA card
therefore read as holding NO card — a CPU run, stated with `driver: cpu`, the
wrong identity for every vector it made. Now a process holding /dev/nvidia<N>
is never read as a CPU run (`ProcFS.card_mib_by_device` → None) and is read
by `device_on_nvidia`, or not stated at all.

## How a run on NVIDIA is read (deploy/vega/vega_wrapper.py, app/vega/nvidia.py)

| what | native Linux | WSL2 (Docker Desktop) |
|---|---|---|
| the card | the minors of the /dev/nvidia<N> the backend holds, mapped to PCI addresses by /proc/driver/nvidia/gpus/*/information | /dev/dxg; the one card `cards` names (more than one: refused) |
| on the card, or not | used memory per card from nvidia-smi (NVML) just before and after the load: a card run moved ≥ 25 % of the weights onto exactly one card; a CPU run ≤ 64 MiB (or 5 %) on every card | the same |
| the driver | the module the card is bound to (`nvidia <version>`, sysfs) | `dxg nvidia <version>` from nvidia-smi |
| the runtime | every CUDA library the backend MAPPED (/proc/<pid>/maps: libcuda, libcudart, libcublas, libcublasLt, libcudnn*, libnvrtc, libnvJitLink), `name sha256:` each | the same |

NVML's process list is not used: inside a container it names host PIDs.
The load's options must agree with the reading (`declared_placement`): a
torch task now declares through its own `device:` option (`device:cuda` a
card, `device:cpu` the CPU) — louise-python keeps `gpu_layers: 0`. A CUDA
context alone costs a few hundred MiB, so a CPU twin shows CUDA no device
(`CUDA_VISIBLE_DEVICES=-1`), as an AMD one does with HIP_VISIBLE_DEVICES.

## The determinism contract (CLAUDE.md invariant 12)

- llama.cpp on CUDA: `parallel:1`; no vendor variable — cuBLAS disallows
  atomics by default. A backend whose name names no vendor (the meta
  `llama-cpp`) is accepted on a card the wrapper READ as NVIDIA.
- torch on CUDA: `LOUISE_TORCH_DETERMINISTIC=1` (torch's deterministic
  algorithms; the servers now also set cuDNN deterministic, benchmark off)
  AND `CUBLAS_WORKSPACE_CONFIG=:4096:8` — without the workspace, torch's
  deterministic mode errors on cuBLAS; the wrapper refuses the run first.
- cuBLAS reproduces only within one toolkit version and SM count: both are
  in the run (the hashed libraries; the card), so a different card or
  toolkit is a new run that passes the calibration gate once.

## Packaging

- `deploy/localai-nvidia/`: LocalAI v4.11.0's CUDA 13 image pinned by
  digest, the toolkit's device reservation, `nvidia-cuda-13` forced as the
  backend capability (`cuda13-llama-cpp`), the node's model YAMLs with the
  CUDA contract (tests/test_localai_nvidia_deploy.py checks each against the
  wrapper's own function).
- `build_vega_package.sh`: `NVIDIA_SMI` → vega.json `nvidia_smi`; the
  package and the staged sources carry app/vega/nvidia.py
  (tests/test_vega_stage_scripts.py: every app module the wrapper imports).
- louise-python `VARIANT=cuda` (requirements-cuda.txt): torch 2.14.0 as PyPI
  ships it, the CUDA 13.0 build, pinned as its first build froze it. Built
  2026-10-21 from the repo's Dockerfile (in a sandbox: the base image from
  Google's Docker Hub mirror, the same digest; the sandbox's TLS CA added in
  a scratch copy only): 10.1 GB, `torch 2.14.0+cu130`, `pip check` clean,
  every task imports; requirements.txt's pins unmoved. Never run on a card.

## Floor

CUDA 13: driver R580 or newer, Turing (7.5) or newer. An older card or
driver needs `cuda12-llama-cpp` (LocalAI's v4.11.0-gpu-nvidia-cuda-12,
sha256:1ee31dce19426ce7948e003bd2143bc88289db984369bdc53179c184d5ad07f9)
and a cu12 torch — not built; asked for when such a card appears.

## Not covered

OpenVINO on NVIDIA (none: MedCPT runs device:cpu); multi-card splits
(refused by design); MIG slices — a process on one holds its whole card's
/dev/nvidia<N>, so a card in MIG mode (`nvidia.mig_enabled`) states no run
until a slice is measured.
