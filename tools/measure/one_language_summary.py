"""Prints the headline tables of score.json (one_language_score.py) compactly.
Usage: python one_language_summary.py tools/measure/out/onelang/score.json"""
import json
import sys

s = json.load(open(sys.argv[1], encoding="utf-8"))
print("scored", s["queries_scored"], "of", s["queries_total"], "| legs", s["legs_check"],
      "| reranked", s["reranked_share"], "| vector withheld", s["vector_leg_withheld"])
print("baseline top-10 slots:", s["baseline_top10_slots"])
for f in ("live", "code", "spec", "vector"):
    rep = s.get(f"fusion={f}")
    if not rep:
        continue
    print(f"\n== fusion {f} (n={rep['n']})")
    print("strategy  " + "  ".join(f"{k:>6}" for k in ("MRR", "R@1", "R@5", "R@10")) + "   read-in-q  read-in-q|held  distinct10")
    for st, m in rep["overall"].items():
        rd = rep["what_the_reader_reads"].get(st, {})
        print(f"{st:8}  {m['mrr']:6.3f}  {m['r1']:6.3f}  {m['r5']:6.3f}  {m['r10']:6.3f}   "
              f"{rd.get('shown_in_query_lang', 0):9.3f}  {rd.get('shown_in_query_lang_when_held', 0):14.3f}  {rd.get('distinct_works_top10', 0):10.2f}")
    print("by relation (MRR):")
    for rel, t in rep["by_relation"].items():
        print(f"  {rel:18} n={t.get('_n', 0):3}  " + "  ".join(f"{k}={v['mrr']:.3f}" for k, v in t.items() if k != "_n" and "mrr" in v))
    print("by query language (MRR):")
    for ql, t in rep["by_qlang"].items():
        if t.get("_n"):
            print(f"  {ql:6} n={t['_n']:3}  " + "  ".join(f"{k}={v['mrr']:.3f}" for k, v in t.items() if k != "_n" and "mrr" in v))
    print("paired:", {k: (v["better"], v["worse"], v["p_sign"]) for k, v in rep["paired"].items()})
