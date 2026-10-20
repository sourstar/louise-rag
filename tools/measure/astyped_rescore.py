"""MEASUREMENT (not product code): the fusion of the code on PYTHONPATH,
replayed over lists captured on the API's as-typed path (translate false,
the 17 language groups as typed, the catalogue, the vector leg; captured in
the live web process before the reranker, docs/notes/query-campaign.md run
3). Prints one JSON line per query, the rank in the fused top 50 of the
first of the query's books, then a summary by kind. Run once per tree:

  PYTHONPATH=<tree> python tools/measure/astyped_rescore.py CAPTURE.jsonl
"""
from __future__ import annotations

import json
import sys

from app import retrieval


def main(path: str) -> None:
    by_kind: dict[str, list] = {}
    for line in open(path, encoding="utf-8"):
        if not line.startswith("{"):
            continue                    # a log line the web process printed
        q = json.loads(line)
        if q.get("error") or not q.get("lists"):
            continue
        merged = retrieval._merge_rrf(q["lists"], q.get("depth") or 50, own=set(q.get("own") or ()),
                                      query_lang=q.get("query_lang"))
        books = set(q["books"])
        ranked = [int(h["book_id"]) for h in merged][:50]
        rank = next((i for i, b in enumerate(ranked, 1) if b in books), None)
        by_kind.setdefault(q["kind"], []).append(rank)
        print(json.dumps({"qid": q["qid"], "kind": q["kind"], "rank": rank}))
    for kind, ranks in by_kind.items():
        n = len(ranks)
        print(json.dumps({"summary": kind, "n": n,
                          "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
                          "r1": round(sum(1 for r in ranks if r and r <= 1) / n, 3),
                          "r10": round(sum(1 for r in ranks if r and r <= 10) / n, 3),
                          "r50": round(sum(1 for r in ranks if r) / n, 3)}), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
