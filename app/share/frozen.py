"""A frozen collection's sharing (04 §4.3, amended 2026-11-07; operator: "you
can still share (if you check the shared box) the parts that are still
aligned with the non frozen state"; docs/notes/collection-freeze.md §3).

A frozen collection keeps the version it holds and takes nothing in. Shared,
it offers only what is ALIGNED:

- a FILE a snapshot lists — a record, relations or figures part, a
  whole-collection file — is aligned when its torrent is that file's torrent
  in the newest snapshot this node has read. 09 §9.16 makes a torrent a
  function of the member, the SHARD-relative path, the length and the pieces
  root alone, and the member follows from the path, so the test is
  (path, root, bytes) equal;
- a BUNDLE is aligned when the record part it covers is (its directory is
  `state/bundles/<space>/<slice>/<NNNNN>/<id>`, 09 §9.7).

"The newest snapshot read" is the node's own, or a newer one a live
neighbour's digest lists, which `track` pulls from that neighbour — `pull
snapshot` with its chain, verified as a fetched snapshot is — and keeps
ASIDE under `<collection>/state/frozen/snapshots/`, never in the SHARD and
never promoted: the copy does not move. Until a newer one announced is read,
the last one read applies — a digest naming a snapshot nobody hands over
changes nothing (no defence may need to know whom to trust, 08). With no
newer snapshot read, everything held is aligned.

Pure but for `track` (the pull) and the aside directory's reads.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.hold import collection as col, snapshot as sn
from app.hold.codex import CodexError, canonical_json

#: under the collection's state/: what this node read to know what is aligned
DIR = "frozen"
SNAPSHOTS = "snapshots"
#: a newer snapshot a neighbour lists and would not hand over is asked for
#: again after this long, not every beat (as `accept.DIGEST_RETRY_S`)
RETRY_S = 15 * 60.0
#: (collection id, snapshot id) -> when its aside pull last failed
_tried: dict[tuple[str, str], float] = {}


def frozen_names(con=None) -> set[str]:
    """The frozen set, or the empty set when the node's store cannot be read
    — then nothing is filtered by freeze here; the worker's own gates read
    the same store and fail the same way."""
    from app import node_state
    try:
        return node_state.frozen_collections(con)
    except Exception:   # noqa: BLE001 — an unreadable store freezes nothing new
        return set()


# ---- the aside reference ---------------------------------------------------------------

def aside_dir(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / DIR / SNAPSHOTS


def _newest_file(root: Path, name: str) -> Path | None:
    d = aside_dir(root, name)
    if not d.is_dir():
        return None
    ids = sorted(p.name[:-5] for p in d.glob("*.json") if not p.name.endswith(".sig.json"))
    return d / f"{ids[-1]}.json" if ids else None


def reference_stamp(root: Path, name: str) -> str | None:
    """The newest aside snapshot's identity — id, size, mtime — without
    reading it; None when none is held. What the seeder compares, a stat a
    beat, before it loads anything."""
    p = _newest_file(root, name)
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    return f"{p.name[:-5]}:{st.st_size}:{st.st_mtime_ns}"


def reference(root: Path, name: str) -> sn.Snapshot | None:
    """The newest snapshot read aside, its parts' container the SHARD's own
    Codex's (`container_beside` of a path in the SHARD); None when none is,
    or when it cannot be read."""
    p = _newest_file(root, name)
    if p is None:
        return None
    container = sn.container_beside(col.shard_dir_of(Path(root), name) / "snapshots" / p.name)
    try:
        return sn.load(p, container=container)
    except (CodexError, OSError, ValueError, KeyError, TypeError):
        return None


def clear(root: Path, name: str) -> bool:
    """Unfrozen: what was read aside goes — the normal beat now takes the
    snapshot into the copy. Whether there was anything."""
    d = Path(root) / name / "state" / DIR
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# ---- alignment -------------------------------------------------------------------------

def _files_of(snapshot: sn.Snapshot) -> list[tuple[str, str, int]]:
    """Every file the snapshot lists, as (path, root, bytes): its record,
    relations and figures parts and its whole-collection files."""
    out = [(s.path, str(s.root), int(s.bytes)) for s in snapshot.shards]
    out += [(r.path, str(r.root), int(r.bytes)) for r in snapshot.relations]
    out += [(f.path, str(f.root), int(f.bytes)) for f in snapshot.figures]
    for path, entry in (snapshot.files or {}).items():
        if isinstance(entry, dict):
            out.append((path, str(entry.get("root")), int(entry.get("bytes") or 0)))
    return out


@dataclass(frozen=True)
class Alignment:
    """What of `own` is aligned with `newest_id`'s snapshot. `stamp` names
    the comparison for a republish test; `everything` is True when no newer
    snapshot was read — then nothing is filtered."""
    own_id: str
    newest_id: str
    newest_hash: str
    files: frozenset[tuple[str, str, int]]
    parts: frozenset[tuple[str, int]]          # own's record parts aligned, (slice, n)
    held: int                                  # files own lists
    everything: bool

    @property
    def stamp(self) -> str:
        return f"{self.newest_id}:{self.newest_hash}"

    def file(self, path: str, root: str, length: int) -> bool:
        return self.everything or (path, str(root), int(length)) in self.files

    def part(self, slice_: str, n: int) -> bool:
        return self.everything or (slice_, int(n)) in self.parts

    def shard(self, shard) -> bool:
        """A snapshot's part entry (`sn.Shard`), for the publisher."""
        return self.part(shard.slice, shard.n)

    def member(self, m) -> bool:
        """A member (app/share/members.Member) offered whole: a bundle when
        its part is aligned, a member a snapshot lists when every file of it
        is, any other (a vector index) as before."""
        name = str(getattr(m, "name", ""))
        if name.startswith("bundle-"):
            return self.bundle_dir(getattr(m, "base", None))
        if not getattr(m, "listed_by_a_snapshot", False):
            return True
        return all(self.file(f.path, f.root, f.bytes) for f in m.files)

    def bundle_dir(self, base) -> bool:
        """`state/bundles/<space>/<slice>/<NNNNN>/<id>` → its part's alignment."""
        if self.everything:
            return True
        if base is None:
            return False
        try:
            p = Path(base)
            return self.part(p.parent.parent.name, int(p.parent.name))
        except (TypeError, ValueError):
            return False

    @property
    def any_aligned(self) -> bool:
        return self.everything or bool(self.files)


