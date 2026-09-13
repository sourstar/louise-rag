"""Retrieval orchestration.

Pipeline per query:
  0. resolve -> expand -> plan (architecture-v2.md §4): regex/dictionary
     entity resolution against the reference layer (app/refs/). No entities
     found -> every step below is a structural no-op, degenerating exactly to
     the pre-P0.3 pipeline. Entities found -> two separate outputs, NEVER
     blended (CLAUDE.md invariant 4): structured lookups become PinnedFacts
     for the LLM context (never ranked, never in RRF); doc_keys-tagged hits
     get a soft RRF boost.
  1. catalog search (SQLite FTS5 over title/author/subjects — subjects are
     English LoC headings for every language, so this is cross-lingual for free)
  2. lexical fan-out: translate query per indexed language group, BM25 each
  3. dense search over whatever is embedded (cross-lingual natively)
  4. reciprocal-rank-fusion merge (+ entity boost), optional external rerank
  5. lazy-embed hook: top books not yet embedded get queued

P0.1 runs a single (Gutenberg) collection; source-qualified, multi-collection
federation is P0.5's job. Search hits keep their pre-refactor int `book_id`
shape (native_id) — chunk/document identity internally is `uid`-keyed, but the
public API surface is unchanged.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import httpx
from . import (collections as collections_mod, config, db, embeddings, fts, llm,
               refs, work_dedup)
from .bundles import verify_on_use

RRF_K = 60
ENTITY_BOOST = 1.0 / RRF_K   # one extra "virtual rank-1 vote" for a tagged hit


@dataclass(frozen=True)
class PinnedFact:
    """Facts-by-key — never a ranked hit. Deliberately NOT a dict and NOT
    subscriptable: CLAUDE.md invariant 4 requires PinnedFact instances never
    enter _merge_rrf's `lists`/`merged`. _merge_rrf accesses h["chunk_id"]
    etc., so passing a PinnedFact into it raises TypeError immediately — the
    boundary is structurally enforced, not just conventional."""
    entity_id: str
    kind: str
    source_id: str
    label: str | None
    fields: dict

def catalog_search(con, query: str, limit: int = 10) -> list[dict]:
    q = " ".join(t for t in query.split() if t.isalnum() or t.replace("'", "").isalnum())
    if not q:
        return []
    try:
        rows = con.execute(
            "SELECT d.native_id AS book_id, f.title, f.authors, f.subjects, "
            "bm25(catalog_fts) AS rank "
            "FROM catalog_fts f JOIN documents d ON d.uid = f.uid "
            "WHERE catalog_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit)).fetchall()
    except Exception:
        return []
    return [{"book_id": int(r["book_id"]), "title": r["title"], "authors": r["authors"],
             "subjects": (r["subjects"] or "")[:300]} for r in rows]

def _merge_rrf(lists: list[list[dict]]) -> list[dict]:
    scored: dict[str, dict] = {}
    for hits in lists:
        for rank, h in enumerate(hits):
            key = h["chunk_id"]
            entry = scored.setdefault(key, {**h, "rrf": 0.0, "sources": set()})
            entry["rrf"] += 1.0 / (RRF_K + rank + 1)
            entry["sources"].add(h["source"])
    merged = sorted(scored.values(), key=lambda x: -x["rrf"])
    for m in merged:
        m["sources"] = sorted(m["sources"])
    return merged

def _rerank(query: str, hits: list[dict], top_n: int) -> list[dict]:
    if not config.RERANK_URL or not hits:
        return hits[:top_n]
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(config.RERANK_URL, json={
                "query": query, "texts": [h["text"][:2000] for h in hits]})
            r.raise_for_status()
            order = r.json()  # TEI format: [{"index": i, "score": s}, ...]
            ranked = sorted(order, key=lambda o: -o["score"])
            out = []
            for o in ranked[:top_n]:
                h = dict(hits[o["index"]])
                h["rerank_score"] = float(o["score"])
                out.append(h)
            return out
    except Exception:
        return hits[:top_n]

def _build_pinned_facts(entities_found, store) -> list[PinnedFact]:
    facts: list[PinnedFact] = []
    seen: set[str] = set()
    for e in entities_found:
        if e.entity_id in seen:
            continue
        seen.add(e.entity_id)
        _, _, native = e.entity_id.partition(":")
        row = store.get(e.kind, native) or {}
        facts.append(PinnedFact(entity_id=e.entity_id, kind=e.kind, source_id=e.source_id,
                                 label=e.label, fields=row))
    return facts

def _tagged_book_ids(con, collection, entity_ids: list[str]) -> set[int]:
    if not entity_ids:
        return set()
    marks = ",".join("?" for _ in entity_ids)
    rows = con.execute(
        f"SELECT DISTINCT d.native_id FROM doc_keys k JOIN documents d ON d.uid=k.uid "
        f"WHERE k.entity_id IN ({marks}) AND d.source=?",
        entity_ids + [collection.plugin]).fetchall()
    return {int(r["native_id"]) for r in rows}

def _enqueue_lazy_embed(con, book_ids: list[int], collection):
    now = int(time.time())
    for bid in book_ids:
        uid = f"{collection.plugin}:{bid}"
        row = con.execute(
            "SELECT embed_done FROM documents WHERE uid=? AND in_scope=1", (uid,)).fetchone()
        if row and not row["embed_done"]:
            con.execute(
                "INSERT INTO embed_queue(uid,priority,ts) VALUES(?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET priority=100, ts=excluded.ts",
                (uid, 100, now))

def _search_one_collection(con, collection, query: str, k: int, mode: str, translate: bool,
                            tagged_book_ids: set[int]) -> dict:
    """catalog + fts fan-out + dense + intra-collection RRF + entity boost —
    the per-collection core, extracted so search() (one collection) and
    federated_search() (N collections) share identical logic. Returns
    {"catalog", "merged", "translations", "timings", "scope"}."""
    timings: dict[str, float] = {}
    scope = db.get_scope(con)

    t0 = time.time()
    catalog = catalog_search(con, query) if mode in ("hybrid", "catalog") else []
    timings["catalog"] = round((time.time() - t0) * 1000, 1)

    lists: list[list[dict]] = []
    translations: dict[str, str] = {}
    if mode in ("hybrid", "fts"):
        t0 = time.time()
        groups = fts.existing_groups(collection)
        if translate and len(groups) > 1:
            langs = [g for g in groups if g not in ("other", "cjk", "en")]
            translations = llm.translate_query(query, langs)
        for group in groups:
            q = translations.get(group, query)
            hits = fts.search(group, q, limit=max(k, 20), collection=collection)
            if hits:
                lists.append(hits)
        timings["fts"] = round((time.time() - t0) * 1000, 1)

    if mode in ("hybrid", "dense"):
        # F1-D6: this leg used to be gated on scope["embed_mode"] != "off",
        # which conflated "don't SPEND on embedding" with "don't USE
        # embeddings". A consumer-role node (§14: import-only) runs with
        # embed_mode=off precisely because its vectors arrive from the
        # network — and the old gate then refused to search the very vectors
        # it had imported. embed_mode governs the worker's spending (lazy
        # enqueue, backfill) and stays respected there; retrieval uses
        # whatever vectors exist. With no table, embeddings.search returns []
        # and an unreachable endpoint degrades via the except below.
        t0 = time.time()
        # A5: claim the encoder before using it. The worker reads this
        # between documents and stands its own embedding down, so a query
        # waits for at most the document already in flight instead of
        # queueing behind a whole backfill batch (db.claim_query_priority).
        db.claim_query_priority(con, getattr(collection, "name", "") or "")
        try:
            lists.append(embeddings.search(query, k=max(k, 20), collection=collection))
        except Exception as e:
            db.log_event(con, "warn", "dense", f"dense search failed: {e}")
        timings["dense"] = round((time.time() - t0) * 1000, 1)

    merged = _merge_rrf(lists)
    if tagged_book_ids:
        for m in merged:
            if m["book_id"] in tagged_book_ids:
                m["rrf"] += ENTITY_BOOST
        merged.sort(key=lambda x: -x["rrf"])

    return {"catalog": catalog, "merged": merged, "translations": translations,
            "timings": timings, "scope": scope}


def _attach_metadata_and_enqueue(con, collection, hits: list[dict]) -> None:
    """Attaches title/authors/lang to each hit and, in 'lazy' embed_mode,
    queues the hit documents not yet embedded. Shared by search() and
    federated_search()'s post-fuse step."""
    ids = list({h["book_id"] for h in hits})
    meta = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in con.execute(
                f"SELECT native_id,title,authors,lang FROM documents "
                f"WHERE source=? AND native_id IN ({marks})",
                [collection.plugin] + [str(i) for i in ids]):
            meta[int(r["native_id"])] = {"title": r["title"], "authors": r["authors"], "lang": r["lang"]}
    for h in hits:
        h.update(meta.get(h["book_id"], {}))
        h.pop("rrf", None)
    # Provenance badge per hit: locally embedded, or imported from a producer
    # and possibly verified (architecture-v2.md §6 T4). Replaces P0.6's
    # hardcoded 'local ✓' placeholder in the Ask provenance drawer.
    verify_on_use.annotate(con, hits)
    scope = db.get_scope(con)
    if scope["embed_mode"] == "lazy":
        _enqueue_lazy_embed(con, ids[:10], collection)


