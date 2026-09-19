# RAGE — the RAG Engine: one image for every model (docs/notes/rage.md).
#
#   docker build -f deploy/rage/Dockerfile \
#     --build-arg LLAMA_CPP_IMAGE=ghcr.io/ggml-org/llama.cpp@sha256:<digest of server-intel-b10630> \
#     --build-arg LLAMA_SWAP_SHA256=<sha256 of the release archive> \
#     -t <registry>/rage:<build> .
#   docker push <registry>/rage:<build>      # then deploy BY DIGEST, never by this tag
#
# TRIAL-BUILT LOCALLY 2026-09-19, never pushed: all steps build, and the front
# ran end to end inside the image over the real llama-swap with a stub model
# (docs/notes/rage.md §17). On that day `server-intel-b10630` was
#   ghcr.io/ggml-org/llama.cpp@sha256:37dd060b3616c35a7f1b9ff8684a3ebcb59b57cf5e40b0d3f578d209279f3fc7
# NOT YET PUBLISHED OR RUN ON THE NODE. Migration step 13d builds it there,
# publishes it by digest and runs docs/notes/rage.md §14. What only that can
# settle is marked PROOF below.
#
# THE IMAGE IS PUBLISHED, by digest, where a stranger can pull it. Every
# Codex created on this node freezes this image as its engine (02 §2.4: "an
# engine whose image is named by digest and obtainable"), so a local-only
# image would make every such collection unverifiable by construction.
#
# THE BASE is the Intel llama.cpp image the node's GPU encoders already run
# from: Ubuntu 24.04 with the Level Zero / OpenCL runtime that drives the
# Arc Pro B70, and /app/llama-server. deploy/medcpt-rerank already runs
# OpenVINO on this base, on this card. torch's XPU wheels bundle their own
# SYCL runtime (libsycl.so.9 and its libur_loader, under /usr/local/lib),
# and the base's LD_LIBRARY_PATH puts oneAPI 2025.3's OLDER libur_loader in
# front of it: `import torch` died on "undefined symbol: urDeviceWaitExp"
# (found by building this image, 2026-09-19). So a torch model DECLARES
# `LD_LIBRARY_PATH = "/usr/local/lib"` in rage.toml — for its own process,
# inside its run identity, and nowhere else: llama-server keeps the base's
# path. Built and imported: torch 2.14.0+xpu. PROOF, still: that torch then
# sees the card through this base's Level Zero loader. The children are
# separate processes, so their oneAPI libraries never share an address
# space; the loader and the driver are what they share.
#
# Pinned by DIGEST at build: a moving `:server-intel` tag once cost a
# multi-day vector-corruption mystery (DEPLOYMENT.md).
ARG LLAMA_CPP_IMAGE
FROM ${LLAMA_CPP_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# What the run identity calls the compute runtime (09 §9.12, `driver`): the
# package versions, written once here and read back at start — no package
# manager runs in the container (app/rage/runs.py, driver_string).
RUN mkdir -p /etc/rage \
 && dpkg-query -W -f='${Package} ${Version}\n' libze-intel-gpu1 intel-opencl-icd libze1 \
      > /etc/rage/runtime-versions \
 && cat /etc/rage/runtime-versions

# llama-swap: the process runner, and nothing more (docs/notes/rage.md §4).
# The version is the one app/rage/swapyaml.py writes its yaml for; a bump
# re-reads that module against the new docs/config.example.yaml. The
# checksum has no default on purpose: a build that does not say which bytes
# it expects does not get any.
#
# VERIFIED 2026-09-19 against the release itself: v256 ships
# `llama-swap_256_linux_amd64.tar.gz` with `llama-swap` at its root, and its
# published checksum — which the downloaded bytes hash to — is
#   557757336cde1a667f0b200d4a84c48c9f5fdf561fd8ae969a2bed9d8c94ec48
# That binary was then run on the yaml app/rage/swapyaml.py renders
# (docs/notes/rage.md §17): it is the value to pass, not a default.
ARG LLAMA_SWAP_VERSION=256
ARG LLAMA_SWAP_SHA256
RUN test -n "${LLAMA_SWAP_SHA256}" || { echo "pass --build-arg LLAMA_SWAP_SHA256=<sha256 of the llama-swap release archive>"; exit 1; } \
 && curl -fsSL -o /tmp/llama-swap.tgz \
      "https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz" \
 && echo "${LLAMA_SWAP_SHA256}  /tmp/llama-swap.tgz" | sha256sum -c - \
 && tar -xzf /tmp/llama-swap.tgz -C /usr/local/bin llama-swap \
 && rm /tmp/llama-swap.tgz && llama-swap -version

# ONE TORCH, THREE PIN SETS (docs/notes/rage.md §9). torch is tied to the
# compute runtime above, so there is one. Each server keeps the pins it was
# proven with, in a venv of its own that sees the system torch:
#   madlad    transformers==4.46.3 — 5.x loads the checkpoint wrongly and
#             translates everything to `ě ě ě` (node, 2026-09-18)
#   dnabert2  transformers<4.50, einops — its remote code's fallback attention
#   medcpt    openvino + tokenizers, and no torch at all
RUN pip3 install --no-cache-dir --break-system-packages \
      torch --index-url https://download.pytorch.org/whl/xpu
RUN python3 -m venv --system-site-packages /opt/venv-dnabert2 \
 && /opt/venv-dnabert2/bin/pip install --no-cache-dir \
      "transformers<4.50" einops fastapi "uvicorn[standard]"
RUN python3 -m venv --system-site-packages /opt/venv-madlad \
 && /opt/venv-madlad/bin/pip install --no-cache-dir \
      "transformers==4.46.3" accelerate sentencepiece protobuf
RUN python3 -m venv /opt/venv-medcpt \
 && /opt/venv-medcpt/bin/pip install --no-cache-dir \
      openvino==2026.3.1 tokenizers==0.22.2 numpy

# The servers: ONE source each, whether an app of its own or under RAGE.
# A .pth in each venv, not PYTHONPATH: an environment variable would enter
# every engine's declared `env`, and so every run identity, for no reason.
# llama-server's libraries, FOUND FROM ANYWHERE. The base image's binary has
# the runpath `/app/build/bin:` — a directory that does not exist, and an
# EMPTY entry, which the loader reads as "the current directory". So it loads
# libllama-server-impl.so only when started FROM /app, which is the base's
# WORKDIR. llama-swap starts its children from RAGE's, and every llama-server
# died at exec: "libllama-server-impl.so: cannot open shared object file"
# (found by building and running this image, 2026-09-19). Registered with the
# loader rather than put on LD_LIBRARY_PATH: that variable is the base's, and
# the engine that produced every vector on the node runs under it unchanged.
RUN echo /app > /etc/ld.so.conf.d/llama-server.conf && ldconfig \
 && cd / && ldd /app/llama-server | grep -q "libllama-server-impl.so => /app/"

COPY deploy/rage/servers/__init__.py deploy/rage/servers/launch.py /opt/rage/servers/
COPY deploy/dnabert2/server.py          /opt/rage/servers/dnabert2.py
COPY deploy/madlad-translate/server.py  /opt/rage/servers/madlad.py
COPY deploy/medcpt-rerank/server.py     /opt/rage/servers/medcpt.py
RUN for v in dnabert2 madlad medcpt; do \
      echo /opt/rage > "$(/opt/venv-$v/bin/python -c 'import site; print(site.getsitepackages()[0])')/rage-servers.pth"; \
    done

# The front: app/rage and the two modules it borrows — the clock, and the ONE
# definition of canonical JSON and of the hash. No more of the platform, no
# GPU stack (CLAUDE.md invariant 7; tests/rage/test_placement.py holds it).
RUN python3 -m venv /opt/venv-front \
 && /opt/venv-front/bin/pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27"
COPY app/__init__.py       /srv/app/__init__.py
COPY app/gpu_clock.py      /srv/app/gpu_clock.py
COPY app/hold/__init__.py  /srv/app/hold/__init__.py
COPY app/hold/codex.py     /srv/app/hold/codex.py
COPY app/rage              /srv/app/rage

WORKDIR /srv
ENV PYTHONPATH=/srv RAGE_CONFIG=/config/rage.toml RAGE_STATE_DIR=/state RAGE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD /opt/venv-front/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
# RAGE_IMAGE is NOT set here: the image cannot know its own digest. The
# deployment sets it, to the digest it pins, and the front refuses to start
# without it (app/rage/runs.py, boot_image).
ENTRYPOINT ["/opt/venv-front/bin/python", "-m", "app.rage"]
