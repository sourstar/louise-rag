"""Retrieval orchestration.

Pipeline per query:
  0. resolve -> expand -> plan (architecture-v2.md §4, docs/05): regex/dictionary
     entity resolution against the reference layer (app/refs/). No entities found
     -> every step below is a structural no-op, degenerating exactly to the
     pre-P0.3 pipeline. Entities found -> two separate outputs, NEVER
     blended (CLAUDE.md invariant 4): structured lookups become PinnedFacts
     for the LLM context (never ranked, never in RRF); doc_keys-tagged hits
     get a soft RRF boost. The boost takes only the entities the query
     NAMES; the facts take those and docs/05's one hop, bounded per link,
     with what the bound left out named in `facts_withheld` (2026-09-21:
     one gene expanded to 21,968 variants).
  1. catalog search (the catalog index over title/author/subjects,
     app/catalog.py — subjects are English LoC headings for every
     language, so this is cross-lingual for free)
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
import contextvars
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import httpx
from . import (cancel, catalog, collections as collections_mod, config, copy_state, db,
               dispatch, embeddings, model_load,
               fts, httpc, language_plan, refs, work_dedup)
from .language_plan import LanguagePlan
from .refs.resolver import row_of
from .bundles import verify_on_use
from .hold import runs as hold_runs

RRF_K = 60
ENTITY_BOOST = 1.0 / RRF_K   # one extra "virtual rank-1 vote" for a tagged hit


@dataclass(frozen=True)
class PinnedFact:
    """Facts-by-key — never a ranked hit. Deliberately NOT a dict and NOT
    subscriptable: CLAUDE.md invariant 4 requires PinnedFact instances never
    enter _merge_rrf's `lists`/`merged`. _merge_rrf accesses h["chunk_id"]
    etc., so passing a PinnedFact into it raises TypeError immediately — the
    boundary is structurally enforced, not just conventional.

    `via`: the link a fact was reached through, when docs/05's expand reached
    it (§5.4) — `{"from": the named entity, "link": the declared link's name,
    "on": its join}` — and None when the query named the entity itself. A
    ClinVar variant pinned beside "BRCA2" otherwise reads as something the
    reader asked about."""
    entity_id: str
    kind: str
    source_id: str
    label: str | None
    fields: dict
    via: dict | None = None

def catalog_search(con, query: str, limit: int = 10,
                   langs: tuple[str, ...] | list[str] = (), collection=None) -> list[dict]:
    """`langs`, when given, keeps only documents written in those languages
    (a query's language restriction, app/language_plan.py).

    The collection's catalog index answers (app/catalog.py): top-k without
    scoring every match, 18–55 ms on pubmed's 40M records where the SQLite
    FTS5 catalog it replaced took 4–57 s. A collection whose index is not
    complete yet (its first backfill, or a rebuild after a version change)
    has an empty catalog leg until it is; the other legs still answer."""
    q = " ".join(t for t in query.split() if t.isalnum() or t.replace("'", "").isalnum())
    if not q:
        return []
    collection = collection or collections_mod.default_collection()
    try:
        return catalog.search(con, collection, q, limit, langs=langs) or []
    except Exception:   # noqa: BLE001 — an index problem empties the leg, never fails a search
        return []

def record_of(hit: dict) -> str:
    """The record a chunk hit belongs to: its uid, `<plugin>:<native_id>` —
    a chunk id is `<uid>:<seq>:<version>` (app/chunking.py), and a native
    id may itself hold a colon, so the split is from the right."""
    return str(hit["chunk_id"]).rsplit(":", 2)[0]


#: 05 §5.1's leg order, for the one tie it leaves open: a record ranked
#: equally by both legs carries the text leg's best chunk
_LEGS = ("text", "vector")


def _merge_rrf(lists: list[list[dict]], depth: int | None = None, *,
               own: frozenset[int] | set[int] = frozenset(),
               query_lang: str | None = None) -> list[dict]:
    """05 §5.1–5.2 (amended 2026-10-09: this is what the code now does).

    TWO LEGS, AND THE RECORD IS THE UNIT. `lists` are what the legs
    returned, chunk hits in rank order: one list per text query variant
    (a language group's translation, or one of a mixed group's), and the
    vector leg's (`source == "dense"`). A record's rank in one list is its
    position among that list's distinct records — the rank of its best
    chunk there. The text lists are variants of ONE leg (§5.2: "a variant
    is a query, not a leg"): the leg orders its records by their best rank
    across the variants, ties by uid bytes, and a record's rank in the leg
    is its POSITION in that one order — so the leg casts one vote per
    rank, however many languages it went out in (counting each variant's
    rank 1 as the leg's rank 1 would let sixteen records share it, which
    is the flood again: R@10 0.237 against 0.388 on the same legs). The
    vector leg is one list already. Each leg contributes its top `depth`
    records, and a record's score is the sum over the legs that ranked it
    of 1 / (60 + rank). Ordered by score, ties by uid bytes (one
    collection: `collection_id` is the same for every record).

    It used to fuse CHUNKS from one list per language group: a gutenberg
    query entered RRF as 16 text lists and one vector list, so each
    group's rank 1 — Hungarian's, Romanian's — scored like the vector
    leg's, and a book filled several slots, one per matching chunk.
    Measured over 451 queries (docs/notes/one-language-per-work.md): the
    work in the fused top 10 for 14.4 % of them that way, 39.7 % fused as
    the specification says.

    Each fused entry is the record's BEST CHUNK — the one that ranked
    highest in the leg that ranked the record highest (the text leg on a
    tie) — with `leg_ranks` (§5.3 `legs`), `sources` (every list that
    returned the record: `fts:<group>`, `dense`) and `rrf`.

    TIES IN THE TEXT LEG (amended 2026-10-09). A book is indexed in one
    language group only, so sixteen variants give sixteen different rank-1
    records, and their order decides positions 1–16. By uid bytes alone it
    was arbitrary, and title queries paid for it (R@10 0.821 → 0.615 on
    39). Equal best ranks now go: a rank reached by the USER'S OWN WORDS
    first — `own`, the indexes of the lists run with the query as typed —
    then a record written in `query_lang`, the language the query is in
    (`LanguagePlan.query_lang`), then uid bytes. Measured on the captured
    legs (tools/measure/one_language_rescore.py): titles R@10 0.744, MRR
    0.501 (uid alone: 0.615, 0.419); descriptive R@10 0.397, MRR 0.219
    (0.388, 0.187). Neither key boosts: both only order records the leg
    already ranks equal (§5.2)."""
    query_lang = (query_lang or "").lower() or None
    # uid -> leg -> (its tie key: rank first, its best chunk)
    best: dict[str, dict[str, tuple[tuple, dict]]] = {}
    sources: dict[str, set[str]] = {}
    # uid -> chunk id -> (its best position in any list, the hit): the
    # record's MATCHED chunks, which the reranker judges (05 §5.6)
    matched: dict[str, dict[str, tuple[int, dict]]] = {}
    for li, hits in enumerate(lists):
        seen: set[str] = set()
        for i, h in enumerate(hits):
            uid = record_of(h)
            mc = matched.setdefault(uid, {})
            if h["chunk_id"] not in mc or i < mc[h["chunk_id"]][0]:
                mc[h["chunk_id"]] = (i, h)
            if uid in seen:
                continue                  # a later chunk of a record this list already ranked
            seen.add(uid)
            rank = len(seen)
            leg = "vector" if h["source"] == "dense" else "text"
            key = (rank, li not in own,
                   query_lang is None or (h.get("lang") or "").lower() != query_lang)
            mine = best.setdefault(uid, {})
            if leg not in mine or key < mine[leg][0]:
                mine[leg] = (key, h)
            sources.setdefault(uid, set()).add(h["source"])
    scores: dict[str, float] = {}
    rank_in: dict[str, dict[str, int]] = {}    # uid -> leg -> its rank in that ONE leg
    for leg in _LEGS:
        ordered = sorted((legs[leg][0], uid) for uid, legs in best.items() if leg in legs)
        for rank, (_, uid) in enumerate(ordered if depth is None else ordered[:depth], 1):
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (RRF_K + rank)
            rank_in.setdefault(uid, {})[leg] = rank
    merged = []
    for uid, score in scores.items():
        ranks = rank_in[uid]
        top = min(ranks, key=lambda leg: (ranks[leg], _LEGS.index(leg)))
        entry = {**best[uid][top][1], "rrf": score, "sources": sorted(sources[uid]),
                 "leg_ranks": {leg: ranks[leg] for leg in _LEGS if leg in ranks}}
        entry[MATCHED] = _matched_chunks(best[uid][top][1], matched[uid])
        merged.append(entry)
    merged.sort(key=lambda m: (-m["rrf"], record_of(m)))
    return merged


#: 05 §5.6 (amended 2026-10-10): how many of a record's matched chunks the
#: reranker judges — its best chunk and the next best-placed — the record
#: taking the best score. Measured live (79 queries, docs/notes/
#: one-language-per-work.md): with the record's title over each, title
#: queries MRR 0.635 (one chunk) → 0.660 (two), descriptive 0.169 → 0.223;
#: three chunks scored no better (0.629, 0.238) and send ~25 % more text.
JUDGED_CHUNKS = 2
#: the fused entry's key holding those chunks; never leaves the search
MATCHED = "_matched"
#: the fused entry's key holding its record's title and authors, which the
#: reranker reads above each chunk (05 §5.6); never leaves the search
HEAD = "_head"
#: at most this many characters of title and authors above a chunk: a
#: 512-token reranker reads the head first, and a paper with forty authors
#: must not spend the chunk's tokens on names
HEAD_CHARS = 300


def _attach_heads(con, collection, hits: list[dict]) -> None:
    """Each hit's record title and authors as one line, `title — authors`,
    under `HEAD`: what the reranker reads above the chunk, so that a byline
    or a heading is judged as part of its book (05 §5.6, amended
    2026-10-10). One indexed manifest query over the candidates — the shape
    `_attach_metadata_and_enqueue` runs on the final list. A manifest that
    cannot answer leaves the chunks bare: the rerank goes on as before."""
    ids = sorted({str(h["book_id"]) for h in hits if h.get("book_id") is not None})
    if not ids or con is None:
        return
    marks = ",".join("?" for _ in ids)
    try:
        rows = con.execute(f"SELECT native_id,title,authors FROM documents "
                           f"WHERE source=? AND native_id IN ({marks})",
                           [collection.plugin] + ids).fetchall()
    except Exception:   # noqa: BLE001 — no heads is the old, bare rerank
        return
    line = {str(r["native_id"]): " — ".join(x for x in (r["title"], r["authors"]) if x)
            for r in rows}
    for h in hits:
        got = line.get(str(h.get("book_id")))
        if got:
            h[HEAD] = got[:HEAD_CHARS]


def _matched_chunks(best: dict, chunks: dict[str, tuple[int, dict]]) -> list[dict]:
    """The record's chunks the legs matched, its best chunk first, then by
    their best position in any list (ties by chunk id), `JUDGED_CHUNKS` of
    them."""
    rest = sorted((pos, cid) for cid, (pos, _) in chunks.items() if cid != best["chunk_id"])
    out = [best] + [chunks[cid][1] for _, cid in rest]
    return out[:JUDGED_CHUNKS]

#: 05 §5.6: the reranker re-orders the top 50 of ONE ranked list — the
#: default of a per-request `rerank_depth` (operator, 2026-09-18: "set the
#: maximum number of results a query returns before and after reranking,
#: with as default the recommended amount").
RERANK_TOP_N = 50
#: 05 §5.4: `limit`, the results per list AFTER the reranker — 10 by default
DEFAULT_RESULTS = 10
#: 05 §5.4 caps `limit` — the results per list — at 100
MAX_RESULTS = 100
#: 05 §5.4 caps the rerank depth at 300 (operator, 2026-09-21: "I cannot
#: increment the rerank from above 100"; then "up to 300 per leg"). It used to
#: share MAX_RESULTS, only because a leg was said to contribute its top 100
#: records — which the code never did: a leg goes to max(limit, depth, 20).
MAX_RERANK_DEPTH = 300


def _rerank_budget_s(depth: int = RERANK_TOP_N) -> float:
    """The node's `[ask] latency_budget_ms` (07 §7.8), in seconds, for a list
    reranked `depth` deep: the budget is set for the default depth of 50 and
    given to a deeper list IN PROPORTION — 300 candidates have six times as
    long (05 §5.6; operator, 2026-09-21: "scale the timeout proportionally").
    Never less than the budget itself: a shallower list keeps it, since a
    reranker's cost is not all per candidate and a squeezed budget would only
    turn more reranks into the fused order."""
    from . import platform_config
    try:
        base = platform_config.load().ask.latency_budget_ms / 1000.0
    except Exception:   # noqa: BLE001 — a broken platform.toml must not break search
        base = platform_config.AskConfig().latency_budget_ms / 1000.0
    return base * max(1.0, depth / RERANK_TOP_N)


def _rerank_list(query: str, hits: list[dict], recipe, budget_s: float,
                 depth: int = RERANK_TOP_N) -> tuple[list[dict], bool]:
    """05 §5.6 for ONE ranked list: the reranker re-orders its top `depth`
    (50 unless the request says otherwise) and the rest keep the fused
    order below them. Each candidate goes whole —
    its chunk's text; this used to cut every one to 2,000 characters,
    so a long chunk was judged on a prefix the model never saw alone. A
    512-token model's own server truncates at its limit (TEI's `truncate`);
    the platform does not. Unreachable, refusing, over the node's budget or
    answering for only some candidates: the fused order stands and the
    second value says False — the fallback is visible, never silent, and
    its reason is noted for `_reranked` (amended 2026-10-10: 4 of 980 live
    fallbacks said nothing).

    THE RECORD IS JUDGED, NOT ONE CHUNK OF IT (amended 2026-10-10, 05
    §5.6). Each record sends its `JUDGED_CHUNKS` best matched chunks
    (`_merge_rrf`'s `MATCHED`), each under the record's own title and
    authors (`HEAD`, `_attach_heads`), and takes the best score; the
    result carries the chunk that scored it. The reranker read only the
    leg's best chunk, bare, and a byline ("by William Shakespeare", 22
    characters) or a heading won it: measured live on 39 title queries,
    MRR 0.342 → 0.660 and R@10 0.718 → 0.872 (26 better, 0 worse), and
    on 40 descriptive ones MRR 0.121 → 0.223
    (docs/notes/one-language-per-work.md). The budget grows with the
    texts sent, as it grows with the depth.

    THE BUDGET IS THE RERANK'S, NOT THE MODEL'S LOAD (amended 2026-10-09,
    05 §5.6): a reranker its runner reports as not loaded is warmed first
    under its LOAD bound (app/model_load.py), and the budget starts after.
    A load past that bound is the fused order, its reason among
    the reasons `_reranked` collects ("model loading took longer than N s")."""
    if recipe is None or not hits:
        return hits, False
    head, tail = hits[:depth], hits[depth:]
    texts: list[str] = []
    owner: list[tuple[int, dict]] = []        # text index -> (its record's index, the chunk)
    for i, h in enumerate(head):
        title = h.get(HEAD) or ""
        for c in (h.get(MATCHED) or [h])[:JUDGED_CHUNKS]:
            body_text = c.get("text") or ""
            texts.append(f"{title}\n\n{body_text}" if title else body_text)
            owner.append((i, c))
    budget_s = budget_s * max(1.0, len(texts) / max(1, len(head)))
    if recipe.api == "jina":
        body = {"query": query, "documents": texts, "top_n": len(texts)}
    else:
        body = {"query": query, "texts": texts, "truncate": True}
    if recipe.model:
        # the file's `rerank_model` (07 §7.8, amended 2026-10-08): a runner
        # that picks the model from the body (LocalAI) is told which; only
        # RERANK_URL names none
        body["model"] = recipe.model
    try:
        _warm_reranker(recipe)
    except model_load.LoadTimeout as e:
        return _fused_order(hits, str(e))
    try:
        with dispatch.call(recipe.url, dispatch.Level.RERANK, timeout=budget_s):
            r = httpc.post(recipe.url, json=body, timeout=budget_s,
                           headers=dispatch.level_headers())
        r.raise_for_status()
        data = r.json()
        rows = data["results"] if isinstance(data, dict) else data
        scores = {int(o["index"]): float(o["relevance_score"] if "relevance_score" in o
                                         else o["score"]) for o in rows}
    except Exception as e:   # noqa: BLE001 — any failure is the fused order, said so
        return _fused_order(hits, rerank_failure(e, budget_s))
    if sorted(scores) != list(range(len(texts))):
        return _fused_order(hits, f"the reranker answered for "
                                  f"{sum(1 for i in scores if 0 <= i < len(texts))} of "
                                  f"{len(texts)} candidates")
    best: dict[int, tuple[float, dict]] = {}
    for t, (i, c) in enumerate(owner):
        if i not in best or scores[t] > best[i][0]:
            best[i] = (scores[t], c)
    ranked = []
    for i in sorted(best, key=lambda i: (-best[i][0], i)):
        score, c = best[i]
        h = dict(head[i])
        if c.get("chunk_id") != h.get("chunk_id"):
            # the chunk that scored the record is the one the result shows
            h.update({f: c[f] for f in ("chunk_id", "text", "chapter") if f in c})
        h["rerank_score"] = score
        ranked.append(h)
    return ranked + tail, True

#: Where `_rerank_list` says WHY a list kept its fused order, when it knows:
#: set by `_reranked` around one call (a context variable, so a test's
#: stand-in for `_rerank_list` keeps the arguments it always took).
_rerank_why: contextvars.ContextVar[list[str] | None] = \
    contextvars.ContextVar("louise_rerank_why", default=None)


def _fused_order(hits: list[dict], reason: str) -> tuple[list[dict], bool]:
    """The fused order, its reason noted where `_reranked` collects it."""
    why = _rerank_why.get()
    if why is not None:
        why.append(reason)
    return hits, False


def rerank_failure(e: BaseException, budget_s: float) -> str:
    """Why a rerank request failed, in a reader's words (05 §5.6: every
    fallback carries its reason — measured 2026-10-10, 4 of 980 live
    searches kept the fused order and none said why): over the budget,
    refused with a status, unreachable, or an answer that could not be
    read. Never the request's body or headers."""
    if isinstance(e, dispatch.GateTimeout):
        return f"the reranker was busy with other work for the whole {budget_s:g} s budget"
    if isinstance(e, httpx.TimeoutException) and not isinstance(e, httpx.ConnectTimeout):
        return f"the reranker did not answer within {budget_s:g} s"
    if isinstance(e, httpx.HTTPStatusError):
        return f"the reranker refused the request (HTTP {e.response.status_code})"
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "the reranker could not be reached"
    if isinstance(e, (KeyError, IndexError, TypeError, ValueError)):
        return f"the reranker's answer could not be read ({type(e).__name__})"
    return f"the reranker failed ({type(e).__name__})"


