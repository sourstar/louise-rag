"""MedCPT's article encoder as a LocalAI backend: `Embedding` of a TEXT PAIR, on
OpenVINO (docs/notes/input-forms.md, RAGE). The model code is
deploy/medcpt-rerank/server.py, copied beside this file as `medcpt.py`, as for
louise-medcpt-rerank: its `make_encoder` is the model's own pair tokenizer
(tokenizer.json), which builds the segment ids llama.cpp's BERT graph zeroes.

WHAT NCBI DID, done here. NCBI's archive vectors are
`model(**tokenizer([[title, abstract], …], truncation=True, padding=True,
max_length=512)).last_hidden_state[:, 0, :]` — the abstract the SECOND segment,
cut longest-first to 512 tokens, the [CLS] row. Measured on the node
(2026-10-08): this encoding on OpenVINO's GPU plugin matches the archive at
cosine 1.00000; "title\\n\\nabstract" as one text does not (0.98926 median).

ONE STRING PER CALL. LocalAI hands a backend one string per embedding, so
VeGa's wrapper sends a pair as `PAIR_MARK + json.dumps([first, second])`
(app/vega/tag.backend_pair) and only to a model whose YAML declares `pairs:true`; this
backend decodes it (louise_backend.decode_pair). A plain string is one text,
segment 0 alone — how NCBI's query encoder reads a query, and what a `text`
form layer would send.

The load call:
    ModelFile     the directory: openvino_model.xml (output: the last hidden
                  state) and tokenizer.json — convert_article.py's output
    ContextSize   tokens per text or pair, cut longest-first (NCBI's 512)
    options       device:xpu|cpu  (OpenVINO's GPU or CPU; default xpu; no AMD or NVIDIA card)
                  pairs:true      a marked string is a pair; without it one
                                  is refused (the front never sends one)
    env           OV_CACHE_DIR, where OpenVINO keeps the compiled model
"""
from __future__ import annotations

import os
from typing import Any

import louise_backend as lb
import medcpt

KNOWN = {"device": lb.device, "pairs": lb.flag}
DEFAULTS = {"device": "xpu", "pairs": False}


class Servicer:
    def __init__(self, pb: Any, status: Any, *, encoder=medcpt.make_encoder,
                 infer=medcpt.make_cls_infer) -> None:
        self.pb, self.status = pb, status
        self._encoder, self._infer = encoder, infer
        self.encode = None
        self.infer = None
        self.pairs = False
        self.dim = 0

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return self.pb.Reply(message=b"OK")

    def _vector(self, item: str | tuple[str, str]) -> tuple[list[float], int]:
        inputs = self.encode([item])
        (vector,) = self.infer(inputs)
        return [float(x) for x in vector], int(sum(inputs["attention_mask"][0]))

    def LoadModel(self, request, context):  # noqa: N802
        try:
            opts = lb.options(request.Options, KNOWN, DEFAULTS)
            path = lb.weights(request.ModelFile)
            xml, tok = path / "openvino_model.xml", path / "tokenizer.json"
            for f in (xml, tok):
                if not f.is_file():
                    raise lb.LoadError(f"{path} holds no {f.name}")
            if request.ContextSize <= 0:
                raise lb.LoadError("context_size (tokens per text or pair) is set in the "
                                   "model's YAML")
            device = lb.openvino_device(opts["device"])
            self.encode = self._encoder(str(tok), request.ContextSize)
            self.infer = self._infer(str(xml), device, os.environ.get("OV_CACHE_DIR") or None)
            # the first inference compiles its kernels, and says the dimension
            vector, _ = self._vector(("warm-up", "the first inference compiles its kernels"))
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            self.encode = self.infer = None
            return lb.refused(self.pb, e)
        self.pairs, self.dim = opts["pairs"], len(vector)
        return lb.loaded(self.pb, f"{path.name} on {device}, dim {self.dim}, "
                                  f"{'pairs' if self.pairs else 'texts only'}")

    def Embedding(self, request, context):  # noqa: N802
        if self.encode is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        if not request.Embeddings:
            context.abort(self.status.INVALID_ARGUMENT, "no text to embed")
        try:
            item = lb.decode_pair(request.Embeddings)
        except ValueError as e:
            context.abort(self.status.INVALID_ARGUMENT, str(e))
        if isinstance(item, tuple) and not self.pairs:
            context.abort(self.status.INVALID_ARGUMENT,
                          "a text pair, to a model loaded without pairs:true")
        try:
            vector, tokens = self._vector(item)
        except Exception as e:                          # noqa: BLE001 — the card
            context.abort(self.status.INTERNAL, f"{type(e).__name__}: {e}")
        # FINAL: pooled here ([CLS]), so LocalAI passes it through and pools
        # nothing itself (`core/backend/embeddings.go`, finishEmbeddingResult)
        return self.pb.EmbeddingResult(embeddings=vector, dim=self.dim, tokens=tokens,
                                       layout=self.pb.EMBEDDING_LAYOUT_FINAL)


if __name__ == "__main__":
    lb.main(Servicer)
