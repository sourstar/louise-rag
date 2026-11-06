# THE CDC EXTENSION (docs/notes/python-hot-spots.md, "D"): app/chunking.py's
# content-defined boundary loop, compiled — Rust through PyO3, built into a
# wheel by maturin, the standard pair for a Python extension. Its own stage:
# the toolchain never reaches the node, only the wheel. Every version pinned:
# the Rust image by exact tag and digest (1.97.0, the toolchain the identity
# tests ran on), maturin by version, PyO3 and every crate by Cargo.lock
# (--locked). abi3: one wheel for every CPython from 3.12. The trixie image,
# as python:3.12-slim's: the wheel needs no newer glibc than the base has.
FROM rust:1.97.0-slim-trixie@sha256:686a437ead83701e8f871e66e838c3ec55f46b5fc235b025756396ac823bdc51 AS cdc-build
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/maturin && /opt/maturin/bin/pip install --no-cache-dir maturin==1.15.0
WORKDIR /build/louise_cdc
COPY native/louise_cdc/Cargo.toml native/louise_cdc/Cargo.lock native/louise_cdc/pyproject.toml ./
COPY native/louise_cdc/src ./src
RUN /opt/maturin/bin/maturin build --release --locked --out /wheels

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
# the cdc extension's wheel, from the stage above: not on PyPI and not in
# requirements.txt (app/chunking.py runs its own loop without it); its
# version is checked against constraints.txt
COPY --from=cdc-build /wheels /tmp/wheels
RUN pip install --no-cache-dir -c constraints.txt /tmp/wheels/louise_cdc-*.whl && rm -rf /tmp/wheels
COPY app ./app
COPY tools ./tools
# BYTECODE COMPILED AT BUILD (2026-11-02, the operator asked whether the
# scripts are pre-built). Otherwise every fresh container (a `docker run --rm`
# tool, a deploy, the read-only gpu-probe) compiles the ~150 app modules it
# imports first: app.worker + app.web + app.louise took 3.2-4.0 s cold against
# 2.6-3.1 s warm. That is startup only: the same bytecode runs either way, so
# the processing speed does not change. pip compiles the installed packages
# already. A source tree bind-mounted over /srv hides these, as it hides the
# rest of the image.
RUN python -m compileall -q -j 0 app tools
# The image does not build unless app/chunking.py cuts through the extension
# AND the extension cuts where the Python loop does, on 300 seeded texts
# (exit 0 is IDENTICAL; without the extension the tool says INCOMPLETE, exit
# 2). The proof that deploys is the same tool on the node's records.
RUN python -m tools.measure.cdc_identity --generated 300 --long 20000 --jobs 1 > /dev/null
# RUNTIME DATA, not a fixture: `app/hold/calibration.py` resolves its sets
# as `<parent of app>/calibration/<modality>.jsonl`, so `hold collection
# create`, `hold embedder check` and `hold vectors export` all fail in a
# container without it. Missed until 2026-09-10 because every local test
# run bind-mounts the repo over /srv, which shadows the image entirely —
# the image's own contents are never exercised there.
COPY calibration ./calibration
EXPOSE 8742
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8742"]

# THE NATIVE BUILD (2026-11-02, docs/notes/python-hot-spots.md "E"): wikidata's
# textifier and its formatter compiled to C by mypyc (tools/build_native.py),
# its entity text ~1.8x faster. A stage FROM base — the image's own Python
# and sources — with a C compiler and the pinned mypy (requirements-build.txt),
# none of which reaches the node: only the .so files leave it, copied into
# the dev and runtime stages below. Python imports an extension before the
# .py of the same name; the .py stays the source and the reference. The
# stage checks the compiled modules against that source on 20,000 seeded
# generated entities and fails the build on one differing byte
# (tools/measure/textify_identity.py); München runs the same tool on the
# node's own entities, in the built image, before any deploy (>= 100,000).
FROM base AS native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-build.txt ./
RUN pip install --no-cache-dir -c constraints.txt -r requirements-build.txt
RUN python -m tools.build_native --out /native \
    && python -m tools.measure.textify_identity --generated 20000

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
# the compiled modules, as the runtime has them: the suite runs against them
# (tests/conftest.py checks every text against the source while they are)
COPY --from=native /native/ ./

FROM base AS runtime
# the compiled modules (stage `native`), beside their sources
COPY --from=native /native/ ./
