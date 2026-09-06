"""Snapshots — a collection's records at one monthly cutoff, content-addressed.
docs/02-collections.md §2.2.

A snapshot's *id* is computable from the Codex and a clock ("2026-09"); its
*hash* is computable from its content. Two honest peers that build the same
month produce the same hash. Pure functions, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .codex import canonical_json, digest, CodexError


@dataclass(frozen=True)
class Shard:
    slice: str                 # "year=1975-1979"
    n: int                     # shard number within the slice
    root: str                  # merkle root of the shard's bytes (sha256:…)
    first_id: str
    last_id: str
    records: int
    chunks: int
    index_min: int = 1         # oldest index version whose centres label this shard correctly

    @property
    def path(self) -> str:
        return f"records/{self.slice}/{self.n:05d}.jsonl.zst"


@dataclass(frozen=True)
class Delta:
    """What changed since `previous`, by content hash — never by the origin's
    own revision fields, and never because a parser changed (§2.7: that is a
    daughter or a corrective sister, not a delta)."""
    added: tuple[str, ...]        # record uids new in this snapshot
    changed: tuple[str, ...]      # uids whose content_hash differs
    tombstoned: tuple[str, ...]   # uids no longer present or retracted


@dataclass(frozen=True)
class Snapshot:
    collection_id: str
    snapshot_id: str              # "YYYY-MM"
    previous: str | None          # hash of the prior snapshot, None for the first
    shards: tuple[Shard, ...]
    delta: Delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "louise-snapshot", "version": 1,
            "collection_id": self.collection_id,
            "snapshot_id": self.snapshot_id,
            "previous": self.previous,
            "shards": [asdict(s) for s in sorted(self.shards, key=lambda s: (s.slice, s.n))],
            "delta": {k: sorted(v) for k, v in asdict(self.delta).items()},
        }

    def canonical(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def hash(self) -> str:
        return digest(self.canonical())

    def members(self, space_id: str | None = None) -> list[str]:
        """What a peer can fetch or share: slice × kind (§2.6)."""
        slices = sorted({s.slice for s in self.shards})
        out = [f"records-{sl}" for sl in slices]
        if space_id:
            out += [f"vectors-{space_id[7:19]}-{sl}" for sl in slices]
        return out


def snapshot_id_for(instant: datetime) -> str:
    """The id of the snapshot whose cutoff is the most recent origin-clock
    midnight on the cadence's day at or before `instant`.

    The monthly cutoff is midnight at the start of the first day of the
    month, in the origin's clock; the snapshot is named for the month that
    just closed. `instant` must already be expressed in the origin's zone.
    """
    if instant.tzinfo is None:
        raise CodexError("instant must be timezone-aware, in the origin's clock")
    y, m = instant.year, instant.month
    # the month that has closed is the previous one
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f"{y:04d}-{m:02d}"


def next_snapshot_id(snapshot_id: str) -> str:
    y, m = (int(x) for x in snapshot_id.split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def compute_delta(previous: dict[str, str] | None, current: dict[str, str]) -> Delta:
    """`previous` and `current` map record uid → content_hash."""
    prev = previous or {}
    added = tuple(sorted(u for u in current if u not in prev))
    changed = tuple(sorted(u for u in current if u in prev and prev[u] != current[u]))
    tombstoned = tuple(sorted(u for u in prev if u not in current))
    return Delta(added, changed, tombstoned)


def verify_chain(snapshots: Iterable[Snapshot]) -> None:
    """Oldest first: each names the hash of the one before, ids are
    consecutive months, and all belong to one collection."""
    prev: Snapshot | None = None
    for s in snapshots:
        if prev is None:
            if s.previous is not None:
                raise CodexError(f"{s.snapshot_id}: first snapshot names a predecessor")
        else:
            if s.previous != prev.hash:
                raise CodexError(f"{s.snapshot_id}: previous does not match {prev.snapshot_id}")
            if s.snapshot_id != next_snapshot_id(prev.snapshot_id):
                raise CodexError(f"{s.snapshot_id}: not the month after {prev.snapshot_id}")
            if s.collection_id != prev.collection_id:
                raise CodexError(f"{s.snapshot_id}: collection changes mid-chain")
        prev = s
