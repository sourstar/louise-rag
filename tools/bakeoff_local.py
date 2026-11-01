"""Embed the bake-off corpus with a sentence-transformers model, locally.

    python -m tools.bakeoff_local --out /bakeoff --name DAR-legal-it \
        --model jurifindit/DAR-legal-it --query-prefix "query: "

WHY THIS EXISTS, AND WHY IT IS WORTH MORE THAN A FOURTH ROW.
`encoder_bakeoff.py` measures what the model pool serves. `jurifindit/DAR-legal-it`
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
import signal
import sys
from pathlib import Path

import numpy as np

from tools.encoder_bakeoff import (DEFAULT_MAX_CHARS, SHARD, Corpus, load,
                                   slug_of)

CHARS_PER_TOKEN = 3.2  # Italian legal text on a multilingual BPE

#: set by SIGTERM/SIGINT; read between shards
_stopping = False


def _stop(signum, frame) -> None:
    global _stopping
    _stopping = True
    print(f"  signal {signum}: finishing this shard, then stopping",
          flush=True)


def install_signal_handlers() -> None:
    """As PID 1 under docker the kernel gives SIGTERM no default action, so
    without this the container is only killable with SIGKILL — which is how
    a run gets abandoned mid-shard. Invariant 2's shape: finish the unit,
    commit it, exit 130. A shard IS the unit here, and it is written before
    the next begins, so stopping between shards loses nothing and the next
    start resumes from disk."""
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def encode_budgeted(model, texts: list[str], max_seq: int, max_batch: int,
                    budget: float) -> np.ndarray:
    """Encode with a batch size that adapts to the longest text in it.

    The attention this model falls back to on XPU materialises a
    batch × heads × seq × seq tensor, so cost goes with the SQUARE of the
    longest text in a batch. A fixed batch size is therefore wrong in both
    directions at once: 16 short articles barely touch the card, 16 long
    ones asked for 48 GiB of it. Grouping by length and spending a fixed
    batch*seq^2 budget instead keeps every forward pass the same size in
    memory, which is the thing that is actually scarce.

    Longest first, so a budget that is too large fails in seconds rather
    than an hour in.
    """
    order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
    out: list[np.ndarray | None] = [None] * len(texts)
    i = 0
    while i < len(order):
        seq = min(max_seq, max(1, int(len(texts[order[i]]) / CHARS_PER_TOKEN)))
        n = max(1, min(max_batch, int(budget / (seq * seq))))
        group = order[i:i + n]
        vectors = model.encode([texts[j] for j in group],
                               batch_size=len(group), convert_to_numpy=True)
        for j, v in zip(group, vectors):
            out[j] = v.astype(np.float32)
        i += n
    return np.stack(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--query-prefix", default="")
    ap.add_argument("--doc-prefix", default="",
                    help="prepended to DOCUMENTS. E5 wants 'passage: ' here "
                         "and 'query: ' on queries; measuring it without "
                         "both understates it. Unlike --query-prefix this "
                         "changes the corpus vectors, so it needs its own "
                         "--name rather than sharing one")
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
    ap.add_argument("--attn-budget", type=float, default=4.0e7,
                    help="max batch*seq^2 per forward pass. Attention memory "
                         "here scales with the SQUARE of the longest text in "
                         "a batch, not with the batch's token count, so a "
                         "fixed --batch either wastes the card on short "
                         "documents or dies on one long one. 4e7 is ~2 GB "
                         "with 12 heads in fp32")
    a = ap.parse_args(argv)
    install_signal_handlers()

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

    # Reusing ANY document vectors — the finished corpus.npy or a half-built
    # set of shards — is only safe while the settings that decide a vector
    # are unchanged. Re-running at a different --max-seq or --doc-prefix
    # would otherwise blend two encodings into one row: plausible,
    # internally consistent, and wrong. Checked before either reuse path.
    stamp = {"model": a.model, "max_seq": seq, "max_chars": a.max_chars,
             "doc_prefix": a.doc_prefix, "attn": a.attn}
    stamp_path = run / "encoding.json"
    if stamp_path.exists():
        was = json.loads(stamp_path.read_text(encoding="utf-8"))
        if was != stamp:
            changed = [k for k in stamp if was.get(k) != stamp[k]]
            raise SystemExit(
                f"{run} holds vectors made with different settings "
                f"({', '.join(changed)} differ). Delete it and re-embed — "
                f"reusing them would mix two encodings into one row.\n"
                f"  on disk: {was}\n  now:     {stamp}")
    else:
        stamp_path.write_text(json.dumps(stamp, indent=2) + "\n",
                              encoding="utf-8")

    doc_path = run / "corpus.npy"
    if doc_path.exists():
        doc = np.load(doc_path)
        print(f"  corpus: reusing {doc.shape[0]} vectors already on disk")
    else:
        # Sharded, like the model pool side. On CPU this is hours, and the long
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
            part = [a.doc_prefix + t
                    for t in corpus.texts[s * SHARD:(s + 1) * SHARD]]
            vectors = encode_budgeted(model, part, seq, a.batch, a.attn_budget)
            np.save(shard, vectors)
            done = min((s + 1) * SHARD, len(corpus.texts))
            print(f"  corpus: {done}/{len(corpus.texts)}", flush=True)
            if _stopping:
                print(f"stopped after {done} documents; re-run to resume")
                return 130
        doc = np.concatenate([np.load(shards / f"{s:05d}.npy")
                              for s in range(n_shards)])
        np.save(doc_path, doc)

    qry = encode_budgeted(model, [a.query_prefix + t for t in queries.texts],
                          seq, a.batch, a.attn_budget)
    np.save(run / f"queries-{slug}.npy", qry)

    (run / f"run-{slug}.json").write_text(json.dumps(
        {"name": a.name, "slug": slug, "url": f"local:{a.device}",
         "model": a.model, "ctx": seq, "max_chars": a.max_chars,
         "query_prefix": a.query_prefix, "doc_prefix": a.doc_prefix,
         "limit": a.limit,
         "dim": int(doc.shape[1]), "documents": int(doc.shape[0]),
         "queries": int(qry.shape[0])},
        indent=2) + "\n", encoding="utf-8")
    print(f"{a.name} [{slug}]: dim {doc.shape[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