def _reranked(query: str, hits: list[dict], recipe, budget_s: float,
              depth: int) -> tuple[list[dict], bool, str | None]:
    """`_rerank_list`, and the reason its list kept the fused order when one
    is known (05 §5.6: the fallback is visible, never silent)."""
    why: list[str] = []
    token = _rerank_why.set(why)
    try:
        ranked, ok = _rerank_list(query, hits, recipe, budget_s, depth)
    finally:
        _rerank_why.reset(token)
    return ranked, ok, (why[0] if why and not ok else None)


def _rerank_withheld(collection, reason: str) -> dict:
    """05 §5.6's reason beside `reranked: false`, for one list."""
    return {"collection": collection.name, "reason": reason}


def _warm_reranker(recipe) -> None:
    """One minimal rerank, in the list's own body shape, when the runner
    says the reranker is not loaded — bounded by its load bound, outside the
    rerank budget (app/model_load.py)."""
    if not recipe.model:
        return
    if recipe.api == "jina":
        body: dict = {"query": "warm", "documents": ["warm"], "top_n": 1}
    else:
        body = {"query": "warm", "texts": ["warm"], "truncate": True}
    body["model"] = recipe.model

    def warm(bound_s: float) -> None:
        with dispatch.interactive(dispatch.Level.RERANK):
            httpc.post(recipe.url, json=body, timeout=bound_s,
                       headers=dispatch.level_headers())

    model_load.ensure_loaded(recipe.url, recipe.model, warm, caller="rerank")


