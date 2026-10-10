"""MEASUREMENT (not product code): which translator for query translation
(docs/notes/translator-choice.md).

Scores, BEFORE the reranker, the product's fusion (`retrieval._merge_rrf`:
one text leg, records, own words first on ties) of translator_choice_search.py's
text lists (the shipped plan, each translator's translations) plus the live
re-check's captured vector leg (the same for every translator: the vector leg
embeds the query as typed), at the work level — as
one_language_notr_score.py does for "rule:label".

Usage: python translator_choice_score.py --old tools/measure/out/onelang
         --live tools/measure/out/onelang_live --results OUT.jsonl
         [--base madlad] [--times tools/measure/out/xlate_choice] [--json PER.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.retrieval import _merge_rrf  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from one_language_rep_score import ALSO, metrics, sign_p  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--live", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--base", default="madlad")
    ap.add_argument("--times", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    lang_of = {}
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
    works = json.loads((a.old / "works.json").read_text("utf-8"))
    work_of = {int(b.split(":")[1]): w["work"] for w in works for b in w["books"]}
    work_langs = {w["work"]: {lang_of.get(int(b.split(":")[1]), "und") for b in w["books"]}
                  for w in works}
    live = {json.loads(l)["qid"]: json.loads(l)
            for l in (a.live / "live_results.jsonl").open(encoding="utf-8")}

    def hits(recs, source):
        return [{"chunk_id": f"gutenberg:{b}:0:1", "book_id": b, "source": source,
                 "lang": lang_of.get(b, "und")} for b in recs]

    per: dict = {}
    models: list[str] = []
    main_seen = set()
    for line in a.results.open(encoding="utf-8"):
        r = json.loads(line)
        main_seen.add(tuple(r["main"]))
        if r["model"] not in models:
            models.append(r["model"])
        q = live[r["qid"]]
        lab = q["lang"]
        lists, mine = [], set()
        for L in r["lists"]:
            if L["typed"]:
                mine.add(len(lists))
            lists.append(hits(L["recs"], f"fts:{L['group']}"))
        dense = q["legs"][-1]
        assert dense["source"] == "dense"
        lists.append(hits(list(dict.fromkeys(b for b, _ in dense["hits"])), "dense"))
        fused = [m["book_id"] for m in _merge_rrf(lists, 50, own=mine, query_lang=lab)][:50]
        W = r["qid"].split("-")[0].split("#")[0]
        targets = {W} | ALSO.get(W, set())
        seen, rank = [], None
        for b in fused:
            w = work_of.get(b, f"B{b}")
            if w in seen:
                continue
            seen.append(w)
            if rank is None and w in targets:
                rank = len(seen)
        row = per.setdefault(r["qid"], {
            "set": "titles" if "#T" in r["qid"] else "descriptive", "en": lab == "en",
            "has": any(lab in work_langs.get(t, set()) for t in targets), "lang": lab})
        row[r["model"]] = rank
    print(f"main languages: {sorted(main_seen)}")
    if a.json:
        a.json.write_text(json.dumps(per, indent=0), encoding="utf-8")
    rr = lambda x: 1 / x if x else 0  # noqa: E731

    def table(title, rs):
        print(f"== {title} (n={len(rs)}), fused list before the reranker, work level")
        for m in models:
            ms = metrics([v[m] for v in rs])
            w = sum(rr(v[m]) > rr(v[a.base]) for v in rs)
            l = sum(rr(v[m]) < rr(v[a.base]) for v in rs)
            print(f"  {m:22s} MRR {ms['mrr']:.3f} R@1 {ms['r1']:.3f} R@10 {ms['r10']:.3f} "
                  f"R@50 {sum(1 for v in rs if v[m]) / len(rs):.3f}  vs {a.base} +{w}/-{l} "
                  f"p={sign_p(w, l)}")

    for s in ("descriptive", "titles"):
        rs = [v for v in per.values() if v["set"] == s and all(m in v for m in models)]
        table(s, rs)
        if s == "descriptive":
            table("descriptive, query in English", [v for v in rs if v["en"]])
            table("descriptive, query not English", [v for v in rs if not v["en"]])
            table("descriptive, not English, work has none in it",
                  [v for v in rs if not v["en"] and not v["has"]])
    if a.times:
        print("== time per query (all targets of one query in flight at once), s")
        for f in sorted(a.times.glob("q_*.jsonl")):
            s = [json.loads(l)["s"] for l in f.open(encoding="utf-8")]
            s.sort()
            print(f"  {f.stem[2:]:22s} n {len(s)} median {statistics.median(s):.2f} "
                  f"p10 {s[len(s) // 10]:.2f} p90 {s[len(s) * 9 // 10]:.2f}")


if __name__ == "__main__":
    main()
