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
  2. lexical fan-out: the query in every language the indexes hold, BM25
     each group with its own (app/language_plan.py — once per search)
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
from . import (collections as collections_mod, config, db, embeddings, fts,
               language_plan, refs, work_dedup)
from .language_plan import LanguagePlan
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

def catalog_search(con, query: str, limit: int = 10,
                   langs: tuple[str, ...] | list[str] = ()) -> list[dict]:
    """`langs`, when given, keeps only documents written in those languages
    (a query's language restriction, app/language_plan.py)."""
    q = " ".join(t for t in query.split() if t.isalnum() or t.replace("'", "").isalnum())
    if not q:
        return []
    lang_clause = ""
    if langs:
        lang_clause = f" AND d.lang IN ({','.join('?' for _ in langs)})"
    try:
        rows = con.execute(
            "SELECT d.native_id AS book_id, f.title, f.authors, f.subjects, d.lang, "
            "bm25(catalog_fts) AS rank "
            "FROM catalog_fts f JOIN documents d ON d.uid = f.uid "
            f"WHERE catalog_fts MATCH ?{lang_clause} ORDER BY rank LIMIT ?",
            (q, *langs, limit)).fetchall()
    except Exception:
        return []
    return [{"book_id": int(r["book_id"]), "title": r["title"], "authors": r["authors"],
             "subjects": (r["subjects"] or "")[:300], "lang": r["lang"]} for r in rows]

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

#: 05 §5.6: the reranker re-orders the top 50 of ONE ranked list — the
#: default of a per-request `rerank_depth` (operator, 2026-09-18: "set the
#: maximum number of results a query returns before and after reranking,
#: with as default the recommended amount").
RERANK_TOP_N = 50
#: 05 §5.4: `limit`, the results per list AFTER the reranker — 10 by default
DEFAULT_RESULTS = 10
#: 05 §5.4 caps `limit` at 100, the depth of a leg; so is the rerank depth
MAX_RESULTS = 100


def _rerank_budget_s() -> float:
    """The node's `[ask] latency_budget_ms` (07 §7.8), in seconds."""
    from . import platform_config
    try:
        return platform_config.load().ask.latency_budget_ms / 1000.0
    except Exception:   # noqa: BLE001 — a broken platform.toml must not break search
        return platform_config.AskConfig().latency_budget_ms / 1000.0


def _rerank_list(query: str, hits: list[dict], recipe, budget_s: float,
                 depth: int = RERANK_TOP_N) -> tuple[list[dict], bool]:
    """05 §5.6 for ONE ranked list: the reranker re-orders its top `depth`
    (50 unless the request says otherwise) and the rest keep the fused
    order below them. Each candidate goes whole —
    its best chunk's text; this used to cut every one to 2,000 characters,
    so a long chunk was judged on a prefix the model never saw alone. A
    512-token model's own server truncates at its limit (TEI's `truncate`);
    the platform does not. Unreachable, refusing, over the node's budget or
    answering for only some candidates: the fused order stands and the
    second value says False — the fallback is visible, never silent."""
    if recipe is None or not hits:
        return hits, False
    head, tail = hits[:depth], hits[depth:]
    texts = [h.get("text") or "" for h in head]
    if recipe.api == "jina":
        body = {"query": query, "documents": texts, "top_n": len(texts)}
        if recipe.model:
            body["model"] = recipe.model
    else:
        body = {"query": query, "texts": texts, "truncate": True}
    try:
        r = httpx.post(recipe.url, json=body, timeout=budget_s)
        r.raise_for_status()
        data = r.json()
        rows = data["results"] if isinstance(data, dict) else data
        scores = {int(o["index"]): float(o["relevance_score"] if "relevance_score" in o
                                         else o["score"]) for o in rows}
    except Exception:   # noqa: BLE001 — any failure is the fused order, said so
        return hits, False
    if sorted(scores) != list(range(len(head))):
        return hits, False
    ranked = []
    for i in sorted(scores, key=lambda i: -scores[i]):
        h = dict(head[i])
        h["rerank_score"] = scores[i]
        ranked.append(h)
    return ranked + tail, True

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

def _best_rank(lists: list[list[dict]], limit: int) -> list[dict]:
    """Several variants of one query against one group, as ONE ranked list:
    a record's rank is its best across the variants (docs/05 §5.2 — a
    variant is a query, not a leg, so three CJK variants cast one vote)."""
    if len(lists) == 1:
        return lists[0]
    best: dict[str, tuple[int, dict]] = {}
    for hits in lists:
        for rank, h in enumerate(hits):
            seen = best.get(h["chunk_id"])
            if seen is None or rank < seen[0]:
                best[h["chunk_id"]] = (rank, h)
    ordered = sorted(best.values(), key=lambda rh: (rh[0], rh[1]["chunk_id"]))
    return [h for _, h in ordered][:limit]