#: docs/05's one hop, bounded: at most this many facts per declared link from
#: each entity the query names. A person reads pinned facts as cards above
#: the results; 25 is already more than a screen of them, and BRCA2 alone
#: reaches 21,961 ClinVar variants (2026-09-21). The rest are counted in the
#: answer's `facts_withheld`, and the traversal tool (docs/05 §5.4) is how
#: they are reached.
FACTS_PER_LINK = 25


def _entity_facts(query: str, store) -> tuple[list, list["PinnedFact"], list[dict]]:
    """resolve, then docs/05's expand — one hop along each declared link,
    bounded — then the facts. Returns the entities the query NAMES (the
    boost's, and only those: the 21,968 variants BRCA2 reaches tag not one
    PubMed document the gene does not), the pinned facts, and what each link
    reached beyond its bound: named in the answer, never dropped in silence
    (docs/05). A fact expand reached names the link (`PinnedFact.via`)."""
    resolved = refs.RESOLVER.resolve(query, store)
    reached = [(r, link) for r in resolved
               for link in refs.RESOLVER.expand_bounded(r, store, FACTS_PER_LINK)]
    facts = _build_pinned_facts(
        [(e, None) for e in resolved]
        + [(e, _via(r, link)) for r, link in reached for e in link.entities], store)
    withheld = [{"entity_id": r.entity_id, "table": link.table,
                 "reached": link.total, "pinned": len(link.entities)}
                for r, link in reached if link.total > len(link.entities)]
    return resolved, facts, withheld


