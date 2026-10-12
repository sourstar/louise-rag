"""Holdings — which members this peer holds (04 §4.3; 09 §9.3 `holdings`),
migration step 9d, second brick.

- WANTED: what the collection's file asks for (07 §7.8) — every member of
  a kind it names, in a slice it names (`"all"` = every slice) — and the
  whole-collection members always: `terms` before anything else (04 §4.4),
  `validation`, `calibration-<set>`, `seals-<id>`. Text-index members are
  a cache a peer rebuilds, never fetched.
- HELD: a member whose files lie in the SHARD at the snapshot's size. The
  SHARD is what is served and what `roots` checks (03 §3.2); a file that
  got there was verified against its root in staging before the rename,
  or was built here.
- THE MARK: `held_members` in the manifest, written after the rename —
  the durable commit comes first, the mark after (invariant 2). It
  records provenance: which snapshot named the member, its root, when
  it was promoted. A mark whose file is gone is cleared.
- PROMOTION: the file's pieces root computed in staging and compared to
  the snapshot's; equal → one rename into the SHARD (`SHARD.promote`), then
  the mark; otherwise the file is refused and left aside under
  `state/staging/refused/` for a look, never promoted.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from app.hold import shard as shard_mod
from app.hold.shard import MemberFile
from app.share import members as mem

STAGING = "staging"          # under the collection's state/
REFUSED = "refused"          # under staging/: what did not hash to its root
WHOLE_KINDS = ("terms", "validation", "calibration", "seals")
FETCHED_KINDS = ("records", "relations", "figures") + WHOLE_KINDS
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS held_members ("
    " path TEXT PRIMARY KEY, member TEXT NOT NULL, snapshot_id TEXT NOT NULL,"
    " root TEXT NOT NULL, bytes INTEGER NOT NULL, promoted REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_held_members_member ON held_members(member)",
)


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


# ---- names --------------------------------------------------------------------------

def kind_of(member: str) -> str:
    """The kind a member name carries (09 §9.16)."""
    if member in ("terms", "validation"):
        return member
    for kind in ("records", "relations", "figures", "vectors", "buckets", "bundle",
                 "centres", "calibration", "seals", "origin", "index"):
        if member.startswith(kind + "-"):
            return kind
    raise mem.MemberError(f"not a member name of 09 §9.16: {member!r}")


def slice_of(member: str) -> str | None:
    """The slice of a per-slice member; None for a whole-collection one."""
    kind = kind_of(member)
    if kind == "index":
        return None                       # `index-<space>`: the vector index, no slice
    if kind in ("records", "relations", "figures", "origin"):
        return member[len(kind) + 1:]
    if kind == "vectors":
        rest = member[len("vectors-"):]
        return rest.split("-", 1)[1] if "-" in rest else None
    return None


# ---- wanted, held, missing ----------------------------------------------------------

def wanted(snapshot, *, slices, kinds) -> list[mem.Member]:
    """The members the collection's file asks for, terms first."""
    kinds = tuple(kinds or ())
    out: list[mem.Member] = []
    for m in mem.members_of(snapshot):
        kind = kind_of(m.name)
        if kind in WHOLE_KINDS:
            out.append(m)
            continue
        if kind not in kinds or kind not in FETCHED_KINDS:
            continue
        sl = slice_of(m.name)
        if slices == "all" or (sl is not None and sl in tuple(slices or ())):
            out.append(m)
    out.sort(key=lambda m: (0 if m.name == "terms" else 1 if kind_of(m.name) in WHOLE_KINDS else 2,
                            m.name.encode("utf-8")))
    return out


def file_held(shard_dir: Path, f: MemberFile, marks: dict | None = None) -> bool:
    """The file is there at the snapshot's size and, where this peer marked
    it when it promoted it, under the snapshot's root. Size alone was the
    test until 2026-10-12, so a re-issued part that kept its size (an edit
    that changed no length) would never have been fetched."""
    p = Path(shard_dir) / f.path
    try:
        if not (p.is_file() and p.stat().st_size == f.bytes):
            return False
    except OSError:
        return False
    m = (marks or {}).get(f.path)
    return m is None or m.get("root") == f.root


def held(shard_dir: Path, member: mem.Member) -> bool:
    return all(file_held(shard_dir, f) for f in member.files)