def _search_one_collection(con, collection, query: str, k: int, mode: str, translate: bool,
                            tagged_book_ids: set[int], plan: LanguagePlan | None = None,
                            depth: int = 0) -> dict:
    """catalog + fts fan-out + dense + intra-collection RRF + entity boost —
    the per-collection core, extracted so search() (one collection) and
    federated_search() (N collections) share identical logic. `plan` is the
    search's language plan, made once for all its collections; without one
    this collection plans alone. Each leg goes at least `depth` deep, so a
    reranker asked for its top N has N candidates to order. Returns
    {"catalog", "merged", "translations", "timings", "scope"}."""
    timings: dict[str, float] = {}
    scope = db.get_scope(con)
    if plan is None:
        plan = language_plan.plan(query, [collection], translate)
    core = plan.core

    t0 = time.time()
    catalog = (catalog_search(con, core, langs=plan.only)
               if mode in ("hybrid", "catalog") else [])
    timings["catalog"] = round((time.time() - t0) * 1000, 1)

    lists: list[list[dict]] = []
    limit = max(k, depth, 20)
    if mode in ("hybrid", "fts"):
        t0 = time.time()
        for group in fts.existing_groups(collection):
            if not plan.searches(group):
                continue
            hits = _best_rank([fts.search(group, q, limit=limit, collection=collection,
                                          langs=plan.lang_filter(group))
                               for q in plan.variants(group)], limit)
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
            # the restriction is a PREFILTER (measured 2026-09-18: +0.8 s at
            # most, on wikipedia); a post-filter would return an empty leg
            # whenever the top k were in other languages
            lists.append(embeddings.search(core, k=limit, collection=collection,
                                           langs=list(plan.only) or None))
        except Exception as e:
            db.log_event(con, "warn", "dense", f"dense search failed: {e}")
        timings["dense"] = round((time.time() - t0) * 1000, 1)

    merged = _merge_rrf(lists)
    if tagged_book_ids:
        for m in merged:
            if m["book_id"] in tagged_book_ids:
                m["rrf"] += ENTITY_BOOST
        merged.sort(key=lambda x: -x["rrf"])

    return {"catalog": catalog, "merged": merged, "translations": plan.translations,
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
           translate: bool = True, collection=None,
           rerank_depth: int = RERANK_TOP_N) -> dict:
    """mode: hybrid | fts | dense | catalog. Single collection — unchanged
    behavior/signature from pre-P0.5. See federated_search() for N&gt;1.
    `k` results after the reranker, which orders the top `rerank_depth`."""
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

    t0 = time.time()
    plan = language_plan.plan(query, [collection], translate)
    plan_ms = round((time.time() - t0) * 1000, 1)
    partial = _search_one_collection(con, collection, query, k, mode, translate,
                                     tagged_book_ids, plan, rerank_depth)
    timings = {"languages": plan_ms, **partial["timings"]}

    t0 = time.time()
    candidates = _attach_work_fields(
        con, collection, partial["merged"][: max(k * 4, rerank_depth)], plan.core)
    ranked, reranked = _rerank_list(plan.core, candidates,
                                    collections_mod.reranker_for(collection),
                                    _rerank_budget_s(), rerank_depth)
    final = ranked[:k]
    timings["rerank"] = round((time.time() - t0) * 1000, 1)

    _attach_metadata_and_enqueue(con, collection, final)

    timings["total"] = round((time.time() - t_total) * 1000, 1)
    return {"query": query, "mode": mode, "translations": partial["translations"],
            "language": plan.summary(),
            "catalog": partial["catalog"], "hits": final, "facts": [asdict(f) for f in facts],
            "timings": timings, "reranked": reranked}


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
                      translate: bool = True, rerank_depth: int = RERANK_TOP_N) -> dict:
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

    # ONE language plan for the whole search: one model call translates the
    # query into the union of what every collection holds, where each
    # collection used to ask for its own — serialised anyway, since the
    # node's Ollama runs one request at a time (OLLAMA_NUM_PARALLEL=1).
    t0 = time.time()
    plan = language_plan.plan(query, collections, translate)
    plan_ms = round((time.time() - t0) * 1000, 1)

    def _run_one(collection):
        try:
            con = db.connect(collection)
        except Exception as e:
            return collection, None, e
        try:
            tagged_book_ids = _tagged_book_ids(con, collection, entity_ids)
            partial = _search_one_collection(con, collection, query, k, mode, translate,
                                              tagged_book_ids, plan, rerank_depth)
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

    per_collection: list[tuple] = []      # (collection, its merged list)
    translations: dict[str, str] = {}
    timings_per_collection: dict[str, dict] = {}
    catalog: list[dict] = []
    for collection, partial, err in outcomes:
        if partial is None:
            continue
        merged = partial["merged"]
        if len(collections) > 1:
            # never below the depth the request asked the reranker for: a
            # budget of 40 under a depth of 50 made "reranked from 50" a
            # claim about 40
            merged = merged[:max(config.FEDERATION_CANDIDATE_BUDGET, rerank_depth)]
        per_collection.append((collection, merged))
        translations.update(partial["translations"])
        timings_per_collection[collection.name] = partial["timings"]
        catalog.extend(partial["catalog"])
    collection_by_name = {c.name: c for c in collections}

    t0 = time.time()
    # 05 §5.6: each collection's list is reranked ON ITS OWN, by that
    # collection's reranker — two spaces are never put on one score scale —
    # and only then fused, by rank. This used to fuse first and rerank the
    # merged list once, with the node's one RERANK_URL: every collection
    # scored by the same model, on one scale. Work dedup runs first and per
    # collection (`work` lives in each collection's own manifest, and
    # `book_id` alone is ambiguous across them), so the reranker draws from
    # the survivors. The lists rerank in parallel: one slow reranker costs
    # the query its own time, not the sum.
    budget_s = _rerank_budget_s()

    def _rerank_one(item):
        collection, merged = item
        candidates = merged[: max(k * 4, rerank_depth)]
        try:
            candidates = _attach_work_fields(db.connect(collection), collection,
                                             candidates, plan.core)
        except Exception:   # noqa: BLE001 — a dedup that cannot run returns
            pass            # every hit, never none
        return _rerank_list(plan.core, candidates,
                            collections_mod.reranker_for(collection), budget_s,
                            rerank_depth)

    with ThreadPoolExecutor(max_workers=max(1, len(per_collection))) as ex:
        ranked = list(ex.map(_rerank_one, per_collection))
    reranked = {c.name: ok for (c, _), (_, ok) in zip(per_collection, ranked)}
    final = _merge_rrf_federated([hits for hits, _ in ranked])[:k]
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
        "languages": plan_ms,
        "catalog": max((t["catalog"] for t in timings_per_collection.values()), default=0.0),
        "fts": max((t.get("fts", 0.0) for t in timings_per_collection.values()), default=0.0),
        "dense": max((t.get("dense", 0.0) for t in timings_per_collection.values()), default=0.0),
        "rerank": rerank_ms,
        "per_collection": timings_per_collection,
    }
    timings["total"] = round((time.time() - t_total) * 1000, 1)

    return {"query": query, "mode": mode, "translations": translations,
            "language": plan.summary(),
            "catalog": catalog, "hits": final, "facts": [asdict(f) for f in facts],
            "timings": timings, "reranked": reranked}