def _article_facts(query: str, collections, facts, as_of) -> list[dict]:
    """The cited articles' texts, pinned by key beside the act (app/ask/
    articles.py; operator 2026-10-13: "art. 12 della legge … n. 2205" ranked
    other acts' articles above its own and never read it). Facts, never
    hits (invariant 4); never fatal to the search."""
    from .ask import articles
    try:
        return articles.pin(query, collections, facts, as_of=as_of)
    except Exception:                       # noqa: BLE001 — the search answers without it
        return []


def _via(named, reached) -> dict:
    """The link a fact was reached through (docs/05 §5.4), from the entity
    the query named."""
    link = reached.link
    return {"from": named.entity_id, "link": link.name,
            "on": f"{link.from_table}.{link.from_column} → {link.to_table}.{link.to_column}"}


def _build_pinned_facts(entities_found, store) -> list[PinnedFact]:
    """`entities_found`: (entity, via) pairs, the named ones first — an
    entity both named and reached is a fact once, and the query named it."""
    facts: list[PinnedFact] = []
    seen: set[str] = set()
    for e, via in entities_found:
        if e.entity_id in seen:
            continue
        seen.add(e.entity_id)
        row = row_of(store, e) or {}
        facts.append(PinnedFact(entity_id=e.entity_id, kind=e.kind, source_id=e.source_id,
                                 label=e.label, fields=row, via=via))
    return facts

