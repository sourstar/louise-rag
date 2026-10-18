"""jina-embeddings-v3 as an embedding model on the card, through torch — the
`wikidata` collection's encoder (docs/notes/wikidata-plugins.md).

WHAT DECIDES THE VECTOR, measured against Wikimedia's archive (2026-10-18:
the same text gives cosine 0.999999 to the vector Jina's API returned them):

- **The adapter.** jina-v3 is one XLM-RoBERTa with LoRA adapters per task;
  `retrieval.passage` embeds documents, `retrieval.query` queries. The model's
  own `encode(task=…)` selects it AND prepends the task's instruction from
  its config (`task_instructions`); nothing is prefixed here.
- **The width.** A Matryoshka cut: the first `dim` components, renormalised.
  Wikimedia's vectors are 512 wide.
- **The weights and the code.** The model folder holds the weights at
  `jinaai/jina-embeddings-v3` ab036b0 AND, as a Hugging Face cache under
  `hf-cache/`, the remote code its config names
  (`jinaai/xlm-roberta-flash-implementation` bd55a5e). So the weights digest
  (09 §9.3) covers the code that computes the vector, and nothing is fetched:
  HF_HUB_CACHE points inside the folder, the hub is offline.

ONE TEXT PER CALL. On the B70 `encode` of a batch fails inside the LoRA
selection (`torch.unique`: "numel: integer multiplication overflow"); one text
works, and LocalAI hands a backend one text per call anyway.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

#: the widths the model's Matryoshka training covers (its config)
WIDTHS = (32, 64, 128, 256, 512, 768, 1024)
#: what the backend may keep reserved between texts (as DAR-legal-it's)
RELEASE_ABOVE = 3 << 30


def hub_cache(model_dir: str) -> str:
    """Where the remote code lives: inside the weights, or an error naming it."""
    cache = Path(model_dir) / "hf-cache"
    if not cache.is_dir():
        raise FileNotFoundError(f"{cache}: the model's remote code is not beside its weights "
                                f"(xlm-roberta-flash-implementation, as a Hugging Face cache)")
    return str(cache)


def normalised(vector) -> list[float]:
    v = [float(x) for x in vector]
    n = math.sqrt(sum(x * x for x in v))
    if not n or not math.isfinite(n):
        raise ValueError("a zero or non-finite vector")
    return [x / n for x in v]


def release(torch_mod, device: str, above: int = RELEASE_ABOVE) -> bool:
    dev = getattr(torch_mod, device, None) if device in ("xpu", "cuda") else None
    if dev is None or not hasattr(dev, "memory_reserved") or not hasattr(dev, "empty_cache"):
        return False
    try:
        if dev.memory_reserved() <= above:
            return False
        dev.empty_cache()
        return True
    except Exception:   # noqa: BLE001 — memory housekeeping never fails a text
        return False


def build_encoder(model_dir: str, device: str, adapter: str, dim: int, max_tokens: int):
    """(encode(text) -> unit vector of `dim`, dim)."""
    if dim not in WIDTHS:
        raise ValueError(f"width {dim}: one of {WIDTHS}")
    os.environ["HF_HUB_CACHE"] = hub_cache(model_dir)
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(model_dir, trust_remote_code=True).to(device).eval()
    adapters = getattr(model.config, "lora_adaptations", None) or ()
    if adapter not in adapters:
        raise ValueError(f"adapter {adapter!r}: the model has {', '.join(adapters)}")

    def encode(text: str) -> list[float]:
        with torch.no_grad():
            (v,) = model.encode([text], task=adapter, truncate_dim=dim, max_length=max_tokens)
        release(torch, device)
        return normalised(v)

    return encode, dim
