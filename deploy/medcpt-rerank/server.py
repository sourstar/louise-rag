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

MedCPT's ARTICLE ENCODER shares it (docs/notes/input-forms.md):
deploy/localai/backends/louise-medcpt-article-ov encodes a (title, abstract)
text pair with `make_encoder`, the same pair tokenizer and segment ids, and
takes the [CLS] row of `make_cls_infer` — its IR built by convert_article.py.

OpenVINO's model cache (its `CACHE_DIR`, `make_infer`'s `cache_dir`): compiling
the model for the card is the slow part of a start, and with a cache the next
start reads the compiled model back instead (docs/notes/rage.md §20). The
backend passes OV_CACHE_DIR, set in the model's YAML and so part of its run
(operator, 2026-10-01).
"""
from __future__ import annotations

import math
import threading

MAX_TOKENS = 512
INPUTS = ("input_ids", "attention_mask", "token_type_ids")


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Reranker:
    """`encode(pairs)` gives the model's three int64 inputs for a list of
    (query, text) pairs; `infer(inputs)` gives one logit per pair.

    `fixed`: ONE STATIC SHAPE, every inference `batch` pairs of the encoder's
    fixed length, a short batch filled with copies of its first pair whose
    scores are dropped. Padded to its own longest pair, a batch's shape, and
    with it the GPU kernel's summation order, depended on the batch-mates: a
    document's score was not a function of (query, document). München,
    2026-10-30, louise-rerank-medcpt on the Arc B70: 56 of 256 scores moved
    when the same documents were cut into requests in reverse order, the same
    56 on every fresh load (max |diff| 6.6e-8). The encoder must be fixed too
    (`make_encoder(fixed=True)`)."""

    def __init__(self, encode, infer, batch: int = 16, *, fixed: bool = False):
        self.encode, self.infer, self.batch = encode, infer, max(1, batch)
        self.fixed = fixed

    def logits(self, query: str, texts: list[str]) -> list[float]:
        # similar lengths share a batch, so little of it is padding (unfixed)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = [0.0] * len(texts)
        for s in range(0, len(order), self.batch):
            idx = order[s:s + self.batch]
            pairs = [(query, texts[i]) for i in idx]
            pairs += [pairs[0]] * (self.batch - len(pairs)) if self.fixed else []
            vals = list(self.infer(self.encode(pairs)))
            if len(vals) != len(pairs):
                raise RuntimeError(f"model gave {len(vals)} scores for {len(pairs)} pairs")
            for i, v in zip(idx, vals):                # a filler's score is dropped
                out[i] = float(v)
        return out


def make_encoder(tokenizer_json: str, max_tokens: int = MAX_TOKENS, *, fixed: bool = False):
    """`encode(items)`: the model's three int64 inputs. `fixed`: every input
    padded to `max_tokens`, so the model sees ONE static shape.

    OpenVINO's GPU plugin is not bit-reproducible at every dynamic shape
    (CLAUDE.md invariant 12). München, 2026-10-30, run 39578bfb (Arc B70):
    7 of 160 pubmed pairs, all of 65–73 tokens, came back different in each
    of four passes (max |diff| 1.4–3.0e-4), the other 153 bit-identical. On
    the CPU, 0 of 160. On the GPU with every input padded to 512, 0 of 160,
    and 640 inferences in 4.0 s against 11.9 s. The attention mask keeps the
    padding out of the [CLS] row; its count of real tokens is unchanged."""
    import numpy as np
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tokenizer_json)
    tok.enable_truncation(max_tokens)       # longest_first, as the model card
    tok.enable_padding(length=max_tokens if fixed else None)    # [PAD] = 0

    def encode(pairs):
        encs = tok.encode_batch(pairs)
        return {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
    return encode


def _compile(model_xml: str, device: str, cache_dir: str | None):
    import openvino as ov
    # CACHE_DIR: the compiled model is kept there and read back at the next
    # start; OpenVINO keys it on the model, the device and its own version
    config = {"CACHE_DIR": cache_dir} if cache_dir else {}
    compiled = ov.Core().compile_model(model_xml, device, config)
    names = {i.get_any_name() for i in compiled.inputs}
    if names != set(INPUTS):
        raise SystemExit(f"{model_xml}: inputs {sorted(names)}, expected {list(INPUTS)} — "
                         f"without token_type_ids MedCPT scores noise")
    return compiled


def _serial(compiled):
    """`compiled.infer_new_request`, one call at a time. Concurrent requests
    on OpenVINO's GPU plugin deadlocked on the node (2026-10-10, pubmed's
    re-embedding at concurrency 4): four gRPC threads stuck in `infer` for
    seven hours, one of them spinning a core, and the encoder answered
    nobody. One forward pass on the card at a time, as louise-dar-legal-it
    does; the tokenizer still runs in parallel."""
    lock = threading.Lock()

    def run(inputs):
        with lock:
            return next(iter(compiled.infer_new_request(inputs).values()))
    return run


def make_infer(model_xml: str, device: str, cache_dir: str | None = None):
    run = _serial(_compile(model_xml, device, cache_dir))

    def infer(inputs):
        return run(inputs).reshape(-1).tolist()
    return infer


def make_cls_infer(model_xml: str, device: str, cache_dir: str | None = None):
    """MedCPT's article and query encoders: the IR's output is the last
    hidden state (batch, tokens, dim), and the vector is its [CLS] row, as
    NCBI's own code takes `last_hidden_state[:, 0, :]`
    (deploy/medcpt-rerank/convert_article.py builds the IR)."""
    run = _serial(_compile(model_xml, device, cache_dir))

    def infer(inputs):
        hidden = run(inputs)
        if hidden.ndim != 3:
            raise RuntimeError(f"the model gave shape {hidden.shape}, not (batch, tokens, dim)")
        return [row.tolist() for row in hidden[:, 0, :]]
    return infer
