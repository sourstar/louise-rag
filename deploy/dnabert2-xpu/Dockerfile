# DNABERT-2 embedding server on an Intel Arc GPU (see ../dnabert2/server.py —
# the SAME server, one variable different: DEVICE resolves to xpu here).
#
#   docker build -t dnabert2-embed-xpu:local -f deploy/dnabert2-xpu/Dockerfile .
#
# Needs `--device /dev/dri`. Runs as root, so no group_add is required — the
# llama.cpp SYCL container on this host does exactly the same.
#
# WHY THIS PPA AND NOT INTEL'S OWN REPO. The compute runtime must be new
# enough for the card. Intel's `repositories.intel.com/gpu ubuntu noble
# unified` ships 25.18, which installs without error, reports nothing wrong,
# and enumerates ZERO devices on an Arc Pro B70 — `clinfo -l` silently empty,
# torch.xpu.is_available() False, and a misleading "Can't initialize Level
# Zero Sysman" warning to send you the wrong way. The kobuk-team PPA ships
# 26.x and works; it is also where llama.cpp's own server-intel image gets
# its runtime, which is how this was found. Verified 2026-08-20:
#   torch 2.13.0+xpu · Intel(R) Arc(TM) Pro B70 Graphics · 34.2 GB · 112 TFLOPS fp16
#
# No intel-extension-for-pytorch: XPU has been built into torch since 2.5.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates \
        python3 python3-venv \
 && add-apt-repository -y ppa:kobuk-team/intel-graphics \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        intel-opencl-icd libze-intel-gpu1 libze1 \
 && apt-get purge -y software-properties-common \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# A venv rather than --break-system-packages: Ubuntu 24.04's Python is
# externally managed, and torch+xpu is a large tree to tangle with apt's.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# torch from the XPU index; flash-attn/triton deliberately absent, exactly as
# in the CPU image — the model's remote code falls back to standard attention
# when they are missing, which is the path that works on every device.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/xpu \
 && pip install --no-cache-dir "transformers<4.50" einops fastapi "uvicorn[standard]"

ENV HF_HOME=/data
ENV DEVICE=xpu
WORKDIR /srv
COPY deploy/dnabert2/server.py .

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
