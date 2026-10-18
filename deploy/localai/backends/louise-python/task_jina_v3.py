"""jina-embeddings-v3 as a louise-python task: `Embedding`, on torch
(docs/notes/wikidata-plugins.md). The model is deploy/jina-embeddings-v3/
server.py, copied beside this file as `jina_v3.py`; what decides the vector
is there.

The load call:
    ModelFile     the folder: the weights and, under hf-cache/, their remote code
    ContextSize   the longest text in tokens (the model's 8,192)
    options       device:xpu|cuda|cpu (default xpu)
                  adapter:<one of the model's LoRA tasks> (default retrieval.passage;
                  a query encoder is its own model YAML with adapter:retrieval.query)
                  dim:<a Matryoshka width> (default 1024; Wikimedia's vectors: 512)
"""
from __future__ import annotations

import threading
from typing import Any

import jina_v3
import louise_backend as lb


def adapter(value: str) -> str:
    value = value.strip()
    if not value or " " in value:
        raise ValueError("a LoRA task name, e.g. retrieval.passage")
    return value


def width(value: str) -> int:
    n = lb.positive(value)
    if n not in jina_v3.WIDTHS:
        raise ValueError(f"one of {', '.join(map(str, jina_v3.WIDTHS))}")
    return n


KNOWN = {"device": lb.device, "adapter": adapter, "dim": width}
DEFAULTS = {"device": "xpu", "adapter": "retrieval.passage", "dim": 1024}


class Servicer:
    def __init__(self, pb: Any, status: Any, *, build=jina_v3.build_encoder) -> None:
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
            encode, dim = self._build(str(path), opts["device"], opts["adapter"], opts["dim"],
                                      request.ContextSize)
            encode("the first pass compiles its kernels")
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            return lb.refused(self.pb, e)
        self.encode, self.dim = encode, dim
        return lb.loaded(self.pb, f"{path.name} on {opts['device']}, {opts['adapter']}, "
                                  f"dim {dim}, max {request.ContextSize} tokens")

    def Embedding(self, request, context):  # noqa: N802
        if self.encode is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        if not request.Embeddings:
            context.abort(self.status.INVALID_ARGUMENT, "no text to embed")
        try:
            with self.lock:
                vector = self.encode(request.Embeddings)
        except Exception as e:                          # noqa: BLE001 — the card, a text too long
            context.abort(self.status.INTERNAL, f"{type(e).__name__}: {e}")
        # FINAL: pooled and normalised here, so LocalAI pools nothing itself
        return self.pb.EmbeddingResult(embeddings=vector, dim=self.dim, tokens=1,
                                       layout=self.pb.EMBEDDING_LAYOUT_FINAL)
