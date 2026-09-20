"""Embed the bake-off corpus with a sentence-transformers model, locally.

    python -m tools.bakeoff_local --out /bakeoff --name DAR-legal-it \
        --model jurifindit/DAR-legal-it --query-prefix "query: "

WHY THIS EXISTS, AND WHY IT IS WORTH MORE THAN A FOURTH ROW.
`encoder_bakeoff.py` measures what RAGE serves. `jurifindit/DAR-legal-it`
is not served there, so its row in the report has been its PUBLISHED table
— produced by the authors, with their truncation and their handling of the
corpus's placeholder nodes, neither of which is recorded anywhere we can
read. That makes it a reference, not a measurement, and every comparison
against it carries that asterisk.

Running it here removes the asterisk twice over:

1. It becomes a row measured on our corpus, our truncation, our metric
   code — comparable to the other rows exactly, like they are to each other.
2. **It checks our metric code against a published number.** If this run
   lands near recall@10 0.8037 / mrr@100 0.7780, then our reimplementation
   of `InformationRetrievalEvaluator` agrees with the real one, and the
   whole table stops being merely self-consistent and becomes comparable to
   anything else published against this set. If it lands far away, one of
   the two is wrong and we would rather know.

CPU IS NOT A CAVEAT HERE. Device changes throughput, not the function —
this deployment measured the same weights cross-backend at cosine 0.9996+
(`approved-encoders.md`). A quality number from CPU is a quality number.

THE ONE REAL DIFFERENCE, AND IT IS DELIBERATE. `--max-seq` defaults to the
model's own `max_seq_length` rather than being forced to match the 8192 the
llama.cpp rows got, because reproducing the authors' figure is the point of
(2) and they will have used the default. It is recorded in the run's JSON.
If it turns out to be much shorter than 8192, that is a finding about the
comparison, not a bug in it.

TRUST_REMOTE_CODE. This model is GTE-architecture on
`Snowflake/snowflake-arctic-embed-m-v2.0` and needs `trust_remote_code` to
load, i.e. it executes code from the Hub repo. That is a deliberate
decision, not a default: it is off unless `--trust-remote-code` is passed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from tools.encoder_bakeoff import (DEFAULT_MAX_CHARS, SHARD, Corpus, load,
                                   slug_of)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--query-prefix", default="")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--max-seq", type=int, default=None,
                    help="override the model's own max_seq_length")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--attn", default="eager",
                    help="attention implementation. GTE's remote code asserts "
                         "'please install xformers' for its memory-efficient "
                         "path, which is a CUDA route; eager is the same "
                         "function, slower")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)

    from sentence_transformers import SentenceTransformer

    corpus, queries = load(a.out, a.max_chars)
    if a.limit:
        corpus = Corpus(ids=corpus.ids[:a.limit], texts=corpus.texts[:a.limit])

    # Attention implementation changes the arithmetic's ORDER, not the
    # function: eager and a fused kernel agree to float noise. It is safe for
    # a quality number in a way that, say, a different pooling would not be.
    model = SentenceTransformer(
        a.model, device=a.device, trust_remote_code=a.trust_remote_code,
        model_kwargs={"attn_implementation": a.attn},
        config_kwargs={"use_memory_efficient_attention": False,
                       "unpad_inputs": False})
    if a.max_seq:
        model.max_seq_length = a.max_seq
    seq = int(model.max_seq_length)
    slug = slug_of(a.query_prefix)
    run = a.out / a.name
    run.mkdir(parents=True, exist_ok=True)
    print(f"{a.name} [{slug}]: {len(corpus.texts)} documents, "
          f"{len(queries.texts)} queries, max_seq_length {seq}, "
          f"device {a.device}", flush=True)

    doc_path = run / "corpus.npy"
    if doc_path.exists():
        doc = np.load(doc_path)
        print(f"  corpus: reusing {doc.shape[0]} vectors already on disk")
    else:
        # Sharded, like the RAGE side. On CPU this is hours, and the long
        # tail of the corpus can still OOM a batch however it is sized —
        # losing the whole run to the last document would be absurd. Shards
        # also let a killed run resume at a smaller --batch.
        shards = run / "corpus"
        shards.mkdir(parents=True, exist_ok=True)
        n_shards = math.ceil(len(corpus.texts) / SHARD)
        for s in range(n_shards):
            shard = shards / f"{s:05d}.npy"
            if shard.exists():
                continue
            part = corpus.texts[s * SHARD:(s + 1) * SHARD]
            vectors = model.encode(part, batch_size=a.batch,
                                   convert_to_numpy=True).astype(np.float32)
            np.save(shard, vectors)
            done = min((s + 1) * SHARD, len(corpus.texts))
            print(f"  corpus: {done}/{len(corpus.texts)}", flush=True)
        doc = np.concatenate([np.load(shards / f"{s:05d}.npy")
                              for s in range(n_shards)])
        np.save(doc_path, doc)

    qry = model.encode([a.query_prefix + t for t in queries.texts],
                       batch_size=a.batch, convert_to_numpy=True).astype(np.float32)
    np.save(run / f"queries-{slug}.npy", qry)

    (run / f"run-{slug}.json").write_text(json.dumps(
        {"name": a.name, "slug": slug, "url": f"local:{a.device}",
         "model": a.model, "ctx": seq, "max_chars": a.max_chars,
         "query_prefix": a.query_prefix, "limit": a.limit,
         "dim": int(doc.shape[1]), "documents": int(doc.shape[0]),
         "queries": int(qry.shape[0])},
        indent=2) + "\n", encoding="utf-8")
    print(f"{a.name} [{slug}]: dim {doc.shape[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
