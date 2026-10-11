"""What changed between two held snapshots of one chain (07 §7.3 `hold
snapshot delta`; 09 §9.4).

Every snapshot already carries its own `delta` against the one before, by
content hash. The change from `from` to `to` is those deltas composed,
oldest first — so nothing is re-hashed and no shard is opened. Composed,
not merely unioned: a uid added and then tombstoned inside the range was
never there at either end and is not reported; one tombstoned and then
re-added existed at both ends and is reported `changed` (its bytes may
differ, and the deltas alone cannot say they do not); one added and then
changed is still `added`.

A gap — a period between the two that this peer does not hold, or a
snapshot whose `previous` is not the hash of the one before — is refused:
a composition over a broken chain would silently miss that period's
changes, and it would look exactly like a complete answer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import collection as col, shard as shard_mod, snapshot as snap
from .codex import CodexError


def _load(root: Path, name: str, snapshot_id: str) -> snap.Snapshot | None:
    p = col.shard_dir_of(Path(root), name) / shard_mod.snapshot_path(snapshot_id)
    return snap.load(p) if p.is_file() else None


def walk(root: Path, name: str, from_id: str, to_id: str) -> list[snap.Snapshot]:
    """`from` … `to`, oldest first, each linked to the one before by its
    `previous` hash and its id the next period. CodexError (exit 2) on a
    snapshot not held, a gap or a broken link."""
    first = _load(root, name, from_id)
    if first is None:
        raise CodexError(f"{name} holds no snapshot {from_id!r} "
                         f"(`hold snapshot list {name}` shows what it holds)")
    if _load(root, name, to_id) is None:
        raise CodexError(f"{name} holds no snapshot {to_id!r} "
                         f"(`hold snapshot list {name}` shows what it holds)")
    if to_id < from_id:
        raise CodexError(f"{to_id!r} is before {from_id!r}: a delta runs forward "
                         f"along the chain")
    chain = [first]
    while chain[-1].snapshot_id != to_id:
        prev = chain[-1]
        nid = snap.next_snapshot_id(prev.snapshot_id)
        if nid > to_id:
            raise CodexError(f"{to_id!r} is not a period of {from_id!r}'s chain "
                             f"(after {prev.snapshot_id} comes {nid})")
        cur = _load(root, name, nid)
        if cur is None:
            raise CodexError(f"a gap in the chain: {name} holds no snapshot {nid!r} "
                             f"between {from_id} and {to_id}, so the changes of that "
                             f"period cannot be counted (`share fetch --snapshot {to_id}` "
                             f"fetches a snapshot and its chain)")
        if cur.previous != prev.hash:
            raise CodexError(f"the chain is broken at {nid}: its `previous` is not "
                             f"the hash of {prev.snapshot_id} (09 §9.4)")
        chain.append(cur)
    return chain


def compose(chain: list[snap.Snapshot]) -> snap.Delta:
    """The deltas of every snapshot after the first, composed oldest first."""
    state: dict[str, tuple[str, str | None]] = {}
    for s in chain[1:]:
        for uid in s.delta.added:
            was = state.get(uid)
            state[uid] = ("changed", None) if was and was[0] == "tombstoned" else ("added", None)
        for uid in s.delta.changed:
            was = state.get(uid)
            if not (was and was[0] == "added"):
                state[uid] = ("changed", None)
        for t in s.delta.tombstoned:
            was = state.get(t.uid)
            if was and was[0] == "added":
                del state[t.uid]
            else:
                state[t.uid] = ("tombstoned", t.reason)
    return snap.Delta(
        added=tuple(u for u, (k, _) in state.items() if k == "added"),
        changed=tuple(u for u, (k, _) in state.items() if k == "changed"),
        tombstoned=tuple(snap.Tombstone(u, r or "withdrawn")
                         for u, (k, r) in state.items() if k == "tombstoned"))


def delta(root: Path, name: str, from_id: str, to_id: str) -> dict[str, Any]:
    chain = walk(root, name, from_id, to_id)
    d = compose(chain)
    return {"from": from_id, "to": to_id,
            "snapshots": [s.snapshot_id for s in chain[1:]],
            "counts": {"added": len(d.added), "changed": len(d.changed),
                       "tombstoned": len(d.tombstoned)},
            "delta": d.to_dict()}
