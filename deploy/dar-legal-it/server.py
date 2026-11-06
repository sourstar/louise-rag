"""DAR-legal-it as an embedding model on the Intel GPU, through torch XPU.

Why this exists (docs/notes/gazzetta-plugin.md §7): the legal collection's
encoder is `jurifindit/DAR-legal-it`, and it cannot be served the way every
other embedder on this node is. It declares `GteModel`, and llama.cpp
registers no such architecture — not at b10630, nor at master. The
arctic-embed GGUFs that exist are the `-l-v2.0` variant, which declares
`XLMRobertaModel`; this model's family is the unsupported one. So GGUF is
closed and Python is the route, as it is for MADLAD and MedCPT.

SERVED BY LocalAI: deploy/localai/backends/louise-dar-legal-it imports this
module and answers `Embedding`, one text per call. SUSPENDED since 2026-10-04
(operator): no collection uses it, and its YAML is in
the model pool's suspended model files (in git before 6a4f517). Its HTTP server, its length-budgeted
batches and its query prefix went with llama-swap at 15g
(docs/notes/legacy-to-delete.md §8): LocalAI sends one text at a time, and
Louise prepends the Codex's `space.query.instruction` herself.

TWO THINGS DECIDE THE VECTOR HERE, and getting either wrong produces
plausible numbers that are not this model's:

- **MEAN pooling**, over the attention mask. Its `1_Pooling/config.json`
  says `pooling_mode_mean_tokens: true`, NOT cls — unlike bge-m3 beside it,
  which llama.cpp runs with `pooling:cls`. Copying that option here would be
  the easy mistake. SentenceTransformer reads the pooling from the snapshot.
- **sdpa attention.** Its remote code asserts "please install xformers" for
  the memory-efficient path, which is CUDA's; sdpa is the same function.

On XPU the attention materialises heads x seq x seq, so a long text costs
the SQUARE of its length (sixteen long ones asked for 48 GiB, 2026-09-20);
one text per call keeps that to one text's square.
"""
from __future__ import annotations


#: what the backend may keep reserved between texts. The weights are ~1.2 GB in
#: float32; one 8,192-token article's attention peaks at several GB (12 heads
#: x seq x seq), and torch's caching allocator keeps every block it once took:
#: on the node the backend sat at 12.6–13.6 GB for a model that needs ~1.5
#: (2026-10-14), pushing the card over LocalAI's eviction threshold.
RELEASE_ABOVE = 3 << 30


def release(torch_mod, device: str, above: int = RELEASE_ABOVE) -> bool:
    """Hand torch's cached blocks back to the driver when more than `above`
    bytes are reserved: after a long article, never after each short one (a
    re-allocation costs a moment). The arithmetic is untouched, so the
    vectors are the same. True when it released."""
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


#: Invariant 12 (ENCODER DETERMINISM) for torch, the same switch as the jina-v3
#: task's (deploy/jina-embeddings-v3/server.py): set in the MODEL's env, so the
#: stated run carries it. DAR-legal-it on the B70 was 1/4 bit-identical against
#: itself (2026-10-17) and its calibration fingerprint moved across three
#: byte-identical trees (2026-10-20); jina-v3 on the same stack went from
#: 204/512 to 512/512 with it (approved-encoders.md, the torch row). Unset,
#: torch's defaults — the baseline the measurement compares against.
DETERMINISM_ENV = "LOUISE_TORCH_DETERMINISTIC"


def determinism(torch_mod, environ=None) -> bool:
    """torch's deterministic algorithms and oneDNN's deterministic primitives,
    when the model's env asks; an op with no deterministic kernel then fails
    the text rather than answering from one that may not reproduce."""
    import os
    environ = os.environ if environ is None else environ
    if environ.get(DETERMINISM_ENV, "").strip().lower() not in ("1", "true", "yes"):
        return False
    torch_mod.use_deterministic_algorithms(True)
    mkldnn = getattr(getattr(torch_mod, "backends", None), "mkldnn", None)
    if mkldnn is not None and hasattr(mkldnn, "deterministic"):
        mkldnn.deterministic = True
    # on CUDA: cuDNN's deterministic algorithms, never one chosen by timing
    # (cuBLAS's own half is the model's env, CUBLAS_WORKSPACE_CONFIG)
    cudnn = getattr(getattr(torch_mod, "backends", None), "cudnn", None)
    if cudnn is not None and hasattr(cudnn, "deterministic"):
        cudnn.deterministic, cudnn.benchmark = True, False
    return True


def build_encoder(model_dir: str, device: str, max_seq: int):
    import torch
    from sentence_transformers import SentenceTransformer

    determinism(torch)                      # before any kernel is chosen
    model = SentenceTransformer(
        model_dir, device=device, trust_remote_code=True,
        model_kwargs={"attn_implementation": "sdpa"},
        config_kwargs={"use_memory_efficient_attention": False,
                       "unpad_inputs": False})
    model.max_seq_length = max_seq

    def encode(batch: list[str]) -> list[list[float]]:
        vectors = model.encode(batch, batch_size=len(batch),
                               convert_to_numpy=True)
        out = [v.astype("float32").tolist() for v in vectors]
        release(torch, device)
        return out

    return encode, int(model.get_sentence_embedding_dimension())
