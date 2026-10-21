"""`louise hold config show | apply` (07 §7.3, §7.8): each collection's
effective configuration, every value labelled with its tier, and the
reload of the files on demand.

THE SAME RESOLUTION THE NODE USES, never a second one (invariant 10): the
collection as `collections.load_all` loads it, its encoder through
`collections.recipe_for`, its reranker through `collections.reranker_for`,
its embed mode as the worker applies it (`db.apply_desired_embed_mode`)
against the override in its state database (`db.get_scope`). A value
shown here and a value the worker runs cannot disagree, because they are
read by the same functions.

The tiers are 07 §7.8's: `environment` (identity), `files` (desired state:
the collection's file, or the default when it says nothing) and `state
database` (a runtime override, until the node next applies the files).
What the Codex fixes — the space's width, dtype, context and query
instruction — is labelled `codex`: no tier changes it.
"""
from __future__ import annotations

import contextlib
import sys
from contextlib import closing
from typing import Any

FILES, ENV, STATE, CODEX = "files", "environment", "state database", "codex"


def _v(value: Any, tier: str | None, **more: Any) -> dict[str, Any]:
    return {"value": value, "tier": tier, **more}


def _embed_mode(collection) -> dict[str, Any]:
    from app import collections as collections_mod, db
    desired = collection.embed_mode_default or collections_mod.DEFAULT_EMBED_MODE
    set_in = (f"collections/{collection.name}.toml" if collection.embed_mode_default
              else "the default — the file sets none")
    try:
        with closing(db.connect(collection)) as con:
            effective = db.get_scope(con).get("embed_mode")
    except Exception as e:                              # noqa: BLE001
        return _v(desired, FILES, set_in=set_in,
                  note=f"state database not readable ({type(e).__name__}); the file's value shown")
    if effective is None or effective == desired:
        return _v(desired, FILES, set_in=set_in)
    return _v(effective, STATE, files=desired, set_in=set_in,
              note="a runtime override: the node's next apply of the files "
                   "(start, `hold config apply`, an edit) puts the file's value back")


def _encoder(collection) -> dict[str, Any]:
    from app import collections as collections_mod
    try:
        r = collections_mod.recipe_for(collection)
    except collections_mod.NoEncoder as e:
        return {"encoder": _v(None, FILES, note=str(e))}
    return {"encoder.url": _v(r.url, FILES), "encoder.api": _v(r.api, FILES),
            "encoder.embed_model": _v(r.model, FILES), "encoder.query_url": _v(r.query_url, FILES),
            "encoder.query_model": _v(r.query_model, FILES),
            "encoder.dtype": _v(r.dtype, CODEX), "encoder.ctx": _v(r.ctx, CODEX),
            "encoder.dim": _v(r.dim, CODEX),
            "encoder.query_instruction": _v(r.query_instruction, CODEX)}


def _reranker(collection) -> dict[str, Any]:
    from app import collections as collections_mod
    r = collections_mod.reranker_for(collection)
    if r is None:
        return _v(None, None, note="no [reranker] and no RERANK_URL: lists keep their fused order")
    tier = FILES if getattr(collection, "reranker", None) is not None else ENV
    return _v({"url": r.url, "api": r.api, "rerank_model": r.model, "window": r.window}, tier)


def of(collection) -> dict[str, Any]:
    """One collection's effective configuration."""
    from app import collections as collections_mod
    codex = getattr(collection, "codex", None)
    values: dict[str, Any] = {"embed_mode": _embed_mode(collection)}
    values.update(_encoder(collection))
    values["reranker"] = _reranker(collection)
    for key in ("share", "share_slices", "share_kinds", "slices", "kinds", "source",
                "genesis_archive", "snapshot"):
        v = getattr(collection, key, None)
        values[key] = _v(list(v) if isinstance(v, tuple) else v, FILES)
    values["corpus_dir"] = _v(str(collection.corpus_dir) if collection.corpus_dir else None, FILES)
    return {"name": collection.name,
            "collection_id": codex.collection_id if codex is not None else None,
            "file": str(collections_mod.config_toml_path(collection.name)),
            "values": values}


def _loaded(name: str | None):
    """(collections, refused) as the node loads them; one named, or all."""
    from app import collections as collections_mod
    with contextlib.redirect_stdout(sys.stderr):      # the loader logs; stdout is the result
        served, unreadable = collections_mod.load_all(skip_unreadable=True)
    refused = [(n, str(why)) for n, why in unreadable]
    refused += [(r.name, r.reason) for r in collections_mod.refusals()
                if r.name not in {n for n, _ in refused}]
    if name is None:
        return served, refused
    got = [c for c in served if c.name == name]
    bad = [(n, why) for n, why in refused if n == name]
    if not got and not bad:
        from app.hold.codex import CodexError
        raise CodexError(f"{name}: no such collection on this node "
                         f"(its file is collections/{name}.toml)")
    return got, bad


def show(name: str | None = None) -> dict[str, Any]:
    from app import node_show, node_state
    served, refused = _loaded(name)
    ov = node_show.overrides()
    return {"node": {"mode": _v(ov["mode"], STATE),
                     "paused_collections": _v(ov["paused_collections"], STATE)},
            "collections": [of(c) for c in sorted(served, key=lambda c: c.name)],
            "refused": [{"name": n, "reason": why} for n, why in refused]}


def apply(name: str | None = None) -> dict[str, Any]:
    """The files applied now — what the node does at start and on an edit
    (07 §7.8 "Pickup"): each collection's Tier-3 embed-mode override
    replaced by its file's value, and the event log told so. A collection
    whose file the node refuses (no encoder, a bad key) is named with its
    reason: there is nothing of it to apply, and its file is what to fix."""
    from app import collections as collections_mod, db
    served, refused = _loaded(name)
    applied = []
    for c in sorted(served, key=lambda c: c.name):
        desired = c.embed_mode_default or collections_mod.DEFAULT_EMBED_MODE
        with closing(db.connect(c)) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                replaced = db.apply_desired_embed_mode(con, c)
                if replaced is not None:
                    with contextlib.redirect_stdout(sys.stderr):
                        db.log_event(con, "info", "control",
                                     f"embed mode {replaced!r} -> {desired!r} on hold config "
                                     f"apply: the file wins")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        applied.append({"name": c.name, "embed_mode": desired,
                        "replaced": replaced, "encoder": "named in its file"})
    return {"applied": applied,
            "refused": [{"name": n, "reason": why} for n, why in refused]}
