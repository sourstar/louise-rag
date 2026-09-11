"""Collection-resolution + search-running helpers shared by the JSON API
(app/api.py) and the server-rendered Ask page (app/web.py) — one code path,
two front ends. Split out from api.py so web.py can reuse it without a
circular import (api.py mounts web.py's routers).
"""
from fastapi import HTTPException

from . import collections as collections_mod, db, retrieval


def withheld_reason(collection) -> str | None:
    """Why this collection may not be SEARCHED, or None.

    Invariant 9 gates a yellow/red source "before its data is USED",
    however it arrived — and the gate covered acquisition, scan and
    archive but never search. A collection whose ack is missing was not
    discovered and not imported, yet anything already in its manifest
    answered queries. `app/licensing.py` is THE resolution; this only
    reads it.
    """
    from . import licensing
    try:
        if licensing.state_for(collection) == licensing.MISSING:
            return ("license not acknowledged: this source's terms require "
                    "an acknowledgement before its data is used (invariant 9)")
    except Exception:       # noqa: BLE001 — never fail a search on the gate's own error
        return None
    return None


def resolve_collections(names: list[str] | None) -> list[collections_mod.Collection]:
    """None = all enabled (collections.load_all()) — P0.5's federation
    default. An explicit list is resolved by name; unknown names 400.

    Withheld collections are dropped from the federate-all branch and
    REFUSED when named: silently returning nothing for a collection the
    caller asked for by name is a lie, and a silently narrower federation
    is "did work is not progress" one layer up. The caller learns which
    were dropped from `withheld_from(...)`.
    """
    if names is None:
        return [c for c in collections_mod.load_all()
                if withheld_reason(c) is None]
    resolved, missing = [], []
    for n in names:
        c = collections_mod.by_name(n)
        (resolved if c else missing).append(c or n)
    if missing:
        raise HTTPException(400, f"unknown collection(s): {missing}")
    if not resolved:
        raise HTTPException(400, "collections must be non-empty when provided")
    for c in resolved:
        reason = withheld_reason(c)
        if reason:
            raise HTTPException(403, f"{c.name}: {reason}")
    return resolved


def withheld_from(names: list[str] | None) -> list[dict]:
    """The collections a federate-all search left out, so the answer can
    say it is incomplete rather than quietly being so."""
    if names is not None:
        return []
    out = []
    for c in collections_mod.load_all():
        reason = withheld_reason(c)
        if reason:
            out.append({"collection": c.name, "reason": reason})
    return out


def run_search(collection_names: list[str] | None, query: str, k: int, mode: str,
               translate: bool, verify: bool = False) -> dict:
    """`verify` opts this query into blocking verify-on-use: every imported
    chunk among the results is recomputed locally before the answer is given
    (architecture-v2.md §6 T4). Off by default — it costs an embed call per
    unverified chunk, and verified state is permanent, so the hot set settles
    quickly and cold chunks never cost anything."""
    participants = resolve_collections(collection_names)
    withheld = withheld_from(collection_names)
    if not participants:
        # Everything was withheld. Saying so beats an empty result set that
        # reads as "nothing matched your query".
        return {"hits": [], "withheld": withheld}
    if len(participants) == 1:
        con = db.connect(participants[0])
        result = retrieval.search(con, query, k=k, mode=mode, translate=translate,
                                   collection=participants[0])
        if verify:
            result["verification"] = _verify_hits(con, participants[0], result["hits"])
        if withheld:
            result["withheld"] = withheld
        return result
    result = retrieval.federated_search(participants, query, k=k, mode=mode,
                                         translate=translate)
    if withheld:
        result["withheld"] = withheld
    if verify:
        summaries = []
        for participant in participants:
            hits = [h for h in result["hits"]
                    if h.get("source_collection") == participant.name]
            if hits:
                summaries.append(_verify_hits(db.connect(participant), participant, hits))
        result["verification"] = _merge_verification(summaries)
    return result


def _verify_hits(con, collection, hits: list[dict]) -> dict:
    from .bundles import verify_on_use
    chunk_ids = [h["chunk_id"] for h in hits if h.get("chunk_id")]
    summary = verify_on_use.verify_chunks(con, collection, chunk_ids)
    verify_on_use.annotate(con, hits)      # refresh badges with the new verdicts
    return summary


def _merge_verification(summaries: list[dict]) -> dict:
    merged = {"checked": 0, "verified": 0, "failed": 0, "quarantined": []}
    for s in summaries:
        for key in ("checked", "verified", "failed"):
            merged[key] += s.get(key, 0)
        merged["quarantined"].extend(s.get("quarantined", []))
    return merged


def source_qualified_ids(hits: list[dict]) -> list[str]:
    """book_id alone is ambiguous across collections under federation —
    chunk_id is already uid-prefixed with the source plugin (app/chunking.py)."""
    return [f"{h.get('chunk_id', '?').split(':', 1)[0]}:{h['book_id']}" for h in hits]