def _attach_work_fields(con, collection, hits: list[dict], query: str) -> list[dict]:
    """Give each hit what `work_dedup` needs, then drop the translations
    that compete with something better.

    BEFORE THE RERANK, deliberately. Dedup after the top-k cut would
    return a short page — twelve hits asked for, seven delivered, and the
    five slots spent on suppressed translations rather than refilled. Run
    here, the reranker draws its k from the survivors and the page is
    full of distinct works.

    Cost is one indexed lookup over the candidate set's ids (≤ 4k, so ≤
    48 by default) against the `(source, native_id)` key — the same query
    shape `_attach_metadata_and_enqueue` already runs on the final set.
    A hit whose document has no `work` row is passed through untouched: a
    guess about what a book IS must never cost a reader the book.
    """
    ids = list({h["book_id"] for h in hits})
    if not ids:
        return hits
    marks = ",".join("?" for _ in ids)
    works: dict[int, dict] = {}
    try:
        rows = con.execute(
            f"SELECT native_id, lang, work FROM documents "
            f"WHERE source=? AND native_id IN ({marks})",
            [collection.plugin] + [str(i) for i in ids]).fetchall()
    except Exception:   # noqa: BLE001 — A DEDUP THAT CANNOT RUN RETURNS EVERY
        return hits     # HIT, NEVER NONE. `work` is added by an ALTER in
        # `db.connect`, so a manifest opened some other way — an older
        # process still running mid-deploy, a hand-opened connection — has
        # no such column, and this is on the path of EVERY query. Losing
        # the dedup costs a reader some duplicate translations; raising
        # here costs them the search.
    for r in rows:
        try:
            parsed = json.loads(r["work"]) if r["work"] else None
        except (TypeError, ValueError):
            parsed = None           # unreadable is no key, never a broken page
        if parsed:
            works[int(r["native_id"])] = {
                "work_author": parsed.get("author", ""),
                "work_subjects": tuple(parsed.get("subjects") or ()),
                "translated": bool(parsed.get("translated")),
                "lang": r["lang"]}
    for h in hits:
        h.update(works.get(h["book_id"], {}))
    return work_dedup.prefer_originals(hits, query=query)


