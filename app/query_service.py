"""Collection-resolution + search-running helpers shared by the JSON API
(app/api.py) and the server-rendered Ask page (app/web.py) — one code path,
two front ends. Split out from api.py so web.py can reuse it without a
circular import (api.py mounts web.py's routers).
"""
from fastapi import HTTPException

from . import collections as collections_mod, db, retrieval


def resolve_collections(names: list[str] | None) -> list[collections_mod.Collection]:
    """None = all enabled (collections.load_all()) — P0.5's federation
    default. An explicit list is resolved by name; unknown names 400."""
    if names is None:
        return collections_mod.load_all()
    resolved, missing = [], []
    for n in names:
        c = collections_mod.by_name(n)
        (resolved if c else missing).append(c or n)
    if missing:
        raise HTTPException(400, f"unknown collection(s): {missing}")
    if not resolved:
        raise HTTPException(400, "collections must be non-empty when provided")
    return resolved


def run_search(collection_names: list[str] | None, query: str, k: int, mode: str,
               translate: bool, verify: bool = False) -> dict:
    """`verify` opts this query into blocking verify-on-use: every imported
    chunk among the results is recomputed locally before the answer is given
    (architecture-v2.md §6 T4). Off by default — it costs an embed call per
    unverified chunk, and verified state is permanent, so the hot set settles
    quickly and cold chunks never cost anything."""
    participants = resolve_collections(collection_names)
    if len(participants) == 1:
        con = db.connect(participants[0])
        result = retrieval.search(con, query, k=k, mode=mode, translate=translate,
                                   collection=participants[0])
        if verify:
            result["verification"] = _verify_hits(con, participants[0], result["hits"])
        return result
    result = retrieval.federated_search(participants, query, k=k, mode=mode,
                                         translate=translate)
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
