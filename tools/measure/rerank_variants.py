"""MEASUREMENT (not product code): three ways to keep the reranker from
undoing the fusion's title matches (docs/notes/query-campaign.md, run 4),
scored on the SAME answers in the live web process — the product's own
search on the API's path (translate false, limit 10, depth 50), its fused
candidates and its reranker, then each variant through the product's own
one-hit-per-work and the cut to 10:

  base  the reranked order, as shipped (05 §5.6)
  a     base, with the catalogue leg's rank-1 record first
  b     the PRODUCT's fusion of the two orders: `_rerank_list` again with
        `retrieval.RERANK_FUSE = True` (dc36f1a, `_rerank_order`) — the
        reranker is bit-exact on reprocessing (2026-10-20, 23:20), so its
        scores are the base's
  c     a second reranker call with each chunk cut to its first C_CHARS
        characters under the same title line (VARIANTS includes "c")

Nothing is written. The script reaches the node on stdin with `DATA`
prepended: [{qid, kind, query, books}], a query's relevant answer being any
of `books` shown or grouped under a hit (its `editions`). One JSON line per
query, then a summary per kind and variant on stderr.
"""
import json
import sys

from app import collections as collections_mod, retrieval
from app.ask import results

# the collection files withhold the reranker until it is chosen (2026-10-20):
# the measurement names it, as a file's [reranker] would
_RECIPE = collections_mod.RerankRecipe(url=globals().get("RERANK_URL", "http://192.168.178.10:30286/v1/rerank"),
                                       api="jina", model=globals().get("RERANK_MODEL", "louise-rerank-bge"))
collections_mod.reranker_for = lambda collection: _RECIPE

C_CHARS = 300
VARIANTS = tuple(globals().get("VARIANTS", ("base", "a", "b", "c")))
stash: dict = {}
_orig_rerank = retrieval._rerank_list
_orig_group = retrieval._one_hit_per_work


def _uid(h):
    return retrieval.record_of(h)


def _rerank(query, hits, recipe, budget_s, depth=retrieval.RERANK_TOP_N):
    ranked, ok = _orig_rerank(query, hits, recipe, budget_s, depth)
    stash.update(fused=list(hits[:depth]), ranked=list(ranked), ok=ok, depth=depth)
    if "b" in VARIANTS and ok:
        retrieval.RERANK_FUSE = True
        try:
            ranked_b, ok_b = _orig_rerank(query, hits, recipe, budget_s, depth)
        finally:
            retrieval.RERANK_FUSE = False
        stash.update(ranked_b=ranked_b if ok_b else None)
    if "c" in VARIANTS and ok:
        short = []
        for h in hits:
            h2 = dict(h)
            if h2.get(retrieval.MATCHED):
                h2[retrieval.MATCHED] = [{**c, "text": (c.get("text") or "")[:C_CHARS]}
                                         for c in h2[retrieval.MATCHED]]
            h2["text"] = (h2.get("text") or "")[:C_CHARS]
            short.append(h2)
        ranked_c, ok_c = _orig_rerank(query, short, recipe, budget_s, depth)
        stash.update(ranked_c=ranked_c if ok_c else None)
    return ranked, ok


def _variant(name: str, ranked: list[dict]) -> list[dict]:
    depth = stash["depth"]
    head, tail = ranked[:depth], ranked[depth:]
    if name == "a":
        first = next((h for h in head if (h.get("leg_ranks") or {}).get("catalog") == 1), None)
        return ([first] + [h for h in head if h is not first] + tail) if first is not None else ranked
    return ranked


def _group(hits, collection, plan, show_in, annotations=None):
    out = _orig_group(hits, collection, plan, show_in, annotations)
    if stash.get("ok"):
        grouped = {}
        for name in VARIANTS:
            if name in ("b", "c"):
                base = stash.get(f"ranked_{name}")
                if base is None:
                    continue
                order = base + [h for h in hits if _uid(h) not in {_uid(x) for x in base}]
            else:
                order = _variant(name, hits)
            grouped[name] = _orig_group([dict(h) for h in order], collection, plan, show_in, annotations)[:10]
        stash["grouped"] = grouped
    return out


retrieval._rerank_list = _rerank
retrieval._one_hit_per_work = _group


def _rank(hits, books):
    for i, h in enumerate(hits, 1):
        if h.get("uid") in books or _uid(h) in books or \
                any(e.get("uid") in books for e in h.get("editions") or []):
            return i
    return None


per: dict = {}
for q in DATA:
    stash.clear()
    books = {f"gutenberg:{b}" for b in q["books"]}
    try:
        results.search(q["query"], collections=["gutenberg"], limit=10)
        err = None if stash.get("grouped") else f"no rerank ({stash.get('ok')})"
    except Exception as e:   # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:200]
    ranks = {n: _rank(g, books) for n, g in (stash.get("grouped") or {}).items()}
    print(json.dumps({"qid": q["qid"], "kind": q["kind"], "query": q["query"], "ranks": ranks, "error": err}),
          flush=True)
    if err is None:
        for n, r in ranks.items():
            per.setdefault((q["kind"], n), []).append(r)
for (kind, name), ranks in sorted(per.items()):
    n = len(ranks)
    print(json.dumps({"kind": kind, "variant": name, "n": n,
                      "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
                      "r1": round(sum(1 for r in ranks if r == 1) / n, 3),
                      "r3": round(sum(1 for r in ranks if r and r <= 3) / n, 3),
                      "r10": round(sum(1 for r in ranks if r) / n, 3)}), file=sys.stderr)
