FROM python:3.12-slim AS base
# git: a deposit collection's `.git` source is shallow-cloned by the worker
# (06 §6.7, app/hold/sources.py; step 16e). Found missing when the merged
# suite ran in this image on 2026-10-05: the branch's sandbox had git.
# lbzip2: the Wikidata dump's one bz2 stream decompressed on every core
# (app/plugins/wikidata/pack.py; operator's yes 2026-10-20): measured on the
# node 195 MB/s against 68.5 with the standard library's bz2, the same bytes.
RUN apt-get update && apt-get install -y --no-install-recommends curl git rsync lbzip2 \
    && rm -rf /var/lib/apt/lists/*
# The GPU's name on the System page (app/gpu_probe.py): pci.ids, pinned by
# commit and checksum. A distribution's copy lags the cards — the node's own
# calls its Arc Pro B70 (8086:e223) "Battlemage G21 [Intel Graphics]".
ADD https://raw.githubusercontent.com/pciutils/pciids/7bdf1c772c2a2253c8eba2fefaa5681e2f145ba1/pci.ids /usr/share/louise/pci.ids
RUN echo "865e934f10d84ab72951b410b0b21d53a7a4c6f4b051c722bdf62833ef5ffb4b  /usr/share/louise/pci.ids" \
        | sha256sum -c - && chmod 644 /usr/share/louise/pci.ids
# The tokenizers a layer's chunker cuts by, under TOKENIZERS_DIR as
# <sha256>.json (app/plugins/wikidata/chunker.py: `wikidata-text` cuts at
# the encoder's own tokens, D6), pinned by commit and checksum:
# jina-embeddings-v3's tokenizer.json at ab036b0. The build plan said the
# image fetched it and no step did: 506 wikidata documents were errored on
# the node, 2026-10-20 ("tokenizer … is not in /opt/louise/tokenizers").
ADD https://huggingface.co/jinaai/jina-embeddings-v3/resolve/ab036b023d30b4d1138c4c3bfa9f0c445ab455d6/tokenizer.json /opt/louise/tokenizers/f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af.json
RUN echo "f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af  /opt/louise/tokenizers/f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af.json" \
        | sha256sum -c - && chmod 644 /opt/louise/tokenizers/*.json
WORKDIR /srv
# every package at the version the gated image ran (constraints.txt): a rebuild
# never moves one on its own
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt
COPY app ./app
COPY tools ./tools
# RUNTIME DATA, not a fixture: `app/hold/calibration.py` resolves its sets
# as `<parent of app>/calibration/<modality>.jsonl`, so `hold collection
# create`, `hold embedder check` and `hold vectors export` all fail in a
# container without it. Missed until 2026-09-10 because every local test
# run bind-mounts the repo over /srv, which shadows the image entirely —
# the image's own contents are never exercised there.
COPY calibration ./calibration
EXPOSE 8742
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8742"]

# puppeteer-core, installed where npm already is: Debian's npm is 363
# packages on top of node's 15, and the test image needs node alone
FROM node:20-slim AS browser-deps
WORKDIR /opt/browser-test
RUN npm install --no-audit --no-fund puppeteer-core@23.11.1 && npm cache clean --force

# THE TEST IMAGE: `docker build --target dev -t louise-rag:dev .` — the
# runtime above plus a headless browser for the page tests that need one
# (tests/browser/: the Ask reader's translate-as-you-read, operator,
# 2026-09-18). The headless shell, not the full browser: 101 packages, not
# 168. Node, Chromium and puppeteer-core never reach the node: the default
# build is the last stage, which adds nothing to the runtime.
FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium-headless-shell fonts-dejavu-core nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=browser-deps /opt/browser-test/node_modules /opt/browser-test/node_modules
ENV NODE_PATH=/opt/browser-test/node_modules \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell
# gRPC for the VeGa wrapper's tests: the test image only (requirements-dev.txt)
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -c constraints.txt -r requirements-dev.txt

FROM base AS runtime
