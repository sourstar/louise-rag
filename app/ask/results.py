"""`ask search` / `louise_search` (05 §5.1, §5.3, §5.4; 07 §7.3) — the
fused lists of the node's retrieval, one per space, each result carrying
what §5.3 says. Migration step 11, first brick.

The search itself is app/query_service.run_search — the legs, the fusion,
the reranker, the facts, the withholding — unchanged. This module gives
its answer §5.4's shape: `lists` grouped by `space_id` (a collection with
no Codex is its own space, named by its plugin), `facts` beside them,
`unresolved`, `withheld`, `legs_withheld`, `translations`; and each hit
§5.3's fields — the collection and lineage, the snapshot read, the uid,
the chunk and its layer, the rank and the legs that ranked it, the trust
level (app/share/levels.py), certification (null until step 10), the
verification state and producer, the text or — for a red collection —
the origin's page, the figures (null: no collection carries plates yet),
and whether the list is the reranker's order.

`filters` restricts both legs to the records the collections' facets admit
(05 §5.2; app/ask/filters.py), through `query_service.run_search`. `as_of`
answers from the held snapshot current at a date (05 §5.1; app/ask/asof.py):
records changed since are dropped and their shards named under
`withheld`, every result names the snapshot that answered, and the
response's `as_of` says which snapshot per collection and how many hits
were dropped. Not in this brick, refused rather than ignored: `freshness`
(the origin asked for the current version).
"""
from __future__ import annotations

import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.hold.codex import CodexError

LEGACY_SPACE = "legacy"
DEFAULT_LIMIT, MAX_LIMIT = 10, 100
DEFAULT_DEPTH, MAX_DEPTH = 50, 300


class AskError(CodexError):
    exit_code = 2


class NotYet(AskError):
    """A flag 05 §5.4 gives that this brick does not implement."""


def _root() -> Path:
    from app import collections as collections_mod
    return collections_mod.louise_root() / "collections"


