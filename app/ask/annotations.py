"""The facts an annotations collection attaches to a search's results
(05 §5.3, amended 2026-10-09; 02 §2.10; docs/notes/annotation-collections.md).

A result whose record is the subject or the object of a claim held here
carries, under `annotations`, the record at the claim's other end — its uid,
title and language as the target holds it — with the claim's type, method,
confidence and contributor, the annotations collection and its trust level,
and the claim's `state` against the text the answer was read from. It is a
FACT placed beside the result, of the kind a reference source's row or a
figure is: never scored, never a leg, never fused (invariant 4). A client
may group a work's versions by it; the lists are not reordered for it.

Which claims apply: those of an annotations collection this peer holds and
may use (a copy in quarantine attaches nothing and is named under
`withheld`, as any copy is — 05 §5.3), whose `annotates.collection_id` is
the result's collection, read from a target snapshot at or after
`annotates.from_snapshot`.

COST: the claims of a copy are indexed once per snapshot
(`annotations.index_of`, keyed on the SHARD's path); per search, one manifest
query per target collection over the uids the attached claims name. It
scales with the results and their claims, never with either collection.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from app.hold import annotations as ann

FIELD = "annotations"


def _held(collections=None) -> list:
    if collections is not None:
        return list(collections)
    from app import collections as collections_mod
    return collections_mod.load_annotations()


def _withheld(a) -> str | None:
    from app import copy_state, licensing
    try:
        if licensing.state_for(a) == licensing.MISSING:
            return "license not acknowledged (invariant 9)"
    except Exception as e:   # noqa: BLE001 — fail closed, as every gate does
        return f"license state cannot be judged ({type(e).__name__}: {e})"
    try:
        return copy_state.withholds(a)
    except Exception as e:   # noqa: BLE001
        return None if not copy_state.enforced() else f"quarantine: state cannot be derived ({e})"


def _trust(root: Path, a) -> str:
    from app import copy_state, identity
    from app.hold import equivalence, trust_file
    from app.share import levels, seeder
    from app.hold import collection as col
    try:
        own = identity.own()
    except Exception:   # noqa: BLE001
        own = None
    try:
        snap = seeder.current_snapshot(col.shard_dir_of(root, a.name))
        bodies = copy_state.attestations(root, a.name, a.codex)
        return levels.level(a.codex, snap, bodies, (), own_key=own,
                            trust=trust_file.as_filter(root.parent),
                            reference_images=equivalence.reference_images(bodies, own, a.codex))["level"]
    except Exception:   # noqa: BLE001 — a level that cannot be computed is the publisher's word
        return levels.UNVERIFIED


def _target_rows(collection, uids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """uid -> {title, lang, content_hash} for the uids the target's manifest
    holds: one indexed query (`uid` is the primary key)."""
    from app import db
    uids = sorted(set(uids))
    out: dict[str, dict[str, Any]] = {}
    if not uids:
        return out
    with closing(db.connect(collection)) as con:
        for i in range(0, len(uids), 500):
            batch = uids[i:i + 500]
            for r in con.execute(
                    f"SELECT uid, title, lang, content_hash FROM documents WHERE uid IN "
                    f"({','.join('?' * len(batch))})", batch):
                out[r[0]] = {"title": r[1], "lang": r[2], "content_hash": r[3]}
    return out


def answers_for(root, name: str, collection_id: str | None) -> tuple[str, ...]:
    """The collection ids an annotations target may name for the collection
    held as `name`: its own, and the one a same-records carry brought its
    records from (`carry.records_twin_of`, its own `state/carry.json`) —
    claims about those records are claims about these (02 §2.4 A1f)."""
    from app.hold import carry
    twin = carry.records_twin_of(Path(root) / name / "state")
    return tuple(dict.fromkeys(i for i in (collection_id, twin) if i))


def target_among(codex, ids: Iterable[str]):
    """The annotations Codex's target entry naming the first of `ids` it
    annotates, or None."""
    from app.hold.codex import target_for
    return next((t for i in ids if (t := target_for(codex, i)) is not None), None)


def attach(lists: list[dict[str, Any]], source_of: dict[int, Any], contexts: dict[str, dict],
           *, root: Path, collections=None) -> list[dict[str, Any]]:
    """Every hit gets `annotations` — `[]` where no held claim names its
    record. Returns the annotations collections withheld, in `withheld`'s
    shape (05 §5.3)."""
    by_coll: dict[str, list[dict[str, Any]]] = {}
    for lst in lists:
        for h in lst["hits"]:
            h[FIELD] = []
            c = source_of.get(id(h))
            if c is not None:
                by_coll.setdefault(c.name, []).append(h)
    held = _held(collections)
    if not held or not by_coll:
        return []
    withheld, usable = [], []
    targets = {i for n in by_coll if n in contexts
               for i in answers_for(root, n, contexts[n]["collection_id"])}
    from app.hold.codex import targets_of
    for a in held:
        if not any(t.collection_id in targets for t in targets_of(a.codex)):
            continue                    # about nothing this search returned: not consulted
        why = _withheld(a)
        if why:
            withheld.append({"collection": a.name, "collection_id": a.codex.collection_id,
                             "shards": "all", "reason": f"annotations: {why}"})
        else:
            usable.append(a)
    root = Path(root)
    from app.hold import collection as col
    for name, hits in by_coll.items():
        ctx = contexts.get(name) or {}
        ids = answers_for(root, name, ctx.get("collection_id"))
        mine = [a for a in usable
                if (t := target_among(a.codex, ids)) is not None
                and ann.applies_from(t.from_snapshot, ctx.get("snapshot_id"))]
        if not mine:
            continue
        found = []
        for a in mine:
            idx = ann.index_of(col.shard_dir_of(root, a.name))
            trust = None
            for h in hits:
                for cl in idx.by_target.get(h["uid"], ()):
                    if trust is None:
                        trust = _trust(root, a)
                    found.append((h, a, idx.snapshot_id, cl, trust))
        if not found:
            continue
        rows = _target_rows(source_of[id(hits[0])],
                            {u for _, _, _, cl, _ in found for u in (cl.claim["subject"], cl.claim["object"])})
        now = {u: r.get("content_hash") for u, r in rows.items()}
        for h, a, snap_id, cl, trust in found:
            c = cl.claim
            role = "subject" if c["subject"] == h["uid"] else "object"
            other = c["object"] if role == "subject" else c["subject"]
            row = rows.get(other)
            h[FIELD].append({
                "collection_id": a.codex.collection_id, "snapshot_id": snap_id, "claim": cl.uid,
                "type": c["type"], "relation": ann.edge_type(c["type"]), "role": role,
                "record": {"uid": other, "title": row.get("title") if row else None,
                           "lang": row.get("lang") if row else None, "held": row is not None},
                "subject_lang": c["subject_lang"], "object_lang": c["object_lang"],
                "method": c["method"], "confidence": c["confidence"],
                "contributor": c["contributor"], "state": ann.state_of(c, now),
                "trust_level": trust})
    return withheld
