FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync \
    && rm -rf /var/lib/apt/lists/*
# The GPU's name on the System page (app/gpu_probe.py): pci.ids, pinned by
# commit and checksum. A distribution's copy lags the cards — the node's own
# calls its Arc Pro B70 (8086:e223) "Battlemage G21 [Intel Graphics]".
ADD https://raw.githubusercontent.com/pciutils/pciids/7bdf1c772c2a2253c8eba2fefaa5681e2f145ba1/pci.ids /usr/share/louise/pci.ids
RUN echo "865e934f10d84ab72951b410b0b21d53a7a4c6f4b051c722bdf62833ef5ffb4b  /usr/share/louise/pci.ids" \
        | sha256sum -c - && chmod 644 /usr/share/louise/pci.ids
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
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
