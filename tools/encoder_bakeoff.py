"""Which encoder actually retrieves Italian statute best — measured, on the
one public set that asks that exact question.

    python -m tools.encoder_bakeoff fetch  --out /data/bakeoff
    python -m tools.encoder_bakeoff embed  --out /data/bakeoff \
        --name bge-m3 --url http://nas:30437/upstream/bge-m3 --model bge-m3
    python -m tools.encoder_bakeoff score  --out /data/bakeoff
    python -m tools.encoder_bakeoff report --out /data/bakeoff

WHY THIS EXISTS. `gazzetta-plugin.md` section 7 says the encoder for a legal
collection is chosen on retrieval quality on Italian legal text, and that
what this node happens to run is NOT an input to that decision. Section 7.2
names the set that decides it — JuriFindIT (Dalla Noce et al., Findings of
EACL 2026), the first statutory-article-retrieval set for Italian — and
12.4 recorded its availability as UNVERIFIED because egress blocked the
check. It is verified: `jurifindit/JuriFindIT` is published as parquet.
This tool turns 7.2's procedure into something runnable against RAGE.

WHAT IS MEASURED. The 179 expert questions of the `validation` split against
the 23,617-node `corpus` split, scored with the metric definitions of
sentence-transformers' `InformationRetrievalEvaluator` — the evaluator the
one published competitor, `jurifindit/DAR-legal-it`, reports against. The
definitions are reimplemented here rather than imported because invariant 7
keeps torch out of the app and this must run in the plain dev image; they
are pinned by hand-computed tests (tests/test_encoder_bakeoff.py).

RECALL IS NOT HIT RATE HERE. A question names 4.49 relevant articles on
average and up to 128, so "did we find one" and "what fraction did we find"
are different numbers. The published table is the second one; computing the
first and comparing is a free win that is not real.

WHAT IS COMPARABLE, AND WHAT IS NOT. The models embedded through this tool
are compared to EACH OTHER rigorously: same corpus, same truncation, same
metric code, same run. Comparison to DAR-legal-it's PUBLISHED numbers is
weaker — the authors' truncation, and whether they embedded the 159
structural placeholder nodes, are not recorded anywhere we can read. Treat
their row as a reference, not as a measurement made here, until that model
is served and run through this same path.

QUERY PREFIXES, AND WHY THEY ARE A RUN PARAMETER. This codebase applies
none: `embeddings._side` switches URL for a bi-encoder pair (MedCPT) and
nothing else, so a query reaches the encoder exactly as typed. That is
right for bge-m3, which is symmetric. It is NOT what Qwen3-Embedding
documents — it expects `Instruct: <task>\nQuery: <text>` on the query side
and bare text on the document side — so measuring it bare measures what
this node serves today, not what the model can do. Both are worth knowing
and the second is nearly free: only the query side carries a prefix, so a
variant re-embeds 179 vectors and reuses all 23,617 document vectors.
`--query-prefix` therefore keys the query shards, never the corpus ones,
and the text used is recorded in the run's JSON. A prefix that silently
differed between two rows would be the whole result.

LICENCE. JuriFindIT is CC-BY-NC-SA-4.0 — a gated class under invariant 9.
`fetch` writes LICENCE.txt and a sha256 of every file beside the data so an
acknowledgement can be recorded against bytes that were actually seen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import pyarrow.parquet as pq

from app import collections as collections_mod, embeddings

BASE = ("https://huggingface.co/datasets/jurifindit/JuriFindIT/"
        "resolve/refs%2Fconvert%2Fparquet")
FILES = {"corpus.parquet": "corpus/corpus/0000.parquet",
         "questions-validation.parquet": "questions/validation/0000.parquet"}
LICENCE = "CC-BY-NC-SA-4.0 — https://creativecommons.org/licenses/by-nc-sa/4.0/"

# Every model in this bake-off declares ctx 8192, and llama.cpp REFUSES an
# input past its context rather than truncating it, so the cut is ours to
# make and must be identical for every model or the comparison is void.
# Italian legal text runs ~3.2 chars/token on a multilingual BPE; 2.5 is the
# conservative floor, which keeps the worst-tokenising text inside 8192.
DEFAULT_MAX_CHARS = 20_000
SHARD = 512
KS = (1, 3, 5, 10, 20, 40, 60, 80, 100)


@dataclass(frozen=True, slots=True)
class Corpus:
    ids: list[int]
    texts: list[str]


@dataclass(frozen=True, slots=True)
class Queries:
    texts: list[str]
    relevant: list[list[int]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name, suffix in FILES.items():
        target = out / name
        if not target.exists():
            with httpx.stream("GET", f"{BASE}/{suffix}", follow_redirects=True,
                              timeout=300) as r:
                r.raise_for_status()
                with target.open("wb") as fh:
                    for block in r.iter_bytes():
                        fh.write(block)
        digests[name] = _sha256(target)
        print(f"{name}  {target.stat().st_size:,} bytes  sha256:{digests[name]}")
    (out / "LICENCE.txt").write_text(LICENCE + "\n", encoding="utf-8")
    (out / "SOURCE.json").write_text(
        json.dumps({"dataset": "jurifindit/JuriFindIT", "licence": LICENCE,
                    "base": BASE, "sha256": digests}, indent=2) + "\n",
        encoding="utf-8")
    print(f"\nLicence is {LICENCE}.\nInvariant 9: record an acknowledgement "
          f"against these digests before the data is used.")
    return 0


def load(out: Path, max_chars: int) -> tuple[Corpus, Queries]:
    c = pq.read_table(out / "corpus.parquet").to_pylist()
    q = pq.read_table(out / "questions-validation.parquet").to_pylist()
    corpus = Corpus(ids=[r["id"] for r in c],
                    texts=[(r["content"] or "")[:max_chars] for r in c])
    queries = Queries(texts=[r["question"] for r in q],
                      relevant=[list(r["relevant_doc_ids"]) for r in q])
    missing = {i for rel in queries.relevant for i in rel} - set(corpus.ids)
    if missing:
        raise SystemExit(f"{len(missing)} relevant ids are not in the corpus — "
                         f"recall@k is undefined against it")
    return corpus, queries


def _recipe(url: str, model: str, dim: int | None, ctx: int,
            concurrency: int) -> collections_mod.EmbedRecipe:
    return collections_mod.EmbedRecipe(url=url, api="openai", model=model,
                                       dtype="f16", ctx=ctx, dim=dim,
                                       concurrency=concurrency)


def _embed_sharded(texts: list[str], where: Path, recipe, *,
                   query: bool) -> np.ndarray:
    """Shard by shard, skipping what is on disk. A run of this length must
    survive an interruption without re-spending the card (invariant 2)."""
    where.mkdir(parents=True, exist_ok=True)
    n_shards = math.ceil(len(texts) / SHARD)
    for s in range(n_shards):
        shard = where / f"{s:05d}.npy"
        if shard.exists():
            continue
        batch = texts[s * SHARD:(s + 1) * SHARD]
        vectors = embeddings.embed_texts(batch, recipe=recipe, query=query)
        np.save(shard, np.asarray(vectors, dtype=np.float32))
        done = min((s + 1) * SHARD, len(texts))
        print(f"  {where.name}: {done}/{len(texts)}", flush=True)
    return np.concatenate([np.load(where / f"{s:05d}.npy")
                           for s in range(n_shards)])


def slug_of(query_prefix: str) -> str:
    """Names the query side of a run. `bare` is the no-prefix case — the one
    this node actually serves — so it reads as a choice, not as a default
    nobody looked at."""
    if not query_prefix:
        return "bare"
    return "q" + hashlib.sha256(query_prefix.encode("utf-8")).hexdigest()[:8]


def embed(out: Path, name: str, url: str, model: str, dim: int | None,
          ctx: int, concurrency: int, max_chars: int,
          query_prefix: str = "", limit: int | None = None) -> int:
    corpus, queries = load(out, max_chars)
    if limit:
        # A smoke run: prove the URL, the model name and the whole pipeline
        # in two minutes rather than finding out 47 minutes into the real
        # one. It is NOT scoreable — see `score`.
        corpus = Corpus(ids=corpus.ids[:limit], texts=corpus.texts[:limit])
    recipe = _recipe(url, model, dim, ctx, concurrency)
    run = out / name
    slug = slug_of(query_prefix)
    print(f"{name} [{slug}]: {len(corpus.texts)} documents, "
          f"{len(queries.texts)} queries" + (f"  (LIMIT {limit})" if limit else ""))
    # The corpus side never carries the prefix, so every variant of a model
    # shares these vectors and only the cheap side is re-embedded.
    doc = _embed_sharded(corpus.texts, run / "corpus", recipe, query=False)
    qry = _embed_sharded([query_prefix + t for t in queries.texts],
                         run / f"queries-{slug}", recipe, query=True)
    np.save(run / "corpus.npy", doc)
    np.save(run / f"queries-{slug}.npy", qry)
    (run / f"run-{slug}.json").write_text(json.dumps(
        {"name": name, "slug": slug, "url": url, "model": model, "ctx": ctx,
         "max_chars": max_chars, "query_prefix": query_prefix,
         "limit": limit, "dim": int(doc.shape[1]),
         "documents": int(doc.shape[0]), "queries": int(qry.shape[0])},
        indent=2) + "\n", encoding="utf-8")
    print(f"{name} [{slug}]: dim {doc.shape[1]}")
    if limit:
        print(f"  smoke run — {limit} documents, NOT scoreable. Delete "
              f"{run} and re-embed without --limit for a real number.")
    return 0


def _dcg(relevances: list[float], k: int) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def metrics_for(ranking: list[int], relevant: set[int], ks) -> dict[str, float]:
    """The definitions of sentence-transformers' InformationRetrievalEvaluator,
    for ONE query. `ranking` is corpus ids, best first."""
    out: dict[str, float] = {}
    for k in ks:
        top = ranking[:k]
        hits = [d in relevant for d in top]
        found = sum(hits)
        out[f"recall@{k}"] = found / len(relevant) if relevant else 0.0
        out[f"ndcg@{k}"] = (
            _dcg([1.0 if h else 0.0 for h in hits], k)
            / _dcg([1.0] * len(relevant), k)) if relevant else 0.0
        rr = 0.0
        for rank, hit in enumerate(hits, start=1):
            if hit:
                rr = 1.0 / rank
                break
        out[f"mrr@{k}"] = rr
        correct = 0
        precisions = 0.0
        for rank, hit in enumerate(hits, start=1):
            if hit:
                correct += 1
                precisions += correct / rank
        out[f"map@{k}"] = (precisions / min(k, len(relevant))) if relevant else 0.0
    return out


def score_run(doc: np.ndarray, qry: np.ndarray, corpus: Corpus,
              queries: Queries, ks=KS) -> dict[str, float]:
    d = doc / np.linalg.norm(doc, axis=1, keepdims=True)
    q = qry / np.linalg.norm(qry, axis=1, keepdims=True)
    ids = np.asarray(corpus.ids)
    top = max(ks)
    totals: dict[str, float] = {}
    for i in range(q.shape[0]):
        sims = d @ q[i]
        best = np.argpartition(-sims, min(top, len(sims) - 1))[:top]
        best = best[np.argsort(-sims[best])]
        row = metrics_for([int(x) for x in ids[best]], set(queries.relevant[i]), ks)
        for key, value in row.items():
            totals[key] = totals.get(key, 0.0) + value
    return {key: value / q.shape[0] for key, value in totals.items()}


def runs_in(out: Path) -> list[tuple[Path, dict]]:
    """Every (model, query-side) pair that has been embedded, in a stable
    order so two reports of the same directory read the same."""
    found = []
    for run in sorted(p for p in out.iterdir() if p.is_dir()):
        for meta in sorted(run.glob("run-*.json")):
            found.append((run, json.loads(meta.read_text(encoding="utf-8"))))
    return found


def label_of(meta: dict) -> str:
    return meta["name"] if meta["slug"] == "bare" else f"{meta['name']} [{meta['slug']}]"


def score(out: Path, max_chars: int) -> int:
    corpus, queries = load(out, max_chars)
    for run, meta in runs_in(out):
        slug = meta["slug"]
        if meta.get("limit"):
            # Recall@k against a truncated corpus is not a smaller version of
            # the real number, it is a larger one: every document removed is a
            # distractor removed. Refuse it rather than print it with a
            # caveat nobody carries into the next conversation.
            print(f"{label_of(meta)}: SKIPPED — embedded with "
                  f"--limit {meta['limit']}; a partial corpus inflates every "
                  f"metric here")
            continue
        doc = np.load(run / "corpus.npy")
        qry = np.load(run / f"queries-{slug}.npy")
        result = score_run(doc, qry, corpus, queries)
        (run / f"scores-{slug}.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"{label_of(meta)}: mrr@100 {result['mrr@100']:.4f}  "
              f"recall@10 {result['recall@10']:.4f}")
    return 0


PUBLISHED = {"DAR-legal-it (published)": {
    "recall@3": 0.7111, "recall@5": 0.7602, "recall@10": 0.8037,
    "recall@20": 0.8331, "recall@40": 0.8609, "recall@60": 0.8861,
    "recall@80": 0.8957, "recall@100": 0.9129, "ndcg@5": 0.7419,
    "ndcg@100": 0.7459, "mrr@5": 0.7719, "mrr@100": 0.7780,
    "map@5": 0.6985, "map@100": 0.7015}}


def report(out: Path) -> int:
    rows = dict(PUBLISHED)
    prefixes: dict[str, str] = {}
    for run, meta in runs_in(out):
        scores = run / f"scores-{meta['slug']}.json"
        if not scores.exists():
            continue
        rows[label_of(meta)] = json.loads(scores.read_text(encoding="utf-8"))
        if meta.get("query_prefix"):
            prefixes[meta["slug"]] = meta["query_prefix"]
    shown = ["recall@3", "recall@5", "recall@10", "recall@100",
             "ndcg@5", "ndcg@100", "mrr@5", "mrr@100", "map@100"]
    width = max(len(name) for name in rows)
    print("| " + "model".ljust(width) + " | "
          + " | ".join(m.rjust(10) for m in shown) + " |")
    print("|" + "-" * (width + 2) + "|"
          + "|".join("-" * 12 for _ in shown) + "|")
    for name, result in rows.items():
        cells = [f"{result[m]:.4f}".rjust(10) if m in result else "-".rjust(10)
                 for m in shown]
        print("| " + name.ljust(width) + " | " + " | ".join(cells) + " |")
    for slug, text in sorted(prefixes.items()):
        print(f"\n[{slug}] query prefix: {text!r}")
    print("\nDAR-legal-it's row is its PUBLISHED table, not a run of this tool: "
          "its truncation and corpus handling are unrecorded. The other rows "
          "are comparable to each other exactly. A row with no [slug] embedded "
          "queries bare, which is what this node serves today.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("fetch", "embed", "score", "report"):
        p = sub.add_parser(cmd)
        p.add_argument("--out", type=Path, required=True)
        if cmd in ("embed", "score"):
            p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
        if cmd == "embed":
            p.add_argument("--name", required=True)
            p.add_argument("--url", required=True)
            p.add_argument("--model", required=True)
            p.add_argument("--dim", type=int, default=None)
            p.add_argument("--ctx", type=int, default=8192)
            p.add_argument("--concurrency", type=int, default=1)
            p.add_argument("--query-prefix", default="",
                           help="prepended to QUERIES only, never to "
                                "documents; recorded in the run's JSON. "
                                "Qwen3-Embedding documents "
                                "'Instruct: <task>\\nQuery: '")
            p.add_argument("--limit", type=int, default=None,
                           help="embed only the first N documents, to prove "
                                "the endpoint in minutes. Marks the run "
                                "unscoreable — a partial corpus inflates "
                                "every metric")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out)
    if a.cmd == "embed":
        return embed(a.out, a.name, a.url, a.model, a.dim, a.ctx,
                     a.concurrency, a.max_chars, a.query_prefix, a.limit)
    if a.cmd == "score":
        return score(a.out, a.max_chars)
    return report(a.out)


if __name__ == "__main__":
    raise SystemExit(main())
