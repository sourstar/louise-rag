"""MEASUREMENT (not product code): the cross-collection merge, judged on the
node's own queries (docs/notes/federated-merge.md; operator, 2026-10-31: B,
"keep A in the list of the potential improvements to be evaluated").

Four steps, each resumable, each reading the previous one's file:

  collect   QUERIES OUT.jsonl [--pass today|reranked] [--rerank-like gutenberg]
            Each query through `retrieval.federated_search` over every
            searchable collection, `keep_lists=True`: each collection's list
            as the merge sees it, its top `--keep` (20) records with the
            reranker's score. `--pass reranked --rerank-like gutenberg` gives
            the collections that have no reranker gutenberg's, for this run
            only (nothing is configured). QUERIES: a text file, one query a
            line, or `--from-query-log` (the default manifest, read-only;
            endpoints search and ask). The searches are real ones: they take
            the card's reranker and the lazy-embed queue as any search does,
            and are not written to the query log.
  judge     LISTS.jsonl OUT.jsonl
            Every record in the top 10 of any list, judged 0–3 for its query
            by the node's chat model with the UMBRELA prompt (Upadhyay et
            al., 2024, after Thomas et al., 2023: LLM relevance labels that
            reproduce TREC assessors' system rankings). Judged once, stored:
            the judge is not held to invariant 12, so the stored labels ARE
            the set, and every later number reproduces from the files.
  agree     JUDGED.jsonl SECOND.jsonl   (and `sample` to make SECOND)
            A second judge (a person, or another model) on a seeded sample:
            Cohen's kappa, linear weights, and the agreement on relevant
            (grade ≥ 2).
  score     LISTS.jsonl JUDGED.jsonl
            Platt scaling per collection (Platt 1999), five query folds:
            fitted on four, the fifth merged with the PRODUCT's own merge
            functions, `retrieval._merge_rrf_federated` (today) and
            `retrieval._merge_by_relevance` (B). MRR@10 (the first record of
            grade ≥ 2) and nDCG@10 (gain 2^g − 1), per-query paired
            randomization test (Smucker, Allan & Carterette 2007), each
            calibration's Brier score and ECE on held-out queries, each
            collection's own MRR before and after its reranker; then the
            calibration fitted on every query, as the TOML line a collection
            file takes.

Why the top 10 of each list is enough: both merges keep each list's own
order, so a merged top 10 is made of prefixes of the lists, none longer
than 10 — every record either merge can show in its top 10 is judged.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

TOP = 10                  # judged, and measured: MRR@10, nDCG@10
RELEVANT = 2              # UMBRELA's "has an answer"
FOLDS = 5
MIN_EACH = 8              # positives and negatives a calibration needs
PERMUTATIONS = 10_000
TEXT_CHARS = 4000         # what the judge reads of a record's shown chunk

UMBRELA = (
    "Given a query and a passage, you must provide a score on an integer scale "
    "of 0 to 3 with the following meanings:\n"
    "0 = represent that the passage has nothing to do with the query,\n"
    "1 = represents that the passage seems related to the query but does not answer it,\n"
    "2 = represents that the passage has some answer for the query, but the answer may "
    "be a bit unclear, or hidden amongst extraneous information and\n"
    "3 = represents that the passage is dedicated to the query and contains the exact answer.\n\n"
    "Important Instruction: Assign category 1 if the passage is somewhat related to the "
    "topic but not completely, category 2 if passage presents something very important "
    "related to the entire topic but also has some extra information and category 3 if the "
    "passage only and entirely refers to the topic. If none of the above satisfies give it "
    "category 0.\n\n"
    "Query: {query}\nPassage: {passage}\n\n"
    "Split this problem into steps:\n"
    "Consider the underlying intent of the search.\n"
    "Measure how well the content matches a likely intent of the query (M).\n"
    "Measure how trustworthy the passage is (T).\n"
    "Consider the aspects above and the relative importance of each, and decide on a "
    "final score (O). Final score must be an integer value only.\n"
    "Do not provide any code in result. Provide each score in the format of: "
    "##final score: score without providing any reasoning. /no_think")


# ---------------------------------------------------------------- files


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def judge_key(query: str, collection: str, book_id: Any) -> str:
    return hashlib.sha256(f"{query}\0{collection}\0{book_id}".encode()).hexdigest()


# ---------------------------------------------------------------- collect


def queries_from_log(manifest: Path, endpoints: Iterable[str] = ("search", "ask")) -> list[str]:
    con = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True, timeout=30)
    try:
        marks = ",".join("?" for _ in endpoints)
        rows = con.execute(f"SELECT query, MIN(ts) FROM query_log WHERE endpoint IN ({marks}) "
                           f"AND TRIM(query) != '' GROUP BY query ORDER BY MIN(ts)",
                           tuple(endpoints)).fetchall()
    finally:
        con.close()
    return [q for q, _ in rows]


def _kept(hit: dict, rank: int) -> dict:
    from app import retrieval
    return {"rank": rank, "book_id": hit.get("book_id"), "chunk_id": hit.get("chunk_id"),
            "rerank_score": hit.get("rerank_score"), "lang": hit.get("lang"),
            "title": hit.get(retrieval.HEAD) or hit.get("title") or "",
            "text": (hit.get("text") or "")[:TEXT_CHARS]}


def collect(queries: list[str], out: Path, *, pass_name: str, rerank_like: str | None,
            names: list[str] | None, keep: int) -> None:
    from app import collections as collections_mod, query_service, retrieval
    participants = query_service.resolve_collections(names)
    if rerank_like:
        lender = collections_mod.reranker_for(collections_mod.by_name(rerank_like))
        if lender is None:
            raise SystemExit(f"{rerank_like} has no reranker to lend")
        participants = [c if collections_mod.reranker_for(c) is not None
                        else dataclasses.replace(c, reranker=dataclasses.replace(lender, calibration=None))
                        for c in participants]
    done = {(r["query"], r["pass"]) for r in read_jsonl(out)}
    for n, q in enumerate(queries, 1):
        if (q, pass_name) in done:
            continue
        result = retrieval.federated_search(participants, q, k=TOP, keep_lists=True)
        lists = {name: {"reranked": li["reranked"], "rerank_model": li["rerank_model"],
                        "hits": [_kept(h, i) for i, h in enumerate(li["hits"][:keep], 1)]}
                 for name, li in result.get("lists", {}).items()}
        append_jsonl(out, {"query": q, "pass": pass_name, "lists": lists,
                           "merged_by": result.get("merged_by"),
                           "rerank_withheld": result.get("rerank_withheld", [])})
        print(f"[{n}/{len(queries)}] {pass_name}: {len(lists)} lists", file=sys.stderr)


# ---------------------------------------------------------------- judge

_GRADE = re.compile(r"final score\s*:\s*([0-3])", re.I)


def grade_of(answer: str) -> int | None:
    m = _GRADE.search(answer or "")
    if m:
        return int(m.group(1))
    digits = re.findall(r"\b([0-3])\b", answer or "")
    return int(digits[-1]) if len(digits) == 1 else None


def to_judge(lists_rows: list[dict]) -> dict[str, dict]:
    """Every record in the top 10 of any list, once: (query, collection,
    record) -> the text the first list that showed it showed."""
    pool: dict[str, dict] = {}
    for row in lists_rows:
        for name, li in sorted(row["lists"].items()):
            for h in li["hits"][:TOP]:
                key = judge_key(row["query"], name, h["book_id"])
                pool.setdefault(key, {"key": key, "query": row["query"], "collection": name,
                                      "book_id": h["book_id"], "chunk_id": h.get("chunk_id"),
                                      "title": h.get("title") or "", "text": h.get("text") or ""})
    return pool


def judge(lists_path: Path, out: Path, *, chat=None) -> None:
    from app import config, llm
    chat = chat or (lambda prompt: llm.chat([{"role": "user", "content": prompt}],
                                            temperature=0.0, max_tokens=32))
    done = {r["key"] for r in read_jsonl(out)}
    pool = [p for k, p in to_judge(read_jsonl(lists_path)).items() if k not in done]
    for n, p in enumerate(pool, 1):
        passage = f"{p['title']}\n\n{p['text']}" if p["title"] else p["text"]
        answer = chat(UMBRELA.format(query=p["query"], passage=passage))
        g = grade_of(answer)
        append_jsonl(out, {"key": p["key"], "query": p["query"], "collection": p["collection"],
                           "book_id": p["book_id"], "chunk_id": p["chunk_id"], "grade": g,
                           "answer": answer[:200], "judge": getattr(config, "CHAT_MODEL", "")})
        if n % 50 == 0:
            print(f"judged {n}/{len(pool)}", file=sys.stderr)


# ---------------------------------------------------------------- agree


def sample(judged_path: Path, lists_path: Path, out: Path, n: int, seed: int = 0) -> None:
    pool = to_judge(read_jsonl(lists_path))
    keys = sorted(r["key"] for r in read_jsonl(judged_path) if r.get("grade") is not None)
    for key in random.Random(seed).sample(keys, min(n, len(keys))):
        p = pool[key]
        append_jsonl(out, {**p, "grade": None})


def cohen_kappa(a: list[int], b: list[int], k: int = 4, weighted: bool = True) -> float:
    n = len(a)
    if not n:
        return float("nan")
    w = [[(abs(i - j) / (k - 1)) if weighted else float(i != j) for j in range(k)] for i in range(k)]
    obs = sum(w[x][y] for x, y in zip(a, b)) / n
    pa = [a.count(i) / n for i in range(k)]
    pb = [b.count(i) / n for i in range(k)]
    exp = sum(w[i][j] * pa[i] * pb[j] for i in range(k) for j in range(k))
    return 1.0 - obs / exp if exp else float("nan")


def agree(judged_path: Path, second_path: Path) -> dict:
    first = {r["key"]: r["grade"] for r in read_jsonl(judged_path) if r.get("grade") is not None}
    pairs = [(first[r["key"]], r["grade"]) for r in read_jsonl(second_path)
             if r.get("grade") is not None and r["key"] in first]
    a, b = [x for x, _ in pairs], [y for _, y in pairs]
    rel = [(x >= RELEVANT) == (y >= RELEVANT) for x, y in pairs]
    return {"pairs": len(pairs), "kappa_linear": round(cohen_kappa(a, b), 3),
            "kappa_relevant": round(cohen_kappa([int(x >= RELEVANT) for x in a],
                                                [int(y >= RELEVANT) for y in b], k=2,
                                                weighted=False), 3),
            "agree_relevant": round(sum(rel) / len(rel), 3) if rel else None}


# ---------------------------------------------------------------- score


def platt(scores: list[float], labels: list[int]) -> tuple[float, float] | None:
    """Platt's sigmoid fit (1999) with his prior-corrected targets, by
    Newton's method on the convex log loss: P = 1/(1 + e^-(a·s + b)).
    None when either class has fewer than MIN_EACH points."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos < MIN_EACH or neg < MIN_EACH:
        return None
    hi, lo = (pos + 1) / (pos + 2), 1 / (neg + 2)
    t = [hi if y else lo for y in labels]
    a, b = 0.0, math.log((pos + 1) / (neg + 1))
    for _ in range(100):
        ga = gb = haa = hab = hbb = 0.0
        for s, ti in zip(scores, t):
            p = 0.5 * (1.0 + math.tanh(0.5 * (a * s + b)))
            w = max(p * (1 - p), 1e-12)
            ga += (p - ti) * s
            gb += p - ti
            haa += w * s * s
            hab += w * s
            hbb += w
        haa += 1e-9
        hbb += 1e-9
        det = haa * hbb - hab * hab
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if max(abs(da), abs(db)) < 1e-10:
            break
    return a, b


