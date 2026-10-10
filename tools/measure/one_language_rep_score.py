"""MEASUREMENT (not product code): scores one_language_rep_search.py's output
— how a record is represented to the reranker, and how deep each leg goes —
at the WORK level, as docs/notes/one-language-per-work.md does (the
relevant answer is the query's work, any of its books; MRR over 50, R@k
over the list with one hit per work).

Everything is recomputed from the legs and reranker scores of ONE run, so
before and after are compared on the same index state (the live index moves
while gutenberg embeds: the captured legs of the re-check no longer match
it). The fusion is the product's own `retrieval._merge_rrf`.

Strategies:
  depth   shallow — every list 50 CHUNKS deep (906f06b);
          deep    — every list cut where it reaches 50 distinct RECORDS
                    (05 §5.1), capped at the depth fetched.
  rep     k       — the reranker judges the record's top-k matched chunks,
                    the record takes the best score (k = 1 is 906f06b);
          min     — a chunk shorter than `min` characters is dropped from a
                    record's judged chunks while the record has a longer
                    matched one.

Usage: python one_language_rep_score.py --old tools/measure/out/onelang
         --results REP.jsonl [--live tools/measure/out/onelang_live/live_results.jsonl]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.retrieval import _merge_rrf, record_of  # noqa: E402

ALSO = {"WQ4509219": {"WQ40185"}, "WQ40185": {"WQ4509219"}, "WQ58190302": {"WQ8258"}}
DEPTH = 50
TINY = 100


def sign_p(w: int, l: int) -> float:
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


def cut_to_records(hits, depth):
    seen = set()
    for i, h in enumerate(hits):
        seen.add(record_of(h))
        if len(seen) >= depth:
            return hits[:i + 1]
    return hits


def run(r, lang_of, depth_mode, k, min_chars, skey="scores", dense_cap=None, text_cap=None):
    lists = []
    for leg in r["legs"]:
        hits = [{"book_id": b, "chunk_id": c, "tlen": n, "source": leg["source"],
                 "lang": lang_of.get(b, "und")} for b, c, n in leg["hits"]]
        dense = leg["source"] == "dense"
        if depth_mode == "shallow":
            hits = hits[:50]
        else:
            cap = dense_cap if dense else text_cap
            if cap:
                hits = hits[:cap]
            hits = cut_to_records(hits, DEPTH)
        lists.append(hits)
    fused = _merge_rrf(lists, DEPTH, own=set(r["own"]), query_lang=r["lang"])
    # every record's matched chunks, best chunk-level position first
    pos: dict[str, dict[str, tuple[int, int]]] = {}
    for hits in lists:
        for i, h in enumerate(hits):
            d = pos.setdefault(record_of(h), {})
            cur = d.get(h["chunk_id"])
            if cur is None or i < cur[0]:
                d[h["chunk_id"]] = (i, h["tlen"])
    head = fused[:DEPTH]
    scored, missing = [], 0
    for idx, h in enumerate(head):
        uid = record_of(h)
        chunks = sorted(pos[uid].items(), key=lambda kv: (kv[1][0], kv[0]))
        rep = h["chunk_id"]
        order = [rep] + [c for c, _ in chunks if c != rep]
        lens = {c: v[1] for c, v in pos[uid].items()}
        if min_chars and any(lens[c] >= min_chars for c in order):
            order = [c for c in order if lens[c] >= min_chars]
        judged = order[:k]
        got = [r[skey][c] for c in judged if c in r[skey]]
        missing += sum(1 for c in judged if c not in r[skey])
        if not got:
            missing += 1
            got = [-1e9]
        best = max(got)
        shown = judged[got.index(best)] if best > -1e9 else rep
        scored.append((-best, idx, uid, shown, lens.get(shown, 0)))
    scored.sort()
    return [(u, c, n) for _, _, u, c, n in scored], missing


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--results", type=Path, nargs="+", required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    works = json.loads((a.old / "works.json").read_text("utf-8"))
    lang_of = {}
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        rec = json.loads(line)
        lang_of[int(rec["uid"].split(":")[1])] = rec.get("lang") or "und"
    work_of = {}
    for w in works:
        for b in w["books"]:
            work_of[b] = w["work"]
    rows = [json.loads(l) for p in a.results for l in p.open(encoding="utf-8") if l.startswith("{")]
    rows = [r for r in rows if not r.get("error") and not r["qid"].startswith("SAMPLE")]

    def wk(uid):
        return work_of.get(uid, uid)

    strategies = [("shallow", 1, 0, "scores")]
    keys = [k for k in ("scores", "scores_h") if all(rr.get(k) for rr in rows)]
    for skey in keys:
        for depth in ("shallow", "deep"):
            for k in (1, 2, 3):
                for mn in (0, 100, 200):
                    strategies.append((depth, k, mn, skey))
    strategies = list(dict.fromkeys(strategies))
    per: dict = {}
    for r in rows:
        W = r["qid"].split("-")[0].split("#")[0]
        targets = {W} | ALSO.get(W, set())
        per[r["qid"]] = {"set": "titles" if "#T" in r["qid"] else "descriptive"}
        for s in strategies:
            ranked, missing = run(r, lang_of, *s)
            seen, rank, tiny = [], None, 0
            for uid, chunk, n in ranked:
                w = wk(uid)
                if w in seen:
                    continue
                seen.append(w)
                if len(seen) <= 10 and n < TINY:
                    tiny += 1
                if rank is None and w in targets:
                    rank = len(seen)
            per[r["qid"]][s] = {"rank": rank, "tiny10": tiny, "missing": missing}
    # leg depth: records per list and chunks needed to reach 50 records
    need_v, rec_v50, need_t = [], [], []
    vec_ms: dict[str, list[float]] = {}
    for r in rows:
        for leg in r["legs"]:
            seen, need = set(), None
            for i, (b, c, n) in enumerate(leg["hits"]):
                seen.add(b)
                if need is None and len(seen) >= DEPTH:
                    need = i + 1
            if leg["source"] == "dense":
                need_v.append(need)
                rec_v50.append(len({b for b, _, _ in leg["hits"][:50]}))
            else:
                need_t.append(need)
        for kk, ms in r["vec_ms"].items():
            vec_ms.setdefault(kk, []).append(ms)

    def pct(xs, p):
        xs = sorted(x for x in xs if x is not None)
        return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None

    report: dict = {"rows": len(rows), "leg_depth": {
        "dense_records_in_50_chunks_median_p10_p90": [pct(rec_v50, .5), pct(rec_v50, .1), pct(rec_v50, .9)],
        "dense_chunks_for_50_records_median_p90_max": [pct(need_v, .5), pct(need_v, .9), pct(need_v, 1.0)],
        "dense_never_50_in_800": sum(1 for x in need_v if x is None),
        "text_chunks_for_50_records_median_p90": [pct(need_t, .5), pct(need_t, .9)],
        "text_lists_never_50_in_300": sum(1 for x in need_t if x is None), "text_lists": len(need_t),
        "vector_ms_median_by_k": {kk: pct(v, .5) for kk, v in vec_ms.items()},
        "vector_ms_p90_by_k": {kk: pct(v, .9) for kk, v in vec_ms.items()}}}
    base = ("shallow", 1, 0, "scores")
    for sname in ("descriptive", "titles"):
        qs = [v for v in per.values() if v["set"] == sname]
        if not qs:
            continue
        rep = {}
        for s in strategies:
            m = metrics([v[s]["rank"] for v in qs])
            w = sum(1 for v in qs if (1 / v[s]["rank"] if v[s]["rank"] else 0) > (1 / v[base]["rank"] if v[base]["rank"] else 0))
            l = sum(1 for v in qs if (1 / v[s]["rank"] if v[s]["rank"] else 0) < (1 / v[base]["rank"] if v[base]["rank"] else 0))
            m["vs_base"] = [w, l, sign_p(w, l)]
            m["tiny10"] = round(sum(v[s]["tiny10"] for v in qs) / (10 * len(qs)), 3)
            m["missing"] = sum(v[s]["missing"] for v in qs)
            rep["/".join(map(str, s))] = m
        report[sname] = rep
    if a.out:
        a.out.write_text(json.dumps({"report": report, "per_query": {q: {"/".join(map(str, s)): x for s, x in v.items() if s != "set"} for q, v in per.items()}}, ensure_ascii=False), "utf-8")
    for sname in ("descriptive", "titles"):
        if sname not in report:
            continue
        print(f"== {sname}")
        for s, m in report[sname].items():
            print(f"{s:16s} mrr {m['mrr']:.3f} r1 {m['r1']:.3f} r5 {m['r5']:.3f} r10 {m['r10']:.3f} "
                  f"vs-base +{m['vs_base'][0]}/-{m['vs_base'][1]} p={m['vs_base'][2]} tiny10 {m['tiny10']} miss {m['missing']}")
    print(json.dumps(report["leg_depth"], indent=1))


if __name__ == "__main__":
    main()
