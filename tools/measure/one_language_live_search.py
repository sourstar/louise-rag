"""MEASUREMENT (not product code): the live re-check of
docs/notes/one-language-per-work.md after 906f06b — every query through the
CURRENT search path as the Ask page calls it by default
(web._ask_result -> query_service.run_search -> retrieval.search: translate
on, all_languages off, show_in None = "results in their own language",
limit 10, rerank_depth 50), reranker and one hit per work included.

Same container and the same process-only changes as one_language_search.py
(read-only manifest, tantivy views, no T4 / lazy-embed / event writes, no
reference-source step), plus:

  * THE CHAT MODEL IS NEVER CALLED. `llm.language_restriction` would load
    louise-chat-qwen3-8b on the shared card, which LocalAI's watchdog evicts
    again within a minute (GPU 73 % > 68 %). Each query runs twice instead,
    on the SAME translations and vector leg (memoised per query):
      read   — the reading as if the chat model answered correctly:
               Reading([], query, <the query set's language label>);
      unread — the reading unavailable (what the path does when the chat
               model cannot answer): query_lang None.
    `llm.chat` raises if anything else reaches for it (the chat fallback of
    a failed MADLAD translation included), and that is recorded.
  * k = 50 with rerank_depth 50, so MRR reaches past 10; the Ask page's own
    top 10 (k = 10 sends the top 50 fused records to the reranker, the same
    50) is recomputed exactly from the reranked list: one hit per work over
    its first 50, cut to 10 (`ask10`).
  * work grouping errors are recorded, never swallowed silently; the
    annotations an API answer would attach (app/ask/annotations.attach, 05
    §5.3) are computed for the `read` top 10.

Usage (via one_language_run.sh, SCRIPT=one_language_live_search.py):
python one_language_live_search.py QUERIES.jsonl   (JSON lines to stdout)
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
from app import db, embeddings, fts, language_plan, llm, retrieval
from app.ask import works

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


def ro_connect(collection=None) -> sqlite3.Connection:
    collection = collection or collections_mod.default_collection()
    con = sqlite3.connect(f"file:{collection.manifest_path}?mode=ro", uri=True, timeout=30,
                          isolation_level=None, factory=db.ManifestConnection)
    con.collection_name = collection.name
    con.row_factory = sqlite3.Row
    return con


_cap: dict = {}
_memo: dict = {}
READING: dict = {"lang": None, "mode": "read"}


def _restriction(query: str):
    if READING["mode"] == "unread":
        return None
    return llm.Reading([], query, READING["lang"])


def _no_chat(*a, **kw):
    _cap.setdefault("chat_calls", 0)
    _cap["chat_calls"] += 1
    raise RuntimeError("chat model disabled in this measurement")


_orig_translations = language_plan._translations


def _translations(core, targets):
    key = ("tr", core, tuple(targets))
    if key not in _memo:
        _memo[key] = _orig_translations(core, targets)
    return _memo[key]


_orig_emb = embeddings.search


def _emb(query, k=20, langs=None, collection=None, book_ids=None):
    key = ("emb", query, k, tuple(langs or ()), tuple(book_ids or ()))
    if key not in _memo:
        _memo[key] = _orig_emb(query, k=k, langs=langs, collection=collection, book_ids=book_ids)
    return [dict(h) for h in _memo[key]]


_orig_merge = retrieval._merge_rrf


def _merge_capture(lists, depth=None, **kw):
    _cap["legs"] = [{"source": (l[0].get("source") if l else None),
                     "langs": sorted({h.get("lang") for h in l if h.get("lang")})[:5],
                     "hits": [[h["book_id"], h.get("chunk_id")] for h in l]} for l in lists]
    _cap["own"] = sorted(kw.get("own") or ())
    _cap["merge_query_lang"] = kw.get("query_lang")
    merged = _orig_merge(lists, depth, **kw)
    _cap["fused"] = [h["book_id"] for h in merged[:100]]
    return merged


_orig_reranked = retrieval._reranked


def _reranked_capture(query, hits, recipe, budget_s, depth):
    t0 = time.time()
    ranked, ok, why = _orig_reranked(query, hits, recipe, budget_s, depth)
    _cap["rerank_ms"] = round((time.time() - t0) * 1000, 1)
    _cap["rerank_ok"], _cap["rerank_why"] = ok, why
    _cap["rerank_query"] = query
    return ranked, ok, why


_orig_groups = works.work_groups


def _groups_capture(collection, uids, **kw):
    try:
        work_of, members = _orig_groups(collection, uids, **kw)
    except Exception as e:   # noqa: BLE001
        _cap["group_error"] = f"{type(e).__name__}: {e}"[:300]
        raise
    _cap["groups"] = len(members)
    return work_of, members


_orig_ohpw = retrieval._one_hit_per_work


def _ohpw_capture(hits, collection, plan, show_in):
    _cap["ranked"] = [[h["book_id"], h.get("lang"), h.get("rerank_score"), len(h.get("text") or "")] for h in hits[:60]]
    _cap["prefer"] = list(works.preference(show_in, plan.query_lang))
    out = _orig_ohpw(hits, collection, plan, show_in)
    # the Ask page's own top 10: k = 10 reranks the same first 50 records
    ask = _orig_ohpw([dict(h) for h in hits[:50]], collection, plan, show_in)[:10]
    _cap["ask10"] = [{"book_id": h["book_id"], "chunk_id": h.get("chunk_id"),
                      "editions": [[e["uid"], e["lang"], e["rank"]] for e in h.get("editions") or []]}
                     for h in ask]
    return out


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


def _log_event(con, level, stage, message, doc_uid=None):
    _cap.setdefault("events", []).append(f"{level}:{stage}: {message}"[:300])


db.connect = ro_connect
db.log_event = _log_event
fts.get_index = _view_index
llm.language_restriction = _restriction
llm.chat = _no_chat
language_plan._translations = _translations
embeddings.search = _emb
retrieval._merge_rrf = _merge_capture
retrieval._reranked = _reranked_capture
retrieval._one_hit_per_work = _ohpw_capture
retrieval._attach_metadata_and_enqueue = _meta_only
retrieval._entity_facts = lambda query, store: ([], [], [])
retrieval.refs.get_store = lambda: None
works.work_groups = _groups_capture


def _annotations(col, top):
    """What an API answer (05 §5.3) would attach beside each of the top 10."""
    from app.ask import annotations as facts
    from app.ask import results
    hs = [{"uid": retrieval.record_of(h)} for h in top]
    try:
        ctx = results.context_for(col)
        withheld = facts.attach([{"hits": hs}], {id(h): col for h in hs}, {col.name: ctx},
                                root=results._root())
    except Exception as e:   # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"per_hit": [len(h.get("annotations") or []) for h in hs],
            "types": sorted({a["type"] for h in hs for a in h.get("annotations") or []}),
            "withheld": withheld, "snapshot": ctx.get("snapshot_id")}


def _one(con, col, q, mode):
    READING["mode"], READING["lang"] = mode, q["lang"]
    for k in [k for k in _cap if k not in ("chat_calls",)]:
        _cap.pop(k)
    language_plan.reset()
    t0 = time.time()
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
    hits = [{"book_id": h["book_id"], "lang": h.get("lang"), "title": (h.get("title") or "")[:80],
             "legs": h.get("leg_ranks"), "sources": h.get("sources"), "tlen": len(h.get("text") or ""),
             "rerank": round(h["rerank_score"], 4) if h.get("rerank_score") is not None else None,
             "editions": [[e["uid"], e["lang"], e["rank"]] for e in h.get("editions") or []]}
            for h in res.get("hits", [])]
    return res, hits, err, round(time.time() - t0, 2)


def main(path: str) -> None:
    col = next(c for c in collections_mod.load_all(skip_unreadable=True)[0] if c.name == "gutenberg")
    held = collections_mod.load_annotations()
    usable = [a.name for a in works._usable(col, None)]
    sys.stderr.write(f"annotations held: {[a.name for a in held]}; usable about gutenberg: {usable}\n")
    con = ro_connect(col)
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        q = json.loads(line)
        _memo.clear()
        out = {"qid": q["qid"], "query": q["query"], "lang": q["lang"], "usable_annotations": usable}
        for attempt in range(3):
            _cap.clear()
            res, hits, err, secs = _one(con, col, q, "read")
            if err is None:
                break
            _memo.clear()
            time.sleep(30)
        out["attempts"] = attempt + 1
        out["read"] = {"hits": hits, "ask10": _cap.get("ask10"), "fused": _cap.get("fused"),
                       "ranked": _cap.get("ranked"), "prefer": _cap.get("prefer"),
                       "query_lang": res.get("query_lang"), "language": res.get("language"),
                       "merge_query_lang": _cap.get("merge_query_lang"), "own": _cap.get("own"),
                       "reranked": res.get("reranked"), "rerank_withheld": res.get("rerank_withheld"),
                       "rerank_ok": _cap.get("rerank_ok"), "rerank_why": _cap.get("rerank_why"),
                       "rerank_query": _cap.get("rerank_query"),
                       "legs_withheld": res.get("legs_withheld"), "groups": _cap.get("groups"),
                       "group_error": _cap.get("group_error"), "events": _cap.get("events"),
                       "chat_calls": _cap.get("chat_calls", 0), "timings": res.get("timings"),
                       "seconds": secs, "error": err}
        out["legs"] = _cap.get("legs")
        out["translations"] = res.get("translations") or {}
        out["annotations"] = _annotations(col, res.get("hits", [])[:10]) if err is None else None
        _cap.clear()
        res2, hits2, err2, secs2 = _one(con, col, q, "unread")
        out["unread"] = {"hits": hits2, "ask10": _cap.get("ask10"), "fused": _cap.get("fused"),
                         "query_lang": res2.get("query_lang"), "language": res2.get("language"),
                         "reranked": res2.get("reranked"), "rerank_withheld": res2.get("rerank_withheld"),
                         "rerank_why": _cap.get("rerank_why"), "groups": _cap.get("groups"),
                         "group_error": _cap.get("group_error"), "events": _cap.get("events"),
                         "chat_calls": _cap.get("chat_calls", 0), "timings": res2.get("timings"),
                         "seconds": secs2, "error": err2}
        out["error"] = err or err2
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1])
