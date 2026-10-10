"""MEASUREMENT (not product code): scores one_language_own_search.py — the
text each language group is searched with — on the fused list BEFORE the
reranker (the product's `_merge_rrf`; the vector leg is the live
re-check's captured one, the same for every strategy), at the work level.

  today    every one-language group searched with its translation (the
           query's own language too: MADLAD's rewrite of it);
  own:X    the group of the query's language searched with the query as
           typed and not translated; X = the label (the chat reading, right)
           or the guess (app/query_language.py);
  typed    the query as typed ALSO searched in every group;
  own+typed  own:guess where there is a guess, typed where there is none.

Usage: python one_language_own_score.py --old tools/measure/out/onelang
         --live tools/measure/out/onelang_live --own OWN.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import query_language  # noqa: E402
from app.retrieval import _merge_rrf, record_of  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from one_language_rep_score import ALSO, metrics, sign_p  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--live", type=Path, required=True)
    ap.add_argument("--own", type=Path, required=True)
    a = ap.parse_args()
    lang_of = {}
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
    held = {c for c in lang_of.values() if c != "und"}
    work_of = {int(b.split(":")[1]): w["work"]
               for w in json.loads((a.old / "works.json").read_text("utf-8")) for b in w["books"]}
    live = {json.loads(l)["qid"]: json.loads(l) for l in (a.live / "live_results.jsonl").open(encoding="utf-8")}
    own = [json.loads(l) for l in a.own.open(encoding="utf-8")]

    def hits(recs, source):
        return [{"chunk_id": f"gutenberg:{b}:0:1", "book_id": b, "source": source,
                 "lang": lang_of.get(b, "und")} for b in recs]

    def fused(r, ql, typed_everywhere, own_group):
        lists, mine = [], set()
        for L in r["lists"]:
            g = L["group"]
            if g == own_group and g not in ("cjk", "other"):
                use = L["typed"]
            else:
                use = L["tr"] or (typed_everywhere and L["typed"])
            if use:
                if L["typed"]:
                    mine.add(len(lists))
                lists.append(hits(L["recs"], f"fts:{g}"))
        dense = live[r["qid"]]["legs"][-1]
        assert dense["source"] == "dense"
        lists.append(hits(list(dict.fromkeys(b for b, _ in dense["hits"])), "dense"))
        return [m["book_id"] for m in _merge_rrf(lists, 50, own=mine, query_lang=ql)][:50]

    per: dict = {}
    for r in own:
        if r["qid"].startswith("SAMPLE"):
            continue
        q = live[r["qid"]]
        lab = q["lang"]
        g = query_language.guess(q["query"], held)
        W = r["qid"].split("-")[0].split("#")[0]
        targets = {W} | ALSO.get(W, set())
        strat = {"today": (None, False, None), "today+label-ties": (lab, False, None),
                 "own:label": (lab, False, lab), "own:guess": (g, False, g),
                 "typed": (None, True, None),
                 "own+typed": (g, g is None, g)}
        row = {"set": "titles" if "#T" in r["qid"] else "descriptive"}
        for name, (ql, te, og) in strat.items():
            seen, rank = [], None
            for b in fused(r, ql, te, og):
                w = work_of.get(b, f"B{b}")
                if w in seen:
                    continue
                seen.append(w)
                if rank is None and w in targets:
                    rank = len(seen)
            row[name] = rank
        per[r["qid"]] = row
    for s in ("descriptive", "titles"):
        rs = [v for v in per.values() if v["set"] == s]
        print(f"== {s} ({len(rs)}), fused list before the reranker")
        for name in ("today", "today+label-ties", "own:label", "own:guess", "typed", "own+typed"):
            m = metrics([v[name] for v in rs])
            rr = lambda x: 1 / x if x else 0  # noqa: E731
            w = sum(rr(v[name]) > rr(v["today"]) for v in rs)
            l = sum(rr(v[name]) < rr(v["today"]) for v in rs)
            print(f"  {name:18s} MRR {m['mrr']:.3f} R@1 {m['r1']:.3f} R@10 {m['r10']:.3f} R@50 "
                  f"{sum(1 for v in rs if v[name]) / len(rs):.3f}  vs today +{w}/-{l} p={sign_p(w, l)}")


if __name__ == "__main__":
    main()
