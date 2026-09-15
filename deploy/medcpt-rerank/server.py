"""MedCPT's cross-encoder as a reranker on the Intel GPU, through OpenVINO.

Why this exists (docs/notes/reranker-scope.md §12): ncbi/MedCPT-Cross-Encoder
is a BERT that reads the query as segment 0 and the article as segment 1,
and both off-the-shelf GPU servers zero the segment ids: llama.cpp's BERT
graph uses token-type row 0 for every token, and OpenVINO Model Server's
/v3/rerank fills token_type_ids with 0. Either way the model scores noise.
Here Hugging Face's own pair tokenizer (the model's tokenizer.json, the
model card's encoding) builds all three inputs and OpenVINO runs the model.

The API is TEI's /rerank, which the platform already speaks (`api = "tei"`
in a collection's [reranker]):

    POST /rerank {"query": str, "texts": [str], "raw_scores"?: bool}
      -> [{"index": int, "score": float}, ...] best first

Scores are the sigmoid of the logit unless `raw_scores`, as TEI's. Pairs
longer than 512 tokens are cut longest-first, as the model card's
`tokenizer(..., truncation=True, max_length=512)` — TEI's `truncate` is
accepted and always on. Any GET answers the health ping.

Environment: MODEL_DIR (openvino_model.xml + tokenizer.json), DEVICE (GPU),
PORT (8080), BATCH (pairs per inference, 16), MAX_TEXTS (256).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_TOKENS = 512
INPUTS = ("input_ids", "attention_mask", "token_type_ids")


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Reranker:
    """`encode(pairs)` gives the model's three int64 inputs for a list of
    (query, text) pairs; `infer(inputs)` gives one logit per pair."""

    def __init__(self, encode, infer, batch: int = 16):
        self.encode, self.infer, self.batch = encode, infer, max(1, batch)

    def logits(self, query: str, texts: list[str]) -> list[float]:
        # similar lengths share a batch, so little of it is padding
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = [0.0] * len(texts)
        for s in range(0, len(order), self.batch):
            idx = order[s:s + self.batch]
            vals = self.infer(self.encode([(query, texts[i]) for i in idx]))
            if len(vals) != len(idx):
                raise RuntimeError(f"model gave {len(vals)} scores for {len(idx)} pairs")
            for i, v in zip(idx, vals):
                out[i] = float(v)
        return out


def handle(reranker: Reranker, body, max_texts: int = 256) -> tuple[int, object]:
    """One /rerank request -> (HTTP status, JSON payload)."""
    if not isinstance(body, dict):
        return 422, {"error": "expected a JSON object", "error_type": "Validation"}
    query, texts = body.get("query"), body.get("texts")
    if not isinstance(query, str) or not isinstance(texts, list) \
            or not all(isinstance(t, str) for t in texts):
        return 422, {"error": 'expected {"query": str, "texts": [str, ...]}',
                     "error_type": "Validation"}
    if len(texts) > max_texts:
        return 413, {"error": f"{len(texts)} texts; at most {max_texts} per request",
                     "error_type": "Validation"}
    if not texts:
        return 200, []
    raw = reranker.logits(query, texts)
    scores = raw if body.get("raw_scores") else [sigmoid(x) for x in raw]
    ranked = sorted(range(len(texts)), key=lambda i: -scores[i])
    return 200, [{"index": i, "score": scores[i]} for i in ranked]


def make_encoder(tokenizer_json: str):
    import numpy as np
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tokenizer_json)
    tok.enable_truncation(MAX_TOKENS)       # longest_first, as the model card
    tok.enable_padding()                    # [PAD] = 0

    def encode(pairs):
        encs = tok.encode_batch(pairs)
        return {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
    return encode


def make_infer(model_xml: str, device: str):
    import openvino as ov
    compiled = ov.Core().compile_model(model_xml, device)
    names = {i.get_any_name() for i in compiled.inputs}
    if names != set(INPUTS):
        raise SystemExit(f"{model_xml}: inputs {sorted(names)}, expected {list(INPUTS)} — "
                         f"without token_type_ids MedCPT scores noise")

    def infer(inputs):
        # one request per call: concurrent calls do not share one
        return next(iter(compiled.infer_new_request(inputs).values())).reshape(-1).tolist()
    return infer


class Handler(BaseHTTPRequestHandler):
    reranker: Reranker
    model = ""
    max_texts = 256

    def do_GET(self):
        self._send(200, {"status": "ok", "model": self.model})

    def do_POST(self):
        if self.path.rstrip("/") != "/rerank":
            return self._send(404, {"error": "POST /rerank"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
        except (ValueError, OSError):
            return self._send(400, {"error": "the body is not JSON"})
        t0 = time.monotonic()
        try:
            status, payload = handle(self.reranker, body, self.max_texts)
        except Exception as e:                          # noqa: BLE001
            status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
        texts = body.get("texts") if isinstance(body, dict) else None
        print(f"rerank {len(texts) if isinstance(texts, list) else '?'} texts -> {status} "
              f"in {1000 * (time.monotonic() - t0):.0f} ms", file=sys.stderr, flush=True)
        self._send(status, payload)

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):                       # one line per request, above
        pass


def main() -> None:
    model_dir = os.getenv("MODEL_DIR", "/models/medcpt-cross-ov")
    device = os.getenv("DEVICE", "GPU")
    Handler.reranker = Reranker(make_encoder(f"{model_dir}/tokenizer.json"),
                                make_infer(f"{model_dir}/openvino_model.xml", device),
                                batch=int(os.getenv("BATCH", "16")))
    Handler.model = f"{os.path.basename(model_dir.rstrip('/'))} on {device}"
    Handler.max_texts = int(os.getenv("MAX_TEXTS", "256"))
    t0 = time.monotonic()
    Handler.reranker.logits("warm-up", ["the first inference compiles its kernels"])
    print(f"{Handler.model}: warm in {time.monotonic() - t0:.1f}s", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
