"""MedCPT's cross-encoder as a reranker on the Intel GPU, through OpenVINO.

Why this exists (docs/notes/reranker-scope.md §12): ncbi/MedCPT-Cross-Encoder
is a BERT that reads the query as segment 0 and the article as segment 1,
and both off-the-shelf GPU servers zero the segment ids: llama.cpp's BERT
graph uses token-type row 0 for every token, and OpenVINO Model Server's
/v3/rerank fills token_type_ids with 0. Either way the model scores noise.
Here Hugging Face's own pair tokenizer (the model's tokenizer.json, the
model card's encoding) builds all three inputs and OpenVINO runs the model.

SERVED BY LocalAI since migration step 15f: deploy/localai/backends/louise-medcpt-rerank
imports this module and answers `Rerank` (LocalAI's /v1/rerank, read as
`api = "jina"`), with scores the sigmoid of the logit as TEI's were. Pairs
longer than the load call's context (the model card's 512) are cut
longest-first, as the card's `tokenizer(..., truncation=True, max_length=512)`.
Its TEI /rerank server went with llama-swap at 15g (docs/notes/legacy-to-delete.md
§8).

OpenVINO's model cache (its `CACHE_DIR`, `make_infer`'s `cache_dir`): compiling
the model for the card is the slow part of a start, and with a cache the next
start reads the compiled model back instead (docs/notes/rage.md §20). The
backend passes OV_CACHE_DIR, set in the model's YAML and so part of its run
(operator, 2026-10-01).
"""
from __future__ import annotations

import math

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


def make_encoder(tokenizer_json: str, max_tokens: int = MAX_TOKENS):
    import numpy as np
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tokenizer_json)
    tok.enable_truncation(max_tokens)       # longest_first, as the model card
    tok.enable_padding()                    # [PAD] = 0

    def encode(pairs):
        encs = tok.encode_batch(pairs)
        return {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
    return encode


def make_infer(model_xml: str, device: str, cache_dir: str | None = None):
    import openvino as ov
    # CACHE_DIR: the compiled model is kept there and read back at the next
    # start; OpenVINO keys it on the model, the device and its own version
    config = {"CACHE_DIR": cache_dir} if cache_dir else {}
    compiled = ov.Core().compile_model(model_xml, device, config)
    names = {i.get_any_name() for i in compiled.inputs}
    if names != set(INPUTS):
        raise SystemExit(f"{model_xml}: inputs {sorted(names)}, expected {list(INPUTS)} — "
                         f"without token_type_ids MedCPT scores noise")

    def infer(inputs):
        # one request per call: concurrent calls do not share one
        return next(iter(compiled.infer_new_request(inputs).values())).reshape(-1).tolist()
    return infer
