"""Which documents this node leaves to another peer (04 §4.5) — the embed
stage's half of rendezvous.

`share contribute` embeds only the parts this peer leads; the worker's own
embed stage under `embed = "all"` used to embed EVERY part, so a node
duplicated what the peers leading the other parts were embedding, and their
bundles landed as checks, not saved work (2026-10-19: the node embedded all
of gutenberg beside two contributing peers). The partition is now the
setting `embed = "p2p"` ("all p2p", 07 §7.8; operator, 2026-10-19), and
`all` ("all local") embeds every part again, by choice. Here, per
collection:

- the open parts whose current leader — 04 §4.5's ordering with its grace
  cascade, `contribute.leaders` — is ANOTHER live peer are left to it;
- a document's part is found from the snapshot alone (05 §5.5): its slice
  and its `native_id` against the slice's sorted id ranges, by bisection;
- the index is rebuilt only when what decides it changes — the snapshot,
  the live set, the coverage rows — at most once a `REBUILD_S`; its cost is
  the number of PARTS, never the corpus (CLAUDE.md engineering rules).

Skipping is an optimisation, so every doubt embeds: a node that is not
itself live (no benchmark for the space yet) or alone skips nothing; a
document whose slice the manifest cannot name and whose id more than one
slice's ranges could hold is embedded; an id the order cannot place is
embedded. The worst case is the old behaviour — a duplicate — never a gap.
Read-only: nothing here writes the manifest (the parts' first-open times
are `share contribute`'s to record).
"""
from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.share import rendezvous

#: the index is reconsidered at most this often (the coverage map changes per
#: accepted bundle, the live set per digest interval — both far slower)
REBUILD_S = 60.0
#: the bound on reading node.sqlite (the digests) from the embed selection
NODE_DB_TIMEOUT_MS = 250


@dataclass(frozen=True)
class _Part:
    lo: Any                # order_key of first_id
    hi: Any                # order_key of last_id
    skipped: bool          # open, and led by another live peer


@dataclass(frozen=True)
class Skips:
    """Per slice, its parts sorted by `lo` (a slice's ranges partition its
    id space, 02 §2.2), and the column naming a document's slice when the
    collection slices by one key the manifest keeps."""
    order: str
    slice_column: str | None
    slices: Mapping[str, tuple[list[Any], list[_Part]]]   # slice -> (los, parts)
    skipped_parts: int
    #: the slice of a record with no value for the key (`slicing.unknown`,
    #: gutenberg's "lang=unknown")
    unknown_slice: str | None = None

    def _part_in(self, slice_name: str, k) -> _Part | None:
        got = self.slices.get(slice_name)
        if got is None:
            return None
        los, parts = got
        i = bisect.bisect_right(los, k) - 1
        if i >= 0 and parts[i].lo <= k <= parts[i].hi:
            return parts[i]
        return None

    def skips(self, native_id: str, slice_value: str | None = None) -> bool:
        """Whether the document belongs to a part another live peer leads."""
        from app.hold.records import RecordsError, order_key
        try:
            k = order_key(str(native_id), self.order)
        except (RecordsError, ValueError, TypeError):
            return False
        if self.slice_column and slice_value not in (None, ""):
            part = self._part_in(f"{self.slice_column}={slice_value}", k)
            return bool(part and part.skipped)
        if self.slice_column and self.unknown_slice and slice_value in (None, ""):
            part = self._part_in(self.unknown_slice, k)
            return bool(part and part.skipped)
        holders = [p for p in (self._part_in(s, k) for s in self.slices) if p is not None]
        return len(holders) == 1 and holders[0].skipped

    def skips_row(self, row) -> bool:
        keys = row.keys() if hasattr(row, "keys") else ()
        value = row[self.slice_column] if self.slice_column and self.slice_column in keys else None
        return self.skips(row["native_id"], value)


def slice_column(codex) -> str | None:
    """The documents column naming a record's slice, when the collection is
    sliced by exactly one key that the manifest keeps (`lang` does; a
    binned `year` does not)."""
    keys = list((getattr(codex, "slicing", None) or {}).get("keys") or [])
    if len(keys) == 1 and keys[0] in ("lang",):
        spec = (codex.slicing or {}).get(keys[0])
        if not isinstance(spec, dict) or not spec.get("bins"):
            return keys[0]
    return None


