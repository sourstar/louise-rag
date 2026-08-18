"""DNABERT-2 embedding server — OpenAI-compatible /v1/embeddings on CPU.

Serves zhihan1996/DNABERT-2-117M (117M params, BPE tokenizer, ALiBi — no
hard positional limit) for the genome collection's dense-by-sequence leg
(architecture-v2.md §9). TEI cannot host it (trust_remote_code), hence this
~100-line stand-in with the same surface the platform already speaks:

    POST /v1/embeddings   {"model": ..., "input": [seq, ...]}
    GET  /health          200 once the model is loaded
    GET  /info            {"model_id", "model_sha"} — same shape as TEI's,
                          so the space pin confirms instead of staying weak

Mean pooling over the attention mask (the DNABERT-2 paper's sequence
embedding); the louise-rag client L2-normalizes, so raw pooled vectors go
out. The GPU boundary invariant holds: torch lives HERE, never in the app.
"""
import os
import threading

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

MODEL_ID = os.getenv("MODEL_ID", "zhihan1996/DNABERT-2-117M")
# Pinned revision: trust_remote_code executes repo code, so a moving "main"
# would be both a supply-chain surface and a silent space change. Verified
# working on CPU (pytorch attention fallback) at this commit.
MODEL_REVISION = os.getenv("MODEL_REVISION",
                            "7bce263b15377fc15361f52cfab88f8b586abda0")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2048"))
MICRO_BATCH = int(os.getenv("MICRO_BATCH", "4"))
torch.set_num_threads(int(os.getenv("TORCH_THREADS", "4")))

app = FastAPI()
_state: dict = {}
_lock = threading.Lock()          # one forward pass at a time: deterministic
                                   # memory footprint on a shared host


class EmbeddingsIn(BaseModel):
    model: str = MODEL_ID
    input: list[str] | str


@app.on_event("startup")
def _load():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True,
                                               revision=MODEL_REVISION)
    model = AutoModel.from_pretrained(MODEL_ID, trust_remote_code=True,
                                       revision=MODEL_REVISION)
    model.eval()
    _state["tokenizer"] = tokenizer
    _state["model"] = model
    _state["sha"] = getattr(model.config, "_commit_hash", None) or MODEL_REVISION


@app.get("/health")
def health():
    if "model" not in _state:
        raise HTTPException(status_code=503, detail="model loading")
    return {"status": "ok"}


@app.get("/info")
def info():
    if "model" not in _state:
        raise HTTPException(status_code=503, detail="model loading")
    return {"model_id": MODEL_ID, "model_sha": _state["sha"]}


@torch.no_grad()
def _embed(texts: list[str]) -> list[list[float]]:
    tokenizer, model = _state["tokenizer"], _state["model"]
    out: list[list[float]] = []
    for start in range(0, len(texts), MICRO_BATCH):
        batch = texts[start:start + MICRO_BATCH]
        enc = tokenizer(batch, return_tensors="pt", padding=True,
                        truncation=True, max_length=MAX_TOKENS)
        hidden = model(input_ids=enc["input_ids"],
                       attention_mask=enc["attention_mask"])[0]
        mask = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        out.extend(pooled.float().tolist())
    return out


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingsIn):
    if "model" not in _state:
        raise HTTPException(status_code=503, detail="model loading")
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    if not texts:
        raise HTTPException(status_code=400, detail="empty input")
    with _lock:
        vectors = _embed(texts)
    return {"object": "list", "model": MODEL_ID,
            "data": [{"object": "embedding", "index": i, "embedding": v}
                     for i, v in enumerate(vectors)]}