def search(con, query: str, k: int = 12, mode: str = "hybrid",
           translate: bool = True, collection=None) -> dict:
    """mode: hybrid | fts | dense | catalog. Single collection — unchanged
    behavior/signature from pre-P0.5. See federated_search() for N&gt;1."""
    collection = collection or collections_mod.default_collection()
    t_total = time.time()

    # resolve -> expand -> plan (architecture-v2.md §4). No entities found
    # (the common case) -> facts=[] and tagged_book_ids=set(), and nothing
    # below reads either until the boost block, which is then skipped —
    # a structural no-op, not a behavior that needs re-verifying per call.
    store = refs.get_store()
    resolved = refs.RESOLVER.resolve(query, store)
    expanded = [e for r in resolved for e in refs.RESOLVER.expand(r, store)]
    entities_found = resolved + expanded
    facts = _build_pinned_facts(entities_found, store)
    tagged_book_ids = _tagged_book_ids(con, collection, [e.entity_id for e in entities_found])

    partial = _search_one_collection(con, collection, query, k, mode, translate, tagged_book_ids)
    timings = partial["timings"]

    t0 = time.time()
    candidates = _attach_work_fields(
        con, collection, partial["merged"][: max(k * 4, 40)], query)
    final = _rerank(query, candidates, top_n=k)
    timings["rerank"] = round((time.time() - t0) * 1000, 1)

    _attach_metadata_and_enqueue(con, collection, final)

    timings["total"] = round((time.time() - t_total) * 1000, 1)
    return {"query": query, "mode": mode, "translations": partial["translations"],
            "catalog": partial["catalog"], "hits": final, "facts": [asdict(f) for f in facts],
            "timings": timings}


