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
#: 09 §9.4: "version 1 is this object without `figures`. A platform writing
#: this document's format writes 2 for every collection, with `figures: []`
#: where there are none; snapshots already written stay 1, and a chain
#: links by `previous` hash rather than by version, so nothing is
#: re-issued." So: 2 is written, 1 and 2 are both read.
SNAPSHOT_VERSION = 2
SNAPSHOT_VERSIONS_READ = (1, 2)
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
class FiguresShard:
    """One figures container (09 §9.4, §9.5): `slice`, `n`, `root`,
    `figures`, `bytes`. It sits beside the record shard of the same slice
    and number and holds every plate that shard's records name.

    NO digest over the figure hashes. They are on the record lines, whose
    shard `root` this same snapshot fixes and a certifier already signs; a
    second digest over them would be a second answer to a question that
    has one.
    """
    slice: str
    n: int
    root: str
    figures: int               # DISTINCT plates stored, not appearances
    bytes: int

    @property
    def path(self) -> str:
        return f"figures/{self.slice}/{self.n:05d}.fig.zst"

    def to_dict(self) -> dict[str, Any]:
        return {"slice": self.slice, "n": self.n, "root": self.root,
                "figures": self.figures, "bytes": self.bytes}


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


class LazyDelta:
    """A loaded snapshot's `delta`, read from its file only when asked.

    A first snapshot's delta lists every record as added — pubmed's 2026-08
    names 39,994,988 uids, 709 MB of JSON and about 4.3 GB as Python strings
    — and nearly every reader (the checks, the seeder, search, the worker's
    every evaluation of the copy) needs its shards and never its delta.
    Loaded whole, every one of them paid the 4.3 GB (measured on the node,
    2026-10-08). The file is immutable once written (02 §2.2), so where the
    delta sits in it is enough; the first access parses that span alone."""

    __slots__ = ("_path", "_start", "_end", "_delta")

    def __init__(self, path, start: int, end: int) -> None:
        self._path, self._start, self._end, self._delta = path, start, end, None

    def _load(self) -> "Delta":
        if self._delta is None:
            import json
            with open(self._path, "rb") as f:
                f.seek(self._start)
                d = json.loads(f.read(self._end - self._start))
            self._delta = Delta(tuple(d["added"]), tuple(d["changed"]),
                                tuple(Tombstone(**t) for t in d["tombstoned"]))
        return self._delta

    
    @property
    def added(self) -> tuple[str, ...]:
        return self._load().added

    
    @property
    def changed(self) -> tuple[str, ...]:
        return self._load().changed

    
    @property
    def tombstoned(self) -> tuple["Tombstone", ...]:
        return self._load().tombstoned

    def to_dict(self) -> dict[str, Any]:
        return self._load().to_dict()

    def __eq__(self, other) -> bool:
        return self._load() == (other._load() if isinstance(other, LazyDelta) else other)

    def __hash__(self) -> int:
        return hash(self._load())

    def __repr__(self) -> str:
        return f"LazyDelta({self._path!s}, {self._end - self._start} bytes)"


