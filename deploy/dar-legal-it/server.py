"""DAR-legal-it as an embedding endpoint on the Intel GPU, through torch XPU.

Why this exists (docs/notes/gazzetta-plugin.md §7): the legal collection's
encoder is `jurifindit/DAR-legal-it`, and it cannot be served the way every
other embedder on this node is. It declares `GteModel`, and llama.cpp
registers no such architecture — not at b10630, which RAGE runs, nor at
master. The arctic-embed GGUFs that exist are the `-l-v2.0` variant, which
declares `XLMRobertaModel`; this model's family is the unsupported one. So
GGUF is closed and a Python server is the route, as it already is for
DNABERT-2, MADLAD and MedCPT.

The API is OpenAI's /v1/embeddings, which a collection's [encoder] already
speaks (`api = "openai"`):

    POST /v1/embeddings {"model": str, "input": [str] | str}
      -> {"data": [{"index": int, "embedding": [float]}, ...]}

THREE THINGS DECIDE THE VECTOR, and getting any of them wrong produces
plausible numbers that are not this model's:

- **MEAN pooling**, over the attention mask. Its `1_Pooling/config.json`
  says `pooling_mode_mean_tokens: true`, NOT cls — unlike bge-m3 beside it,
  which llama-server runs with `--pooling cls`. Copying that flag here
  would be the easy mistake.
- **`query: ` on queries and nothing on documents.** The prefix is worth
  more than most model choices (§7.1b measured 4.2 MRR points on the
  analogous Qwen3 case), and the platform has no way to declare it yet
  (§7.4b), so this server carries the default and `QUERY_PREFIX` overrides
  it. Callers that already prefix send `prefixed=true`.
- **sdpa attention.** Its remote code asserts "please install xformers" for
  the memory-efficient path, which is CUDA's; sdpa is the same function.

BATCHING IS BY SEQUENCE LENGTH, NOT COUNT. On XPU the attention falls back
to a path that materialises batch x heads x seq x seq, so cost goes with the
SQUARE of the longest text in a batch: sixteen short articles barely touch
the card and sixteen long ones asked for 48 GiB of it (measured, 2026-09-20).
`ATTN_BUDGET` caps batch*seq^2 instead.

Environment: MODEL_DIR, DEVICE (xpu|cpu), PORT (8080), MAX_SEQ (8192),
ATTN_BUDGET (1.5e7), MAX_BATCH (32), QUERY_PREFIX ("query: ").
Needs LD_LIBRARY_PATH=/usr/local/lib on XPU — torch's SYCL runtime sits
behind oneAPI's older libur_loader on the base image's path, and `import
torch` dies on "undefined symbol: urDeviceWaitExp" without it
(deploy/rage/Dockerfile records the same).
"""
from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHARS_PER_TOKEN = 3.2  # Italian legal text on this tokenizer


def budgeted_batches(texts: list[str], max_seq: int, max_batch: int,
                     budget: float) -> list[list[int]]:
    """Indices grouped so every forward pass costs about the same in memory.

    Longest first, so a budget set too high fails on the first batch rather
    than deep into a corpus.
    """
    order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
    groups, i = [], 0
    while i < len(order):
        seq = min(max_seq, max(1, int(len(texts[order[i]]) / CHARS_PER_TOKEN)))
        n = max(1, min(max_batch, int(budget / (seq * seq))))
        groups.append(order[i:i + n])
        i += n
    return groups


class Embedder:
    def __init__(self, encode, max_seq: int, max_batch: int, budget: float,
                 query_prefix: str):
        self.encode = encode
        self.max_seq, self.max_batch, self.budget = max_seq, max_batch, budget
        self.query_prefix = query_prefix

    def embed(self, texts: list[str], *, query: bool,
              prefixed: bool) -> list[list[float]]:
        if query and not prefixed and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]
        out: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        for group in budgeted_batches(texts, self.max_seq, self.max_batch,
                                      self.budget):
            for j, v in zip(group, self.encode([texts[j] for j in group])):
                out[j] = v
        return out  # type: ignore[return-value]


def build_encoder(model_dir: str, device: str, max_seq: int):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        model_dir, device=device, trust_remote_code=True,
        model_kwargs={"attn_implementation": "sdpa"},
        config_kwargs={"use_memory_efficient_attention": False,
                       "unpad_inputs": False})
    model.max_seq_length = max_seq

    def encode(batch: list[str]) -> list[list[float]]:
        vectors = model.encode(batch, batch_size=len(batch),
                               convert_to_numpy=True)
        return [v.astype("float32").tolist() for v in vectors]

    return encode, int(model.get_sentence_embedding_dimension())


def make_handler(embedder: Embedder, dim: int):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802  — any GET answers the health ping
            self._send(200, {"status": "ok", "dim": dim})

        def do_POST(self):  # noqa: N802
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": f"bad request: {exc}"})
            texts = req.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list) or not texts:
                return self._send(400, {"error": "input must be a non-empty "
                                                 "string or list of strings"})
            try:
                vectors = embedder.embed(
                    [str(t) for t in texts],
                    query=bool(req.get("query", False)),
                    prefixed=bool(req.get("prefixed", False)))
            except Exception as exc:  # the card, or a text that will not fit
                return self._send(503, {"error": str(exc)})
            self._send(200, {
                "object": "list", "model": req.get("model", "dar-legal-it"),
                "data": [{"object": "embedding", "index": i, "embedding": v}
                         for i, v in enumerate(vectors)]})

        def log_message(self, *args):
            pass

    return Handler


def main() -> int:
    model_dir = os.environ.get("MODEL_DIR", "/models/dar-legal-it")
    device = os.environ.get("DEVICE", "xpu")
    port = int(os.environ.get("PORT", "8080"))
    max_seq = int(os.environ.get("MAX_SEQ", "8192"))
    encode, dim = build_encoder(model_dir, device, max_seq)
    embedder = Embedder(
        encode, max_seq,
        int(os.environ.get("MAX_BATCH", "32")),
        float(os.environ.get("ATTN_BUDGET", "1.5e7")),
        os.environ.get("QUERY_PREFIX", "query: "))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), make_handler(embedder, dim))

    # llama-swap stops a child with a signal, and RAGE unloads models on
    # demand — a server that ignores SIGTERM is one that has to be killed,
    # which on this card means a Level Zero context nobody released.
    def stop(signum, frame):
        print(f"dar-legal-it: signal {signum}, stopping", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"dar-legal-it: {model_dir} on {device}, dim {dim}, "
          f"max_seq {max_seq}, listening on {host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
