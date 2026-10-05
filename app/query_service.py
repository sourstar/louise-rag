"""Collection-resolution + search-running helpers shared by the JSON API
(app/api.py) and the server-rendered Ask page (app/web.py) — one code path,
two front ends. Split out from api.py so web.py can reuse it without a
circular import (api.py mounts web.py's routers).
"""
from fastapi import HTTPException

from . import cancel, collections as collections_mod, db, retrieval


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
    except Exception as e:  # noqa: BLE001
        # FAIL CLOSED (audit, 2026-09-13). This returned None — searchable —
        # when the gate itself raised, so every search, chat and verify
        # opened on an error. A collection whose license cannot be judged
        # is withheld until it can.
        return (f"license state cannot be judged ({type(e).__name__}: {e}); "
                f"withheld until it can (invariant 9)")
    # 03 §3.6: a copy in quarantine — not yet tested on THIS peer — enters no
    # list, in either leg. Only once the node enforces it (copy_state.enforced:
    # the first deploy computes and shows the states and withholds nothing).
    from . import copy_state
    try:
        return copy_state.withholds(collection)
    except Exception as e:  # noqa: BLE001
        # a state that cannot be derived withholds nothing by accident: the
        # licence gate above has already failed closed on its own
        return None if not copy_state.enforced() else (
            f"quarantine: this copy's state cannot be derived "
            f"({type(e).__name__}: {e})")


def resolve_collections(names: list[str] | None, *,
                        private: bool = False) -> list[collections_mod.Collection]:
    """None = all enabled (collections.load_all()) — P0.5's federation
    default. An explicit list is resolved by name; unknown names 400.

    Withheld collections are dropped from the federate-all branch and
    REFUSED when named: silently returning nothing for a collection the
    caller asked for by name is a lie, and a silently narrower federation
    is "did work is not progress" one layer up. The caller learns which
    were dropped from `withheld_from(...)`.
    """
    # A deposit collection is invisible to an open route (07 §7.4): left out
    # of "all" without being named as withheld, and, when named, unknown —
    # the same answer as a name nothing holds, so the route does not even
    # say that it exists.
    visible = (lambda c: True) if private else \
        (lambda c: not collections_mod.is_private(c))
    if names is None:
        return [c for c in collections_mod.load_all()
                if visible(c) and withheld_reason(c) is None]
    resolved, missing = [], []
    for n in names:
        c = collections_mod.by_name(n)
        c = c if c is not None and visible(c) else None
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


def withheld_from(names: list[str] | None, *, private: bool = False) -> list[dict]:
    """The collections a federate-all search left out, so the answer can
    say it is incomplete rather than quietly being so."""
    if names is not None:
        return []
    out = []
    for c in collections_mod.load_all():
        if collections_mod.is_private(c) and not private:
            continue
        reason = withheld_reason(c)
        if reason:
            entry = {"collection": c.name, "reason": reason}
            codex = getattr(c, "codex", None)
            if codex is not None:
                # 05 §5.3's shape: a copy withheld whole names its id, and
                # `shards` is "all" rather than a list
                entry.update(collection_id=codex.collection_id, shards="all")
            out.append(entry)
    return out


def run_search(collection_names: list[str] | None, query: str, k: int, mode: str,
               translate: bool, verify: bool = False,
               rerank_depth: int = retrieval.RERANK_TOP_N,
               all_languages: bool = False, private: bool = False) -> dict:
    """`verify` opts this query into blocking verify-on-use: every imported
    chunk among the results is recomputed locally before the answer is given
    (architecture-v2.md §6 T4). Off by default — it costs an embed call per
    unverified chunk, and verified state is permanent, so the hot set settles
    quickly and cold chunks never cost anything."""
    participants = resolve_collections(collection_names, private=private)
    withheld = withheld_from(collection_names, private=private)
    if not participants:
        # Everything was withheld. Saying so beats an empty result set that
        # reads as "nothing matched your query". `timings` like every other
        # answer: every caller logs them, and this one raised KeyError in
        # /api/search — reached whenever everything was withheld, and on a
        # node whose only collections are deposit collections (07 §7.4).
        return {"hits": [], "withheld": withheld, "timings": {"total": 0}}
    if len(participants) == 1:
        # interrupted if the caller goes (app/cancel.py): a statement running
        # on it ends then, not at its next row
        con = cancel.watch(db.connect(participants[0]))
        result = retrieval.search(con, query, k=k, mode=mode, translate=translate,
                                   collection=participants[0], rerank_depth=rerank_depth,
                                   all_languages=all_languages)
        if verify:
            cancel.checkpoint()            # an embed call per unverified chunk
            result["verification"] = _verify_hits(con, participants[0], result["hits"])
        if withheld:
            result["withheld"] = withheld
        return result
    result = retrieval.federated_search(participants, query, k=k, mode=mode,
                                         translate=translate, rerank_depth=rerank_depth,
                                         all_languages=all_languages)
    if withheld:
        result["withheld"] = withheld
    if verify:
        summaries = []
        for participant in participants:
            hits = [h for h in result["hits"]
                    if h.get("source_collection") == participant.name]
            if hits:
                cancel.checkpoint()
                summaries.append(_verify_hits(cancel.watch(db.connect(participant)),
                                              participant, hits))
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
