"""A real backend for VeGa's gRPC tests: LocalAI's service, served as LocalAI
starts a backend (`run.sh --addr 127.0.0.1:<port>`), answering what the tests
check. Every call it receives is appended to $FAKE_BACKEND_LOG, one JSON line.

Its vectors hold the two values LocalAI's JSON is hard on — negative zero and
the float32 Python's reading lands halfway on (tools/vega_f32_json.go) — so
the whole chain carries them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import struct
import threading
from concurrent import futures

import grpc

import backend_pb2
import backend_pb2_grpc

HALFWAY = struct.unpack("<f", bytes.fromhex("fd43ae15"))[0]

#: With $FAKE_TOKENIZER set, a word tokenizer as llama.cpp's backend answers
#: one: TokenizeString without the special tokens, Detokenize as pieces that
#: each begin with a space (a sentencepiece word start), and an Embedding
#: that reports the tokens it evaluated, SPECIALS added. Without it,
#: TokenizeString is not implemented, as in LocalAI's Python backends.
#: `EmbeddingTokens` are ignored, as stock LocalAI v4.11.0's llama.cpp backend
#: ignores them (an empty text beside them is refused, as there), unless
#: $FAKE_TOKEN_IDS is set: then they are embedded as the text they are the
#: tokens of (our patched backend, localai-bugs P2).
SPECIALS = 2
_vocab: dict[str, int] = {}
_words: dict[int, str] = {}


def _ids(text: str) -> list[int]:
    out = []
    for w in text.split():
        if w not in _vocab:
            _vocab[w] = len(_vocab) + 10
            _words[_vocab[w]] = w
        out.append(_vocab[w])
    return out


def note(context, **kw) -> None:
    md = {k: v for k, v in context.invocation_metadata() if k.startswith("x-")}
    with open(os.environ["FAKE_BACKEND_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(kw | {"metadata": md}) + "\n")


class Servicer(backend_pb2_grpc.BackendServicer):
    def __init__(self) -> None:
        self.loaded = None

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return backend_pb2.Reply(message=b"OK")

    def LoadModel(self, request, context):  # noqa: N802
        note(context, method="LoadModel", model=request.Model, options=list(request.Options))
        if not os.path.isfile(request.ModelFile):
            return backend_pb2.Result(success=False, message=f"no weights at {request.ModelFile}")
        self.loaded = request
        return backend_pb2.Result(success=True, message="loaded")

    def Embedding(self, request, context):  # noqa: N802
        ids = list(request.EmbeddingTokens)
        note(context, method="Embedding", text=request.Embeddings, tokens=ids)
        if self.loaded is None:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "no model is loaded")
        text = (" ".join(_words[t] for t in ids) if ids and os.environ.get("FAKE_TOKEN_IDS")
                else request.Embeddings)
        if ids and not text:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Input content cannot be empty")
        h = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [(b - 128) / 256 for b in h[:5]] + [-0.0, HALFWAY, 0.1]
        counted = len(_ids(text)) + SPECIALS if os.environ.get("FAKE_TOKENIZER") else 0
        return backend_pb2.EmbeddingResult(embeddings=vector, dim=len(vector), tokens=1,
                                           prompt_tokens=counted,
                                           layout=backend_pb2.EMBEDDING_LAYOUT_FINAL)

    def TokenizeString(self, request, context):  # noqa: N802
        if not os.environ.get("FAKE_TOKENIZER"):
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Method not implemented!")
        note(context, method="TokenizeString", prompt=request.Prompt)
        ids = _ids(request.Prompt)
        return backend_pb2.TokenizationResponse(length=len(ids), tokens=ids)

    def Detokenize(self, request, context):  # noqa: N802
        note(context, method="Detokenize", tokens=list(request.tokens))
        return backend_pb2.DetokenizeResponse(content="".join(" " + _words[t] for t in request.tokens))

    def Predict(self, request, context):  # noqa: N802
        note(context, method="Predict", prompt=request.Prompt)
        context.set_trailing_metadata((("x-fake", "trailer"),))
        return backend_pb2.Reply(message=b"echo:" + request.Prompt.encode("utf-8"))

    def PredictStream(self, request, context):  # noqa: N802
        note(context, method="PredictStream", prompt=request.Prompt)
        for i in range(3):
            yield backend_pb2.Reply(message=f"{request.Prompt}:{i}".encode("utf-8"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--addr", required=True)
    addr = p.parse_args().addr
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    backend_pb2_grpc.add_BackendServicer_to_server(Servicer(), server)
    server.add_insecure_port(addr)
    server.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop(2).wait()


if __name__ == "__main__":
    main()
