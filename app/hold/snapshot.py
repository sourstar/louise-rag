"""Snapshots — a collection's records at one cutoff, content-addressed.
docs/02-collections.md §2.2; docs/09-formats.md §9.4 fixes the bytes.

A snapshot's *id* is computable from the Codex and a clock ("2026-09",
"2026-W36"); its *hash* is computable from its content. Two honest peers
that build the same period produce the same hash. Pure functions, no I/O.

The snapshot is RECORDS ONLY: no field of it depends on vectors, on centres
or on coverage (02 §2.2). `chunks` and `chunks_root` are here because both
are functions of the records and the Codex's layers — the same reason they
are not in a member that depends on an encoder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from .codex import canonical_json, digest, CodexError

SNAPSHOT_KIND = "louise-snapshot"
SNAPSHOT_VERSION = 1
SIG_KIND = "louise-snapshot-sig"


@dataclass(frozen=True)
class Shard:
    """One record shard, as 09 §9.4's shard entry."""
    slice: str                 # "year=1975-1979"
    n: int                     # shard number within the slice, from 1
    root: str                  # BitTorrent v2 pieces root of the stored file
    records_root: str          # SHA-256 over the content_hash digests, in record order
    first_id: str              # the native_id of the first record
    last_id: str               # …and of the last
    records: int
    chunks: int                # chunks over all its records under the Codex's layers
    chunks_root: str           # §9.4: over each chunk's id and the hash of its text
    tokens: int                # chunks × space.ctx — the upper bound §4.5 uses
    bytes: int                 # size of the stored file
    toolchain: str = "own"     # "own" | "mother" (a daughter's inherited shards)

    @property
    def path(self) -> str:
        return f"records/{self.slice}/{self.n:05d}.jsonl.zst"

    def to_dict(self) -> dict[str, Any]:
        return {"slice": self.slice, "n": self.n, "root": self.root,
                "records_root": self.records_root, "first_id": self.first_id,
                "last_id": self.last_id, "records": self.records,
                "chunks": self.chunks, "chunks_root": self.chunks_root,
                "tokens": self.tokens, "bytes": self.bytes,
                "toolchain": self.toolchain}


@dataclass(frozen=True)
class RelationShard:
    """One relations shard (09 §9.14): `slice`, `n`, `root`, `edges`,
    `bytes`. It sits beside the record shard of the same slice and number."""
    slice: str
    n: int
    root: str
    edges: int
    bytes: int

    @property
    def path(self) -> str:
        return f"relations/{self.slice}/{self.n:05d}.jsonl.zst"

    def to_dict(self) -> dict[str, Any]:
        return {"slice": self.slice, "n": self.n, "root": self.root,
                "edges": self.edges, "bytes": self.bytes}


@dataclass(frozen=True)
class Tombstone:
    """A uid no longer present, with the reason (09 §9.4). `"retracted"` —
    the origin says the record should not have existed; `"withdrawn"` — the
    origin no longer supplies it."""
    uid: str
    reason: str                # "retracted" | "withdrawn"

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "reason": self.reason}


REASONS = ("retracted", "withdrawn")


@dataclass(frozen=True)
class Delta:
    """What changed since `previous`, BY CONTENT HASH — never by the origin's
    own revision fields, and never because a parser changed (02 §2.7: that is
    a daughter or a corrective sister, not a delta)."""
    added: tuple[str, ...]              # record uids new in this snapshot
    changed: tuple[str, ...]            # uids whose content_hash differs
    tombstoned: tuple[Tombstone, ...]   # uids no longer present, with a reason

    def to_dict(self) -> dict[str, Any]:
        return {"added": sorted(self.added), "changed": sorted(self.changed),
                "tombstoned": [t.to_dict() for t in
                               sorted(self.tombstoned, key=lambda t: t.uid)]}


