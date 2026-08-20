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
torch.set_num_threads(int(os.getenv("TORCH_THREADS", "4")))


def _pick_device() -> str:
    """`xpu` when an Intel GPU is present and usable, else `cpu`.

    DEVICE=cpu forces the fallback, which is what makes an A/B possible: the
    same image, the same weights, one variable. The model's remote code is
    device-agnostic — every tensor it creates follows `hidden_states.device`,
    and its only CUDA-adjacent dependency (triton flash-attention) is a soft
    import with a plain-PyTorch fallback that we already rely on — so nothing
    here is CUDA-specific and nothing needed patching.
    """
    want = os.getenv("DEVICE", "auto").lower()
    if want == "cpu":
        return "cpu"
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    if want == "xpu":
        raise RuntimeError(
            "DEVICE=xpu was requested but torch.xpu.is_available() is False. "
            "Almost always the compute runtime is too old for the card: it "
            "installs cleanly and enumerates zero devices. Check `clinfo -l` "
            "is non-empty inside the container.")
    return "cpu"


DEVICE = _pick_device()
# A GPU is idle at batch 4; a CPU thrashes at batch 32. Neither number is a
# tuning preference — they are different machines.
MICRO_BATCH = int(os.getenv("MICRO_BATCH", "32" if DEVICE == "xpu" else "4"))

app = FastAPI()
_state: dict = {}
_lock = threading.Lock()          # one forward pass at a time: deterministic
                                   # memory footprint on a shared host


class EmbeddingsIn(BaseModel):
    model: str = MODEL_ID
    input: list[str] | str


def _disable_flash_attention(model) -> None:
    """Force the model's plain-PyTorch attention path.

    DNABERT-2's remote code imports a triton flash-attention kernel inside a
    try/except and falls back when the import fails — which is why the CPU
    image, where triton is simply absent, has always worked.

    On XPU it is NOT absent: torch's own XPU wheels depend on
    pytorch-triton-xpu, so the import SUCCEEDS, the flash path is taken, and
    the very first forward pass dies on `assert q.is_cuda and k.is_cuda and
    v.is_cuda` inside flash_attn_triton.py. The kernel is CUDA-only
    regardless of which triton is installed.

    So the fallback is selected explicitly here rather than left to depend on
    a package happening not to be installed. The remote code re-checks
    `flash_attn_qkvpacked_func is None` at every call site, so nulling the
    symbol in its module is exactly the switch it already has.
    """
    import sys

    module = sys.modules.get(type(model).__module__)
    if module is None:
        return
    if getattr(module, "flash_attn_qkvpacked_func", None) is not None:
        module.flash_attn_qkvpacked_func = None


@app.on_event("startup")
def _load():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True,
                                               revision=MODEL_REVISION)
    model = AutoModel.from_pretrained(MODEL_ID, trust_remote_code=True,
                                       revision=MODEL_REVISION)
    model.eval()
    _disable_flash_attention(model)
    if DEVICE == "xpu":
        # fp16 on the GPU, fp32 on CPU — half precision is a large win on the
        # Arc and a pessimisation on CPU, where several ops fall back to
        # slow paths. Pooling is done in fp32 either way (see _embed), so the
        # vectors stay comparable across devices.
        model = model.half()
    model = model.to(DEVICE)
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
    # model_id and model_sha are what the space pin hashes; `device` is
    # reported alongside for operators and deliberately takes no part in it,
    # because the same weights on another processor compute the same
    # function (architecture-v2.md amendment 12).
    return {"model_id": MODEL_ID, "model_sha": _state["sha"],
            "device": DEVICE, "micro_batch": MICRO_BATCH}


@torch.no_grad()
def _embed(texts: list[str]) -> list[list[float]]:
    tokenizer, model = _state["tokenizer"], _state["model"]
    out: list[list[float]] = []
    for start in range(0, len(texts), MICRO_BATCH):
        batch = texts[start:start + MICRO_BATCH]
        enc = tokenizer(batch, return_tensors="pt", padding=True,
                        truncation=True, max_length=MAX_TOKENS)
        ids = enc["input_ids"].to(DEVICE)
        attn = enc["attention_mask"].to(DEVICE)
        hidden = model(input_ids=ids, attention_mask=attn)[0]
        # Pool in fp32 even when the model ran in fp16: summing hundreds of
        # half-precision activations is exactly where the error would show,
        # and the whole point is that a CPU vector and a GPU vector land in
        # the same space.
        hidden = hidden.float()
        mask = attn.unsqueeze(-1).float()
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        out.extend(pooled.cpu().tolist())
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
