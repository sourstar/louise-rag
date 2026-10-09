"""Queries still to run: one_language_queries.tsv minus the qids already in
results.jsonl, as the JSON lines one_language_search.py reads.
Usage: python one_language_pending.py QUERIES.tsv RESULTS.jsonl > pending.jsonl"""
import json
import sys

done = set()
try:
    for line in open(sys.argv[2], encoding="utf-8"):
        if line.startswith("{"):
            r = json.loads(line)
            if not r.get("error"):
                done.add(r["qid"])
except FileNotFoundError:
    pass
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    work, lang, query = line.rstrip("\n").split("\t")
    qid = f"{work}-{lang}"
    if qid not in done:
        print(json.dumps({"qid": qid, "work": work, "lang": lang, "query": query}, ensure_ascii=False))