def context_for(collection, *, root: Path | None = None, now: float | None = None,
                own_key: str | None = None) -> dict[str, Any]:
    """What every result of one collection shares: ids, the snapshot read,
    the trust level, the class."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return {"collection_id": None, "lineage_id": None, "snapshot_id": None,
                "space_id": f"{LEGACY_SPACE}:{collection.plugin}", "trust_level": "unverified",
                "certification": None, "red": False, "figures": None, "codex": None}
    from app import copy_state
    from app.hold import collection as col, equivalence, trust_file
    from app.hold.codex import lineage_id
    from app.share import coverage as cov, levels, seeder
    root = Path(root) if root is not None else _root()
    name = collection.name
    try:
        drag = col.drag_of(root, name)
        snap = seeder.current_snapshot(drag)
    except Exception:   # noqa: BLE001 — a drag that cannot be read: no snapshot named
        snap = None
    try:
        lid = lineage_id(codex, col.resolver(root))
    except Exception:   # noqa: BLE001
        lid = codex.collection_id
    bodies = copy_state.attestations(root, name, codex)
    rows: list[dict[str, Any]] = []
    if (root / name / "state" / "manifest.sqlite").is_file():
        try:
            from contextlib import closing
            with closing(cov.manifest(root, name)) as mcon:
                rows = cov.rows(mcon)
        except Exception:   # noqa: BLE001
            rows = []
    level = levels.level(codex, snap, bodies, rows, now=now, own_key=own_key,
                         trust=trust_file.as_filter(root.parent),
                         reference_images=equivalence.reference_images(bodies, own_key, codex))
    return {"collection_id": codex.collection_id, "lineage_id": lid,
            "snapshot_id": snap.snapshot_id if snap else None, "space_id": codex.space_id,
            "trust_level": level["level"], "certification": None,
            "red": codex.license.cls == "red",
            "figures": None if getattr(codex, "record_line", 1) == 1 else [], "codex": codex}


def _uid(collection, hit: dict[str, Any]) -> str:
    return f"{collection.plugin}:{hit.get('book_id')}"


def _layer(collection, hit: dict[str, Any]) -> str | None:
    try:
        from app import layers
        return layers.layer_of(collection, int(hit["book_id"])).chunker.id
    except Exception:   # noqa: BLE001 — a legacy collection or a document the rules do not place
        return None


def _origin_url(collection, hit: dict[str, Any]) -> str | None:
    try:
        from app import reader
        make = reader.ORIGIN.get(collection.plugin)
        return make({"native_id": hit.get("book_id"), "lang": hit.get("lang"),
                     "title": hit.get("title")}) if make else None
    except Exception:   # noqa: BLE001 — a missing field is not a lost result
        return None


def result_of(collection, ctx: dict[str, Any], hit: dict[str, Any], rank: int, *, own_key: str | None,
              reranked: bool) -> dict[str, Any]:
    """One hit as 05 §5.3 carries it."""
    legs = hit.get("leg_ranks") or {}
    verification = hit.get("verification") or "local"
    producer = hit.get("producer")
    out: dict[str, Any] = {
        "collection_id": ctx["collection_id"], "lineage_id": ctx["lineage_id"],
        "snapshot_id": ctx["snapshot_id"], "uid": _uid(collection, hit),
        "chunk_id": hit.get("chunk_id"), "layer": _layer(collection, hit), "rank": rank,
        "legs": {"text": legs.get("text"), "vector": legs.get("vector")},
        "trust_level": ctx["trust_level"], "certification": ctx["certification"],
        "verified_on_use": verification,
        "producer_key": own_key if producer in (None, "local") else producer,
        "title": hit.get("title"), "lang": hit.get("lang"),
        "figures": ctx["figures"], "reranked": reranked,
    }
    if ctx["red"]:
        out["origin_url"] = _origin_url(collection, hit) or hit.get("origin_url")
    else:
        out["text"] = hit.get("text")
    return out


def _plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def search(query: str, *, collections: list[str] | None = None, space: str | None = None,
           filters: dict[str, Any] | None = None, limit: int = DEFAULT_LIMIT,
           rerank_depth: int = DEFAULT_DEPTH, as_of: str | None = None, freshness: bool = False,
           translate: bool = False, all_languages: bool = False, root: Path | None = None,
           now: float | None = None, run=None) -> dict[str, Any]:
    """05 §5.4's `louise_search`: `{lists, facts, unresolved, withheld,
    legs_withheld, translations}`. `run` is query_service.run_search unless
    a test gives another."""
    from app import collections as collections_mod, identity, query_service
    from app.ask import asof as asof_mod
    if filters is not None and not isinstance(filters, dict):
        raise AskError("filters is an object {facet field: value | {not: value}} (05 §5.4)")
    day = asof_mod.parse_date(as_of) if as_of else None
    if freshness:
        raise NotYet("freshness: asking the origin for the current version is not built yet (05 §5.3)")
    if not isinstance(query, str) or not query.strip():
        raise AskError("a query is a non-empty string")
    limit = max(1, min(int(limit), MAX_LIMIT))
    rerank_depth = max(1, min(int(rerank_depth), MAX_DEPTH))
    now = time.time() if now is None else now
    try:
        own_key = identity.own()
    except Exception:   # noqa: BLE001 — no key: this peer's own vectors name no key
        own_key = None
    run = run or query_service.run_search
    named = query_service.resolve_collections(collections) if collections else query_service.resolve_collections(None)
    if space:
        named = [c for c in named if getattr(c, "codex", None) is not None and c.codex.space_id == space]
        if not named:
            raise AskError(f"no collection held here is in space {space}")
    # 05 §5.1 `as_of`: per collection, the held snapshot current at the date,
    # or the collection withheld whole and said so (app/ask/asof.py)
    dated: dict[str, Any] = {}
    withheld_as_of: list[dict[str, Any]] = []
    if day is not None:
        from app.hold import collection as col
        root_ = Path(root) if root is not None else _root()
        kept = []
        for c in named:
            codex = getattr(c, "codex", None)
            if codex is None:
                withheld_as_of.append({"collection": c.name, "collection_id": None, "shards": "all",
                                       "reason": f"as of {day.isoformat()}: a collection without a Codex has "
                                                 f"no dated snapshots"})
                continue
            r = asof_mod.resolve(codex, col.drag_of(root_, c.name), day)
            if r.snapshot is None:
                withheld_as_of.append(asof_mod.withheld_entry(c, codex, r, day))
                continue
            dated[c.name] = r
            kept.append(c)
        named = kept
    names = [c.name for c in named]
    if named:
        result = run(names, query, limit, "hybrid", translate, rerank_depth=rerank_depth,
                     all_languages=all_languages, filters=filters or None)
    else:
        # nothing to search — everything withheld, or no collection dated as
        # asked: said so, never a request for "no collections" one layer down
        result = {"hits": [], "withheld": query_service.withheld_from(collections)}
    by_name = {c.name: c for c in named}
    contexts = {n: context_for(c, root=root, now=now, own_key=own_key) for n, c in by_name.items()}
    for n, r in dated.items():
        contexts[n]["snapshot_id"] = r.snapshot.snapshot_id       # the snapshot that answered
    reranked = bool(result.get("reranked"))
    per_space: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    excluded: dict[str, int] = {}
    for c in named:
        sid = contexts[c.name]["space_id"]
        if sid not in order:
            order.append(sid)
    for hit in result.get("hits", []):
        cname = hit.get("source_collection") or (names[0] if len(names) == 1 else None)
        c = by_name.get(cname)
        if c is None:
            continue
        r = dated.get(c.name)
        if r is not None and _uid(c, hit) in r.changed:
            excluded[c.name] = excluded.get(c.name, 0) + 1   # changed since: never answered from a later state
            continue
        ctx = contexts[c.name]
        per_space.setdefault(ctx["space_id"], []).append(result_of(c, ctx, hit, 0, own_key=own_key,
                                                                    reranked=reranked))
    for n, r in dated.items():
        if r.shards or excluded.get(n):
            withheld_as_of.append(asof_mod.withheld_entry(by_name[n], by_name[n].codex, r, day))
    lists = []
    for sid in order:
        hits = per_space.get(sid, [])[:limit]
        for i, h in enumerate(hits, 1):
            h["rank"] = i
        lists.append({"space_id": sid, "question": "relevance", "hits": hits})
    facts = [_plain(f) for f in result.get("facts", [])]
    return {
        "lists": lists,
        "facts": facts,
        "unresolved": [str(u) for u in result.get("unresolved", [])],
        "withheld": list(result.get("withheld", [])) + withheld_as_of,
        "legs_withheld": result.get("legs_withheld", []),
        "translations": result.get("translations", {}),
        "facts_withheld": result.get("facts_withheld", []),
        "filters": filters or {},
        "filtered": result.get("filtered"),
        "as_of": ({"date": day.isoformat(), "snapshots": {n: r.snapshot.snapshot_id for n, r in dated.items()},
                   "excluded": excluded} if day is not None else None),
        "timings": result.get("timings", {}),
    }
