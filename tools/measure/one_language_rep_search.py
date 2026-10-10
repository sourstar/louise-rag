"""MEASUREMENT (not product code): the data behind the 2026-10-10 fixes of
docs/notes/one-language-per-work.md's "Live re-check" — how a record is
represented to the reranker, and how deep each leg must go to yield its
depth in RECORDS (05 §5.1).

Same throwaway container and the same process-only changes as
one_language_live_search.py (read-only manifest, tantivy views, nothing
written). The script reaches the node on STDIN (`python -`), its queries
embedded as `DATA` (one JSON object per line: qid, query, lang and the
translations the live re-check captured), so nothing is copied to the
node. NO MODEL IS LOADED: the translations are the captured ones (no
MADLAD, no chat model); the query encoder and the reranker are the
collection's own, both resident. With HEADED, every scored chunk is also
scored under its record's `title — authors` line (`scores_h`).

Per query:
  * every text variant of the plan (the captured translations, the plan's
    own `variants`), searched DEEP_T chunks deep, timed at 50 and DEEP_T;
  * the vector leg: the query embedded once, then searched with the same
    vector at k = 50, 100, 200, 400 and DEEP_V chunks, each timed;
  * the reranker over every candidate record's matched chunks (up to
    MAX_CHUNKS per record, best-ranked first, each whole), for the union of
    the top 50 records fused from 50-chunk lists (what 906f06b did) and
    from the deep lists cut at 50 records (05 §5.1).
Output: one JSON line per query, to stdout.
"""
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import numpy as np
import tantivy

from app import collections as collections_mod
from app import db, dispatch, embeddings, fts, httpc, retrieval, vector_index
from app.language_plan import LanguagePlan

DEEP_T = 300
DEEP_V = 800
VEC_KS = (50, 100, 200, 400, DEEP_V)
DEPTH = 50
MAX_CHUNKS = int(os.environ.get("MAX_CHUNKS", "3"))
HEADED = True
BATCH = 50

VIEW = Path(os.environ.get("FTS_VIEW", "/fts"))
_view_lock = threading.Lock()
_view_meta: dict = {}
_view_idx: dict = {}


def _view_index(group, collection=None):
    real = collection.fts_dir / group
    stamp = (real / "meta.json").stat().st_mtime_ns
    key = (str(real), group)
    with _view_lock:
        if _view_meta.get(key) == stamp and key in _view_idx:
            return _view_idx[key]
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
        idx = tantivy.Index(fts._schema(group), path=str(view))
        _view_idx[key], _view_meta[key] = idx, stamp
        return idx


def ro_connect(collection=None):
    collection = collection or collections_mod.default_collection()
    con = sqlite3.connect(f"file:{collection.manifest_path}?mode=ro", uri=True, timeout=30,
                          isolation_level=None, factory=db.ManifestConnection)
    con.collection_name = collection.name
    con.row_factory = sqlite3.Row
    return con


db.connect = ro_connect
db.log_event = lambda *a, **k: None
fts.get_index = _view_index


def rerank(recipe, query, texts):
    scores, ms = [], []
    for i in range(0, len(texts), BATCH):
        part = texts[i:i + BATCH]
        if recipe.api == "jina":
            body = {"query": query, "documents": part, "top_n": len(part)}
        else:
            body = {"query": query, "texts": part, "truncate": True}
        if recipe.model:
            body["model"] = recipe.model
        t0 = time.time()
        with dispatch.call(recipe.url, dispatch.Level.RERANK, timeout=120):
            r = httpc.post(recipe.url, json=body, timeout=120, headers=dispatch.level_headers())
        r.raise_for_status()
        data = r.json()
        rows = data["results"] if isinstance(data, dict) else data
        got = {int(o["index"]): float(o.get("relevance_score", o.get("score"))) for o in rows}
        if sorted(got) != list(range(len(part))):
            raise RuntimeError("partial rerank answer")
        scores += [got[j] for j in range(len(part))]
        ms.append(round((time.time() - t0) * 1000, 1))
    return scores, ms