def build(codex, snapshot, *, leaders: Mapping[str, str | None], me: str | None,
          live: Sequence[rendezvous.Peer]) -> Skips | None:
    """The index, or None when nothing is to be skipped: no snapshot, this
    node not live, no other live peer, or no open part led elsewhere.
    `leaders` maps an open part's shard id (`coverage.shard_id`) to its
    current leader's key."""
    from app.hold.records import RecordsError, order_key
    from app.share import coverage as cov
    if snapshot is None or me is None:
        return None
    keys = {p.key for p in live}
    if me not in keys or len(keys) < 2:
        return None
    order = (getattr(codex, "slicing", None) or {}).get("order", "bytes")
    by_slice: dict[str, list[_Part]] = {}
    skipped = 0
    for s in snapshot.shards:
        sid = cov.shard_id(codex, f"{s.slice}/{s.n:05d}")
        lead = leaders.get(sid)
        is_skipped = lead is not None and lead != me
        try:
            part = _Part(order_key(s.first_id, order), order_key(s.last_id, order), is_skipped)
        except RecordsError:
            continue
        by_slice.setdefault(s.slice, []).append(part)
        skipped += is_skipped
    if not skipped:
        return None
    slices = {}
    for name, parts in by_slice.items():
        parts.sort(key=lambda p: p.lo)
        slices[name] = ([p.lo for p in parts], parts)
    unknown = (getattr(codex, "slicing", None) or {}).get("unknown")
    return Skips(order=order, slice_column=slice_column(codex), slices=slices,
                 skipped_parts=skipped, unknown_slice=unknown if isinstance(unknown, str) else None)


# ---- the worker's cached view ---------------------------------------------------

_cache: dict[str, tuple[float, tuple, Skips | None]] = {}
_cache_lock = threading.Lock()


#: grace windows run in days (`update.grace_days`); a leader whose window
#: ran out with nothing changing but the clock is noticed within this
WINDOW_TICK_S = 3600.0


def _fingerprint(snapshot, live: Sequence[rendezvous.Peer], con, now: float) -> tuple:
    try:
        n, last = con.execute(
            "SELECT COUNT(*), MAX(accepted_at) FROM coverage").fetchone()
    except Exception:   # noqa: BLE001 — no coverage table yet: nothing accepted
        n, last = 0, None
    return (getattr(snapshot, "snapshot_id", None),
            tuple(sorted((p.key, p.benchmark) for p in live)), int(n or 0), last,
            int(now // WINDOW_TICK_S))


def skip_test(collection, con, *, root: Path | None = None, now: float | None = None,
              node_con=None, own_key: str | None = None,
              live: Sequence[rendezvous.Peer] | None = None,
              snapshot=None) -> Callable[[Any], bool] | None:
    """`row -> bool` for the embed stage's candidates, or None when nothing
    is skipped (the stage then runs exactly as before). Never raises: any
    failure to read the inputs skips nothing, logged by the caller's
    absence of a filter, never by an error on a document."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return None
    key = str(getattr(collection, "manifest_path", "") or collection.name)
    now = time.time() if now is None else now
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit[0] < REBUILD_S and live is None:
        # the hot path: the embed stage asks every round; the inputs (digests,
        # statements, the coverage map) are read at most once a REBUILD_S
        return None if hit[2] is None else hit[2].skips_row
    try:
        from app import collections as collections_mod
        from app.hold import collection as col
        from app.share import contribute, coverage as cov, seeder
        root = Path(root) if root is not None else collections_mod.louise_root() / "collections"
        if snapshot is None:
            snapshot = seeder.current_snapshot(col.shard_dir_of(root, collection.name))
        if own_key is None:
            from app import identity
            own_key = identity.own()
        if live is None:
            from contextlib import closing
            from app.share import peers
            own = cov.own_benchmarks(root, collection.name, codex, own_key)
            if node_con is None:
                with closing(peers.connect()) as nc:
                    # a pipeline thread never waits without a bound: a locked
                    # node.sqlite costs NODE_DB_TIMEOUT_MS, then skips nothing
                    nc.execute(f"PRAGMA busy_timeout = {int(NODE_DB_TIMEOUT_MS)}")
                    live = cov.live_peers(codex, nc, now=now, own_key=own_key, own_benchmarks=own)
            else:
                live = cov.live_peers(codex, node_con, now=now, own_key=own_key, own_benchmarks=own)
        fp = _fingerprint(snapshot, live, con, now)
        if hit is not None and hit[1] == fp:
            skips = hit[2]                       # nothing that decides it moved
            with _cache_lock:
                _cache[key] = (now, fp, skips)
        else:
            from app.share import deprecate
            # deprecated copies held beside their current one count toward no
            # part (04 §4.5); named only when there are any
            redundant = deprecate.redundant_here(root, collection.name, codex, con)
            extra = {"redundant": redundant} if redundant else {}
            leaders = contribute.leaders(codex, snapshot, con, live=live, now=now, **extra) \
                if snapshot is not None else {}
            skips = build(codex, snapshot, leaders=leaders, me=own_key, live=live)
            with _cache_lock:
                _cache[key] = (now, fp, skips)
    except Exception:   # noqa: BLE001 — any doubt embeds (module docstring)
        # cached like a success: a locked node.sqlite or an unreadable digest
        # costs its timeout once a REBUILD_S, never every round (review of
        # 45535ea)
        with _cache_lock:
            _cache[key] = (now, ("unreadable",), None)
        return None
    return None if skips is None else skips.skips_row


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()