def _tagged_among(con, collection, entity_ids: list[str], book_ids) -> set[int]:
    """Which of these CANDIDATES carry any of `entity_ids` — for the boost.

    The boost asks one thing: is this hit tagged? So the question is put for
    the candidates alone, after the merge, and its cost is theirs — a few
    hundred at most — never the entity's. It used to be the other way round:
    every document tagged with the entity, fetched before the search, to test
    a hundred hits against. On pubmed (40.0 M documents, 51.5 GB, 2026-09-21)
    BRCA2 tags 12,053 documents and took 2.5 s that way; the heaviest gene,
    HGNC:5172, tags 265,237.

    And before THAT fix it never finished at all. A manifest nobody has
    ANALYZEd has no statistics, so the planner takes any equality on an
    indexed column to be selective — and `d.source = ?` is not, every
    document of a collection has the same source. It drove the loop from all
    40 M documents; one search was still running after 75 minutes, its
    caller long gone. `CROSS JOIN` fixes the order here, and with BOTH
    columns of the unique (source, native_id) index constrained, each
    candidate is known to be at most one row whatever the statistics say.

    `str(book_id)` is how a candidate is found again: a book_id IS
    int(native_id), exact for every native id without leading zeros — which
    is every collection this node holds."""
    if not entity_ids or not book_ids:
        return set()
    ids = sorted({str(b) for b in book_ids})
    rows = con.execute(
        f"SELECT d.native_id FROM documents d CROSS JOIN doc_keys k ON k.uid = d.uid "
        f"WHERE d.source = ? AND d.native_id IN ({','.join('?' * len(ids))}) "
        f"AND k.entity_id IN ({','.join('?' * len(entity_ids))})",
        [collection.plugin, *ids, *entity_ids]).fetchall()
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
    variant is a query, not a leg, so three CJK variants cast one vote).
    The search no longer calls it (2026-10-09): every variant's list goes
    to `_merge_rrf`, which ranks a record by its best across all of them."""
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


#: 05 §5.1 (amended 2026-10-20): the source of the text leg's catalogue
#: variant — a record's title, creators and subjects (app/catalog.py)
CATALOG_SOURCE = "catalog"
#: the legacy prompt's "[catalog matches]" block reads at most this many
CATALOG_PROMPT = 10
#: the catalogue variant's chunk lookup asks one text query per language
#: group for all its records at once, this many chunk hits per record deep
CATALOG_CHUNKS_PER_RECORD = 8


def _seq_of(chunk_id: str) -> int:
    try:
        return int(str(chunk_id).rsplit(":", 2)[1])
    except (IndexError, ValueError):
        return 1 << 30


def _catalog_variant(collection, hits: list[dict], query: str, restrict: dict) -> list[dict]:
    """05 §5.1 (amended 2026-10-20; query campaign, docs/notes/
    query-campaign.md): the catalogue's ranked records as ONE MORE VARIANT
    of the text leg, in the catalogue's order, each carried by a chunk of
    its own — the one the query's words match best in that record, else
    its first. Until today the catalogue's hits reached only the legacy
    prompt: "War and Peace" missed the top 100 though 2600 is held and
    indexed, because a title reached a book only through its own text and
    anthologies quoting it won. A variant, not a leg (invariant 4: ranked
    lists answering one question are fused; §5.2: a variant is a query, not
    a leg) — it casts the text leg's one vote per rank. A versioned
    collection's window takes no fallback chunk: a record whose matching
    chunk is outside it is left to the other lists.

    ONE text query per language group for all its records (`book_ids`),
    not one per record: per record it cost pubmed 160–758 ms a query, fifty
    lookups over 40 M chunks (München, 2026-10-20). The hits come sorted as
    each per-record query sorted them (score, then chunk id), so a record's
    first hit is the chunk a query of its own would have returned; a record
    absent from a FULL answer is asked alone, one absent from a short one
    has no matching chunk."""
    groups = set(fts.existing_groups(collection))
    window = restrict.get("validity")
    kw = {"validity": window} if window else {}
    wanted: list[tuple[int, str]] = []
    by_group: dict[str, list[int]] = {}
    for h in hits:
        try:
            bid = int(h["book_id"])
        except (KeyError, TypeError, ValueError):
            continue
        group = fts.group_for(h.get("lang") or "")
        if group not in groups or bid in by_group.get(group, ()):
            continue
        wanted.append((bid, group))
        by_group.setdefault(group, []).append(bid)
    best: dict[tuple[int, str], dict | None] = {}
    for group, ids in by_group.items():
        limit = len(ids) * CATALOG_CHUNKS_PER_RECORD
        got = fts.search(group, query, limit=limit, collection=collection, book_ids=ids, **kw)
        for c in got:
            best.setdefault((int(c["book_id"]), group), c)
        if len(got) >= limit:
            # a full answer: a record crowded out by others' chunks is asked alone
            for bid in ids:
                if (bid, group) not in best:
                    one = fts.search(group, query, limit=1, collection=collection, book_ids=[bid], **kw)
                    if one:
                        best[(bid, group)] = one[0]
    out: list[dict] = []
    for bid, group in wanted:
        chunk = best.get((bid, group))
        if chunk is None and not window:
            chunks = fts.book_chunks(group, bid, collection=collection)
            chunk = min(chunks, key=lambda c: _seq_of(c["chunk_id"])) if chunks else None
        if chunk is not None:
            out.append({**chunk, "score": 0.0, "source": CATALOG_SOURCE})
    return out


def _search_one_collection(con, collection, query: str, k: int, mode: str, translate: bool,
                            entity_ids: list[str], plan: LanguagePlan | None = None,
                            depth: int = 0, book_ids: set[int] | None = None,
                            as_of: int | None = None) -> dict:
    """catalog + fts fan-out + dense + intra-collection RRF + entity boost —
    the per-collection core, extracted so search() (one collection) and
    federated_search() (N collections) share identical logic. `plan` is the
    search's language plan, made once for all its collections; without one
    this collection plans alone. Each leg goes at least `depth` deep, so a
    reranker asked for its top N has N candidates to order. Returns
    {"catalog", "merged", "translations", "timings", "scope"}."""
    timings: dict[str, float] = {}
    vector_leg: str | None = None      # why the vector leg was withheld, if it was
    scope = db.get_scope(con)
    if plan is None:
        plan = language_plan.plan(query, [collection], translate)
    core = plan.core

    # 05 §5.2: a facet filter RESTRICTS every leg to the records it admits —
    # a prefilter on both legs, never a boost; one admitting nothing returns
    # nothing, which is an answer
    if book_ids is not None and not book_ids:
        return {"catalog": [], "merged": [], "translations": plan.translations,
                "timings": timings, "scope": scope, "vector_leg": vector_leg, "filtered": 0}
    # the restriction reaches a leg only when there is one: an unfiltered
    # search calls the legs exactly as before
    restrict = {"book_ids": sorted(book_ids)} if book_ids else {}
    # 05 §5.1 (amended 2026-10-12), app/validity.py: a versioned collection's
    # legs see one version per Work — the latest text, or the version in
    # force as of `as_of` — by a column range; every other collection sees
    # no window at all
    from app import validity as validity_mod
    window = validity_mod.window(collection, as_of)
    if window:
        restrict = dict(restrict, validity=window)

    # A leg starts only while the caller is still there (app/cancel.py).
    cancel.checkpoint()
    t0 = time.time()
    # as deep as the text leg's other variants: it is one of them (§5.1)
    catalog_hits = (catalog_search(con, core, limit=max(k, depth, 20), langs=plan.only, collection=collection)
                    if mode in ("hybrid", "catalog") else [])
    if book_ids:
        catalog_hits = [h for h in catalog_hits if int(h.get("book_id", -1)) in book_ids]
    if window and catalog_hits:
        # the catalog index carries no validity columns: its hits are
        # admitted by the version table, bounded by the leg's own count
        allowed = validity_mod.admit(con, window, [int(h.get("book_id", -1)) for h in catalog_hits])
        catalog_hits = [h for h in catalog_hits if int(h.get("book_id", -1)) in allowed]
    timings["catalog"] = round((time.time() - t0) * 1000, 1)

    lists: list[list[dict]] = []
    own: set[int] = set()              # the lists run with the query as typed
    limit = max(k, depth, 20)
    if mode in ("hybrid", "fts"):
        t0 = time.time()
        # 05 §5.2 (amended 2026-10-10): the main languages, the query's own,
        # the mixed groups — the other one-language indexes get no variant
        main = plan.main_for(collection) if hasattr(plan, "main_for") else None
        for group in fts.existing_groups(collection):
            if not plan.searches(group):
                continue
            cancel.checkpoint()            # one group per language: a query can fan out wide
            held = fts.group_languages(group, collection)
            # every variant's list goes to the fusion as it is: they are
            # variants of the ONE text leg, and `_merge_rrf` ranks a record
            # by its best rank across all of them (05 §5.2)
            for q in (plan.variants(group, held, main) if main is not None
                      else plan.variants(group, held)):
                # deep enough to hold `limit` RECORDS, not chunks (05 §5.1,
                # amended 2026-10-10: app/leg_depth.py)
                hits = fts.search_records(group, q, limit, collection=collection,
                                          langs=plan.lang_filter(group), **restrict)
                if hits:
                    if q == core:
                        own.add(len(lists))      # the user's own words: first on a tie
                    lists.append(hits)
        if catalog_hits:
            # 05 §5.1 (amended 2026-10-20): the catalogue, one more variant
            # of the text leg, run with the reader's own words
            cancel.checkpoint()
            variant = _catalog_variant(collection, catalog_hits, core, restrict)
            if variant:
                own.add(len(lists))
                lists.append(variant)
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
        cancel.checkpoint()
        t0 = time.time()
        try:
            # 03 §3.3: a run that FAILED the gate withholds this leg, and the
            # text leg goes on — known before the encoder is even asked
            vector_leg = copy_state.vector_leg_withheld(collection)
            # A5, now at the level of a request (docs/notes/scheduling.md
            # §12): the claim stands corpus embedding aside on the whole
            # device for as long as this query embeds, and the worker
            # yields between CHUNKS — no longer between documents, which on
            # gutenberg measured up to 145 s.
            with dispatch.interactive(dispatch.Level.QUERY):
                # the restriction is a PREFILTER (measured 2026-09-18: +0.8
                # s at most, on wikipedia); a post-filter would return an
                # empty leg whenever the top k were in other languages
                if vector_leg is None:
                    lists.append(embeddings.search_records(core, limit, collection=collection,
                                                           langs=list(plan.only) or None,
                                                           **restrict))
        except (hold_runs.RunUnvalidated, hold_runs.RunRefused) as e:
            # the answer named a run not yet tested, or one that failed — or
            # VeGa's receipt did not fit it (embeddings.ReceiptRefused): its
            # vector is discarded, and the response says so (05 §5.3: never
            # a silent drop)
            vector_leg = str(e)
        except model_load.LoadTimeout as e:
            # the query encoder was cold and its load outlasted the bound
            # (app/model_load.py): the leg is missing, and the response
            # says why (05 §5.3) — the next search finds it loaded
            vector_leg = str(e)
        except embeddings.EndpointTimedOut as e:
            # the query encoder took the query and did not answer in time —
            # a hung backend (2026-10-09): the leg is missing, the text leg
            # goes on, and the response says why (05 §5.3: never a silent drop)
            vector_leg = f"the query encoder did not answer in time: {e}"
            db.log_event(con, "warn", "dense", f"dense search withheld: {e}")
        except embeddings.EndpointMisconfigured as e:
            # the query encoder's endpoint is the wrong one or refuses us
            # (404, 401, …): waiting will not bring the leg back, so the
            # response says why it is missing (05 §5.3: never a silent drop)
            vector_leg = f"the query encoder's endpoint refused the query: {e}"
            db.log_event(con, "warn", "dense", f"dense search withheld: {e}")
        except embeddings.ValidityColumnsMissing as e:
            # the table predates the collection's validity columns: the
            # text leg answers by validity, this one says why it cannot
            vector_leg = str(e)
            db.log_event(con, "warn", "dense", f"dense search withheld: {e}")
        except Exception as e:
            db.log_event(con, "warn", "dense", f"dense search failed: {e}")
        timings["dense"] = round((time.time() - t0) * 1000, 1)

    merged = _merge_rrf(lists, limit, own=own, query_lang=getattr(plan, "query_lang", None))
    # what the reranker will read above each candidate's chunks (05 §5.6)
    if collections_mod.reranker_for(collection) is not None:
        _attach_heads(con, collection, merged[:limit])
    tagged = _tagged_among(con, collection, entity_ids, [m["book_id"] for m in merged])
    if tagged:
        for m in merged:
            if m["book_id"] in tagged:
                m["rrf"] += ENTITY_BOOST
        merged.sort(key=lambda x: (-x["rrf"], record_of(x)))

    return {"catalog": catalog_hits[:CATALOG_PROMPT], "merged": merged, "translations": plan.translations,
            "timings": timings, "scope": scope, "vector_leg": vector_leg,
            "filtered": len(book_ids) if book_ids is not None else None}


def _leg_withheld(collection, reason: str) -> dict:
    """05 §5.3, §5.4's `legs_withheld` entry: the copy is SEARCHED — its text leg
    goes on — and one leg of it is not, with the reason (03 §3.3: a run that
    failed the gate, or an answer from a run not yet tested). Beside
    `withheld`, which names copies left out whole."""
    entry = {"collection": collection.name, "leg": "vector", "reason": reason}
    codex = getattr(collection, "codex", None)
    if codex is not None:
        entry["collection_id"] = codex.collection_id
    return entry


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
        h.pop(MATCHED, None)        # the search's own bookkeeping, never a field
        h.pop(HEAD, None)
    # Provenance badge per hit: locally embedded, or imported from a producer
    # and possibly verified (architecture-v2.md §6 T4). Replaces P0.6's
    # hardcoded 'local ✓' placeholder in the Ask provenance drawer.
    verify_on_use.annotate(con, hits)
    # 05 §5.3 / 03 §3.5 T4: the chunks this search cites are queued for the
    # worker to re-embed once, asynchronously — a search never waits on the
    # encoder, and a queue that cannot be written never costs the answer
    try:
        verify_on_use.cite(con, [h["chunk_id"] for h in hits if h.get("chunk_id")])
    except Exception:   # noqa: BLE001
        pass
    scope = db.get_scope(con)
    if scope["embed_mode"] == "lazy":
        _enqueue_lazy_embed(con, ids[:10], collection)


def _attach_work_fields(con, collection, hits: list[dict], query: str) -> list[dict]:
    """Give each hit what `work_dedup` needs, then drop the translations
    that compete with something better.

    NO LONGER ON THE SEARCH PATH (2026-10-09). Dropping an edition lost
    the work the reader wanted on 21 of 451 measured queries and found it
    on 2 (docs/notes/one-language-per-work.md); a search now groups a
    work's editions after the reranker instead (app/ask/works.py), from
    claims rather than the (author, LCSH) guess. Kept, with its tests,
    until the operator retires `work_dedup` and `documents.work`.

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