@dataclass(frozen=True)
class Snapshot:
    collection_id: str
    snapshot_id: str                    # "YYYY-MM" or "YYYY-Www", by the cadence
    previous: str | None                # hash of the prior snapshot; None on the first
    shards: tuple[Shard, ...]
    delta: Delta
    relations: tuple[RelationShard, ...] = ()
    files: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SNAPSHOT_KIND, "version": SNAPSHOT_VERSION,
            "collection_id": self.collection_id,
            "snapshot_id": self.snapshot_id,
            "previous": self.previous,
            "shards": [s.to_dict() for s in
                       sorted(self.shards, key=lambda s: (s.slice.encode(), s.n))],
            "relations": [r.to_dict() for r in
                          sorted(self.relations, key=lambda r: (r.slice.encode(), r.n))],
            "files": self.files,
            "delta": self.delta.to_dict(),
        }

    def canonical(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def hash(self) -> str:
        return digest(self.canonical())

    @property
    def slices(self) -> list[str]:
        return sorted({s.slice for s in self.shards})

    @property
    def records(self) -> int:
        return sum(s.records for s in self.shards)

    @property
    def chunks(self) -> int:
        return sum(s.chunks for s in self.shards)

    @property
    def tokens(self) -> int:
        """The work this snapshot represents, as 04 §4.5 and §4.7 read it."""
        return sum(s.tokens for s in self.shards)

    def members(self, space: str | None = None) -> list[str]:
        """What a peer can fetch or share: slice × kind (02 §2.6). `space` is
        the 16-hex `<space>` segment, not the whole `space_id`."""
        from . import drag
        out = [drag.records_member(sl) for sl in self.slices]
        out += [drag.relations_member(sl)
                for sl in sorted({r.slice for r in self.relations})]
        if space:
            out += [drag.vectors_member(space, sl) for sl in self.slices]
        return out

    def shard_of(self, native_id: str) -> Shard | None:
        """The shard whose id range contains this native_id (05 §5.5).

        Ranges are a partition because `hold records build` refuses a record
        that sorts before the open shard's `first_id` (02 §2.2), so at most
        one shard can contain it and a linear scan is honest about that.
        """
        for s in self.shards:
            if s.first_id <= native_id <= s.last_id:
                return s
        return None


# --------------------------------------------------------------------------
# ids: the period that CLOSED at the cutoff

def snapshot_id_for(instant: datetime, cadence: str = "monthly") -> str:
    """The id of the snapshot whose cutoff is at or before `instant`.

    09 §9.4: a snapshot is named for the period of the collection's cadence
    that CLOSED at its cutoff, and the cutoff is 00:00:00 on `update.day` of
    the period that follows. `instant` must already be expressed in the
    origin's clock.
    """
    if instant.tzinfo is None:
        raise CodexError("instant must be timezone-aware, in the origin's clock")
    if cadence == "monthly":
        y, m = instant.year, instant.month
        return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"
    if cadence == "weekly":
        y, w, _ = instant.isocalendar()
        return _iso_week_id(y, w, -1)
    raise CodexError(f"cadence must be monthly|weekly, got {cadence!r}")


def _iso_week_id(year: int, week: int, offset: int) -> str:
    """`offset` weeks from ISO (year, week), as `YYYY-Www`. Computed through
    a real date so that a 52- or 53-week year is never guessed at."""
    monday = date.fromisocalendar(year, week, 1).toordinal() + 7 * offset
    y, w, _ = date.fromordinal(monday).isocalendar()
    return f"{y:04d}-W{w:02d}"


def next_snapshot_id(snapshot_id: str) -> str:
    """The next period of the same cadence, read off the id's own form."""
    if "-W" in snapshot_id:
        y, w = snapshot_id.split("-W")
        return _iso_week_id(int(y), int(w), +1)
    y, m = (int(x) for x in snapshot_id.split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def cadence_of(snapshot_id: str) -> str:
    return "weekly" if "-W" in snapshot_id else "monthly"


# --------------------------------------------------------------------------

def compute_delta(previous: dict[str, str] | None, current: dict[str, str], *,
                  reasons: dict[str, str] | None = None) -> Delta:
    """`previous` and `current` map record uid → content_hash.

    `reasons` gives the tombstone reason per uid; a uid the origin simply
    stopped supplying is `"withdrawn"`, which is the honest default — only
    the origin can say a record should never have existed.
    """
    prev, reasons = previous or {}, reasons or {}
    added = tuple(sorted(u for u in current if u not in prev))
    changed = tuple(sorted(u for u in current
                           if u in prev and prev[u] != current[u]))
    gone = sorted(u for u in prev if u not in current)
    for uid, why in reasons.items():
        if why not in REASONS:
            raise CodexError(f"tombstone reason for {uid} must be "
                             f"{' or '.join(REASONS)}, got {why!r}")
    tombstoned = tuple(Tombstone(u, reasons.get(u, "withdrawn")) for u in gone)
    return Delta(added, changed, tombstoned)


def verify_chain(snapshots: Iterable[Snapshot]) -> None:
    """Oldest first: each names the hash of the one before, ids are
    consecutive periods of one cadence, and all belong to one collection."""
    prev: Snapshot | None = None
    for s in snapshots:
        if prev is None:
            if s.previous is not None:
                raise CodexError(f"{s.snapshot_id}: first snapshot names a predecessor")
        else:
            if s.previous != prev.hash:
                raise CodexError(f"{s.snapshot_id}: previous does not match "
                                 f"{prev.snapshot_id}")
            if cadence_of(s.snapshot_id) != cadence_of(prev.snapshot_id):
                raise CodexError(f"{s.snapshot_id}: the cadence changes mid-chain; "
                                 f"a chain is one cadence and only a sister, who "
                                 f"begins her own, may change it (09 §9.3)")
            if s.snapshot_id != next_snapshot_id(prev.snapshot_id):
                raise CodexError(f"{s.snapshot_id}: not the period after "
                                 f"{prev.snapshot_id}")
            if s.collection_id != prev.collection_id:
                raise CodexError(f"{s.snapshot_id}: collection changes mid-chain")
        prev = s


def reissued(previous: Snapshot | None, current: Snapshot) -> list[Shard]:
    """The shards this snapshot re-issued: those whose `root` differs from the
    same `(slice, n)` in `previous`. 09 §9.4 — "nothing lists it separately",
    so it is derived here rather than stored."""
    if previous is None:
        return sorted(current.shards, key=lambda s: (s.slice, s.n))
    before = {(s.slice, s.n): s.root for s in previous.shards}
    return sorted((s for s in current.shards
                   if before.get((s.slice, s.n)) not in (None, s.root)),
                  key=lambda s: (s.slice, s.n))


# --------------------------------------------------------------------------
# creating one from a build (07 §7.4: `hold.snapshot.create`)

def create(root, ref: str, *, snapshot_id: str | None = None, log=print) -> Snapshot:
    """`louise hold snapshot create <collection>` — freeze the records the
    drag now holds as this period's snapshot.

    The shard entries come from the staging build database, where step 2c's
    `close_shard` put them: `root`, `records_root`, `chunks_root` and the
    native-id range are computed where the shards are cut, because two of
    the three cannot be recomputed from the drag alone without re-chunking
    the corpus.

    Re-running for an id that exists: identical bytes is nothing to do and
    exits 0; different bytes is refused (07 §7.3), because a snapshot is
    content-addressed and two different ones under one id is the dispute
    04 §4.3 exists to resolve, not something to create locally.
    """
    import json
    import sqlite3
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from . import collection as col, drag as drag_mod, records as rec

    name, codex = col.resolve(Path(root), ref)
    base = Path(root) / name
    drag_dir, staging = base / "drag", base / rec.STATE / rec.BUILD

    db_path = staging / ".build" / "build.db"
    if not db_path.is_file():
        raise CodexError(f"{name}: no build to freeze; `hold records build` "
                         f"first (07 §7.3)")

    if snapshot_id is None:
        clock = ZoneInfo(codex.origin.clock)
        snapshot_id = snapshot_id_for(datetime.now(clock), codex.update.cadence)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM shards ORDER BY slice, n")]
    finally:
        con.close()
    if not rows:
        raise CodexError(f"{name}: the build wrote no shards")

    missing = [r["path"] for r in rows if not r.get("chunks_root")]
    if missing:
        raise CodexError(
            f"{name}: {len(missing)} shard(s) were built before the roots of "
            f"09 §9.4 were computed — rebuild them; a snapshot may not carry "
            f"a root it did not compute (first: {missing[0]})")

    shards = tuple(
        Shard(slice=r["slice"], n=r["n"], root=r["root"],
              records_root=r["records_root"],
              first_id=r["first_native_id"], last_id=r["last_native_id"],
              records=r["records"], chunks=r["chunks"],
              chunks_root=r["chunks_root"],
              tokens=r["chunks"] * codex.space.ctx,
              bytes=r["bytes"], toolchain="own")
        for r in rows)

    previous, prev_hashes = _previous(drag_dir, snapshot_id)
    current = _content_hashes(drag_dir, shards)
    snap = Snapshot(
        collection_id=codex.collection_id, snapshot_id=snapshot_id,
        previous=previous.hash if previous else None,
        shards=shards,
        delta=compute_delta(prev_hashes, current),
        relations=_relation_shards(drag_dir, shards),
        files=_files(drag_dir, codex),
    )

    out = drag_dir / drag_mod.snapshot_path(snapshot_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        if out.read_bytes() == snap.canonical():
            log(f"[snapshot create] {name}: {snapshot_id} already frozen, "
                f"identical bytes")
            return snap
        raise CodexError(
            f"{name}: {snapshot_id} exists with different bytes. A snapshot is "
            f"content-addressed and is written once; two under one id is a "
            f"dispute between peers (04 §4.3), not something to overwrite here")
    out.write_bytes(snap.canonical())
    log(f"[snapshot create] {name}: {snapshot_id} — {snap.records} records, "
        f"{snap.chunks} chunks, {len(shards)} shard(s), hash {snap.hash[:23]}…")
    return snap


def _previous(drag_dir, snapshot_id: str):
    """The snapshot before this one in the chain, and its uid → content_hash
    map. `None` when this is the first."""
    from pathlib import Path
    from . import drag as drag_mod
    d = Path(drag_dir) / "snapshots"
    if not d.is_dir():
        return None, None
    ids = sorted(p.stem for p in d.glob("*.json")
                 if not p.name.endswith(".sig.json") and p.stem < snapshot_id)
    if not ids:
        return None, None
    raw = (d / f"{ids[-1]}.json").read_bytes()
    prev = _load(raw)
    return prev, _content_hashes(drag_dir, prev.shards)


def _load(raw: bytes) -> Snapshot:
    import json
    d = json.loads(raw)
    if d.get("kind") != SNAPSHOT_KIND:
        raise CodexError(f"not a snapshot: kind={d.get('kind')!r}")
    if d.get("version") != SNAPSHOT_VERSION:
        raise CodexError(f"unsupported snapshot version {d.get('version')!r}")
    return Snapshot(
        collection_id=d["collection_id"], snapshot_id=d["snapshot_id"],
        previous=d["previous"],
        shards=tuple(Shard(**s) for s in d["shards"]),
        delta=Delta(tuple(d["delta"]["added"]), tuple(d["delta"]["changed"]),
                    tuple(Tombstone(**t) for t in d["delta"]["tombstoned"])),
        relations=tuple(RelationShard(**r) for r in d["relations"]),
        files=d["files"])


def load(path) -> Snapshot:
    from pathlib import Path
    return _load(Path(path).read_bytes())


def _content_hashes(drag_dir, shards) -> dict[str, str]:
    """uid → content_hash over the shards named, read from the drag."""
    from pathlib import Path
    from app.pack import records as pack_records
    out = {}
    for s in shards:
        p = Path(drag_dir) / s.path
        if not p.is_file():
            continue
        for r in pack_records.iter_records(p):
            out[r["uid"]] = r["content_hash"]
    return out


def _relation_shards(drag_dir, shards) -> tuple[RelationShard, ...]:
    """One beside each record shard of the same slice and number, where the
    plugin wrote any (09 §9.14). `()` when the collection has none."""
    from pathlib import Path
    from . import drag as drag_mod
    out = []
    for s in shards:
        p = Path(drag_dir) / drag_mod.relations_path(s.slice, s.n)
        if not p.is_file():
            continue
        raw = p.read_bytes()
        out.append(RelationShard(slice=s.slice, n=s.n,
                                 root=drag_mod.pieces_root(raw),
                                 edges=_count_edges(p), bytes=len(raw)))
    return tuple(out)


def _count_edges(path) -> int:
    from app.pack import records as pack_records
    return sum(1 for _ in pack_records.iter_records(path))


def _files(drag_dir, codex) -> dict:
    """Every whole-collection member with its transport root (09 §9.4). A
    member that is not there is `null` where §9.4 allows one and absent
    where it does not — nothing is invented."""
    from pathlib import Path
    from . import drag as drag_mod
    out: dict = {}
    for member in (drag_mod.VALIDATION_PATH, drag_mod.TERMS_PATH):
        p = Path(drag_dir) / member
        if p.is_file():
            raw = p.read_bytes()
            out[member] = {"root": drag_mod.pieces_root(raw), "bytes": len(raw)}
        elif member == drag_mod.TERMS_PATH:
            out[member] = None          # green: no terms member (02 §2.6)
    return out
