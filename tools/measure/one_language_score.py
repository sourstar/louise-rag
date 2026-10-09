"""MEASUREMENT (not product code), step 4 of docs/notes/one-language-per-work.md:
score every one-language-per-work strategy on the SAME ranked lists.

Inputs (tools/measure/out/onelang/): works.json, records.jsonl, results.jsonl
(one_language_search.py's output: gutenberg's real top 50, chunk hits, no
dedup), and tools/measure/one_language_queries.tsv (work, query language, text).

A strategy keeps, for every work that has books in >= 2 languages, the books
of ONE language and drops the others from the list ("skip the other ones");
single-language works and books in no work are untouched. The language:
  A  English if the work has it, else the original
  B  the original if held, else English
  C  the query's language if the work has it, else English
  D  the query's language if the work has it, else the original
  (neither held -> the work's most frequent language)
  cur  today's rule (app/work_dedup.py, as the live search would have applied it)
  E-x  "group, don't filter": the list keeps every work at the rank of its BEST
       edition and drops later editions of it in other books; the edition shown
       is rule x's. Ranks are rule-independent; only the language shown differs.

The relevant answer of a query is its WORK (any of its books). Metrics on the
list as the Ask page shows it (chunk hits): MRR@50, recall@1/5/10.

Usage: python one_language_score.py --out tools/measure/out/onelang
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

#: works whose books overlap in content, so a query for one is answered by the other
ALSO = {"WQ4509219": {"WQ40185"}, "WQ40185": {"WQ4509219"}, "WQ58190302": {"WQ8258"}}
RULES = {"A": ("en", "orig"), "B": ("orig", "en"), "C": ("q", "en"), "D": ("q", "orig")}
MAIN_LANGS = ["en", "fr", "de", "it", "es", "el", "fi", "nl", "la"]


def load(out: Path, queries_tsv: Path):
    works = json.loads((out / "works.json").read_text("utf-8"))
    lang_of: dict[int, str] = {}
    for line in (out / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
    work_of: dict[int, str] = {}
    winfo: dict[str, dict] = {}
    for w in works:
        books = [int(b.split(":")[1]) for b in w["books"]]
        langs = Counter(lang_of.get(b, "und") for b in books)
        winfo[w["work"]] = {"langs": langs, "orig": w["original"], "books": books}
        if len(langs) >= 2:
            for b in books:
                work_of[b] = w["work"]
    qmeta = {}
    for line in queries_tsv.open(encoding="utf-8"):
        if line.strip():
            work, lang, text = line.rstrip("\n").split("\t")
            # "WQ480#T": the title set's query for work WQ480
            qmeta[f"{work}-{lang}"] = {"work": work.split("#")[0], "lang": lang, "text": text}
    results = {}
    for line in (out / "results.jsonl").open(encoding="utf-8"):
        if line.startswith("{"):
            r = json.loads(line)
            if not r.get("error") and r["qid"] in qmeta:
                results[r["qid"]] = r
    return lang_of, work_of, winfo, qmeta, results


def chosen_lang(info: dict, q: str, rule: str) -> str:
    for pref in RULES[rule]:
        code = {"en": "en", "orig": info["orig"], "q": q}[pref]
        if code and info["langs"].get(code):
            return code
    return sorted(info["langs"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def ranked_list(r: dict, q: str, strat: str, lang_of, work_of, winfo,
                fusion: str = "live") -> list[tuple[int, str, str]]:
    """[(book, shown language, work key)] as the strategy presents the list.
    `live` is the reranked list the search returned, filtered after the fact;
    the other fusions are recomputed from the captured legs with the skipped
    books taken out of every leg BEFORE fusion, as an index without them would."""
    out = []
    if fusion == "live":
        hits = [(int(h["book_id"]), h.get("lang") or lang_of.get(int(h["book_id"]), "und")) for h in r["hits"]]
    else:
        if strat == "cur":
            return []
        keep = keep_fn(q, strat, lang_of, work_of, winfo)
        books = {"code": fuse_code, "spec": fuse_spec, "vector": fuse_vector}[fusion](r["legs"], keep)
        hits = [(b, lang_of.get(b, "und")) for b in books[:50]]
        if not strat.startswith(("E", "W")):
            return [(b, l, work_of.get(b, f"B{b}")) for b, l in hits]
    if strat == "base":
        return [(b, l, work_of.get(b, f"B{b}")) for b, l in hits]
    if strat == "cur":
        dropped = set(r.get("candidates") or []) - set(r.get("current_kept") or [])
        return [(b, l, work_of.get(b, f"B{b}")) for b, l in hits if b not in dropped]
    if strat.startswith("W"):
        # one hit per work: every later hit of a work already shown goes,
        # another chunk of the same book included (the record as the unit)
        rule = strat.split("-")[1]
        seen: set = set()
        for b, l in hits:
            w = work_of.get(b, f"B{b}")
            if w in seen:
                continue
            seen.add(w)
            out.append((b, chosen_lang(winfo[w], q, rule) if w in winfo else l, w))
        return out
    if strat.startswith("E"):
        rule = strat.split("-")[1]
        first_book: dict[str, int] = {}
        for b, l in hits:
            w = work_of.get(b)
            if w is None:
                out.append((b, l, f"B{b}"))
                continue
            if w in first_book and first_book[w] != b:
                continue                 # a later edition of a work already shown
            first_book.setdefault(w, b)
            out.append((b, chosen_lang(winfo[w], q, rule), w))
        return out
    for b, l in hits:
        w = work_of.get(b)
        if w is None or l == chosen_lang(winfo[w], q, strat):
            out.append((b, l, w or f"B{b}"))
    return out


RRF_K = 60


def keep_fn(q: str, strat: str, lang_of, work_of, winfo):
    """Which books a filter strategy leaves in the index ("skip the other ones")."""
    if strat in ("base", "cur") or strat.startswith(("E", "W")):
        return lambda b: True

    def keep(b: int) -> bool:
        w = work_of.get(b)
        return w is None or lang_of.get(b, "und") == chosen_lang(winfo[w], q, strat)
    return keep


def fuse_code(legs: list[dict], keep) -> list[int]:
    """retrieval._merge_rrf as the code runs it: every text index group is its
    own list, chunks are the unit, ties keep insertion order."""
    score: dict = {}
    book: dict = {}
    for leg in legs:
        rank = 0
        for b, cid in leg["hits"]:
            if not keep(b):
                continue
            rank += 1
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            book[cid] = b
    return [book[c] for c in sorted(score, key=lambda c: -score[c])]


def fuse_spec(legs: list[dict], keep) -> list[int]:
    """docs/05 §5.1-5.2 as written: two legs (text, vector); the record is the
    unit, its rank in a leg is its best chunk's; the text index's language
    groups are variants of ONE leg, a record ranking by its best rank across
    them; ties by uid bytes."""
    def records(hits):
        out = []
        for b, _ in hits:
            if keep(b) and b not in out:
                out.append(b)
        return out
    text_best: dict[int, int] = {}
    vec: list[int] = []
    for leg in legs:
        recs = records(leg["hits"])
        if leg["source"] == "dense":
            vec = recs
        else:
            for i, b in enumerate(recs, 1):
                text_best[b] = min(text_best.get(b, i), i)
    text = sorted(text_best, key=lambda b: (text_best[b], f"gutenberg:{b}"))
    score: dict[int, float] = {}
    for lst in (text, vec):
        for i, b in enumerate(lst, 1):
            score[b] = score.get(b, 0.0) + 1.0 / (RRF_K + i)
    return sorted(score, key=lambda b: (-score[b], f"gutenberg:{b}"))


def fuse_vector(legs: list[dict], keep) -> list[int]:
    for leg in legs:
        if leg["source"] == "dense":
            out = []
            for b, _ in leg["hits"]:
                if keep(b) and b not in out:
                    out.append(b)
            return out
    return []


def relation(q: str, info: dict) -> str:
    o = info["orig"]
    if q == "en":
        return "q=en=orig" if o == "en" else "q=en"
    if q == o:
        return "q=orig"
    return "q=other(held)" if info["langs"].get(q) else "q=other(not held)"


def metrics(ranks: list[int | None]) -> dict:
    n = len(ranks)
    if not n:
        return {}
    rr = [1 / r if r else 0.0 for r in ranks]
    return {"n": n, "mrr": round(sum(rr) / n, 3),
            "r1": round(sum(1 for r in ranks if r and r <= 1) / n, 3),
            "r5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
            "r10": round(sum(1 for r in ranks if r and r <= 10) / n, 3)}


def sign_p(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * p), 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--queries", type=Path, default=Path("tools/measure/one_language_queries.tsv"))
    ap.add_argument("--name", default="score")
    a = ap.parse_args()
    lang_of, work_of, winfo, qmeta, results = load(a.out, a.queries)
    strats = ["base", "cur", "A", "B", "C", "D", "E-A", "E-B", "E-C", "E-D", "W-A", "W-B", "W-C", "W-D"]
    fusions = ["live", "code", "spec", "vector"]
    per_q: dict[str, dict] = {}
    check = Counter()
    for qid, r in results.items():
        m = qmeta[qid]
        W, q = m["work"], m["lang"]
        targets = {W} | ALSO.get(W, set())
        info = winfo[W]
        row = {"work": W, "lang": q, "rel": relation(q, info), "orig": info["orig"],
               "held": sorted(info["langs"]), "q_held": bool(info["langs"].get(q)),
               "reranked": r.get("reranked"), "vector_withheld": bool(r.get("legs_withheld"))}
        if r.get("legs"):
            # the captured legs must reproduce the fused order the search had
            mine = fuse_code(r["legs"], lambda b: True)[:len(r["fused"])]
            check["fused order reproduced" if mine == [b for b, _ in r["fused"]] else "fused order differs"] += 1
        for f in fusions:
            if f != "live" and not r.get("legs"):
                continue
            for s in strats:
                if f != "live" and s == "cur":
                    continue
                lst = ranked_list(r, q, s, lang_of, work_of, winfo, fusion=f)
                rank, shown = None, None
                for i, (b, l, w) in enumerate(lst, 1):
                    if w in targets:
                        rank, shown = i, l
                        break
                top10 = lst[:10]
                row[f"{f}:{s}"] = {"rank": rank, "shown": shown, "distinct10": len({w for _, _, w in top10}),
                                   "inq10": sum(1 for _, l, _ in top10 if l == q)}
        # what fills the baseline's top 10 (the live list)
        seen_books, seen_works, waste = set(), {}, Counter()
        for b, l, w in ranked_list(r, q, "base", lang_of, work_of, winfo)[:10]:
            if b in seen_books:
                waste["same book again (another chunk)"] += 1
            elif w in seen_works:
                waste["another edition, same language" if l in seen_works[w] else
                      "another edition, other language"] += 1
            else:
                waste["new work"] += 1
            seen_books.add(b)
            seen_works.setdefault(w, set()).add(l)
        row["waste"] = dict(waste)
        base50 = ranked_list(r, q, "base", lang_of, work_of, winfo)
        tb = {b for b, _, w in base50 if w in targets}
        row["target_books_top50"] = len(tb)
        row["target_langs_top50"] = sorted({l for b, l, w in base50 if w in targets})
        ed = Counter()
        for b, l, w in base50:
            if not str(w).startswith("B"):
                ed[w] += 0
        books_of = defaultdict(set)
        for b, l, w in base50:
            books_of[w].add(b)
        row["works_with_2plus_books_top50"] = sum(1 for w, bs in books_of.items() if len(bs) >= 2 and not str(w).startswith("B"))
        per_q[qid] = row

    def keys_for(f):
        return [f"{f}:{s}" for s in strats if f"{f}:{s}" in next(iter(per_q.values()), {})] if per_q else []

    def table(rows, f) -> dict:
        ks = [k for k in (f"{f}:{s}" for s in strats) if all(k in v for v in rows)]
        return {k.split(":")[1]: metrics([v[k]["rank"] for v in rows]) for k in ks}

    report: dict = {"queries_scored": len(per_q), "queries_total": len(qmeta), "legs_check": dict(check)}
    for f in fusions:
        rows_all = [v for v in per_q.values() if f"{f}:base" in v]
        if not rows_all:
            continue
        rep = {"n": len(rows_all), "overall": table(rows_all, f)}
        rep["by_relation"] = {rel: table([v for v in rows_all if v["rel"] == rel], f) | {"_n": sum(v["rel"] == rel for v in rows_all)}
                              for rel in ["q=en=orig", "q=en", "q=orig", "q=other(held)", "q=other(not held)"]}
        rep["by_qlang"] = {ql: table([v for v in rows_all if v["lang"] == ql], f) | {"_n": sum(v["lang"] == ql for v in rows_all)}
                           for ql in MAIN_LANGS}
        oth = [v for v in rows_all if v["lang"] not in MAIN_LANGS]
        rep["by_qlang"]["other"] = table(oth, f) | {"_n": len(oth)}
        rep["by_orig_held"] = {
            "original held": table([v for v in rows_all if v["orig"] in v["held"]], f),
            "original not held": table([v for v in rows_all if v["orig"] and v["orig"] not in v["held"]], f)}
        pairs = {}
        for s1, s2 in [("A", "base"), ("B", "base"), ("C", "base"), ("D", "base"), ("cur", "base"),
                       ("E-A", "base"), ("W-C", "base"), ("W-C", "C"), ("C", "A"), ("C", "B"), ("D", "B"), ("C", "D"), ("A", "B")]:
            k1, k2 = f"{f}:{s1}", f"{f}:{s2}"
            if k1 not in rows_all[0] or k2 not in rows_all[0]:
                continue
            w = l = 0
            for v in rows_all:
                r1 = 1 / v[k1]["rank"] if v[k1]["rank"] else 0
                r2 = 1 / v[k2]["rank"] if v[k2]["rank"] else 0
                w += r1 > r2
                l += r1 < r2
            pairs[f"{s1} vs {s2}"] = {"better": w, "worse": l, "same": len(rows_all) - w - l, "p_sign": sign_p(w, l)}
        rep["paired"] = pairs
        reads = {}
        for s in strats:
            k = f"{f}:{s}"
            if k not in rows_all[0]:
                continue
            found = [v for v in rows_all if v[k]["rank"] and v[k]["rank"] <= 10]
            possible = [v for v in found if v["q_held"]]
            reads[s] = {
                "found_top10": len(found),
                "shown_in_query_lang": round(sum(v[k]["shown"] == v["lang"] for v in found) / max(1, len(found)), 3),
                "shown_in_query_lang_when_held": round(
                    sum(v[k]["shown"] == v["lang"] for v in possible) / max(1, len(possible)), 3),
                "n_when_held": len(possible),
                "shown_in_english": round(sum(v[k]["shown"] == "en" for v in found) / max(1, len(found)), 3),
                "shown_in_original": round(sum(v[k]["shown"] == v["orig"] for v in found) / max(1, len(found)), 3),
                "top10_hits_in_query_lang": round(sum(v[k]["inq10"] for v in rows_all) / (10 * len(rows_all)), 3),
                "distinct_works_top10": round(sum(v[k]["distinct10"] for v in rows_all) / len(rows_all), 2)}
        rep["what_the_reader_reads"] = reads
        report[f"fusion={f}"] = rep
    waste = Counter()
    for v in per_q.values():
        waste.update(v["waste"])
    report["baseline_top10_slots"] = {k: round(n / (10 * len(per_q)), 3) for k, n in waste.most_common()}
    report["target_found_top50_with_2plus_books"] = sum(1 for v in per_q.values() if v["target_books_top50"] >= 2)
    report["target_found_top50_in_2plus_langs"] = sum(1 for v in per_q.values() if len(v["target_langs_top50"]) >= 2)
    report["target_found_top50"] = sum(1 for v in per_q.values() if v["target_books_top50"] >= 1)
    report["mean_multiedition_works_in_top50"] = round(sum(v["works_with_2plus_books_top50"] for v in per_q.values()) / max(1, len(per_q)), 2)
    report["reranked_share"] = round(sum(1 for v in per_q.values() if v["reranked"]) / max(1, len(per_q)), 3)
    report["vector_leg_withheld"] = sum(1 for v in per_q.values() if v["vector_withheld"])
    (a.out / f"{a.name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (a.out / f"{a.name}_per_query.json").write_text(json.dumps(per_q, ensure_ascii=False, indent=0), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