def _one_hit_per_work(hits: list[dict], collection, plan: LanguagePlan,
                      show_in: str | None, annotations=None) -> list[dict]:
    """05 §5.3 (amended 2026-10-09): each work once, after the reranker and
    before the cut to `k`, shown in the reader's language where one of its
    editions in it was found (app/ask/works.py). Grouping that cannot run
    returns the list as ranked. `annotations`: the annotations collections
    whose work claims group (None = every one held; the Ask page's choice,
    2026-10-19)."""
    from .ask import works
    return works.one_hit_per_work(hits, collection,
                                  works.preference(show_in, plan.query_lang),
                                  annotations=annotations)


def search(con, query: str, k: int = 12, mode: str = "hybrid",
           translate: bool = True, collection=None,
           rerank_depth: int = RERANK_TOP_N, all_languages: bool = False,
           book_ids: set[int] | None = None, show_in: str | None = None,
           as_of: int | None = None, annotations=None) -> dict:
    """mode: hybrid | fts | dense | catalog. Single collection — unchanged
    behavior/signature from pre-P0.5. See federated_search() for N&gt;1.
    `k` results after the reranker, which orders the top `rerank_depth`;
    a work's editions are one of them (`show_in`: the language the reader
    asked results in, the Ask page's "show results in")."""
    collection = collection or collections_mod.default_collection()
    t_total = time.time()

    # resolve -> expand -> plan (architecture-v2.md §4, docs/05). No entities
    # found (the common case) -> no facts and no entity ids, and nothing
    # below reads either until the boost block, which is then skipped — a
    # structural no-op, not a behavior that needs re-verifying per call.
    store = refs.get_store()
    resolved, facts, facts_withheld = _entity_facts(query, store)

    cancel.checkpoint()                    # the plan may call the translator
    t0 = time.time()
    plan = language_plan.plan(query, [collection], translate, every=all_languages)
    plan_ms = round((time.time() - t0) * 1000, 1)
    partial = _search_one_collection(con, collection, query, k, mode, translate,
                                     [e.entity_id for e in resolved], plan, rerank_depth,
                                     **({"book_ids": book_ids} if book_ids is not None else {}),
                                     **({"as_of": as_of} if as_of is not None else {}))
    timings = {"languages": plan_ms, **partial["timings"]}

    cancel.checkpoint()
    t0 = time.time()
    # Every record competes: the edition-dropping rule that ran here
    # (`_attach_work_fields`) lost the work the reader wanted on 21 of 451
    # queries and found it on 2 (docs/notes/one-language-per-work.md) —
    # editions are grouped after the reranker instead, never dropped.
    candidates = partial["merged"][: max(k * 4, rerank_depth)]
    ranked, reranked, rerank_why = _reranked(plan.core, candidates,
                                             collections_mod.reranker_for(collection),
                                             _rerank_budget_s(rerank_depth), rerank_depth)
    final = _one_hit_per_work(ranked, collection, plan, show_in, annotations)[:k]
    timings["rerank"] = round((time.time() - t0) * 1000, 1)

    # This one WRITES (the lazy-embed queue): a search nobody is waiting for
    # enqueues nothing.
    cancel.checkpoint()
    _attach_metadata_and_enqueue(con, collection, final)

    timings["total"] = round((time.time() - t_total) * 1000, 1)
    result = {"query": query, "mode": mode, "translations": partial["translations"],
              "language": plan.summary(), "query_lang": plan.query_lang,
              "query_lang_by": getattr(plan, "query_lang_by", ""),
              "main_languages": list(getattr(plan, "main", ())),
              "catalog": partial["catalog"], "hits": final,
              "facts": [asdict(f) for f in facts] + _article_facts(query, [collection], facts, as_of),
              "facts_withheld": facts_withheld,
              "timings": timings, "reranked": reranked}
    if partial.get("vector_leg"):
        result["legs_withheld"] = [_leg_withheld(collection, partial["vector_leg"])]
    if rerank_why:
        result["rerank_withheld"] = [_rerank_withheld(collection, rerank_why)]
    if partial.get("filtered") is not None:
        result["filtered"] = {collection.name: partial["filtered"]}
    return result


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
                      translate: bool = True, rerank_depth: int = RERANK_TOP_N,
                      all_languages: bool = False, book_ids_for=None,
                      show_in: str | None = None, as_of: int | None = None,
                      annotations=None) -> dict:
    """Query-time federation across N collections (architecture-v2.md §3/§4).
    Entity resolution/PinnedFacts happen ONCE, collection-agnostic — never
    multiplied per collection (CLAUDE.md invariant 4). Each collection's
    hybrid search runs in parallel (ThreadPoolExecutor, matching
    embeddings.py's _embed_texts()'s own parallelism idiom — this codebase is
    synchronous throughout, no asyncio); a collection whose connection or
    search fails is isolated (logged, contributes no candidates) rather than
    failing the whole query. With exactly one collection this degenerates to
    the same candidate order search() itself produces — see
    config.FEDERATION_CANDIDATE_BUDGET, which only applies when len(collections)>1."""
    t_total = time.time()

    # The same entity step as search(), through the same helper: the two paths
    # came in a pair, and fixing one left the other pinning 21,969 facts.
    store = refs.get_store()
    resolved, facts, facts_withheld = _entity_facts(query, store)
    entity_ids = [e.entity_id for e in resolved]

    # ONE language plan for the whole search: one model call translates the
    # query into the union of what every collection holds, where each
    # collection used to ask for its own — serialised anyway, since the
    # node's Ollama runs one request at a time (OLLAMA_NUM_PARALLEL=1).
    cancel.checkpoint()                    # the plan may call the translator
    t0 = time.time()
    plan = language_plan.plan(query, collections, translate, every=all_languages)
    plan_ms = round((time.time() - t0) * 1000, 1)

    def _run_one(collection):
        try:
            con = cancel.watch(db.connect(collection))
        except Exception as e:
            return collection, None, e
        try:
            admitted = book_ids_for(collection) if book_ids_for else None
            partial = _search_one_collection(con, collection, query, k, mode, translate,
                                              entity_ids, plan, rerank_depth,
                                              **({"book_ids": admitted} if admitted is not None else {}),
                                              **({"as_of": as_of} if as_of is not None else {}))
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

    # `carry`: a pool thread does not inherit the search's token, and without
    # it every per-collection leg would run on after its caller had gone.
    # Cancelled is a BaseException, so `_run_one`'s `except Exception` lets it
    # through rather than logging a cancelled search as a failed collection.
    with ThreadPoolExecutor(max_workers=max(1, len(collections))) as ex:
        outcomes = list(ex.map(cancel.carry(_run_one), collections))
    cancel.checkpoint()
    # ex.map preserves input order already, but sort explicitly on
    # collection.name for a deterministic tie-break in the federated fuse
    # below regardless of executor internals (invariant 3).
    outcomes.sort(key=lambda o: o[0].name)

    per_collection: list[tuple] = []      # (collection, its merged list)
    translations: dict[str, str] = {}
    timings_per_collection: dict[str, dict] = {}
    legs_withheld: list[dict] = []
    catalog: list[dict] = []
    filtered: dict[str, int] = {}         # 05 §5.2: records a filter admitted, per collection
    for collection, partial, err in outcomes:
        if partial is None:
            continue
        if partial.get("filtered") is not None:
            filtered[collection.name] = partial["filtered"]
        merged = partial["merged"]
        if len(collections) > 1:
            # never below the depth the request asked the reranker for: a
            # budget of 40 under a depth of 50 made "reranked from 50" a
            # claim about 40
            merged = merged[:max(config.FEDERATION_CANDIDATE_BUDGET, rerank_depth)]
        per_collection.append((collection, merged))
        translations.update(partial["translations"])
        timings_per_collection[collection.name] = partial["timings"]
        if partial.get("vector_leg"):
            legs_withheld.append(_leg_withheld(collection, partial["vector_leg"]))
        catalog.extend(partial["catalog"])
    collection_by_name = {c.name: c for c in collections}

    t0 = time.time()
    # 05 §5.6: each collection's list is reranked ON ITS OWN, by that
    # collection's reranker — two spaces are never put on one score scale —
    # and only then fused, by rank. This used to fuse first and rerank the
    # merged list once, with the node's one RERANK_URL: every collection
    # scored by the same model, on one scale. A work's editions are grouped
    # after it, per collection (work claims name one collection's records,
    # and `book_id` alone is ambiguous across them; 05 §5.3). The lists
    # rerank in parallel: one slow reranker costs the query its own time,
    # not the sum.
    budget_s = _rerank_budget_s(rerank_depth)

    def _rerank_one(item):
        collection, merged = item
        candidates = merged[: max(k * 4, rerank_depth)]
        ranked, ok, why = _reranked(plan.core, candidates,
                                    collections_mod.reranker_for(collection), budget_s,
                                    rerank_depth)
        # each work once, per collection (work claims are about one
        # collection's records), before the fuse cuts to k
        return _one_hit_per_work(ranked, collection, plan, show_in, annotations), ok, why

    with ThreadPoolExecutor(max_workers=max(1, len(per_collection))) as ex:
        ranked = list(ex.map(cancel.carry(_rerank_one), per_collection))
    reranked = {c.name: ok for (c, _), (_, ok, _) in zip(per_collection, ranked)}
    rerank_withheld = [_rerank_withheld(c, why)
                       for (c, _), (_, _, why) in zip(per_collection, ranked) if why]
    final = _merge_rrf_federated([hits for hits, _, _ in ranked])[:k]
    rerank_ms = round((time.time() - t0) * 1000, 1)

    # metadata attach + lazy-embed enqueue, grouped by source_collection
    # since bare book_id is ambiguous across collections
    by_collection: dict[str, list[dict]] = {}
    for h in final:
        by_collection.setdefault(h["source_collection"], []).append(h)
    # This WRITES (the lazy-embed queue): nothing is enqueued for a search
    # nobody is waiting for.
    cancel.checkpoint()
    for name, hits in by_collection.items():
        collection = collection_by_name[name]
        con = cancel.watch(db.connect(collection))
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

    result = {"query": query, "mode": mode, "translations": translations,
              "language": plan.summary(), "query_lang": plan.query_lang,
              "query_lang_by": getattr(plan, "query_lang_by", ""),
              "main_languages": list(getattr(plan, "main", ())),
              "catalog": catalog, "hits": final,
              "facts": [asdict(f) for f in facts] + _article_facts(query, collections, facts, as_of),
              "facts_withheld": facts_withheld,
              "timings": timings, "reranked": reranked}
    if legs_withheld:
        result["legs_withheld"] = legs_withheld
    if rerank_withheld:
        result["rerank_withheld"] = rerank_withheld
    if filtered:
        result["filtered"] = filtered
    return result

