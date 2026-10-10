"""MEASUREMENT (not product code): embeds the queries of the live re-check
(their captured translations included) into one_language_rep_search.py as
`DATA`, so the script and its data reach the node as one stdin stream
(one_language_rep_run.sh).

Usage: python one_language_rep_build.py LIVE_RESULTS.jsonl SCRIPT.py OUT.py N [SKIP_QIDS,...]
(N = 0 for all)."""
import json, sys
src, script, out, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
skip = set(sys.argv[5].split(",")) if len(sys.argv) > 5 else set()
lines = []
for l in open(src, encoding="utf-8"):
    r = json.loads(l)
    if r["qid"] in skip:
        continue
    lines.append(json.dumps({"qid": r["qid"], "query": r["query"], "lang": r["lang"],
                             "translations": r["translations"]}, ensure_ascii=False))
lines = lines[:n] if n else lines
s = open(script, encoding="utf-8").read()
assert s.count('\nDATA = ""\n') == 1
s = s.replace('\nDATA = ""\n', "\nDATA = " + json.dumps("\n".join(lines)) + "\n")
open(out, "w", encoding="utf-8").write(s)
print(len(lines))
