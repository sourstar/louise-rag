# DNABERT-2 CPU embedding server (see server.py). Build once, weights
# download on first start into the mounted HF cache (/data):
#   docker build -t dnabert2-embed:local deploy/dnabert2
FROM python:3.12-slim

# CPU-only torch (no CUDA payload); DNABERT-2's remote code needs einops.
# flash-attn/triton are deliberately NOT installed — the model's remote
# code falls back to standard attention when they are absent, which is
# the only path that works on CPU.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "transformers<4.50" einops fastapi "uvicorn[standard]"

ENV HF_HOME=/data
WORKDIR /srv
COPY server.py .

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
