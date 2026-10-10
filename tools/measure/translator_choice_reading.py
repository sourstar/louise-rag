"""MEASUREMENT (not product code): chrF / BLEU (sacrebleu 2.4.3, corpus level) of each
translator on the classical set (classical-translation.md's evalset.json, whose MADLAD /
Qwen3-30B / Gemma translations are reused from /root/xlate-classical/out) and on the modern
set (evalset_modern.json). Scored on the HALF subset (items[::2], what every candidate ran)
and, where a model ran all, on all. len = output chars / reference chars (truncation shows).
Usage: python translator_choice_reading.py [OLD_DIR NEW_DIR]  (sacrebleu 2.4.3)
OLD_DIR holds the classical evalset.json and the 2026-10-09 translations; NEW_DIR the
candidates eval_*.json / evalm_*.json and evalset_modern.json. docs/notes/translator-choice.md."""
import json
import statistics
import sys
from pathlib import Path

import sacrebleu

OLD = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/root/xlate-classical/out")
NEW = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/root/xlate-choice/out")
res = {}
for setname, ev, prefix in (("classical", OLD / "evalset.json", "eval_"),
                            ("modern", NEW / "evalset_modern.json", "evalm_")):
    allitems = json.loads(ev.read_text())
    files = sorted(NEW.glob(f"{prefix}*.json"))
    if setname == "classical":
        files += [OLD / f"eval_{m}.json" for m in ("madlad", "qwen3-30b-a3b", "gemma-3-27b-it")]
    for f in files:
        m = f.stem[len(prefix):] + (" (2026-10-09)" if f.parent == OLD else "")
        got = json.loads(f.read_text())
        for sub, items in (("half", allitems[::2]), ("all", allitems)):
            row = {}
            for lang in sorted({i["lang"] for i in items}):
                xs = [i for i in items if i["lang"] == lang]
                if not all((got.get(i["id"]) or {}).get("text") for i in xs):
                    row = None
                    break
                hyp = [" ".join(got[i["id"]]["text"].split()) for i in xs]
                ref = [" ".join(i["ref"].split()) for i in xs]
                row[lang] = {"n": len(xs), "chrF": round(sacrebleu.corpus_chrf(hyp, [ref]).score, 1),
                             "BLEU": round(sacrebleu.corpus_bleu(hyp, [ref]).score, 1),
                             "len": round(sum(map(len, hyp)) / sum(map(len, ref)), 2)}
            if row is None:
                continue
            ids = {i["id"] for i in items}
            s = [g["s"] for k, g in got.items() if k in ids and g.get("s")]
            tok = sum((g.get("usage") or {}).get("completion_tokens") or 0 for k, g in got.items() if k in ids)
            row["s_median"] = round(statistics.median(s), 1)
            if tok:
                row["tok_s"] = round(tok / sum(s), 1)
            res.setdefault(setname, {}).setdefault(sub, {})[m] = row
            print(setname, sub, m, json.dumps(row))
(NEW / "reading_scores.json").write_text(json.dumps(res, indent=1))