ANSWER_SYSTEM = (
    "You are a research assistant over a large multilingual library of "
    "public-domain books (Project Gutenberg). Answer the user's question using "
    "ONLY the numbered sources provided. Cite sources inline as [n]. Sources "
    "may be in a different language than the question: read them in their "
    "language, answer in the user's language. If the sources are insufficient, "
    "say so plainly. Blocks marked [reference] are authoritative structured "
    "facts, not ranked evidence — use them but do not cite them with [n]."
)

#: QWEN3'S OWN SWITCH, in the prompt — as `llm._RESTRICT_SYSTEM` carries it.
#: The node's chat model ignores `chat_template_kwargs.enable_thinking` and
#: thinks before it answers. Measured on "Diritto all'oblio" over
#: normattiva (2026-10-13, five runs): about 1,400 characters of hidden
#: reasoning per answer, the first visible word after 19–34 s, and the
#: reasoning — written in English — pulled the Italian question's answer
#: into English once in four runs. With the switch: no reasoning, the first
#: word after 0.4 s warm (9.8 s cold), the answer in the question's
#: language and whole. Nothing is lost: the reasoning was never shown.
THINK_OFF = " /no_think"

#: THE CITATIONS, ASKED LAST AND BY EXAMPLE. With the switch on, the model
#: stopped citing: "Diritto all'oblio" over normattiva, 2026-10-13, 0
#: markers in Italian and 1 without a language, where the thinking runs
#: had cited four sources — the reasoning was where it planned them, and
#: the page's [n] links went with it ("complete, but without the links",
#: operator). The system prompt's "cite as [n]" sits far from the end;
#: asked again after the question it is followed, and a reminder written
#: as "[n]" was copied literally ("[n4]", 0 markers in four runs) where
#: the example "[2] or [2][5]" gave 4–6 markers in each of three runs. The
#: page links `[digits]` only (ask.html linkCitations), so the example
#: shows that shape and nothing else inside the brackets.
CITE_REMINDER = ("After each statement, cite the source it rests on by its number in square "
                 "brackets, like [2] or [2][5]: the numbers of the sources above, nothing "
                 "else inside the brackets.")

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
        via = fact.get("via")
        reached = (f"\nreached from {via['from']} through {via['link']} ({via['on']})"
                   if via else "")
        blocks.append(f"[reference] {label} — {fact['entity_id']}{reached}\n{field_lines}")
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
    system += THINK_OFF
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Sources:\n\n{context}\n\nQuestion: {query}\n\n{CITE_REMINDER}"},
    ]
