"""`as_of` (05 §5.1; 07 §7.3's `--as-of DATE`): a search that reads, per
collection, the newest snapshot of the chain whose cutoff is at or before
a date, instead of the newest held — *multivigenza*: what did the record
say then, not what does it say now. Migration step 11.

What this peer can answer from its one index — built from the newest
snapshot's records — is exactly the records whose content did not change
between the snapshot asked for and the newest: same `content_hash`, same
bytes, same chunks, so a hit on one of them is a hit on the as-of state.
A record added, changed or tombstoned by any snapshot after the one asked
for is NOT answered from a later state (05 §5.1: "a wrong date is worse
than no answer"): its hits are dropped, and the as-of snapshot's shards
that hold such records are named in `withheld`, whether this peer holds
them or not — held, they are not indexed for search; not held, they are
not here at all. The chain between the two snapshots must be held whole,
or the deltas are unknown and the collection is withheld.

The date is `YYYY-MM-DD`, read at 00:00:00 in the collection's
`origin.clock`; the cutoff of a snapshot is 00:00:00 on `update.day` of
the period after the one it names (02 §2.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.hold import snapshot as sn
from app.hold.codex import CodexError

MAX_CHANGED = 500_000      # uids changed since the as-of snapshot that a request will walk


class AsOfError(CodexError):
    exit_code = 2


class TooFarBack(CodexError):
    """More has changed since that snapshot than a request walks (exit 3)."""
    exit_code = 3


def parse_date(text) -> date:
    """`YYYY-MM-DD`, nothing shorter (07 §7.3)."""
    if not isinstance(text, str) or len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise AsOfError(f"as_of is a date YYYY-MM-DD, got {text!r} (07 §7.3)")
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise AsOfError(f"as_of {text!r}: {e}") from None


def target_id(codex, day: date) -> str:
    """The id of the newest snapshot of this chain whose cutoff is at or
    before 00:00:00 of `day` in the origin's clock."""
    cadence, update_day = codex.update.cadence, codex.update.day
    instant = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(codex.origin.clock))
    # the period that closed most recently closed at 00:00 on `update.day`:
    # before that day of the month (week), the latest cutoff is the one before
    if cadence == "monthly" and instant.day < update_day:
        instant = instant.replace(day=1) - timedelta(days=1)
    elif cadence == "weekly" and instant.isoweekday() < update_day:
        instant = instant - timedelta(days=7)
    return sn.snapshot_id_for(instant, cadence)


@dataclass
class Resolved:
    snapshot: sn.Snapshot | None          # the snapshot that answers; None when withheld whole
    current: sn.Snapshot | None           # the newest held
    changed: set[str] = field(default_factory=set)   # uids whose state differs between the two
    shards: list[str] = field(default_factory=list)  # the as-of snapshot's shards holding them, "<slice>/<n>"
    held: int = 0                                     # how many of those shards' files this peer has
    reason: str | None = None             # withheld whole: why

    @property
    def same_as_current(self) -> bool:
        return self.snapshot is not None and self.current is not None \
            and self.snapshot.hash == self.current.hash


def held_ids(shard_dir: Path) -> list[str]:
    d = Path(shard_dir) / "snapshots"
    if not d.is_dir():
        return []
    return sorted(p.name[:-5] for p in d.glob("*.json") if not p.name.endswith(".sig.json"))


def _load(shard_dir: Path, snapshot_id: str) -> sn.Snapshot:
    return sn.load(Path(shard_dir) / "snapshots" / f"{snapshot_id}.json")


def _holds(path: Path, uid: str) -> bool:
    from app.pack import records as pack_records
    try:
        return uid in pack_records.cached_index(path).uids
    except Exception:   # noqa: BLE001 — a shard that does not read: named, not trusted
        return True


def resolve(codex, shard_dir: Path, day: date) -> Resolved:
    """Which held snapshot answers for `day`, and what it cannot answer."""
    target = target_id(codex, day)
    ids = held_ids(shard_dir)
    if not ids:
        return Resolved(None, None, reason="no snapshot is held")
    current = _load(shard_dir, ids[-1])
    before = [i for i in ids if i <= target]       # one cadence per chain: text order is time order
    if not before:
        return Resolved(None, current, reason=f"no snapshot of this chain has a cutoff at or before "
                                               f"{day.isoformat()}; the first held is {ids[0]}")
    chosen = _load(shard_dir, before[-1])
    if chosen.hash == current.hash:
        return Resolved(chosen, current)
    changed: set[str] = set()
    previous = chosen
    for sid in ids:
        if sid <= chosen.snapshot_id:
            continue
        s = _load(shard_dir, sid)
        if s.previous != previous.hash:
            return Resolved(None, current, reason=f"the chain between {chosen.snapshot_id} and "
                                                   f"{current.snapshot_id} is not held whole here: "
                                                   f"{sid} does not follow {previous.snapshot_id}")
        changed.update(s.delta.added)
        changed.update(s.delta.changed)
        changed.update(t.uid for t in s.delta.tombstoned)
        if len(changed) > MAX_CHANGED:
            raise TooFarBack(f"more than {MAX_CHANGED} records changed since snapshot {chosen.snapshot_id}: "
                             f"too many to answer as of {day.isoformat()} from the current index")
        previous = s
    shards: set[str] = set()
    order = (getattr(codex, "slicing", None) or {}).get("order", "bytes")
    for uid in changed:
        native = uid.split(":", 1)[1] if ":" in uid else uid
        for s in chosen.shards_of(native, order):
            # one candidate per slice: a held shard says whether it holds
            # the record; one not held is named, since it may
            if (Path(shard_dir) / s.path).is_file() and not _holds(Path(shard_dir) / s.path, uid):
                continue
            shards.add(f"{s.slice}/{s.n}")
    by_key = {f"{s.slice}/{s.n}": s for s in chosen.shards}
    held = sum(1 for k in shards if (Path(shard_dir) / by_key[k].path).is_file())
    return Resolved(chosen, current, changed, sorted(shards), held)


def withheld_entry(collection, codex, r: Resolved, day: date) -> dict:
    """05 §5.3's `withheld` entry for what the as-of search leaves out."""
    if r.snapshot is None:
        return {"collection": collection.name, "collection_id": codex.collection_id, "shards": "all",
                "reason": f"as of {day.isoformat()}: {r.reason}"}
    return {"collection": collection.name, "collection_id": codex.collection_id, "shards": list(r.shards),
            "reason": (f"as of {day.isoformat()}: {len(r.changed)} records of snapshot {r.snapshot.snapshot_id} "
                       f"changed in a later snapshot and are not answered from it; {len(r.shards)} shards of "
                       f"{r.snapshot.snapshot_id} hold them ({r.held} held here, not indexed for search; "
                       f"{len(r.shards) - r.held} not held)")}