def mrr(grades: list[int | None]) -> float:
    for i, g in enumerate(grades[:TOP], 1):
        if g is not None and g >= RELEVANT:
            return 1.0 / i
    return 0.0


def ndcg(grades: list[int | None], pool: list[int]) -> float:
    def dcg(gs):
        return sum((2 ** (g or 0) - 1) / math.log2(i + 2) for i, g in enumerate(gs[:TOP]))
    ideal = dcg(sorted(pool, reverse=True))
    return dcg(grades) / ideal if ideal else 0.0


def randomization(d: list[float], n: int = PERMUTATIONS, seed: int = 0) -> float:
    """Two-sided paired randomization test on the mean difference."""
    if not d:
        return float("nan")
    rnd = random.Random(seed)
    obs = abs(sum(d) / len(d))
    hits = sum(1 for _ in range(n)
               if abs(sum(x if rnd.random() < 0.5 else -x for x in d) / len(d)) >= obs - 1e-15)
    return (hits + 1) / (n + 1)


def _fold(query: str) -> int:
    return int(hashlib.sha256(query.encode()).hexdigest(), 16) % FOLDS


def _merged(lists: dict, how: str, cals: dict | None = None) -> list[tuple[str, Any]]:
    from app import retrieval
    names = sorted(lists)
    tagged = [[{**h, "source_collection": n} for h in lists[n]["hits"]] for n in names]
    if how == "rank":
        out = retrieval._merge_rrf_federated(tagged)
    else:
        out = retrieval._merge_by_relevance([(hits, cals[n]) for n, hits in zip(names, tagged)])
    return [(h["source_collection"], h["book_id"]) for h in out[:TOP]]