@dataclass(frozen=True)
class Snapshot:
    collection_id: str
    snapshot_id: str                    # "YYYY-MM" or "YYYY-Www", by the cadence
    previous: str | None                # hash of the prior snapshot; None on the first
    shards: tuple[Shard, ...]
    delta: Delta
    relations: tuple[RelationShard, ...] = ()
    figures: tuple[FiguresShard, ...] = ()
    files: dict[str, Any] = field(default_factory=dict)
    #: THE VERSION THIS SNAPSHOT WAS WRITTEN AT, not the one this build
    #: writes. 09 §9.4: "snapshots already written stay 1, and a chain
    #: links by `previous` hash rather than by version, so nothing is
    #: re-issued". A Snapshot is content-addressed — `hash` is over
    #: `canonical()` — so re-serialising a loaded v1 document at v2 gives
    #: it a hash its own file does not have, which invalidates every
    #: signature over it and breaks every `previous` link to it.
    #:
    #: Caught on pubmed the day the version moved: the sidecar covered
    #: sha256:3bfe1d1d… (the file's real bytes, version 1, no `figures`
    #: key) and the loaded object hashed to sha256:47df84a7…. The
    #: `signature` check was right and the loader was wrong.
    version: int = SNAPSHOT_VERSION

    def to_dict(self) -> dict[str, Any]:
        out = {
            "kind": SNAPSHOT_KIND, "version": self.version,
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
        if self.version >= 2:
            # "version 1 is this object WITHOUT `figures`" — so v1 omits the
            # key entirely rather than writing an empty list, which would be
            # a third shape the format does not have.
            out["figures"] = [f.to_dict() for f in
                              sorted(self.figures,
                                     key=lambda f: (f.slice.encode(), f.n))]
        elif self.figures:
            raise CodexError(
                f"{self.snapshot_id}: a version 1 snapshot carries no "
                f"`figures` and this one names {len(self.figures)} (09 §9.4)")
        return out

    def canonical(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def hash(self) -> str:
        # a snapshot loaded from its file knows its hash from the file's own
        # bytes (they ARE the canonical form); anything built or replaced
        # in memory computes it
        raw = self.__dict__.get("_raw_hash")
        return raw if raw is not None else digest(self.canonical())

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
        from . import shard as shard_mod
        out = [shard_mod.records_member(sl) for sl in self.slices]
        out += [shard_mod.relations_member(sl)
                for sl in sorted({r.slice for r in self.relations})]
        if space:
            out += [shard_mod.vectors_member(space, sl) for sl in self.slices]
        return out

    def shards_of(self, native_id: str, order: str = "bytes") -> list[Shard]:
        """Every shard whose id range contains this native_id (05 §5.5),
        compared in the collection's id order (09 §9.3 `slicing.order`).

        A SLICE's ranges are a partition — `hold records build` refuses a
        record that sorts before the open shard's `first_id` (02 §2.2) — so
        at most one shard per slice; a collection of several slices holds an
        id range in each, and which slice a record is in is its record's to
        say, not the snapshot's. Until 2026-10-07 this compared the ids as
        text, so in a numeric collection "1000" fell in the shard ending at
        "999", and it returned the first slice's shard whatever the record's.
        """
        from .records import RecordsError, order_key
        try:
            k = order_key(native_id, order)
            return [s for s in self.shards
                    if order_key(s.first_id, order) <= k <= order_key(s.last_id, order)]
        except RecordsError:
            return []                       # an id this order cannot place: no shard holds it

    def shard_of(self, native_id: str, order: str = "bytes") -> Shard | None:
        """The ONE shard that can hold this native_id; None when none can,
        or when shards of several slices can and only the record knows which."""
        got = self.shards_of(native_id, order)
        return got[0] if len(got) == 1 else None


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
    SHARD now holds as this period's snapshot.

    The shard entries come from the staging build database, where step 2c's
    `close_shard` put them: `root`, `records_root`, `chunks_root` and the
    native-id range are computed where the shards are cut, because two of
    the three cannot be recomputed from the SHARD alone without re-chunking
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

    from . import collection as col, shard as shard_mod, records as rec

    name, codex = col.resolve(Path(root), ref)
    # invariant 9: a snapshot is signed and published with the SHARD. Its
    # input exists only after a gated build, but the terms can change in
    # between — built through the gate like every other stage (audit)
    rec.local_collection(Path(root), name, codex)
    base = Path(root) / name
    shard_dir, staging = base / "shard", base / rec.STATE / rec.BUILD

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

    previous, prev_hashes = _previous(shard_dir, snapshot_id)
    current = _content_hashes(shard_dir, shards)
    snap = Snapshot(
        collection_id=codex.collection_id, snapshot_id=snapshot_id,
        previous=previous.hash if previous else None,
        shards=shards,
        delta=compute_delta(prev_hashes, current),
        relations=_relation_shards(shard_dir, shards),
        figures=_figures_shards(shard_dir, codex, shards),
        files=_files(shard_dir, codex, snapshot_id),
    )

    out = shard_dir / shard_mod.snapshot_path(snapshot_id)
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


# --------------------------------------------------------------------------
# the sidecar (09 §9.4)

SIDECAR_KIND = "louise-snapshot-sig"


def sig_message(snapshot_hash: str) -> bytes:
    """§9.4: `sig` is over `"louise/snapshot/v1" ‖ 0x00 ‖ <the 32 raw bytes
    of snapshot>`.

    NOT §9.2's envelope construction, which hashes a canonical-JSON body.
    The sidecar is the one signed object in the design that is not an
    envelope — because it is a SET that two peers merge by union, and a
    body signed whole could not gain a signature without invalidating the
    ones already there.
    """
    from . import envelope as env
    raw = snapshot_hash.split(":", 1)[-1]
    return env.SNAPSHOT.encode("ascii") + b"\x00" + bytes.fromhex(raw)


def sidecar_add(existing: dict | None, snapshot_hash: str, key, *,
                now: str) -> dict:
    """This key's signature, merged into the sidecar by UNION.

    "Two peers merge sidecars by union; a signature whose `snapshot` does
    not equal the file's hash is dropped." So a sidecar that names another
    snapshot is not merged into — it is replaced, having nothing to say
    about this one.
    """
    from . import envelope as env
    doc = existing if isinstance(existing, dict) and \
        existing.get("snapshot") == snapshot_hash else \
        {"kind": SIDECAR_KIND, "version": 1, "snapshot": snapshot_hash,
         "signatures": []}
    mine = {"key": env.key_text(key.verify_key), "time": now,
            "sig": key.sign(sig_message(snapshot_hash)).signature.hex()}
    others = [s for s in doc.get("signatures") or []
              if (s.get("key"), s.get("time")) != (mine["key"], mine["time"])]
    return {"kind": SIDECAR_KIND, "version": 1, "snapshot": snapshot_hash,
            "signatures": sorted(others + [mine],
                                 key=lambda s: (s["key"], s["time"]))}


def sidecar_keys(doc: dict, snapshot_hash: str) -> tuple[list[str], list[str]]:
    """`(verified keys, complaints)` — every signature §9.4 keeps.

    A signature whose `snapshot` is not this file's hash is DROPPED rather
    than failed: it is a signature about another snapshot that landed in
    the wrong file, and calling it a forgery would be a verdict the bytes
    do not support.
    """
    from nacl import signing as nacl_signing
    from nacl.exceptions import BadSignatureError

    from . import envelope as env
    bad: list[str] = []
    if not isinstance(doc, dict) or doc.get("kind") != SIDECAR_KIND:
        return [], [f"not a {SIDECAR_KIND} (09 §9.4)"]
    if doc.get("snapshot") != snapshot_hash:
        return [], [f"the sidecar covers {str(doc.get('snapshot'))[:23]}… and "
                    f"this snapshot is {snapshot_hash[:23]}…"]
    msg = sig_message(snapshot_hash)
    ok: list[str] = []
    for s in doc.get("signatures") or []:
        try:
            vk = nacl_signing.VerifyKey(env.key_bytes(s["key"]))
            vk.verify(msg, bytes.fromhex(s["sig"]))
            ok.append(s["key"])
        except (BadSignatureError, KeyError, ValueError, TypeError) as e:
            bad.append(f"{str(s.get('key'))[:23]}…: {e or 'bad signature'}")
    return ok, bad


def sign_snapshot(root, name: str, snapshot: "Snapshot", key, *,
                  now: str) -> dict:
    """Write this peer's signature into the snapshot's sidecar.

    "A signature says *I computed this too* (02 §2.2)" — so it is written
    where the snapshot was computed, and merged rather than replaced,
    because a sidecar is a set.
    """
    import json
    from pathlib import Path

    from . import shard as shard_mod
    from .codex import canonical_json
    p = Path(root) / name / "shard" / shard_mod.snapshot_sig_path(snapshot.snapshot_id)
    existing = None
    if p.is_file():
        try:
            existing = json.loads(p.read_bytes())
        except Exception:                               # noqa: BLE001
            existing = None
    doc = sidecar_add(existing, snapshot.hash, key, now=now)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(canonical_json(doc))
    return doc


def _previous(shard_dir, snapshot_id: str):
    """The snapshot before this one in the chain, and its uid → content_hash
    map. `None` when this is the first."""
    from pathlib import Path
    from . import shard as shard_mod
    d = Path(shard_dir) / "snapshots"
    if not d.is_dir():
        return None, None
    ids = sorted(p.stem for p in d.glob("*.json")
                 if not p.name.endswith(".sig.json") and p.stem < snapshot_id)
    if not ids:
        return None, None
    raw = (d / f"{ids[-1]}.json").read_bytes()
    prev = _load(raw)
    return prev, _content_hashes(shard_dir, prev.shards)


def _load(raw: bytes) -> Snapshot:
    import json
    d = json.loads(raw)
    if d.get("kind") != SNAPSHOT_KIND:
        raise CodexError(f"not a snapshot: kind={d.get('kind')!r}")
    if d.get("version") not in SNAPSHOT_VERSIONS_READ:
        raise CodexError(f"unsupported snapshot version {d.get('version')!r}")
    return Snapshot(
        collection_id=d["collection_id"], snapshot_id=d["snapshot_id"],
        previous=d["previous"],
        shards=tuple(Shard(**s) for s in d["shards"]),
        delta=Delta(tuple(d["delta"]["added"]), tuple(d["delta"]["changed"]),
                    tuple(Tombstone(**t) for t in d["delta"]["tombstoned"])),
        relations=tuple(RelationShard(**r) for r in d["relations"]),
        # absent in version 1, which IS version 1 — never null, never a
        # missing key to be guessed at
        figures=tuple(FiguresShard(**f) for f in d.get("figures") or ()),
        files=d["files"],
        version=int(d["version"]))


def load(path) -> Snapshot:
    """A snapshot from its file. A file in canonical form (every file this
    platform writes) is read with its delta left in the file (`LazyDelta`)
    and its hash taken from its bytes; anything else is parsed whole."""
    import mmap
    from pathlib import Path
    p = Path(path)
    with open(p, "rb") as f:
        if p.stat().st_size == 0:
            return _load(b"")
        # mapped, not read: the file's pages are the page cache's, hashed
        # and searched in place, so a 709 MB snapshot costs no 709 MB copy
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            lazy = _load_lazy(p, raw)
            if lazy is not None:
                return lazy
            return _load(raw[:])


def _load_lazy(path, raw) -> Snapshot | None:
    """Canonical JSON sorts its keys, so `delta` follows `collection_id`
    and is followed by `figures` (version 2) or `files` (version 1); its
    span is found by those two markers, which no uid or reason can hold.
    None when the bytes are not canonical — then the caller parses whole."""
    import json
    import hashlib
    head = b'{"collection_id":'
    if raw[:len(head)] != head or raw[-1:] != b"}" or b"\n" in raw[:4096]:
        return None
    i = raw.find(b',"delta":')
    if i < 0:
        return None
    start = i + len(b',"delta":')
    end = -1
    for marker in (b',"figures":', b',"files":'):
        end = raw.find(marker, start)
        if end >= 0:
            break
    if end < 0 or raw[start:start + 1] != b"{" or raw[end - 1:end] != b"}":
        return None
    try:
        d = json.loads(raw[:start] + b"{}" + raw[end:])
    except ValueError:
        return None
    if d.get("kind") != SNAPSHOT_KIND:
        raise CodexError(f"not a snapshot: kind={d.get('kind')!r}")
    if d.get("version") not in SNAPSHOT_VERSIONS_READ:
        raise CodexError(f"unsupported snapshot version {d.get('version')!r}")
    snap = Snapshot(
        collection_id=d["collection_id"], snapshot_id=d["snapshot_id"], previous=d["previous"],
        shards=tuple(Shard(**s) for s in d["shards"]),
        delta=LazyDelta(path, start, end),             # type: ignore[arg-type]
        relations=tuple(RelationShard(**r) for r in d["relations"]),
        figures=tuple(FiguresShard(**f) for f in d.get("figures") or ()),
        files=d["files"], version=int(d["version"]))
    h = hashlib.sha256()
    for at in range(0, len(raw), 1 << 22):
        h.update(raw[at:at + (1 << 22)])
    object.__setattr__(snap, "_raw_hash", "sha256:" + h.hexdigest())
    return snap


def _content_hashes(shard_dir, shards) -> dict[str, str]:
    """uid → content_hash over the shards named, read from the SHARD."""
    from pathlib import Path
    from app.pack import records as pack_records
    out = {}
    for s in shards:
        p = Path(shard_dir) / s.path
        if not p.is_file():
            continue
        for r in pack_records.iter_records(p):
            out[r["uid"]] = r["content_hash"]
    return out


def _relation_shards(shard_dir, shards) -> tuple[RelationShard, ...]:
    """One beside each record shard of the same slice and number, where the
    plugin wrote any (09 §9.14). `()` when the collection has none."""
    from pathlib import Path
    from . import shard as shard_mod
    out = []
    for s in shards:
        p = Path(shard_dir) / shard_mod.relations_path(s.slice, s.n)
        if not p.is_file():
            continue
        raw = p.read_bytes()
        out.append(RelationShard(slice=s.slice, n=s.n,
                                 root=shard_mod.pieces_root(raw),
                                 edges=_count_edges(p), bytes=len(raw)))
    return tuple(out)


def _count_edges(path) -> int:
    from app.pack import records as pack_records
    return sum(1 for _ in pack_records.iter_records(path))


def _figures_shards(shard_dir, codex, shards) -> tuple[FiguresShard, ...]:
    """One beside each record shard of the same slice and number (09 §9.5).
    `()` for a `record_line` 1 collection, which has no plates by
    definition and must not be made to look as though it might.

    A MISSING container is refused, not skipped. The build writes shard and
    container together, so a shard whose records name plates and whose
    container is absent means the two came apart — and a snapshot that
    quietly omitted it would publish records naming plates no peer could
    ever fetch, which is exactly what §9.4's "a file no snapshot lists is
    not served" makes unrecoverable.
    """
    from pathlib import Path
    from . import shard as shard_mod, figures as figures_mod

    if int(getattr(codex, "record_line", 1) or 1) != 2:
        return ()
    out = []
    for s in shards:
        p = Path(shard_dir) / shard_mod.figures_path(s.slice, s.n)
        if not p.is_file():
            # A shard whose every record has `figures: []` — a slice of
            # books published without plates — writes no container, and
            # that is the one absence that is not a fault.
            if _names_no_figures(Path(shard_dir) / s.path):
                continue
            raise CodexError(
                f"{s.slice}/{s.n:05d}: the shard's records name plates and "
                f"{shard_mod.figures_path(s.slice, s.n)} is missing. A "
                f"container is written with its shard (09 §9.5); rebuild "
                f"rather than publish records naming plates nothing holds")
        raw = p.read_bytes()
        out.append(FiguresShard(slice=s.slice, n=s.n,
                                root=shard_mod.pieces_root(raw),
                                figures=len(figures_mod.read_index(p)),
                                bytes=len(raw)))
    return tuple(out)


def _names_no_figures(shard_path) -> bool:
    from pathlib import Path
    from app.pack import records as pack_records
    if not Path(shard_path).is_file():
        return True
    return all(not r.get("figures") for r in pack_records.iter_records(shard_path))


def _seal_lists(shard_dir, snapshot_id: str) -> list[str]:
    """The trusted lists the snapshot's seals name (`seals/lists/<sha256>.xml`):
    they travel in its seals member, so a peer can judge the seals offline
    (09 §9.15, §9.16, amended 2026-10-08). Required once the seals file
    names them — a seals member without its lists is unverifiable."""
    import json
    from . import shard as shard_mod
    p = shard_dir / shard_mod.seals_path(snapshot_id)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_bytes())
    except ValueError:
        return []                       # the seals check reports a member that does not read
    out = set()
    for e in doc.get("entries") or []:
        tl = e.get("trusted_list") or {}
        for k in ("tl_sha256", "lotl_sha256"):
            if isinstance(tl.get(k), str) and tl[k].startswith("sha256:"):
                out.add(shard_mod.trusted_list_path(tl[k]))
    return sorted(out)


def _files(shard_dir, codex, snapshot_id: str | None = None) -> dict:
    """Every whole-collection member with its transport root (09 §9.4).

    ALL FIVE of §9.4's entries, and the reason is 07 §7.8: "`share` refuses
    a SHARD directory holding a file that neither the Codex names, nor a
    snapshot of the chain lists — as a shard or in its `files` (09 §9.4)".
    So a member left out here does not merely go unserved; it makes the
    whole SHARD unshareable. That became live the day `create` started
    writing `calibration/<set>.safetensors`, which is why the two
    calibration entries are here and are not nullable — §9.4 gives "or
    null" to LICENSE-TERMS and to seals, and to nothing else.
    """
    from pathlib import Path
    from . import shard as shard_mod

    member, sig = shard_mod.calibration_paths(codex.space.calibration.set)
    required = [shard_mod.VALIDATION_PATH, member, sig]
    nullable = [shard_mod.TERMS_PATH]
    if snapshot_id is not None:
        nullable.append(shard_mod.seals_path(snapshot_id))
        required += _seal_lists(Path(shard_dir), snapshot_id)

    hint = {shard_mod.VALIDATION_PATH: " — run `hold records validation`"}

    out: dict = {}
    for name in required + nullable:
        p = Path(shard_dir) / name
        if p.is_file():
            raw = p.read_bytes()
            out[name] = {"root": shard_mod.pieces_root(raw), "bytes": len(raw)}
        elif name in nullable:
            out[name] = None            # green: no terms; unsealed: no seal
        else:
            raise CodexError(
                f"{name} is missing from the SHARD and 09 §9.4 lists it in "
                f"every snapshot's `files` without allowing null"
                f"{hint.get(name, '')}. A snapshot that omits it makes the "
                f"SHARD unshareable (07 §7.8), and one that named a file that "
                f"is not there would be worse")
    return out