def _merge_rrf_federated(lists: list[list[dict]]) -> list[dict]:
    """Second RRF pass across collections' already-intra-collection-merged
    (+entity-boosted) candidate lists. Rank is each list's OWN position —
    raw per-collection score/rrf values are never compared across
    collections (architecture-v2.md's RRF-fuses-rankable-lists design, and
    this task's own "scores never compared across collections" contract).
    Each list belongs to exactly one collection (already deduped
    intra-collection by _merge_rrf), so no cross-list key collisions are
    possible — this is flat rank-to-score assignment, not an accumulating
    merge like _merge_rrf. dict(h) on a bare PinnedFact raises TypeError
    immediately (a frozen dataclass isn't a mapping), so this inherits the
    same invariant-4 type-safety guarantee as _merge_rrf."""
    scored = []
    for hits in lists:
        for rank, h in enumerate(hits):
            entry = dict(h)
            entry["rrf"] = 1.0 / (RRF_K + rank + 1)
            scored.append(entry)
    scored.sort(key=lambda x: -x["rrf"])
    return scored


def federated_search(collections: list, query: str, k: int = 12, mode: str = "hybrid",
                      translate: bool = True) -> dict:
    """Query-time federation across N collections (architecture-v2.md §3/§4).
    Entity resolution/PinnedFacts happen ONCE, collection-agnostic — never
    multiplied per collection (CLAUDE.md invariant 4). Each collection's
    hybrid search runs in parallel (ThreadPoolExecutor, matching
    embeddings.py's embed_texts()'s own parallelism idiom — this codebase is
    synchronous throughout, no asyncio); a collection whose connection or
    search fails is isolated (logged, contributes no candidates) rather than
    failing the whole query. With exactly one collection this degenerates to
    the same candidate order search() itself produces — see
    config.FEDERATION_CANDIDATE_BUDGET, which only applies when len(collections)>1."""
    t_total = time.time()

    store = refs.get_store()
    resolved = refs.RESOLVER.resolve(query, store)
    expanded = [e for r in resolved for e in refs.RESOLVER.expand(r, store)]
    entities_found = resolved + expanded
    facts = _build_pinned_facts(entities_found, store)
    entity_ids = [e.entity_id for e in entities_found]

    def _run_one(collection):
        try:
            con = db.connect(collection)
        except Exception as e:
            return collection, None, e
        try:
            tagged_book_ids = _tagged_book_ids(con, collection, entity_ids)
            partial = _search_one_collection(con, collection, query, k, mode, translate,
                                              tagged_book_ids)
            for h in partial["merged"]:
                h["source_collection"] = collection.name
            return collection, partial, None
        except Exception as e:
            try:
                db.log_event(con, "warn", "federation",
                             f"collection '{collection.name}' search failed: {e}")
            except Exception:
                pass
            return collection, None, e

    with ThreadPoolExecutor(max_workers=max(1, len(collections))) as ex:
        outcomes = list(ex.map(_run_one, collections))
    # ex.map preserves input order already, but sort explicitly on
    # collection.name for a deterministic tie-break in the federated fuse
    # below regardless of executor internals (invariant 3).
    outcomes.sort(key=lambda o: o[0].name)

    per_collection_lists: list[list[dict]] = []
    translations: dict[str, str] = {}
    timings_per_collection: dict[str, dict] = {}
    catalog: list[dict] = []
    for collection, partial, err in outcomes:
        if partial is None:
            continue
        merged = partial["merged"]
        if len(collections) > 1:
            merged = merged[:config.FEDERATION_CANDIDATE_BUDGET]
        per_collection_lists.append(merged)
        translations.update(partial["translations"])
        timings_per_collection[collection.name] = partial["timings"]
        catalog.extend(partial["catalog"])

    fused = _merge_rrf_federated(per_collection_lists)

    t0 = time.time()
    # Same as search(): dedup the CANDIDATES, so the reranker fills k from
    # the survivors. Grouped by collection because `book_id` alone is
    # ambiguous across them, and because `work` lives in each collection's
    # own manifest — a work is only ever deduplicated against itself.
    candidates = fused[: max(k * 4, 40)]
    collection_by_name = {c.name: c for c in collections}
    grouped: dict[str, list[dict]] = {}
    for h in candidates:
        grouped.setdefault(h["source_collection"], []).append(h)
    kept: set[int] = set()
    for name, hits in grouped.items():
        target = collection_by_name.get(name)
        if target is None:
            kept.update(id(h) for h in hits)
            continue
        try:
            kept.update(id(h) for h in _attach_work_fields(
                db.connect(target), target, hits, query))
        except Exception:   # noqa: BLE001 — a dedup that cannot run returns
            kept.update(id(h) for h in hits)        # every hit, never none
    candidates = [h for h in candidates if id(h) in kept]
    final = _rerank(query, candidates, top_n=k)
    rerank_ms = round((time.time() - t0) * 1000, 1)

    # metadata attach + lazy-embed enqueue, grouped by source_collection
    # since bare book_id is ambiguous across collections
    by_collection: dict[str, list[dict]] = {}
    for h in final:
        by_collection.setdefault(h["source_collection"], []).append(h)
    for name, hits in by_collection.items():
        collection = collection_by_name[name]
        con = db.connect(collection)
        _attach_metadata_and_enqueue(con, collection, hits)

    timings = {
        "catalog": max((t["catalog"] for t in timings_per_collection.values()), default=0.0),
        "fts": max((t.get("fts", 0.0) for t in timings_per_collection.values()), default=0.0),
        "dense": max((t.get("dense", 0.0) for t in timings_per_collection.values()), default=0.0),
        "rerank": rerank_ms,
        "per_collection": timings_per_collection,
    }
    timings["total"] = round((time.time() - t_total) * 1000, 1)

    return {"query": query, "mode": mode, "translations": translations,
            "catalog": catalog, "hits": final, "facts": [asdict(f) for f in facts],
            "timings": timings}

