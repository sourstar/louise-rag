"""jurifindit/DAR-legal-it as a LocalAI backend: `Embedding`, on torch XPU
(migration step 15e-2). The model is deploy/dar-legal-it/server.py, copied
beside this file as `dar_legal_it.py`; why it is a Python server and not a GGUF
is there (it declares `GteModel`, which llama.cpp does not implement), and so
are the three things that decide its vector: MEAN pooling, sdpa attention, and
the query prefix.

THE QUERY PREFIX IS NOT HERE. The Codex's `space.query.instruction` ("query: ")
is prepended by Louise to queries only (app/embeddings.py `_asked`), exactly as
calibration and the gate prepend it; LocalAI's /v1/embeddings carries no
"this is a query" flag to a backend, and a second prefix here would be a query
side the Codex does not name. The server's `query`/`prefixed` fields were never
sent by Louise.

LocalAI serves it at `/v1/embeddings`, one text per `Embedding` call
(model-pool-over-localai §3.2), so the server's length-budgeted batches are not used:
each text is its own forward pass.

The load call:
    ModelFile     the snapshot directory (its remote code included: it is part
                  of the weights digest, 09 §9.3)
    ContextSize   the longest text in tokens (the server's MAX_SEQ, 8192)
    options       device:xpu|cuda|cpu  (default xpu; cuda = NVIDIA or ROCm)
"""
from __future__ import annotations

import threading
from typing import Any

import dar_legal_it
import louise_backend as lb

KNOWN = {"device": lb.device}
DEFAULTS = {"device": "xpu"}


class Servicer:
    def __init__(self, pb: Any, status: Any, *, build=dar_legal_it.build_encoder) -> None:
        self.pb, self.status = pb, status
        self._build = build
        self.encode = None
        self.dim = 0
        self.lock = threading.Lock()        # one forward pass on the card at a time

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return self.pb.Reply(message=b"OK")

    def LoadModel(self, request, context):  # noqa: N802
        try:
            opts = lb.options(request.Options, KNOWN, DEFAULTS)
            path = lb.weights(request.ModelFile)
            if request.ContextSize <= 0:
                raise lb.LoadError("context_size (the longest text, in tokens) is set in "
                                   "the model's YAML")
            encode, dim = self._build(str(path), opts["device"], request.ContextSize)
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            return lb.refused(self.pb, e)
        self.encode, self.dim = encode, dim
        return lb.loaded(self.pb, f"{path.name} on {opts['device']}, dim {dim}, "
                                  f"max_seq {request.ContextSize}")

    def Embedding(self, request, context):  # noqa: N802
        if self.encode is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        if not request.Embeddings:
            context.abort(self.status.INVALID_ARGUMENT, "no text to embed")
        try:
            with self.lock:
                (vector,) = self.encode([request.Embeddings])
        except Exception as e:                          # noqa: BLE001 — the card, a text too long
            context.abort(self.status.INTERNAL, f"{type(e).__name__}: {e}")
        # FINAL: pooled here, so LocalAI passes it through and pools nothing
        # itself (`core/backend/embeddings.go`, finishEmbeddingResult)
        return self.pb.EmbeddingResult(embeddings=vector, dim=self.dim, tokens=1,
                                       layout=self.pb.EMBEDDING_LAYOUT_FINAL)


if __name__ == "__main__":
    lb.main(Servicer)