def one(col, recipe_rr, q):
    plan = LanguagePlan(core=q["query"], translations=q["translations"], status="ok",
                        by="translation model", query_lang=q["lang"])
    lists, own, meta = [], set(), []
    text: dict[str, str] = {}
    for group in fts.existing_groups(col):
        held = fts.group_languages(group, col)
        for v in plan.variants(group, held):
            t0 = time.time()
            fts.search(group, v, limit=50, collection=col)
            ms50 = round((time.time() - t0) * 1000, 2)
            t0 = time.time()
            hits = fts.search(group, v, limit=DEEP_T, collection=col)
            msd = round((time.time() - t0) * 1000, 2)
            if hits:
                if v == plan.core:
                    own.add(len(lists))
                lists.append(hits)
                meta.append({"source": f"fts:{group}", "ms50": ms50, "ms_deep": msd})
    recipe = collections_mod.recipe_for(col)
    t0 = time.time()
    with dispatch.interactive(dispatch.Level.QUERY):
        vec = embeddings.embed_query(col, q["query"], recipe=recipe)
    embed_ms = round((time.time() - t0) * 1000, 1)
    tbl = embeddings._conn(col).open_table(embeddings.TABLE)
    qvec = np.asarray(vec, dtype=embeddings._np_dtype_of_table(tbl))
    vec_ms, dense = {}, []
    for k in VEC_KS:
        t0 = time.time()
        rows = vector_index.query(tbl.search(qvec), col).limit(k).to_list()
        vec_ms[k] = round((time.time() - t0) * 1000, 1)
        if k == DEEP_V:
            dense = [{"score": 1.0 - float(r.get("_distance", 0.0)) / 2.0, "book_id": int(r["book_id"]),
                      "chunk_id": r["id"], "chapter": r["chapter"], "lang": r["lang"], "text": r["text"],
                      "source": "dense"} for r in rows]
        if k == 50:
            dense50 = [r["id"] for r in rows]
    lists.append(dense)
    meta.append({"source": "dense"})
    for hits in lists:
        for h in hits:
            text.setdefault(h["chunk_id"], h.get("text") or "")
    # the two candidate sets
    shallow = retrieval._merge_rrf([l[:50] for l in lists[:-1]] + [dense[:50]], DEPTH,
                                   own=own, query_lang=q["lang"])[:DEPTH]
    deep = retrieval._merge_rrf(lists, DEPTH, own=own, query_lang=q["lang"])[:DEPTH]
    cands = list(dict.fromkeys([retrieval.record_of(h) for h in shallow + deep]))
    # each candidate's matched chunks, best chunk-level position first
    pos: dict[str, dict[str, int]] = {}
    for hits in lists:
        for i, h in enumerate(hits):
            u = retrieval.record_of(h)
            d = pos.setdefault(u, {})
            d[h["chunk_id"]] = min(d.get(h["chunk_id"], 10**9), i)
    want: list[str] = []
    for u in cands:
        chunks = sorted(pos.get(u, {}).items(), key=lambda kv: (kv[1], kv[0]))
        want += [c for c, _ in chunks[:MAX_CHUNKS]]
    for h in shallow + deep:
        want.append(h["chunk_id"])
    want = list(dict.fromkeys(want))
    t0 = time.time()
    scores, rr_ms = rerank(recipe_rr, plan.core, [text[c] for c in want])
    scores_h: list[float] = []
    if HEADED:
        # the same chunks, each under its record's title and authors
        books = sorted({int(c.split(":")[1]) for c in want})
        docs = {}
        con = ro_connect(col)
        marks = ",".join("?" for _ in books)
        for row in con.execute(f"SELECT native_id,title,authors FROM documents "
                               f"WHERE source=? AND native_id IN ({marks})",
                               [col.plugin] + [str(b) for b in books]):
            docs[int(row["native_id"])] = (row["title"] or "", row["authors"] or "")
        con.close()

        def head(c):
            t, a = docs.get(int(c.split(":")[1]), ("", ""))
            line = " — ".join(x for x in (t, a) if x)
            return f"{line}\n\n{text[c]}" if line else text[c]
        scores_h, rr_ms_h = rerank(recipe_rr, plan.core, [head(c) for c in want])
        rr_ms = rr_ms + rr_ms_h
    return {
        "qid": q["qid"], "lang": q["lang"],
        "legs": [{**m, "hits": [[h["book_id"], h["chunk_id"], len(h.get("text") or "")] for h in l]}
                 for m, l in zip(meta, lists)],
        "own": sorted(own), "dense50": dense50, "embed_ms": embed_ms, "vec_ms": vec_ms,
        "shallow": [h["chunk_id"] for h in shallow], "deep": [h["chunk_id"] for h in deep],
        "scores": dict(zip(want, [round(s, 4) for s in scores])),
        "scores_h": dict(zip(want, [round(s, 4) for s in scores_h])),
        "heads": {c: text[c][:80] for c in want if len(text[c]) < 300},
        "rerank_ms": rr_ms, "rerank_total_ms": round((time.time() - t0) * 1000, 1),
    }


def main():
    data = sys.stdin.read() if not DATA else DATA
    col = next(c for c in collections_mod.load_all(skip_unreadable=True)[0] if c.name == "gutenberg")
    recipe_rr = collections_mod.reranker_for(col)
    retrieval._warm_reranker(recipe_rr)
    for line in data.splitlines():
        if not line.startswith("{"):
            continue
        q = json.loads(line)
        for attempt in range(3):
            try:
                out = one(col, recipe_rr, q)
                break
            except Exception as e:   # noqa: BLE001
                out = {"qid": q["qid"], "error": f"{type(e).__name__}: {e}"[:300]}
                time.sleep(20)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


DATA = ""
if __name__ == "__main__":
    main()