ANSWER_SYSTEM = (
    "You are a research assistant over a large multilingual library of "
    "public-domain books (Project Gutenberg). Answer the user's question using "
    "ONLY the numbered sources provided. Cite sources inline as [n]. Sources "
    "may be in a different language than the question: read them in their "
    "language, answer in the user's language. If the sources are insufficient, "
    "say so plainly. Blocks marked [reference] are authoritative structured "
    "facts, not ranked evidence — use them but do not cite them with [n]."
)

def build_answer_messages(query: str, result: dict, answer_lang: str | None = None) -> list[dict]:
    """`answer_lang` (an ISO code, STATUS 5j "show results in"): the model
    answers in that language directly — cheaper than translating the
    answer afterwards, and the [n] citations stay aligned."""
    blocks = []
    for fact in result.get("facts", []):
        label = fact.get("label") or fact["entity_id"]
        field_lines = "\n".join(f"{k}: {v}" for k, v in fact.get("fields", {}).items())
        blocks.append(f"[reference] {label} — {fact['entity_id']}\n{field_lines}")
    for i, h in enumerate(result["hits"], 1):
        # Source-qualified location (e.g. "gutenberg:1342 CHAPTER I",
        # "pubmed:38112345 Title") — chunk_id is uid-prefixed with the
        # source plugin (app/chunking.py), so this is recoverable even for
        # search()'s single-collection hits, not just federated_search()'s.
        source_id = h.get("chunk_id", "").split(":", 1)[0] or "?"
        loc = f"{source_id}:{h['book_id']}"
        if h.get("chapter"):
            loc += f" {h['chapter']}"
        head = f"[{i}] {loc} — {h.get('title','?')} — {h.get('authors','?')} ({h.get('lang','?')})"
        blocks.append(head + "\n" + h["text"][:1800])
    if result["catalog"]:
        cat = "\n".join(f"- {c['title']} — {c['authors']} (book #{c['book_id']})"
                        for c in result["catalog"][:8])
        blocks.append("[catalog matches]\n" + cat)
    context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
    system = ANSWER_SYSTEM
    if answer_lang:
        system += (f" Write your whole answer in the language with ISO code "
                   f"'{answer_lang}', whatever language the question or the "
                   f"sources are in.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Sources:\n\n{context}\n\nQuestion: {query}"},
    ]
