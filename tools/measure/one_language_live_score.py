"""MEASUREMENT (not product code): scores one_language_live_search.py's
output (the live re-check of docs/notes/one-language-per-work.md) at the
WORK level, as the note did: the relevant answer is the query's work (any
of its books, works.json), MRR over the returned list (50 deep, one hit per
work), R@1/5/10, and on the Ask page's own top 10 (`ask10`): distinct
works, slots repeating a book or a work, and the edition shown when the
work has a book in the query's language. Compared per query with the
note's old live list (score_per_query.json: `live:base`, `live:W-C`).

Usage: python one_language_live_score.py --old tools/measure/out/onelang
         --results tools/measure/out/onelang_live/live_results.jsonl
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

ALSO = {"WQ4509219": {"WQ40185"}, "WQ40185": {"WQ4509219"}, "WQ58190302": {"WQ8258"}}
TINY = 100      # characters: a best chunk this short is a heading, not a passage


def sign_p(w: int, l: int) -> float:
    import math
    n = w + l
    if not n:
        return 1.0
    k = min(w, l)
    return round(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), 4)


def metrics(ranks):
    n = len(ranks)
    rr = [1 / r if r else 0.0 for r in ranks]
    return {"n": n, "mrr": round(sum(rr) / n, 3),
            "r1": round(sum(1 for r in ranks if r and r <= 1) / n, 3),
            "r5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
            "r10": round(sum(1 for r in ranks if r and r <= 10) / n, 3)}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    works = json.loads((a.old / "works.json").read_text("utf-8"))
    lang_of = {}
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
    work_of, winfo = {}, {}
    for w in works:
        books = [int(b.split(":")[1]) for b in w["books"]]
        winfo[w["work"]] = {"langs": Counter(lang_of.get(b, "und") for b in books),
                            "orig": w["original"], "books": books}
        for b in books:
            work_of[b] = w["work"]
    old = {}
    for name in ("score_per_query.json", "score_titles_per_query.json"):
        p = a.old / name
        if p.exists():
            old.update(json.loads(p.read_text("utf-8")))
    rows = [json.loads(l) for l in a.results.open(encoding="utf-8") if l.startswith("{")]
    report: dict = {"rows": len(rows), "errors": [r["qid"] for r in rows if r.get("error")]}
    per_q: dict = {}

    def wk(b):
        return work_of.get(int(b), f"B{b}")

    for r in rows:
        if r.get("error") or r["qid"].startswith("SAMPLE"):
            continue
        qid = r["qid"]
        W = qid.split("-")[0].split("#")[0]
        q = r["lang"]
        targets = {W} | ALSO.get(W, set())
        info = winfo[W]
        row = {"set": "titles" if "#T" in qid else "descriptive", "lang": q,
               "q_held": bool(info["langs"].get(q)), "orig": info["orig"]}
        for v in ("read", "unread"):
            x = r[v]
            hits = x["hits"]
            rank = next((i for i, h in enumerate(hits, 1) if wk(h["book_id"]) in targets), None)
            ask = x["ask10"] or []
            lang_hit = {h["book_id"]: h["lang"] for h in hits}
            arank, shown = None, None
            for i, h in enumerate(ask, 1):
                if wk(h["book_id"]) in targets:
                    arank, shown = i, lang_hit.get(h["book_id"]) or lang_of.get(int(h["book_id"]))
                    break
            books = [h["book_id"] for h in ask]
            wks = [wk(b) for b in books]
            top = hits[:10]
            row[v] = {"rank": rank, "ask_rank": arank, "shown": shown,
                      "distinct10": len(set(wks)), "rep_book": len(books) - len(set(books)),
                      "rep_work": len(wks) - len(set(wks)),
                      "editions10": sum(1 for h in ask if h["editions"]),
                      "multi_book_work10": sum(1 for b in books if len(winfo.get(wk(b), {}).get("books", [])) >= 2),
                      "multi_book_work10_with_editions": sum(
                          1 for h in ask if h["editions"] and len(winfo.get(wk(h["book_id"]), {}).get("books", [])) >= 2),
                      "tiny10": sum(1 for h in top if h.get("tlen", 1000) < TINY),
                      "tiny1": bool(top and top[0].get("tlen", 1000) < TINY),
                      "query_lang": x["query_lang"], "reranked": x["reranked"],
                      "rerank_withheld": bool(x.get("rerank_withheld")),
                      "vector_withheld": bool(x.get("legs_withheld")), "group_error": x.get("group_error"),
                      "chat_calls": x.get("chat_calls", 0), "seconds": x["seconds"],
                      "timings": x.get("timings") or {}}
        ann = r.get("annotations") or {}
        row["ann_per_hit"] = ann.get("per_hit")
        row["ann_error"] = ann.get("error")
        row["ann_withheld"] = ann.get("withheld")
        # record depth of each list as it entered the fusion
        depth_text, depth_dense = [], None
        for leg in r.get("legs") or []:
            n = len({b for b, _ in leg["hits"]})
            if leg["source"] == "dense":
                depth_dense = n
            else:
                depth_text.append(n)
        row["depth_text"], row["depth_dense"] = depth_text, depth_dense
        o = old.get(qid)
        if o:
            row["old_base"] = o["live:base"]["rank"]
            row["old_wc"] = o["live:W-C"]["rank"]
        per_q[qid] = row

    for s in ("descriptive", "titles"):
        rs = [v for v in per_q.values() if v["set"] == s]
        if not rs:
            continue
        rep: dict = {"n": len(rs)}
        if all("old_base" in v for v in rs):
            rep["old_live_base"] = metrics([v["old_base"] for v in rs])
            rep["old_live_W-C"] = metrics([v["old_wc"] for v in rs])
        for v_ in ("read", "unread"):
            m = metrics([v[v_]["rank"] for v in rs])
            m["ask10"] = metrics([v[v_]["ask_rank"] for v in rs])
            found = [v for v in rs if v[v_]["ask_rank"]]
            held = [v for v in found if v["q_held"]]
            lacks = [v for v in found if not v["q_held"]]
            m["found_top10"] = len(found)
            m["shown_in_query_lang_when_held"] = round(sum(v[v_]["shown"] == v["lang"] for v in held) / max(1, len(held)), 3)
            m["n_when_held"] = len(held)
            m["lacking_query_lang_shown"] = {
                "n": len(lacks),
                "en": round(sum(v[v_]["shown"] == "en" for v in lacks) / max(1, len(lacks)), 3),
                "original": round(sum(v[v_]["shown"] == v["orig"] and v["orig"] != "en" for v in lacks) / max(1, len(lacks)), 3)}
            m["distinct_works_top10"] = round(sum(v[v_]["distinct10"] for v in rs) / len(rs), 2)
            m["slots_repeating_book"] = round(sum(v[v_]["rep_book"] for v in rs) / (10 * len(rs)), 4)
            m["slots_repeating_work"] = round(sum(v[v_]["rep_work"] for v in rs) / (10 * len(rs)), 4)
            m["hits_with_editions_top10"] = round(sum(v[v_]["editions10"] for v in rs) / (10 * len(rs)), 4)
            mb = sum(v[v_]["multi_book_work10"] for v in rs)
            m["multi_book_work_hits_top10"] = mb
            m["…_with_editions"] = sum(v[v_]["multi_book_work10_with_editions"] for v in rs)
            m["query_lang_set"] = sum(1 for v in rs if v[v_]["query_lang"])
            m["reranked_false"] = sum(1 for v in rs if not v[v_]["reranked"])
            m["rerank_withheld"] = sum(1 for v in rs if v[v_]["rerank_withheld"])
            m["vector_withheld"] = sum(1 for v in rs if v[v_]["vector_withheld"])
            m["group_errors"] = sum(1 for v in rs if v[v_]["group_error"])
            m["chat_calls"] = sum(v[v_]["chat_calls"] for v in rs)
            m["tiny_chunk_share_top10"] = round(sum(v[v_]["tiny10"] for v in rs) / (10 * len(rs)), 3)
            m["tiny_chunk_rank1"] = round(sum(v[v_]["tiny1"] for v in rs) / len(rs), 3)
            secs = [v[v_]["seconds"] for v in rs]
            m["seconds_median_p90"] = [round(statistics.median(secs), 2), pct(secs, 0.9)]
            for part in ("languages", "fts", "dense", "rerank", "total"):
                xs = [v[v_]["timings"].get(part) for v in rs if v[v_]["timings"].get(part) is not None]
                if xs:
                    m[f"ms_{part}_median_p90"] = [round(statistics.median(xs)), pct(xs, 0.9)]
            if "old_base" in rs[0]:
                for ref in ("old_base", "old_wc"):
                    w = l = 0
                    for v in rs:
                        r1 = 1 / v[v_]["rank"] if v[v_]["rank"] else 0
                        r2 = 1 / v[ref] if v[ref] else 0
                        w += r1 > r2
                        l += r1 < r2
                    m[f"vs_{ref}"] = {"better": w, "worse": l, "p_sign": sign_p(w, l)}
            rep[v_] = m
        w = l = 0
        for v in rs:
            r1 = 1 / v["read"]["rank"] if v["read"]["rank"] else 0
            r2 = 1 / v["unread"]["rank"] if v["unread"]["rank"] else 0
            w += r1 > r2
            l += r1 < r2
        rep["read_vs_unread"] = {"better": w, "worse": l, "p_sign": sign_p(w, l)}
        anns = [x for v in rs for x in (v["ann_per_hit"] or [])]
        rep["annotations"] = {"hits": len(anns), "mean_per_hit": round(sum(anns) / max(1, len(anns)), 3),
                              "hits_with_any": round(sum(1 for x in anns if x) / max(1, len(anns)), 4),
                              "errors": sum(1 for v in rs if v["ann_error"]),
                              "withheld": sum(1 for v in rs if v["ann_withheld"])}
        dt = [d for v in rs for d in v["depth_text"]]
        dd = [v["depth_dense"] for v in rs if v["depth_dense"] is not None]
        rep["records_per_list"] = {"text_variants_per_query": round(sum(len(v["depth_text"]) for v in rs) / len(rs), 1),
                                   "text_median_p10_p90": [statistics.median(dt), pct(dt, 0.1), pct(dt, 0.9)],
                                   "dense_median_p10_p90": [statistics.median(dd), pct(dd, 0.1), pct(dd, 0.9)]}
        by_lang = {}
        for ql in sorted({v["lang"] for v in rs}, key=lambda x: -sum(v["lang"] == x for v in rs)):
            sub = [v for v in rs if v["lang"] == ql]
            if len(sub) >= 9:
                by_lang[ql] = {"n": len(sub), "new": metrics([v["read"]["rank"] for v in sub])["mrr"],
                               "old": metrics([v["old_base"] for v in sub])["mrr"] if "old_base" in sub[0] else None}
        rep["mrr_by_query_lang"] = by_lang
        report[s] = rep
    if a.out:
        a.out.write_text(json.dumps({"report": report, "per_query": per_q}, ensure_ascii=False, indent=0), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
