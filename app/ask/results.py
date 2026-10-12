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
    from app.hold import anchor as anchor_mod, collection as col, equivalence, trust_file
    from app.hold.codex import lineage_id
    from app.share import coverage as cov, levels, seeder
    root = Path(root) if root is not None else _root()
    name = collection.name
    try:
        shard_tree = col.shard_dir_of(root, name)
        snap = seeder.current_snapshot(shard_tree)
    except Exception:   # noqa: BLE001 — a SHARD that cannot be read: no snapshot named
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
            "trust_level": level["level"],
            # 05 §5.3: certification beside the trust level — two axes, never one
            "certification": anchor_mod.certification(root, name, codex, snap, now=now),
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
    if hit.get("editions"):
        # 05 §5.3 (amended 2026-10-09): the work's other editions, grouped
        # under its one hit (app/ask/works.py)
        out["editions"] = hit["editions"]
    return out


def _attach_validity(per_space, source_of, by_validity: set[str]) -> None:
    """05 §5.3 (amended 2026-10-12): a versioned collection's result says
    which version it is — when it came into force, when the next one did,
    the origin's force status — read from the version table, bounded by the
    hits (app/validity.py)."""
    from contextlib import closing
    from app import db, validity as validity_mod
    by_coll: dict[str, list[dict[str, Any]]] = {}
    for hits in per_space.values():
        for h in hits:
            c = source_of.get(id(h))
            if c is not None and c.name in by_validity:
                by_coll.setdefault(c.name, []).append(h)
    for name, hits in by_coll.items():
        c = source_of[id(hits[0])]
        ids = []
        for h in hits:
            try:
                ids.append(int(str(h["uid"]).split(":", 1)[1]))
            except (ValueError, IndexError):
                continue
        try:
            with closing(db.connect(c)) as con:
                got = validity_mod.describe(con, ids)
        except Exception:   # noqa: BLE001 — a fact that cannot be read is absent, never fatal
            got = {}
        for h in hits:
            try:
                h["validity"] = got.get(int(str(h["uid"]).split(":", 1)[1]))
            except (ValueError, IndexError):
                h["validity"] = None


def _plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def _place_figures(lists, source_of, contexts, root) -> None:
    """05 §5.3: the plates whose markers fall inside each returned chunk,
    for a collection whose records carry figures (`record_line` 2)."""
    from app.ask import figures as figures_mod
    from app.hold import collection as col
    from app.share import seeder
    by_coll: dict[str, list[dict[str, Any]]] = {}
    for lst in lists:
        for h in lst["hits"]:
            c = source_of.get(id(h))
            if c is not None and h.get("figures") is not None:
                by_coll.setdefault(c.name, []).append(h)
    for name, hits in by_coll.items():
        c = source_of[id(hits[0])]
        root_ = Path(root) if root is not None else _root()
        shard_tree = col.shard_dir_of(root_, name)
        try:
            snap = seeder.current_snapshot(shard_tree)
        except Exception:   # noqa: BLE001 — no snapshot: nothing to place from
            continue
        figures_mod.place(c, contexts[name]["codex"], shard_tree, snap, hits, red=contexts[name]["red"])


