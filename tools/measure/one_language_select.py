"""MEASUREMENT (not product code), step 2b of docs/notes/one-language-per-work.md:
draw the works the query set is written for — a SEEDED stratified sample of
the cross-language works (works.json), so the choice is not the writer's —
and print, per work, what a query writer needs: titles per language, author,
LCSH subjects, the MARC 520 summary.

Strata (works with books in >=2 languages):
  orig-held   the original language is held and is not English, quota per
              that language (fr de it es el la nl fi, the rest pooled)
  orig-en     the original is English and a translation is held
  orig-absent the original is known and NOT held (Ibsen, Tolstoy, ...)

Usage: python one_language_select.py --out tools/measure/out/onelang > candidates.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

SEED = "one-language-per-work-2026-10-09"
QUOTA_HELD = {"fr": 22, "de": 18, "it": 10, "es": 10, "el": 12, "la": 6, "nl": 5, "fi": 4, "other": 8}
QUOTA_EN = 30
QUOTA_ABSENT = 25


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    works = json.loads((a.out / "works.json").read_text("utf-8"))
    recs = {}
    for line in (a.out / "records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        recs[r["uid"]] = r
    cross = [w for w in works if len(w["langs"]) >= 2]
    rng = random.Random(hashlib.sha256(SEED.encode()).hexdigest())
    rng.shuffle(cross)
    picked: list[tuple[str, dict]] = []
    held_n = {k: 0 for k in QUOTA_HELD}
    n_en = n_abs = 0
    for w in cross:
        o = w["original"]
        if o and o != "en" and o in w["langs"]:
            k = o if o in QUOTA_HELD else "other"
            if held_n[k] < QUOTA_HELD[k]:
                held_n[k] += 1
                picked.append((f"orig-held:{o}", w))
        elif o == "en":
            if n_en < QUOTA_EN:
                n_en += 1
                picked.append(("orig-en", w))
        elif o and o not in w["langs"]:
            if n_abs < QUOTA_ABSENT:
                n_abs += 1
                picked.append((f"orig-absent:{o}", w))
    sel = [{"work": w["work"], "stratum": s, "original": w["original"], "langs": w["langs"],
            "books": w["books"]} for s, w in picked]
    (a.out / "selected_works.json").write_text(json.dumps(sel, ensure_ascii=False, indent=0), "utf-8")
    for s, w in picked:
        print(f"## {w['work']}  [{s}]  label={w['labels'][:1]}  orig={w['original']}  langs={w['langs']}  n={len(w['books'])}")
        seen = set()
        for b in w["books"]:
            r = recs[b]
            if (r["lang"], r["title"][:40]) in seen:
                continue
            seen.add((r["lang"], r["title"][:40]))
            print(f"   {r['lang']}  {r['title'][:90]}  | {', '.join(r.get('creators', [])[:2])}")
            if len(seen) >= 5:
                break
        en = next((recs[b] for b in w["books"] if recs[b].get("summary")), None)
        subj = next((recs[b].get("subjects") for b in w["books"] if recs[b].get("subjects")), [])
        print(f"   subjects: {'; '.join(subj[:5])}")
        if en:
            print(f"   summary: {en['summary'][:330]}")
    print(f"\n# {len(picked)} works: held {held_n}, orig-en {n_en}, orig-absent {n_abs}")


if __name__ == "__main__":
    main()
