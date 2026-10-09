"""MEASUREMENT (not product code), step 3 of docs/notes/one-language-per-work.md:
run each query of the query set through gutenberg's REAL search path and keep
the ranked list WITHOUT the work dedup, so every collapsing strategy can be
applied to the same list afterwards.

What runs is `retrieval.search` as the Ask page calls it (query_service.run_search
-> retrieval.search, translate=True, mode hybrid, rerank_depth 50): the language
plan (restriction + MADLAD query translation), catalog, the text legs in every
main language, the vector leg (gutenberg's query encoder on RAGE), RRF, the
reranker. EVERY MOUNT IS READ-ONLY, so this process cannot write on the node;
what that needs is changed in THIS process only:

  * the manifest is opened `?mode=ro` (db.connect would run its idempotent
    ALTERs and schema script);
  * `work_dedup.prefer_originals` returns every hit (the baseline is "no
    collapsing"); what the real one WOULD have kept is recorded per query as
    `current_kept`, so today's rule is scored like the other strategies;
  * `_attach_metadata_and_enqueue` attaches title/lang only: no T4 citation
    queue, no lazy-embed queue;
  * the reference-source step (`_entity_facts`) is skipped: refs.db would be
    schema-ensured on open, and gutenberg's records carry no `keys`, so a
    resolved id restricts nothing there (05 §5.2) and the list is unchanged;
  * tantivy takes a lock file even to read, so each text index is read
    through a VIEW in the container's tmpfs: symlinks to the index's files,
    meta.json copied (a frozen snapshot, rebuilt when the live meta.json
    changes), the lock files created in the tmpfs;
  * the dispatch claim cannot be written (it never raises, by design): the
    worker is not asked to yield while a query embeds.

k = 50 with rerank_depth 50 gives exactly the default page's legs (50 deep)
and reranked top 50, so the default top 10 is this list's prefix.

Usage (on the node, via one_language_run.sh): python one_language_search.py QUERIES.jsonl
Results go to stdout as JSON lines, one query at a time, sequentially.
"""
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import tantivy

from app import collections as collections_mod
from app import db, fts, language_plan, retrieval, work_dedup

VIEW = Path(os.environ.get("FTS_VIEW", "/fts"))
_view_lock = threading.Lock()
_view_meta: dict = {}
_view_idx: dict = {}


def _view_index(group: str, collection=None) -> "tantivy.Index":
    real = collection.fts_dir / group
    meta = real / "meta.json"
    stamp = meta.stat().st_mtime_ns
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

_orig = work_dedup.prefer_originals
_cap: dict = {}


def _no_dedup(hits, query="", **kw):
    kept = _orig([dict(h) for h in hits], query=query, **kw)
    _cap["kept"] = sorted({h["book_id"] for h in kept})
    _cap["cands"] = sorted({h["book_id"] for h in hits})
    # the fused order BEFORE the reranker, as the candidates reach it
    _cap["fused"] = [[h["book_id"], h.get("chunk_id")] for h in hits[:100]]
    return list(hits)


_orig_merge = retrieval._merge_rrf


def _merge_capture(lists):
    """The legs exactly as they enter RRF: one list per text index group, one
    for the vector leg — kept so other fusions can be recomputed offline."""
    _cap["legs"] = [{"source": (l[0].get("source") if l else None),
                     "langs": sorted({h.get("lang") for h in l if h.get("lang")})[:5],
                     "hits": [[h["book_id"], h.get("chunk_id")] for h in l]} for l in lists]
    return _orig_merge(lists)


def _meta_only(con, collection, hits):
    ids = list({h["book_id"] for h in hits})
    meta = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in con.execute(f"SELECT native_id,title,lang FROM documents "
                             f"WHERE source=? AND native_id IN ({marks})",
                             [collection.plugin] + [str(i) for i in ids]):
            meta[int(r["native_id"])] = {"title": r["title"], "lang": r["lang"]}
    for h in hits:
        h.update(meta.get(h["book_id"], {}))
        h.pop("rrf", None)


def ro_connect(collection) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{collection.manifest_path}?mode=ro", uri=True, timeout=30,
                          isolation_level=None, factory=db.ManifestConnection)
    con.collection_name = collection.name
    con.row_factory = sqlite3.Row
    return con


work_dedup.prefer_originals = _no_dedup
retrieval._merge_rrf = _merge_capture
retrieval._attach_metadata_and_enqueue = _meta_only
retrieval._entity_facts = lambda query, store: ([], [], [])
retrieval.refs.get_store = lambda: None
db.connect = ro_connect
fts.get_index = _view_index


def main(path: str) -> None:
    col = next(c for c in collections_mod.load_all(skip_unreadable=True)[0] if c.name == "gutenberg")
    con = ro_connect(col)
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        q = json.loads(line)
        t0 = time.time()
        for attempt in range(3):
            # A LEG THAT FAILED IS NOT A RESULT. The search swallows a failed
            # encoder call (the vector leg is simply absent) and a failed
            # translation (every group gets the query as typed); the encoder
            # on the shared card failed intermittently during this run, so
            # such an answer is retried, and recorded as an error if it stays.
            _cap.clear()
            language_plan.reset()
            try:
                res = retrieval.search(con, q["query"], k=50, mode="hybrid", translate=True,
                                       collection=col, rerank_depth=50)
                err = None
                if not any(leg["source"] == "dense" for leg in _cap.get("legs") or []):
                    err = "vector leg missing"
                elif not res.get("translations"):
                    err = "no query translations"
            except Exception as e:   # noqa: BLE001
                res, err = {}, f"{type(e).__name__}: {e}"
            if err is None:
                break
            time.sleep(30)
        hits = [{"book_id": h["book_id"], "lang": h.get("lang"), "title": (h.get("title") or "")[:80],
                 "legs": h.get("leg_ranks"), "sources": h.get("sources")}
                for h in res.get("hits", [])]
        out = {"qid": q["qid"], "query": q["query"], "hits": hits,
               "current_kept": _cap.get("kept"), "candidates": _cap.get("cands"),
               "fused": _cap.get("fused"), "legs": _cap.get("legs"),
               "reranked": res.get("reranked"), "language": res.get("language"),
               "translations": res.get("translations") or {},
               "catalog": [c.get("book_id") for c in res.get("catalog", [])][:10],
               "legs_withheld": res.get("legs_withheld"), "rerank_withheld": res.get("rerank_withheld"),
               "timings": res.get("timings"), "seconds": round(time.time() - t0, 2), "attempts": attempt + 1, "error": err}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1])