def _from_shard(c, hit) -> str | None:
    """The SHARD-relative shard a hit's record was read from: a fetched
    collection's locator is `<shard path>#<uid>` (app/plugins/shard.py)."""
    from contextlib import closing
    from app import db
    try:
        with closing(db.connect(c)) as con:
            row = con.execute("SELECT path FROM documents WHERE uid=?", (_uid(c, hit),)).fetchone()
    except Exception:   # noqa: BLE001 — a hit whose source cannot be read stays
        return None
    if not row or "#" not in str(row[0]):
        return None
    shard = Path(str(row[0]).rpartition("#")[0])
    try:
        return shard.relative_to(Path(c.corpus_dir)).as_posix()
    except ValueError:
        return None


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
    # 05 §5.1 (amended 2026-10-12), app/validity.py: a collection that
    # declares a validity axis answers a date BY VALIDITY — the version of
    # each Work in force on it, from the newest held snapshot — never by the
    # chain, which is when the origin's state was held
    from app import validity as validity_mod
    by_validity = {c.name for c in named if validity_mod.declared(c) is not None}
    if day is not None:
        from app.hold import collection as col
        root_ = Path(root) if root is not None else _root()
        kept = []
        for c in named:
            codex = getattr(c, "codex", None)
            if c.name in by_validity:
                kept.append(c)
                continue
            if codex is None:
                withheld_as_of.append({"collection": c.name, "collection_id": None, "shards": "all",
                                       "reason": f"as of {day.isoformat()}: a collection without a Codex has "
                                                 f"no dated snapshots"})
                continue
            r = asof_mod.resolve(codex, col.shard_dir_of(root_, c.name), day)
            if r.snapshot is None:
                withheld_as_of.append(asof_mod.withheld_entry(c, codex, r, day))
                continue
            dated[c.name] = r
            kept.append(c)
        named = kept
    names = [c.name for c in named]
    if named:
        result = run(names, query, limit, "hybrid", translate, rerank_depth=rerank_depth,
                     all_languages=all_languages, filters=filters or None,
                     **({"as_of": validity_mod.day_int(day)} if day is not None and by_validity else {}))
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
    # 03 §3.8: a certified manifest-only copy holding shards no signed
    # snapshot lists answers from the certified rest, and says so
    from app.hold import integrity
    bad: dict[str, set[str]] = {}
    for c in named:
        shard_tree = Path(c.corpus_dir) if getattr(c, "source", "origin") == "shard" else None
        if shard_tree is not None and integrity.applies(getattr(c, "codex", None)):
            found = integrity.violations(shard_tree.parent.parent, shard_tree.parent.name, c.codex)
            if found:
                bad[c.name] = set(found)
    dropped: dict[str, int] = {}
    source_of: dict[int, Any] = {}
    for c in named:
        sid = contexts[c.name]["space_id"]
        if sid not in order:
            order.append(sid)
    for hit in result.get("hits", []):
        cname = hit.get("source_collection") or (names[0] if len(names) == 1 else None)
        c = by_name.get(cname)
        if c is None:
            continue
        if c.name in bad and _from_shard(c, hit) in bad[c.name]:
            dropped[c.name] = dropped.get(c.name, 0) + 1
            continue
        r = dated.get(c.name)
        if r is not None and _uid(c, hit) in r.changed:
            excluded[c.name] = excluded.get(c.name, 0) + 1   # changed since: never answered from a later state
            continue
        ctx = contexts[c.name]
        r = result_of(c, ctx, hit, 0, own_key=own_key, reranked=reranked)
        source_of[id(r)] = c
        per_space.setdefault(ctx["space_id"], []).append(r)
    if by_validity:
        _attach_validity(per_space, source_of, by_validity)
    for n, r in dated.items():
        if r.shards or excluded.get(n):
            withheld_as_of.append(asof_mod.withheld_entry(by_name[n], by_name[n].codex, r, day))
    # 05 §5.3: named per collection as the shards withheld and why
    withheld_integrity = [{"collection": n, "collection_id": by_name[n].codex.collection_id, "shards": sorted(bad[n]),
                           "reason": f"integrity violated (03 §3.8): record shards no certifier-signed snapshot "
                                     f"lists; {dropped.get(n, 0)} result(s) from them left out, the certified "
                                     f"rest answered"} for n in sorted(bad)]
    lists = []
    for sid in order:
        hits = per_space.get(sid, [])[:limit]
        for i, h in enumerate(hits, 1):
            h["rank"] = i
        lists.append({"space_id": sid, "question": "relevance", "hits": hits})
    _place_figures(lists, source_of, contexts, root)
    # 05 §5.3 (amended 2026-10-09): the claims an annotations collection held
    # here makes about a result's record, placed beside it as facts — never
    # in a list's order (invariant 4; app/ask/annotations.py)
    from app.ask import annotations as annotation_facts
    try:
        withheld_annotations = annotation_facts.attach(
            lists, source_of, contexts, root=Path(root) if root is not None else _root())
    except Exception as e:   # noqa: BLE001 — a fact that cannot be placed is said, never fatal
        withheld_annotations = [{"collection": None, "collection_id": None, "shards": "all",
                                 "reason": f"annotations not attached: {type(e).__name__}: {e}"[:300]}]
    facts = [_plain(f) for f in result.get("facts", [])]
    fresh_summary: dict[str, Any] | None = None
    if freshness:
        # 05 §5.3: the top five of each list asked of their origins now,
        # one collection's results at a time; what is newer at the source
        # comes beside the lists as a fact, never into them
        from contextlib import closing
        from app import db
        from app.ask import freshness as fresh_mod
        fresh_summary = {"checked": 0, "states": {}}
        for lst in lists:
            top = lst["hits"][:fresh_mod.TOP]
            by_coll: dict[str, list[dict[str, Any]]] = {}
            for h in top:
                by_coll.setdefault(h["uid"].split(":", 1)[0], []).append(h)
            for plugin_name, hs in by_coll.items():
                c = next((cc for cc in named if cc.plugin == plugin_name), None)
                if c is None:
                    continue
                with closing(db.connect(c)) as fcon:
                    got = fresh_mod.check(c, hs, con=fcon, red=contexts[c.name]["red"])
                facts.extend(got["facts"])
                fresh_summary["checked"] += got["checked"]
                for k, v in got["states"].items():
                    fresh_summary["states"][k] = fresh_summary["states"].get(k, 0) + v
    out = {
        "lists": lists,
        "facts": facts,
        "unresolved": [str(u) for u in result.get("unresolved", [])],
        "withheld": (list(result.get("withheld", [])) + withheld_as_of + withheld_integrity
                     + withheld_annotations),
        "legs_withheld": result.get("legs_withheld", []),
        "translations": result.get("translations", {}),
        "facts_withheld": result.get("facts_withheld", []),
        "filters": filters or {},
        "filtered": result.get("filtered"),
        "as_of": ({"date": day.isoformat(), "snapshots": {n: r.snapshot.snapshot_id for n, r in dated.items()},
                   "excluded": excluded,
                   **({"by_validity": sorted(by_validity)} if by_validity else {})} if day is not None else None),
        "freshness": fresh_summary,
        "timings": result.get("timings", {}),
    }
    if result.get("rerank_withheld"):
        # 05 §5.6: why a list kept its fused order, when that is known (a
        # reranker whose load outlasted its bound, app/model_load.py)
        out["rerank_withheld"] = result["rerank_withheld"]
    return out
