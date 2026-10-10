"""MEASUREMENT (not product code): which text each language group is
searched with — the 2026-10-10 fix of MADLAD rewriting a query into its
OWN language (docs/notes/one-language-per-work.md, "Fixed (2026-10-10)").

Text index only: no model, no encoder, no reranker. For every query of the
live re-check and every text index group, the group is searched with each
variant today's plan gives it (the captured translations) AND with the
query as typed, each to its first 50 RECORDS (400 chunks at most, 05
§5.1). Output per list: the records in rank order. Same read-only
container as one_language_rep_search.py; reaches the node on stdin
(one_language_rep_run.sh) with its queries embedded as DATA
(one_language_rep_build.py).
"""
import json
import os
import shutil
import sqlite3
import sys
import threading
from pathlib import Path

import tantivy

from app import collections as collections_mod
from app import db, fts
from app.language_plan import LanguagePlan

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
    for line in DATA.splitlines():
        if not line.startswith("{"):
            continue
        q = json.loads(line)
        plan = LanguagePlan(core=q["query"], translations=q["translations"], status="ok")
        lists = []
        for group in fts.existing_groups(col):
            held = fts.group_languages(group, col)
            texts = list(dict.fromkeys(plan.variants(group, held) + [q["query"]]))
            for t in texts:
                hits = fts.search(group, t, limit=400, collection=col)
                if hits:
                    lists.append({"group": group, "typed": t == q["query"],
                                  "tr": t in plan.variants(group, held),
                                  "recs": records(hits), "langs": sorted({h["lang"] for h in hits})[:5]})
        sys.stdout.write(json.dumps({"qid": q["qid"], "lists": lists}) + "\n")
        sys.stdout.flush()


DATA = ""
if __name__ == "__main__":
    main()
