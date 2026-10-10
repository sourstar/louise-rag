"""MEASUREMENT (not product code): the deterministic query-language guess
(app/query_language.py) on the 490 labelled queries of
docs/notes/one-language-per-work.md, and what it does to the edition shown
— the work's one hit in the query's language (05 §5.3).

Accuracy: the guess against each query's label, among the languages
gutenberg holds (records.jsonl). Edition shown: the live re-check's
reranked lists (`read.ranked`, top 60) collapsed one hit per work over
works.json with the preference (query_lang, en), for query_lang = the label
(the chat model's reading, right), None (unavailable) and the guess; scored
as one_language_live_score.py does (top 10, found queries whose work has a
book in the query's language).

Usage: python one_language_guess.py --old tools/measure/out/onelang
         --live tools/measure/out/onelang_live
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import query_language  # noqa: E402

ALSO = {"WQ4509219": {"WQ40185"}, "WQ40185": {"WQ4509219"}, "WQ58190302": {"WQ8258"}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path, required=True)
    ap.add_argument("--live", type=Path, required=True)
    a = ap.parse_args()
    lang_of, held = {}, Counter()
    for line in (a.old / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        lang_of[int(r["uid"].split(":")[1])] = r.get("lang") or "und"
        held[r.get("lang") or "und"] += 1
    held_set = {c for c in held if c != "und"}
    work_of, wlangs = {}, {}
    for w in json.loads((a.old / "works.json").read_text("utf-8")):
        books = [int(b.split(":")[1]) for b in w["books"]]
        wlangs[w["work"]] = {lang_of.get(b) for b in books}
        for b in books:
            work_of[b] = w["work"]
    rows = [json.loads(l) for l in (a.live / "live_results.jsonl").open(encoding="utf-8")]
    rows = [r for r in rows if not r["qid"].startswith("SAMPLE") and not r.get("error")]
    conf = Counter()
    by_lang: dict[str, Counter] = {}
    shown = {"label": [0, 0], "none": [0, 0], "guess": [0, 0]}
    for r in rows:
        g = query_language.guess(r["query"], held_set)
        lab = r["lang"]
        outcome = "right" if g == lab else ("none" if g is None else "wrong")
        conf[outcome] += 1
        by_lang.setdefault(lab, Counter())[outcome] += 1
        if outcome == "wrong":
            conf[f"{lab}->{g}"] += 1
        W = r["qid"].split("-")[0].split("#")[0]
        targets = {W} | ALSO.get(W, set())
        if lab not in wlangs.get(W, set()):
            continue
        ranked = r["read"]["ranked"] or []
        for name, ql in (("label", lab), ("none", None), ("guess", g)):
            prefer = [c for c in dict.fromkeys([ql, "en"]) if c]
            seen: dict[str, tuple] = {}
            order: list[str] = []
            for b, lang, *_ in ranked:
                w = work_of.get(b, f"B{b}")
                if w not in seen:
                    order.append(w)
                    seen[w] = []
                seen[w].append((lang or lang_of.get(b)))
            if not any(w in targets for w in order[:10]):
                continue
            w = next(w for w in order[:10] if w in targets)
            langs = seen[w]
            pick = next((l for p in prefer for l in langs if l == p), langs[0])
            shown[name][1] += 1
            shown[name][0] += pick == lab
    n = len(rows)
    print(f"queries {n}; held languages {len(held_set)}")
    print("guess:", {k: v for k, v in conf.items() if "->" not in k},
          f"accuracy {conf['right'] / n:.3f}, wrong {conf['wrong'] / n:.3f}, none {conf['none'] / n:.3f}")
    print("errors:", sorted(((k, v) for k, v in conf.items() if "->" in k), key=lambda kv: -kv[1]))
    print("by label:", {k: dict(v) for k, v in sorted(by_lang.items(), key=lambda kv: -sum(kv[1].values()))})
    for k, (x, m) in shown.items():
        print(f"edition shown in the query's language, query_lang={k}: {x}/{m} = {x / max(1, m):.3f}")


if __name__ == "__main__":
    main()
