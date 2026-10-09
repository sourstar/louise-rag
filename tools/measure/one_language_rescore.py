"""MEASUREMENT (not product code): the fusion and grouping fixes of
2026-10-09, scored OFFLINE on the legs `one_language_search.py` captured
(docs/notes/one-language-per-work.md, §3 "Captured legs").

The captured legs are the lists exactly as they entered RRF on the node —
one per text index group, one for the vector leg — and today's fusion
recomputed from them reproduces the live fused order on every query. So the
fixes are scored by running THE PRODUCT CODE on the same legs:

  before   today's fusion (`fuse_code` of one_language_score.py: chunks, one
           list per language group)
  after    `app.retrieval._merge_rrf(legs, 50, own=…, query_lang=…)` — two
           legs, the record as the unit, ties in the text leg by the user's
           own words, then the query's language (05 §5.1-5.2, as the code
           now does it)
  after+W  the same, then `app.ask.works.collapse` (one hit per work) with
           the work groups of works.json and the query's language, then
           English, as the preference

No reranker: the captured legs carry no chunk text, so these are the fused
lists BEFORE it (the reranker orders the top 50 of whichever list it is
given). A mixed group's variants were merged before capture
(`_best_rank`), so for "other"/"cjk" the captured list stands for its
variants — the only place the replay differs from the product's new path.

Usage (Docker, repo root):
  python tools/measure/one_language_rescore.py --out tools/measure/out/onelang
  [--queries tools/measure/one_language_titles.tsv]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import one_language_score as sc  # noqa: E402

from app import retrieval  # noqa: E402
from app.ask import works  # noqa: E402


def legs_as_lists(legs: list[dict], lang_of: dict) -> list[list[dict]]:
    return [[{"book_id": b, "chunk_id": cid, "source": leg["source"], "lang": lang_of.get(b, "und")}
             for b, cid in leg["hits"]] for leg in legs if leg["hits"]]


def own_lists(legs: list[dict], query: str, translations: dict) -> set[int]:
    """The lists run with the query as typed — `q == plan.core` in the
    product — known offline as a group whose variant equals the query."""
    kept = [leg for leg in legs if leg["hits"]]
    return {i for i, leg in enumerate(kept) if leg["source"] != "dense"
            and translations.get((leg["source"] or "").split(":")[-1]) == query}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--queries", type=Path, default=Path("tools/measure/one_language_queries.tsv"))
    ap.add_argument("--depth", type=int, default=50)
    a = ap.parse_args()
    lang_of, work_of, winfo, qmeta, results = sc.load(a.out, a.queries)
    # the work groups as the annotations collection would hold them
    w_of = {f"gutenberg:{b}": w for b, w in work_of.items()}
    members = {w: [works.Edition(f"gutenberg:{b}", None, lang_of.get(b), True) for b in info["books"]]
               for w, info in winfo.items() if len(info["langs"]) >= 2}
    rows = {"before": [], "after": [], "after+W": []}
    shown = {"after": [0, 0], "after+W": [0, 0]}
    distinct = {k: 0 for k in rows}
    repeats = {k: 0 for k in rows}
    check = {"reproduced": 0, "differs": 0}
    n = 0
    for qid, r in results.items():
        if not r.get("legs"):
            continue
        n += 1
        m = qmeta[qid]
        W, q = m["work"], m["lang"]
        targets = {W} | sc.ALSO.get(W, set())
        before = sc.fuse_code(r["legs"], lambda b: True)
        mine = before[:len(r["fused"])]
        check["reproduced" if mine == [b for b, _ in r["fused"]] else "differs"] += 1
        # the query's language: the query set's label stands for the model's
        # reading (`LanguagePlan.query_lang`), which the capture predates
        merged = retrieval._merge_rrf(legs_as_lists(r["legs"], lang_of), a.depth,
                                      own=own_lists(r["legs"], r["query"], r.get("translations") or {}),
                                      query_lang=q)
        after = [int(h["book_id"]) for h in merged]
        for h in merged:
            h["lang"] = lang_of.get(int(h["book_id"]), "und")
        grouped = works.collapse(merged, w_of, members, works.preference(None, q))
        lists = {"before": [(b, lang_of.get(b, "und")) for b in before[:50]],
                 "after": [(b, lang_of.get(b, "und")) for b in after[:50]],
                 "after+W": [(int(h["book_id"]), h["lang"]) for h in grouped[:50]]}
        for k, lst in lists.items():
            rank = next((i for i, (b, _) in enumerate(lst, 1)
                         if work_of.get(b, f"B{b}") in targets), None)
            rows[k].append(rank)
            top = lst[:10]
            distinct[k] += len({work_of.get(b, f"B{b}") for b, _ in top})
            repeats[k] += len(top) - len({b for b, _ in top})
            if k in shown and rank and rank <= 10 and winfo[W]["langs"].get(q):
                shown[k][1] += 1
                shown[k][0] += lst[rank - 1][1] == q
    report = {"queries": n, "legs_check": check}
    for k, ranks in rows.items():
        report[k] = sc.metrics(ranks) | {"r50": round(sum(1 for x in ranks if x) / n, 3),
            "distinct_works_top10": round(distinct[k] / n, 2),
            "repeated_book_slots_top10": round(repeats[k] / (10 * n), 3)}
        if k in shown:
            report[k]["shown_in_query_lang_when_held"] = round(shown[k][0] / max(1, shown[k][1]), 3)
    pairs = {}
    for x, y in (("after", "before"), ("after+W", "after")):
        better = sum(1 for p, q_ in zip(rows[x], rows[y]) if (1 / p if p else 0) > (1 / q_ if q_ else 0))
        worse = sum(1 for p, q_ in zip(rows[x], rows[y]) if (1 / p if p else 0) < (1 / q_ if q_ else 0))
        pairs[f"{x} vs {y}"] = {"better": better, "worse": worse, "p_sign": sc.sign_p(better, worse)}
    report["paired"] = pairs
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
