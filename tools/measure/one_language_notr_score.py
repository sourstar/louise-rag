"""MEASUREMENT (not product code): do we need query translation?
(docs/notes/one-language-per-work.md, "Do we need query translation?
(2026-10-10)").

Scores, BEFORE the reranker, the product's fusion (`retrieval._merge_rrf`
at HEAD: one text leg, records, own words first on ties) on the captured
per-variant text lists of one_language_own_search.py (every group searched
with each translation AND with the query as typed, 50 records deep) plus
the live re-check's captured vector leg, at the work level:

  today:X      the shipped plan: the query's own group as typed, every other
               one-language group with its MADLAD translation, the mixed
               groups as today (other: as typed; cjk: its translations);
               X = the label (a right reading) or the guess
  none-all     no translation: the query as typed in EVERY group (what the
               product does with translate off; ties by the guess)
  none-own:X   no translation: as typed in the query's own group and the
               mixed groups only; the guess's fallback, no guess, = none-all
  vector       the vector leg alone
  few:S/rest   translations only into the languages S; own group typed,
               mixed groups typed; the other one-language groups skipped
               (rest=skip) or searched as typed (rest=typed, what the
               product's variants() would do with no translation for them)

Usage: python one_language_notr_score.py --old tools/measure/out/onelang
         --live tools/measure/out/onelang_live
         --own tools/measure/out/onelang_live/own_all.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import query_language  # noqa: E402
from app.retrieval import _merge_rrf  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from one_language_rep_score import ALSO, metrics, sign_p  # noqa: E402

MIXED = ("cjk", "other")
BIG4 = {"en", "fr", "fi", "de"}          # the four largest gutenberg languages by books
BIG7 = BIG4 | {"nl", "it", "es"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--live", type=Path, required=True)
    ap.add_argument("--own", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    lang_of = {}
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
    held = {c for c in lang_of.values() if c != "und"}
    works = json.loads((a.old / "works.json").read_text("utf-8"))
    work_of = {int(b.split(":")[1]): w["work"] for w in works for b in w["books"]}
    work_langs = {w["work"]: {lang_of.get(int(b.split(":")[1]), "und") for b in w["books"]}
                  for w in works}
    live = {json.loads(l)["qid"]: json.loads(l)
            for l in (a.live / "live_results.jsonl").open(encoding="utf-8")}
    own = [json.loads(l) for l in a.own.open(encoding="utf-8")]

    def hits(recs, source):
        return [{"chunk_id": f"gutenberg:{b}:0:1", "book_id": b, "source": source,
                 "lang": lang_of.get(b, "und")} for b in recs]

    def fused(r, ql, use, text=True, vector=True):
        lists, mine = [], set()
        if text:
            for L in r["lists"]:
                if use(L):
                    if L["typed"]:
                        mine.add(len(lists))
                    lists.append(hits(L["recs"], f"fts:{L['group']}"))
        if vector:
            dense = live[r["qid"]]["legs"][-1]
            assert dense["source"] == "dense"
            lists.append(hits(list(dict.fromkeys(b for b, _ in dense["hits"])), "dense"))
        return [m["book_id"] for m in _merge_rrf(lists, 50, own=mine, query_lang=ql)][:50]

    def today(og):
        def use(L):
            if L["group"] == og and og not in MIXED:
                return L["typed"]
            return L["tr"]
        return use

    def none_all(L):
        return L["typed"]

    def none_own(og):
        def use(L):
            return L["typed"] and (L["group"] in MIXED or L["group"] == og)
        return use

    def few(S, og, rest_typed):
        def use(L):
            g = L["group"]
            if g in MIXED or (g == og):
                return L["typed"]
            if g in S:
                return L["tr"]
            return rest_typed and L["typed"]
        return use

    per: dict = {}
    for r in own:
        if r["qid"].startswith("SAMPLE"):
            continue
        q = live[r["qid"]]
        lab = q["lang"]
        g = query_language.guess(q["query"], held)
        W = r["qid"].split("-")[0].split("#")[0]
        targets = {W} | ALSO.get(W, set())
        has = any(lab in work_langs.get(t, set()) for t in targets)
        strat = {
            "today:label": (lab, today(lab), True, True),
            "today:guess": (g, today(g), True, True),
            "none-all": (g, none_all, True, True),
            "none-all:label-ties": (lab, none_all, True, True),
            "none-own:label": (lab, none_own(lab), True, True),
            "none-own:guess": (g, none_own(g) if g else none_all, True, True),
            "text-only:today": (lab, today(lab), True, False),
            "text-only:none-all": (g, none_all, True, False),
            "vector": (None, none_all, False, True),
            "few:en/skip": (lab, few({"en"}, lab, False), True, True),
            "few:en/typed": (lab, few({"en"}, lab, True), True, True),
            "few:big4/skip": (lab, few(BIG4, lab, False), True, True),
            "few:big4/typed": (lab, few(BIG4, lab, True), True, True),
            "few:big7/skip": (lab, few(BIG7, lab, False), True, True),
            "few:big7/typed": (lab, few(BIG7, lab, True), True, True),
        }
        row = {"set": "titles" if "#T" in r["qid"] else "descriptive",
               "en": lab == "en", "has": has, "lang": lab}
        for name, (ql, use, t, v) in strat.items():
            seen, rank, pre = [], None, None
            for b in fused(r, ql, use, t, v):
                w = work_of.get(b, f"B{b}")
                if w in seen:
                    continue
                seen.append(w)
                if rank is None and w in targets:
                    rank = len(seen)
            row[name] = rank
        per[r["qid"]] = row
    if a.json:
        a.json.write_text(json.dumps(per, indent=0), encoding="utf-8")
    names = list(next(iter(per.values())).keys())[4:]
    rr = lambda x: 1 / x if x else 0  # noqa: E731

    def table(title, rs, base="today:label"):
        print(f"== {title} (n={len(rs)}), fused list before the reranker, work level")
        for name in names:
            m = metrics([v[name] for v in rs])
            w = sum(rr(v[name]) > rr(v[base]) for v in rs)
            l = sum(rr(v[name]) < rr(v[base]) for v in rs)
            print(f"  {name:20s} MRR {m['mrr']:.3f} R@1 {m['r1']:.3f} R@5 {m['r5']:.3f} "
                  f"R@10 {m['r10']:.3f} R@50 {sum(1 for v in rs if v[name]) / len(rs):.3f}"
                  f"  vs {base} +{w}/-{l} p={sign_p(w, l)}")

    for s in ("descriptive", "titles"):
        rs = [v for v in per.values() if v["set"] == s]
        table(s, rs)
        if s == "descriptive":
            table("descriptive, query in English", [v for v in rs if v["en"]])
            table("descriptive, query not English", [v for v in rs if not v["en"]])
            table("descriptive, not English, work has an edition in it",
                  [v for v in rs if not v["en"] and v["has"]])
            table("descriptive, not English, work has none in it",
                  [v for v in rs if not v["en"] and not v["has"]])
            table("descriptive, English, work has an English edition",
                  [v for v in rs if v["en"] and v["has"]])
            table("descriptive, English, work has no English edition",
                  [v for v in rs if v["en"] and not v["has"]])


if __name__ == "__main__":
    main()
