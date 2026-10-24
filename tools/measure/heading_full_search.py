"""MEASUREMENT (not product code): BM25F's heading weight k through the FULL
search (docs/notes/fts-headings.md) — the Ask page's default path,
`query_service.run_search(["gutenberg"], q, 10, "hybrid", True)`, reranker
on, as deployed, on the whole labelled set.

Each query is searched once per ARM. Arm 0 is the live text indexes; arm k
answers gutenberg's `en` group from the scratch index built with
`fts.HEADING_IN_BODY = k` (tools/measure/heading_build.py, mounted at
/scratch/k<k>) with the query read the BM25F way (`HEADING_IN_BODY = k`
inside that call only; every other group stays live, at 0). The language
plan is cached per query (`language_plan.plan`), so the arms share one
translation; the dense leg, the fusion and the reranker run per arm as the
product runs them. Nothing is written.

The script reaches the node on stdin with `DATA` prepended:
[{qid, kind, lang, query, books}], and optionally `ARMS` (default (0, 2, 3)).
One JSON line per query on stdout; a summary per kind and arm on stderr.
"""
import copy
import json
import sys
import time
from pathlib import Path

from app import fts, query_service, retrieval

ARMS = tuple(globals().get("ARMS", (0, 2, 3)))
SCRATCH = Path(globals().get("SCRATCH", "/scratch"))
#: a CPU-only smoke test sets MODE = "fts", TRANSLATE = False, RERANK = False
MODE = globals().get("MODE", "hybrid")
TRANSLATE = bool(globals().get("TRANSLATE", True))
RERANK = bool(globals().get("RERANK", True))
arm = 0
stash: dict = {}
_scratch: dict = {}
_orig_search = fts.search
_orig_reranked = retrieval._reranked


def _search(group, query, limit=20, collection=None, **kw):
    if arm and group == "en" and collection is not None and collection.name == "gutenberg":
        key = (arm, str(collection.fts_dir))
        if key not in _scratch:
            c2 = copy.copy(collection)
            object.__setattr__(c2, "fts_dir", SCRATCH / f"k{arm}")
            _scratch[key] = c2
        fts.HEADING_IN_BODY = arm
        try:
            return _orig_search(group, query, limit, _scratch[key], **kw)
        finally:
            fts.HEADING_IN_BODY = 0
    return _orig_search(group, query, limit, collection, **kw)


def _reranked(*a, **kw):
    t = time.monotonic()
    out = _orig_reranked(*a, **kw)
    stash["reranked"], stash["ms"] = out[1], round((time.monotonic() - t) * 1000)
    return out


fts.search = _search
retrieval._reranked = _reranked
if not RERANK:
    from app import collections as _collections
    _collections.reranker_for = lambda collection: None


def _rank(hits, books):
    for i, h in enumerate(hits, 1):
        if h.get("uid") in books or retrieval.record_of(h) in books or \
                any(e.get("uid") in books for e in h.get("editions") or []):
            return i
    return None


per: dict = {}
for q in DATA:  # noqa: F821 — prepended by the caller
    books = set(q["books"])
    row = {"qid": q["qid"], "kind": q["kind"], "lang": q["lang"], "query": q["query"],
           "error": None, "ranks": {}, "ms": {}}
    for a in ARMS:
        arm = a
        stash.clear()
        try:
            hits = query_service.run_search(["gutenberg"], q["query"], 10, MODE, TRANSLATE)["hits"]
            if RERANK and not stash.get("reranked"):
                row["error"] = f"no rerank (arm {a})"
            row["ranks"][str(a)] = _rank(hits[:10], books)
            row["ms"][str(a)] = stash.get("ms")
        except Exception as e:   # noqa: BLE001
            row["error"] = f"arm {a}: {type(e).__name__}: {e}"[:200]
        finally:
            arm = 0
        if row["error"]:
            break
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if row["error"] is None:
        for a, r in row["ranks"].items():
            per.setdefault((q["kind"], a), []).append(r)
for (kind, a), ranks in sorted(per.items()):
    n = len(ranks)
    print(json.dumps({"kind": kind, "k": int(a), "n": n,
                      "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
                      "r1": round(sum(1 for r in ranks if r == 1) / n, 3),
                      "r10": round(sum(1 for r in ranks if r) / n, 3)}), file=sys.stderr)