def score(lists_path: Path, judged_path: Path) -> dict:
    from app import collections as collections_mod
    rows = read_jsonl(lists_path)
    grade = {r["key"]: r["grade"] for r in read_jsonl(judged_path) if r.get("grade") is not None}
    by_pass: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_pass.setdefault(r["pass"], {})[r["query"]] = r["lists"]
    today, reranked = by_pass.get("today", {}), by_pass.get("reranked", {})
    queries = sorted(set(today) & set(reranked)) if today and reranked else sorted(today or reranked)
    if not queries:
        raise SystemExit("no query has both passes collected")
    base = today or reranked
    test = reranked or today

    def g(q, name, bid):
        return grade.get(judge_key(q, name, bid))

    def points(qs):
        pts: dict[str, list[tuple[float, int]]] = {}
        for q in qs:
            for name, li in test[q].items():
                if not li["reranked"]:
                    continue
                for h in li["hits"][:TOP]:
                    gr = g(q, name, h["book_id"])
                    if gr is not None and h.get("rerank_score") is not None:
                        pts.setdefault(name, []).append((h["rerank_score"], int(gr >= RELEVANT)))
        return pts

    def fit(qs):
        cals = {}
        for name, pts in points(qs).items():
            ab = platt([s for s, _ in pts], [y for _, y in pts])
            if ab and ab[0] > 0:
                model = next((test[q][name]["rerank_model"] for q in qs if name in test[q]), "")
                cals[name] = collections_mod.Calibration(ab[0], ab[1], model or "", len(pts))
        return cals

    per_query: list[dict] = []
    brier: dict[str, list[float]] = {}
    bins: dict[str, list[tuple[float, int]]] = {}
    skipped_folds = 0
    for f in range(FOLDS):
        held = [q for q in queries if _fold(q) == f]
        cals = fit([q for q in queries if _fold(q) != f])
        for q in held:
            # the ideal for nDCG: every judged record of the query, once
            # (a record in both passes is one record)
            pooled = {judge_key(q, n, h["book_id"]): g(q, n, h["book_id"])
                      for p in (base, test) for n, li in p[q].items() for h in li["hits"][:TOP]}
            pool = [x for x in pooled.values() if x is not None]
            if not any(x >= RELEVANT for x in pool):
                per_query.append({"query": q, "no_relevant": True})
                continue
            row = {"query": q}
            for label, lists, how in (("today", base[q], "rank"), ("rank_all", test[q], "rank"),
                                      ("relevance", test[q], "relevance")):
                if how == "relevance" and not all(n in cals and lists[n]["reranked"] for n in lists):
                    row[label] = None
                    continue
                order = _merged(lists, how, cals)
                grades = [g(q, n, b) for n, b in order]
                row[label] = {"mrr": mrr(grades), "ndcg": ndcg(grades, pool)}
            per_query.append(row)
            for name, li in test[q].items():
                cal = cals.get(name)
                if not cal or not li["reranked"]:
                    continue
                for h in li["hits"][:TOP]:
                    gr = g(q, name, h["book_id"])
                    if gr is None or h.get("rerank_score") is None:
                        continue
                    p, y = cal.probability(h["rerank_score"]), int(gr >= RELEVANT)
                    brier.setdefault(name, []).append((p - y) ** 2)
                    bins.setdefault(name, []).append((p, y))
        skipped_folds += 0 if cals else 1

    judged = [r for r in per_query if not r.get("no_relevant")]

    def mean(label, metric):
        xs = [r[label][metric] for r in judged if r.get(label)]
        return round(sum(xs) / len(xs), 4) if xs else None

    def versus(label, metric):
        both = [r for r in judged if r.get(label) and r.get("today")]
        d = [r[label][metric] - r["today"][metric] for r in both]
        return {"queries": len(both), "better": sum(x > 1e-12 for x in d),
                "worse": sum(x < -1e-12 for x in d),
                "p": round(randomization(d), 4) if d else None}

    def ece(pts):
        out, n = 0.0, len(pts)
        for i in range(10):
            b = [(p, y) for p, y in pts if i / 10 <= p < (i + 1) / 10 or (i == 9 and p == 1.0)]
            if b:
                out += len(b) / n * abs(sum(p for p, _ in b) / len(b) - sum(y for _, y in b) / len(b))
        return round(out, 4)

    def own(q_lists, q, name):
        li = q_lists[q].get(name)
        return mrr([g(q, name, h["book_id"]) for h in li["hits"]]) if li else None

    own_lists = {}
    for name in sorted({n for q in judged for n in test[q["query"]]}):
        before = [own(base, r["query"], name) for r in judged]
        after = [own(test, r["query"], name) for r in judged]
        pairs = [(x, y) for x, y in zip(before, after) if x is not None and y is not None]
        if pairs:
            own_lists[name] = {"mrr_before": round(sum(x for x, _ in pairs) / len(pairs), 4),
                               "mrr_after": round(sum(y for _, y in pairs) / len(pairs), 4),
                               "queries": len(pairs)}
    final = fit(queries)
    return {
        "queries": len(queries), "judged_queries": len(judged),
        "no_relevant": len(per_query) - len(judged),
        "mrr10": {k: mean(k, "mrr") for k in ("today", "rank_all", "relevance")},
        "ndcg10": {k: mean(k, "ndcg") for k in ("today", "rank_all", "relevance")},
        "vs_today": {k: {m: versus(k, m) for m in ("mrr", "ndcg")} for k in ("rank_all", "relevance")},
        "calibration_heldout": {n: {"brier": round(sum(v) / len(v), 4), "ece": ece(bins[n]),
                                    "points": len(v)} for n, v in sorted(brier.items())},
        "own_list_mrr10": own_lists,
        "toml": {n: f"calibration = {{ a = {c.a:.6g}, b = {c.b:.6g}, rerank_model = "
                    f"\"{c.rerank_model}\", judged = {c.judged}, set = \"{Path(judged_path).stem}\" }}"
                 for n, c in sorted(final.items())},
    }


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("queries", nargs="?")
    c.add_argument("out")
    c.add_argument("--from-query-log", action="store_true")
    c.add_argument("--pass", dest="pass_name", choices=("today", "reranked"), default="today")
    c.add_argument("--rerank-like")
    c.add_argument("--collections")
    c.add_argument("--keep", type=int, default=20)
    c.add_argument("--limit", type=int)
    j = sub.add_parser("judge")
    j.add_argument("lists")
    j.add_argument("out")
    s = sub.add_parser("sample")
    s.add_argument("judged")
    s.add_argument("lists")
    s.add_argument("out")
    s.add_argument("-n", type=int, default=300)
    a = sub.add_parser("agree")
    a.add_argument("judged")
    a.add_argument("second")
    sc = sub.add_parser("score")
    sc.add_argument("lists")
    sc.add_argument("judged")
    args = ap.parse_args(argv)
    if args.cmd == "collect":
        if args.from_query_log:
            from app import collections as collections_mod
            qs = queries_from_log(collections_mod.default_collection().manifest_path)
        elif args.queries:
            qs = [q.strip() for q in Path(args.queries).read_text(encoding="utf-8").splitlines()
                  if q.strip()]
        else:
            ap.error("collect needs QUERIES or --from-query-log")
        if args.limit:
            qs = qs[: args.limit]
        collect(qs, Path(args.out), pass_name=args.pass_name, rerank_like=args.rerank_like,
                names=args.collections.split(",") if args.collections else None, keep=args.keep)
    elif args.cmd == "judge":
        judge(Path(args.lists), Path(args.out))
    elif args.cmd == "sample":
        sample(Path(args.judged), Path(args.lists), Path(args.out), args.n)
    elif args.cmd == "agree":
        print(json.dumps(agree(Path(args.judged), Path(args.second)), indent=2))
    else:
        print(json.dumps(score(Path(args.lists), Path(args.judged)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
