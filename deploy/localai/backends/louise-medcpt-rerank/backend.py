"""MedCPT's cross-encoder as a LocalAI backend: `Rerank`, on OpenVINO (migration
step 15e-2). The model is deploy/medcpt-rerank/server.py, copied beside this
file as `medcpt.py`; why it is not llama.cpp's or OpenVINO Model Server's is
there (both zero the segment ids, and MedCPT then scores noise).

LocalAI serves it at `/v1/rerank` in Jina's shape, which a collection's
[reranker] reads as `api = "jina"`:

    POST /v1/rerank {"query": str, "documents": [str], "top_n": int}
      -> {"results": [{"index": int, "relevance_score": float, ...}]} best first

Scores are the sigmoid of the logit, as TEI's /rerank gave them.

The load call:
    ModelFile     the directory: openvino_model.xml and tokenizer.json
    ContextSize   tokens per pair, cut longest-first (the model card's 512)
    NBatch        pairs per inference
    options       device:xpu|cpu  (OpenVINO's GPU or CPU; default xpu; no AMD or NVIDIA card)
    env           OV_CACHE_DIR, where OpenVINO keeps the model compiled for
                  the card (model-pool.md@c572c3b §20) — in the run, as the
                  engine's `env` (operator, 2026-10-01)
"""
from __future__ import annotations

import os
from typing import Any

import louise_backend as lb
import medcpt

KNOWN = {"device": lb.device}
DEFAULTS = {"device": "xpu"}


class Servicer:
    def __init__(self, pb: Any, status: Any, *, encoder=medcpt.make_encoder,
                 infer=medcpt.make_infer) -> None:
        self.pb, self.status = pb, status
        self._encoder, self._infer = encoder, infer
        self.reranker: medcpt.Reranker | None = None

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return self.pb.Reply(message=b"OK")

    def LoadModel(self, request, context):  # noqa: N802
        try:
            opts = lb.options(request.Options, KNOWN, DEFAULTS)
            path = lb.weights(request.ModelFile)
            xml, tok = path / "openvino_model.xml", path / "tokenizer.json"
            for f in (xml, tok):
                if not f.is_file():
                    raise lb.LoadError(f"{path} holds no {f.name}")
            if request.ContextSize <= 0 or request.NBatch <= 0:
                raise lb.LoadError("context_size (tokens per pair) and batch (pairs per "
                                   "inference) are set in the model's YAML")
            device = lb.openvino_device(opts["device"])
            # ONE STATIC SHAPE on the GPU: padded to its batch's longest pair, a
            # document's score depended on its batch-mates (invariant 12;
            # medcpt.Reranker)
            fixed = device == "GPU"
            reranker = medcpt.Reranker(
                self._encoder(str(tok), request.ContextSize, fixed=fixed),
                self._infer(str(xml), device, os.environ.get("OV_CACHE_DIR") or None),
                batch=request.NBatch, fixed=fixed)
            reranker.logits("warm-up", ["the first inference compiles its kernels"])
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            return lb.refused(self.pb, e)
        self.reranker = reranker
        return lb.loaded(self.pb, f"{path.name} on {device}")

    def Rerank(self, request, context):  # noqa: N802
        if self.reranker is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        texts = list(request.documents)
        if not texts:
            return self.pb.RerankResult(usage=self.pb.Usage(), results=[])
        try:
            scores = [medcpt.sigmoid(x) for x in self.reranker.logits(request.query, texts)]
        except Exception as e:                          # noqa: BLE001
            context.abort(self.status.INTERNAL, f"{type(e).__name__}: {e}")
        ranked = sorted(range(len(texts)), key=lambda i: -scores[i])
        if request.top_n > 0:
            ranked = ranked[:request.top_n]
        return self.pb.RerankResult(
            usage=self.pb.Usage(),
            results=[self.pb.DocumentResult(index=i, text=texts[i], relevance_score=scores[i])
                     for i in ranked])


if __name__ == "__main__":
    lb.main(Servicer)
