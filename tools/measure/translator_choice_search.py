"""MEASUREMENT (not product code): which translator for query translation
(docs/notes/translator-choice.md).

Text index only: no model, no encoder, no reranker. For every query of the
live re-check and every translator, the product's own plan decides what the
text leg runs: HEAD's `LanguagePlan.variants(group, held, main)` with that
translator's translations, the query's labelled language as `query_lang`
and gutenberg's main languages (05 §5.2, amended 2026-10-10: en, fr, fi,
de; CJK's three keep their translations). Each variant is searched to its first 50 RECORDS
(400 chunks at most), exactly as one_language_own_search.py did, so the
scorer can fuse them with the re-check's captured vector leg.

Same read-only container as one_language_own_search.py; reaches the node on
stdin (one_language_rep_run.sh) with its queries embedded as DATA
(translator_choice_build.py). Output: one line per (query, translator).
"""
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import tantivy

from app import collections as collections_mod
from app import config, db, fts

#: gutenberg's main languages at `[ask] translate_share` 0.90 (05 §5.2, amended
#: 2026-10-10; docs/notes/one-language-per-work.md "Implemented"). The node runs
#: bb5e403, which predates the rule, so the plan is HEAD's, copied below.
MAIN = ("en", "fr", "fi", "de")
MIXED = ("cjk", "other")


def variants(core, translations, query_lang, group, held, main):
    """`LanguagePlan.variants` at HEAD (ed47233), verbatim in effect."""
    if query_lang and group == query_lang and group not in MIXED:
        return [core]
    if main is not None and group not in MIXED and group not in main:
        return []
    if group in MIXED:
        pool = held or (sorted(config.CJK_LANGS) if group == "cjk" else [])
        langs = [c for c in pool if fts.group_for(c) == group]
    else:
        langs = [group]
    out = []
    for c in langs:
        t = translations.get(c)
        if t and t not in out:
            out.append(t)
    if group in MIXED and any(c not in translations for c in langs):
        if core not in out:
            out.append(core)
    return out or [core]


VIEW = Path(os.environ.get("FTS_VIEW", "/fts"))
_lock = threading.Lock()
_idx: dict = {}


def _view_index(group, collection=None):
    real = collection.fts_dir / group
    key = str(real)
    with _lock:
        if key in _idx:
            return _idx[key]
        view = VIEW / collection.name / group
        if view.exists():
            shutil.rmtree(view)
        view.mkdir(parents=True)
        for f in real.iterdir():
            if f.name.startswith(".tantivy-") and f.name.endswith(".lock"):
                continue
            if f.name == "meta.json":
                shutil.copyfile(f, view / f.name)
            else:
                os.symlink(f, view / f.name)
        _idx[key] = tantivy.Index(fts._schema(group), path=str(view))
        return _idx[key]


fts.get_index = _view_index
db.log_event = lambda *a, **k: None


def records(hits, depth=50):
    out = []
    for h in hits:
        if h["book_id"] not in out:
            out.append(h["book_id"])
            if len(out) >= depth:
                break
    return out


def main():
    col = next(c for c in collections_mod.load_all(skip_unreadable=True)[0] if c.name == "gutenberg")
    main_langs = MAIN
    groups = [(g, fts.group_languages(g, col)) for g in fts.existing_groups(col)]
    sys.stderr.write(f"main languages: {main_langs}; groups: {[g for g, _ in groups]}\n")
    for line in DATA.splitlines():
        if not line.startswith("{"):
            continue
        q = json.loads(line)
        cache: dict = {}
        for model, tr in q["tr"].items():
            lists = []
            for group, held in groups:
                for t in variants(q["query"], tr, q["lang"], group, held, main_langs):
                    if (group, t) not in cache:
                        hits = fts.search(group, t, limit=400, collection=col)
                        cache[(group, t)] = records(hits) if hits else []
                    if cache[(group, t)]:
                        lists.append({"group": group, "typed": t == q["query"],
                                      "recs": cache[(group, t)]})
            sys.stdout.write(json.dumps({"qid": q["qid"], "model": model, "main": main_langs,
                                         "lists": lists}) + "\n")
        sys.stdout.flush()


DATA = ""
if __name__ == "__main__":
    main()