def alignment(own: sn.Snapshot, newest: sn.Snapshot | None) -> Alignment:
    """Pure: `own`'s files against `newest`'s. A `newest` that is not newer
    than `own` (or none, or another collection's) aligns everything."""
    own_files = _files_of(own)
    if newest is None or newest.collection_id != own.collection_id \
            or newest.snapshot_id <= own.snapshot_id:
        return Alignment(own.snapshot_id, own.snapshot_id, own.hash, frozenset(own_files),
                         frozenset((s.slice, int(s.n)) for s in own.shards), len(own_files), True)
    theirs = set(_files_of(newest))
    files = frozenset(f for f in own_files if f in theirs)
    parts = frozenset((s.slice, int(s.n)) for s in own.shards
                      if (s.path, str(s.root), int(s.bytes)) in theirs)
    return Alignment(own.snapshot_id, newest.snapshot_id, newest.hash, files, parts, len(own_files), False)


def alignment_for(root: Path, collection, own: sn.Snapshot | None, *,
                  frozen: Iterable[str] | None = None) -> Alignment | None:
    """The alignment the collection's sharing obeys, or None when it is not
    frozen (or holds no snapshot): then nothing here filters anything."""
    name = getattr(collection, "name", None)
    if own is None or name is None:
        return None
    names = set(frozen) if frozen is not None else frozen_names()
    if name not in names:
        return None
    return alignment(own, reference(root, name))


def offered(al: Alignment | None, members: Sequence) -> list:
    """The members a digest lists: under freeze, the aligned ones whole."""
    if al is None:
        return list(members)
    return [m for m in members if al.member(m)]


# ---- reading the newer snapshot, aside ----------------------------------------------------

def _announced(node_con, codex, *, now: float) -> tuple[str, str, Any] | None:
    """`(snapshot id, hash, source)` of the newest snapshot of THIS collection
    a live neighbour's digest lists; None when none does."""
    from app.share import fetch as fetch_mod
    best = None
    for s in fetch_mod.sources(node_con, codex.collection_id, now=now):
        e = s.entry or {}
        sid, h = e.get("snapshot_id"), e.get("snapshot_hash")
        if e.get("collection_id") != codex.collection_id or not s.key \
                or not isinstance(sid, str) or not isinstance(h, str):
            continue
        if best is None or sid > best[0]:
            best = (sid, h, s)
    return best


