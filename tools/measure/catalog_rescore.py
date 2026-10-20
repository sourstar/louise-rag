"""MEASUREMENT (not product code): 2efe665's catalogue variant (05 §5.1,
amended 2026-10-20; docs/notes/query-campaign.md) scored OFFLINE, as
one_language_rescore.py scored the 2026-10-09 fusion fixes: the captured
legs (tools/measure/out/onelang/results.jsonl) through the PRODUCT's
`_merge_rrf`, without and with the catalogue's ranked records as one more
text variant run as the reader's own words — the records exactly as
`retrieval._catalog_variant` returned them on the node (a capture made with
2efe665's code in a read-only container; JSON lines {qid, hits, catalog_ms,
variant_ms}). Pre-rerank, like the numbers it is compared with.

  python tools/measure/catalog_rescore.py --out tools/measure/out/onelang \
      --capture CAPTURE.jsonl [--queries tools/measure/one_language_titles.tsv]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import one_language_rescore as rs  # noqa: E402
import one_language_score as sc  # noqa: E402

from app import retrieval  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--queries", type=Path, default=Path("tools/measure/one_language_queries.tsv"))
    ap.add_argument("--depth", type=int, default=50)
    a = ap.parse_args()
    lang_of, work_of, _winfo, qmeta, results = sc.load(a.out, a.queries)
    cap = {}
    for line in a.capture.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("collection") == "gutenberg":
            cap[r["qid"]] = r
    rows = {"after": [], "after+C": []}
    with_catalog, costs = 0, []
    for qid, r in results.items():
        if not r.get("legs") or qid not in cap:
            continue
        m = qmeta[qid]
        targets = {m["work"]} | sc.ALSO.get(m["work"], set())
        lists = rs.legs_as_lists(r["legs"], lang_of)
        own = rs.own_lists(r["legs"], r["query"], r.get("translations") or {})
        variant = [{"book_id": b, "chunk_id": c, "source": "catalog", "lang": lang_of.get(b, "und")}
                   for b, c in cap[qid]["hits"]]
        costs.append(cap[qid]["catalog_ms"] + cap[qid]["variant_ms"])
        with_catalog += bool(variant)
        for k, ls, ow in (("after", lists, own),
                          ("after+C", lists + ([variant] if variant else []),
                           own | ({len(lists)} if variant else set()))):
            merged = retrieval._merge_rrf(ls, a.depth, own=ow, query_lang=m["lang"])
            ranked = [int(h["book_id"]) for h in merged][:50]
            rows[k].append(next((i for i, b in enumerate(ranked, 1)
                                 if work_of.get(b, f"B{b}") in targets), None))
    n = len(rows["after"])
    report = {"queries": n, "with_catalogue_hits": with_catalog,
              "catalogue_ms": {"median": round(statistics.median(costs), 1), "max": round(max(costs), 1)}}
    for k, ranks in rows.items():
        report[k] = sc.metrics(ranks) | {"r50": round(sum(1 for x in ranks if x) / n, 3)}
    better = sum(1 for p, q in zip(rows["after+C"], rows["after"]) if (1 / p if p else 0) > (1 / q if q else 0))
    worse = sum(1 for p, q in zip(rows["after+C"], rows["after"]) if (1 / p if p else 0) < (1 / q if q else 0))
    report["after+C vs after"] = {"better": better, "worse": worse, "p_sign": sc.sign_p(better, worse)}
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
