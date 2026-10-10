"""MEASUREMENT (not product code): embeds the queries of the live re-check and
each translator's translations of them into translator_choice_search.py as
`DATA`, so script and data reach the node as one stdin stream
(one_language_rep_run.sh). docs/notes/translator-choice.md.

  madlad-recheck  the translations captured by the live re-check (MADLAD, 19 languages)
  <model>      out/xlate_choice/q_<model>.jsonl (the node's xlate.py: the main
               languages + CJK, never the query's own)

Usage: python translator_choice_build.py LIVE_RESULTS.jsonl XLATE_DIR SCRIPT.py OUT.py MODEL...
"""
import json
import sys

live, xdir, script, out, models = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
qs = {}
for line in open(live, encoding="utf-8"):
    r = json.loads(line)
    if r["qid"].startswith("SAMPLE"):
        continue
    qs[r["qid"]] = {"qid": r["qid"], "query": r["query"], "lang": r["lang"],
                    "tr": {"madlad-recheck": r["translations"]}}
for m in models:
    n = 0
    for line in open(f"{xdir}/q_{m}.jsonl", encoding="utf-8"):
        r = json.loads(line)
        if r["qid"] in qs:
            qs[r["qid"]]["tr"][m] = r["translations"]
            n += 1
    print(m, n, file=sys.stderr)
lines = [json.dumps(q, ensure_ascii=False) for q in qs.values()
         if all(m in q["tr"] for m in models)]
s = open(script, encoding="utf-8").read()
assert s.count('\nDATA = ""\n') == 1
s = s.replace('\nDATA = ""\n', "\nDATA = " + json.dumps("\n".join(lines)) + "\n")
open(out, "w", encoding="utf-8").write(s)
print(len(lines))