def missing(shard_dir: Path, members, marks: dict | None = None) -> list[tuple[mem.Member, MemberFile]]:
    """Every file of every wanted member that is not in the SHARD at the
    snapshot's version (`file_held`)."""
    return [(m, f) for m in members for f in m.files if not file_held(shard_dir, f, marks)]


# ---- the mark -------------------------------------------------------------------------

def marks(con) -> dict[str, dict[str, Any]]:
    ensure_schema(con)
    return {r["path"]: dict(r) for r in con.execute("SELECT * FROM held_members").fetchall()}


def mark(con, *, snapshot_id: str, member: str, f: MemberFile, now: float | None = None) -> None:
    ensure_schema(con)
    con.execute(
        "INSERT INTO held_members(path, member, snapshot_id, root, bytes, promoted) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET member=excluded.member, "
        "snapshot_id=excluded.snapshot_id, root=excluded.root, bytes=excluded.bytes, "
        "promoted=excluded.promoted",
        (f.path, member, snapshot_id, f.root, f.bytes, time.time() if now is None else now))


def unmark(con, path: str) -> None:
    ensure_schema(con)
    con.execute("DELETE FROM held_members WHERE path=?", (path,))


def sweep_marks(con, shard_dir: Path) -> list[str]:
    """Marks whose file is gone are cleared; the paths cleared."""
    gone = [p for p in marks(con) if not (Path(shard_dir) / p).is_file()]
    for p in gone:
        unmark(con, p)
    return gone


# ---- promotion ----------------------------------------------------------------------

class Refused(shard_mod.ShardError):
    """A staged file that does not hash to the snapshot's root."""


def promote(con, staged: Path, shard_dir: Path, *, snapshot_id: str, member: str,
            f: MemberFile, now: float | None = None, allowed_roots: set[str] | None = None) -> Path:
    """Verify, rename, mark (04 §4.3). The staged file's pieces root must be
    the snapshot's; then one rename into the SHARD, then the mark.
    `allowed_roots` is 03 §3.8's manifest-only gate for a certified
    collection: the roots listed by a held snapshot a certifier key signed
    (app/hold/anchor.py `signed_roots`); a file whose root is not among
    them is set aside, whatever snapshot names it."""
    staged, shard_dir = Path(staged), Path(shard_dir)
    got_size = staged.stat().st_size
    if got_size != f.bytes:
        _set_aside(staged)
        raise Refused(f"{f.path}: {got_size} bytes staged, the snapshot says {f.bytes}; set aside")
    got = shard_mod.pieces_root_of(staged)
    if got != f.root:
        _set_aside(staged)
        raise Refused(f"{f.path}: pieces root {got[:23]}…, the snapshot says {f.root[:23]}…; "
                      f"set aside, not promoted (04 §4.3)")
    if allowed_roots is not None and got not in allowed_roots:
        _set_aside(staged)
        raise Refused(f"{f.path}: root {got[:23]}… is not listed by any snapshot of the chain a certifier "
                      f"key signed; set aside, not promoted — manifest-only (03 §3.8)")
    target = shard_dir / f.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shard_mod.promote(staged, target, shard_dir=shard_dir, new_root=got)
    mark(con, snapshot_id=snapshot_id, member=member, f=f, now=now)
    return target


def _set_aside(staged: Path) -> None:
    """Under staging/refused/, keeping the member's layout; the next fetch
    starts the file afresh."""
    staging = staged
    while staging.name != STAGING and staging.parent != staging:
        staging = staging.parent
    if staging.name != STAGING:
        staged.unlink(missing_ok=True)
        return
    aside = staging / REFUSED / staged.relative_to(staging)
    aside.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(aside)


# ---- what show lists ----------------------------------------------------------------

def summary(shard_dir: Path, snapshot) -> dict[str, Any]:
    """09 §9.3's `holdings`: `{slices, kinds, members}` — what the SHARD holds
    of the snapshot's members, by presence."""
    if snapshot is None:
        return {"slices": [], "kinds": [], "members": 0}
    slices: set[str] = set()
    kinds: set[str] = set()
    n = 0
    for m in mem.members_of(snapshot, shard_dir=shard_dir):
        if not held(shard_dir, m):
            continue
        n += 1
        kinds.add(kind_of(m.name))
        sl = slice_of(m.name)
        if sl is not None:
            slices.add(sl)
    return {"slices": sorted(slices, key=lambda s: s.encode("utf-8")), "kinds": sorted(kinds),
            "members": n}
