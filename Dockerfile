FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync \
    && rm -rf /var/lib/apt/lists/*
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