ANSWER_SYSTEM = (
    "You are a research assistant over a large multilingual library of "
    "public-domain books (Project Gutenberg). Answer the user's question using "
    "ONLY the numbered sources provided. Cite sources inline as [n]. Sources "
    "may be in a different language than the question: read them in their "
    "language, answer in the user's language. If the sources are insufficient, "
    "say so plainly. Blocks marked [reference] are authoritative structured "
    "facts, not ranked evidence — use them but do not cite them with [n]."
)

#: Characters of passages an answer is given. The chat runs with a 16,384-
#: token context and Ollama silently drops the START of a prompt that does
#: not fit — the instructions first. The results count is the reader's to
#: set now (up to 100 x 1,800 characters), so the passages stop at this
#: budget: the default 10 (18,000) always fit, and a longer list gives the
#: answer its first passages and never a truncated prompt.
ANSWER_SOURCE_CHARS = 24_000


def build_answer_messages(query: str, result: dict, answer_lang: str | None = None) -> list[dict]:
    """`answer_lang` (an ISO code, STATUS 5j "show results in"): the model
    answers in that language directly — cheaper than translating the
    answer afterwards, and the [n] citations stay aligned."""
    blocks = []
    for fact in result.get("facts", []):
        label = fact.get("label") or fact["entity_id"]
        field_lines = "\n".join(f"{k}: {v}" for k, v in fact.get("fields", {}).items())
        blocks.append(f"[reference] {label} — {fact['entity_id']}\n{field_lines}")
    spent = 0
    for i, h in enumerate(result["hits"], 1):
        # numbered by position on the page, so [n] still opens hit n
        spent += len(h["text"][:1800])
        if i > 1 and spent > ANSWER_SOURCE_CHARS:
            break
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
