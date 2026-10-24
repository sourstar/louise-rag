"""MEASUREMENT (not product code): query campaign run 5
(docs/notes/query-campaign.md) — 834baf5's rerank, each candidate against
the question in its own language, against the reranker as shipped, on the
SAME fused candidates in ONE live search per query.

Runs in a process holding 834baf5's `app/` (its `_rerank_list` with
`queries=None` is the shipped behaviour: one group, the typed question).
Each query goes through the Ask page's default path,
`query_service.run_search(["gutenberg"], q, 10, "hybrid", True)` — translate
on, so the language plan's variants exist. `retrieval._reranked` is wrapped
to rerank the candidates twice:

  base   queries=None   (the typed question for every candidate)
  score  the plan's variants by language, ordered by raw score (834baf5)
  slots  the same scores, each language keeping its fused positions (8bee186)

and each order goes through the product's own `_one_hit_per_work` and the
cut to 10. The search itself continues with `base`. Nothing is written.

The script reaches the node on stdin with `DATA` prepended:
[{qid, kind, lang, query, books}]; a query's answer is any of `books`
shown or grouped under a hit (its `editions`). One JSON line per query on
stdout; a summary per kind and variant on stderr.
"""
import json
import sys
import time

from app import query_service, retrieval

stash: dict = {}
_orig_reranked = retrieval._reranked
_orig_group = retrieval._one_hit_per_work
#: run 5: all three; run 6 (another reranker): ("base",), with RERANK_MODEL set
VARIANTS = tuple(globals().get("VARIANTS", ("base", "score", "slots")))
if globals().get("RERANK_MODEL"):
    from app import collections as _collections
    _RECIPE = _collections.RerankRecipe(url=globals().get("RERANK_URL", "http://192.168.178.10:30286/v1/rerank"),
                                        api="jina", model=RERANK_MODEL)  # noqa: F821
    _collections.reranker_for = lambda collection: _RECIPE


def _reranked(query, hits, recipe, budget_s, depth, queries=None):
    got = {}
    for name, qs, order in (v for v in (("base", None, "score"), ("score", queries, "score"), ("slots", queries, "slots")) if v[0] in VARIANTS):
        retrieval.RERANK_LANGUAGE_ORDER = order
        t = time.monotonic()
        got[name] = _orig_reranked(query, [dict(h) for h in hits], recipe, budget_s, depth, qs)
        stash["ms_" + name] = round((time.monotonic() - t) * 1000)
    retrieval.RERANK_LANGUAGE_ORDER = "score"
    stash.update(got, variants=sorted((queries or {}).keys()))
    return got["base"]


def _group(hits, collection, plan, show_in, annotations=None):
    out = _orig_group(hits, collection, plan, show_in, annotations)
    grouped = {}
    for name in VARIANTS:
        got = stash.get(name)
        if got and got[1]:
            grouped[name] = _orig_group([dict(h) for h in got[0]], collection, plan, show_in, annotations)[:10]
    stash["grouped"] = grouped
    return out


retrieval._reranked = _reranked
retrieval._one_hit_per_work = _group


def _rank(hits, books):
    for i, h in enumerate(hits, 1):
        if h.get("uid") in books or retrieval.record_of(h) in books or \
                any(e.get("uid") in books for e in h.get("editions") or []):
            return i
    return None


per: dict = {}
for q in DATA:  # noqa: F821 — prepended by the caller
    stash.clear()
    books = set(q["books"])
    try:
        query_service.run_search(["gutenberg"], q["query"], 10, "hybrid", True)
        err = None if len(stash.get("grouped") or {}) == len(VARIANTS) else \
            "no rerank (" + ", ".join(f"{n} {bool(stash.get(n) and stash[n][1])}" for n in VARIANTS) + ")"
    except Exception as e:   # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:200]
    grouped = stash.get("grouped") or {}
    row = {"qid": q["qid"], "kind": q["kind"], "lang": q["lang"], "query": q["query"], "error": err,
           "ranks": {n: _rank(g, books) for n, g in grouped.items()},
           "other_lang": {n: sum(1 for h in g if (h.get("lang") or "und") != q["lang"]) for n, g in grouped.items()},
           "ms": {n: stash.get("ms_" + n) for n in VARIANTS}, "variants": stash.get("variants")}
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if err is None:
        for n, r in row["ranks"].items():
            per.setdefault((q["kind"], n), []).append((r, row["other_lang"][n], row["ms"][n]))
for (kind, name), rows in sorted(per.items()):
    n = len(rows)
    ranks = [r for r, _, _ in rows]
    ms = sorted(m for _, _, m in rows if m is not None)
    print(json.dumps({"kind": kind, "variant": name, "n": n,
                      "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
                      "r1": round(sum(1 for r in ranks if r == 1) / n, 3),
                      "r10": round(sum(1 for r in ranks if r) / n, 3),
                      "other_lang_per_10": round(sum(o for _, o, _ in rows) / n, 2),
                      "rerank_ms_median": ms[len(ms) // 2] if ms else None}), file=sys.stderr)