def track(root: Path, collections, node_con, *, client=None, key=None, own_key: str | None = None,
          stop=None, now: float | None = None,
          log: Callable[[str], None] | None = None) -> dict[str, int]:
    """For each FROZEN collection given: the newest snapshot a live
    neighbour's digest lists, when it is newer than both the copy's own and
    the one read aside, pulled from that neighbour — the snapshot and its
    chain down to what this node holds (`fetch.pull_snapshots`) — and kept
    aside when its hash is the one the digest advertised and a signature on
    it verifies. Nothing enters the SHARD; no member follows. A pull that
    fails waits RETRY_S. Counts: read, failed, waiting."""
    from app.share import fetch as fetch_mod, seeder
    root = Path(root)
    now = time.time() if now is None else now
    out = {"read": 0, "failed": 0, "waiting": 0}
    for c in collections:
        if stop is not None and stop.is_set():
            break
        codex = getattr(c, "codex", None)
        if codex is None:
            continue
        own = seeder.current_snapshot(col.shard_dir_of(root, c.name))
        got = _announced(node_con, codex, now=now)
        if got is None:
            continue
        sid, advertised, source = got
        if own is not None and sid <= own.snapshot_id:
            continue
        ref = _newest_file(root, c.name)
        if ref is not None and ref.name[:-5] >= sid:
            continue                                   # read already
        if now - _tried.get((codex.collection_id, sid), -RETRY_S) < RETRY_S:
            out["waiting"] += 1
            continue
        try:
            if key is None or own_key is None:
                from app import identity
                from app.bundles import keys as node_keys
                key, own_key = node_keys.load_signing_key(), identity.own()
            p = fetch_mod.Puller(key=key, own_key=own_key, client=client, stop=stop, con=node_con,
                                 log=log, now=now)
            pulled = fetch_mod.pull_snapshots(p, source, codex, snapshot_id=sid,
                                              held=fetch_mod.held_snapshots(root, codex.collection_id))
            head = pulled[0]
            if head.snapshot.hash != advertised:
                p.lie(source, f"snapshot {sid} hashes otherwise than the digest advertised (09 §9.9)")
                raise fetch_mod.FetchError(f"snapshot {sid} hashes {head.snapshot.hash[:23]}…, the digest "
                                           f"advertised {advertised[:23]}…")
            if not head.signed:
                raise fetch_mod.FetchError(f"snapshot {sid} carries no verified signature: "
                                           f"{'; '.join(head.complaints)}")
        except Exception as e:   # noqa: BLE001 — a pull is a deferral, never the beat's error
            _tried[(codex.collection_id, sid)] = now
            out["failed"] += 1
            if log:
                log(f"[{c.name}] frozen: snapshot {sid}, listed by {source.address}, not read "
                    f"({type(e).__name__}: {e}); what is aligned is judged against the last one "
                    f"read; asked again in {RETRY_S / 60:.0f} min"[:500])
            continue
        _tried.pop((codex.collection_id, sid), None)
        d = aside_dir(root, c.name)
        d.mkdir(parents=True, exist_ok=True)
        _write_whole(d / f"{sid}.json", head.data)
        if head.sidecar is not None:
            _write_whole(d / f"{sid}.sig.json", canonical_json(head.sidecar))
        for old in d.glob("*.json"):
            if not old.name.startswith(f"{sid}."):
                old.unlink(missing_ok=True)            # only the newest read is kept
        out["read"] += 1
        if log:
            al = alignment(own, head.snapshot) if own is not None else None
            tail = (f"{len(al.files)} of {al.held} file(s) held are still current and stay shared"
                    if al is not None else "nothing held yet")
            log(f"[{c.name}] frozen: snapshot {sid} read from {source.address}, kept aside "
                f"(the copy stays at {own.snapshot_id if own else 'none'}); {tail}")
    return out


def _write_whole(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.part")
    tmp.write_bytes(data)
    tmp.replace(target)


def summary(root: Path, collection, own: sn.Snapshot | None) -> dict[str, Any] | None:
    """For a display: what a frozen collection shares — the snapshot held,
    the newest read, and how many of its files are aligned; None when it is
    not frozen."""
    al = alignment_for(root, collection, own)
    if al is None:
        return None
    return {"snapshot_id": al.own_id, "newest_read": None if al.everything else al.newest_id,
            "aligned_files": al.held if al.everything else len(al.files), "files": al.held}

