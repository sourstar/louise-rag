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
deploy/rage/localai-models/suspended/. Its HTTP server, its length-budgeted
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


def build_encoder(model_dir: str, device: str, max_seq: int):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        model_dir, device=device, trust_remote_code=True,
        model_kwargs={"attn_implementation": "sdpa"},
        config_kwargs={"use_memory_efficient_attention": False,
                       "unpad_inputs": False})
    model.max_seq_length = max_seq

    def encode(batch: list[str]) -> list[list[float]]:
        vectors = model.encode(batch, batch_size=len(batch),
                               convert_to_numpy=True)
        return [v.astype("float32").tolist() for v in vectors]

    return encode, int(model.get_sentence_embedding_dimension())
