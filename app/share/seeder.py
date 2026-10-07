"""Serving a collection's members from the drag (04 §4.3; 09 §9.16) —
migration step 9b.

The worker serves and announces every member of the current snapshot of
every public collection it holds IN USE whose file says `share = true`
(07 §7.8; 03 §3.6: a copy in quarantine is not announced and not served),
through the seeder it already runs for published corpora
(app/acquire/seeding.py). One torrent per member file, laid out as
libtorrent wants it — `state/share/<snapshot>/<member>/<path>`, a hard
link to the drag's file (the drag is immutable once promoted) — and built
by libtorrent from the file; it is REFUSED when the infohash libtorrent
computes differs from the one 09 §9.16's info dictionary gives for the
snapshot's (path, length, root): a file that is not what the snapshot says
it is is not served. Every served snapshot is announced under its
collection key and its lineage key (04 §4.2) through the session's DHT.

A `hold` command never opens a port (07 §7.4): `share <collection>` only
writes the desired state, `share = true` in the collection's file, and
the worker does the rest at its next beat.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app import db
from app.hold import drag, snapshot as sn
from app.hold.codex import lineage_id as _lineage_id
from app.share import members as mem

SHARE_DIR = "share"                   # under the collection's state/
TORRENTS_DIR = "torrents"
CONTROL_KEY = "share_served"          # per collection: what the worker publishes now
ANNOUNCE_EVERY_S = 15 * 60            # a DHT announcement lives about that long
_last_announce: dict[bytes, float] = {}


def current_snapshot(drag_dir: Path) -> sn.Snapshot | None:
    """The newest snapshot in the drag's chain, by id — `YYYY-MM` and
    `YYYY-Www` each sort as text within their cadence (02 §2.2)."""
    d = Path(drag_dir) / "snapshots"
    if not d.is_dir():
        return None
    ids = sorted(p.name[:-5] for p in d.glob("*.json") if not p.name.endswith(".sig.json"))
    return sn.load(d / f"{ids[-1]}.json") if ids else None


def _lt():
    from app.acquire.transport_torrent import _lt
    return _lt()


def _place(share_root: Path, snapshot_id: str, member: str, rel_path: str,
           target: Path) -> Path:
    """`<state>/share/<snapshot>/<member>/<rel_path>`, a hard link to the
    drag's file (a symbolic link when the two cannot share one)."""
    link = share_root / snapshot_id / member / rel_path
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        os.link(target, link)
    except OSError:
        link.symlink_to(target.resolve())
    return share_root / snapshot_id / member


def build_torrent(member_dir: Path, torrent: mem.Torrent, out_dir: Path) -> Path | None:
    """libtorrent's torrent of the member's one file, written beside the
    others; None when its infohash is not the one the snapshot's (path,
    length, root) give — the file is not what the snapshot says."""
    lt = _lt()
    fs = lt.file_storage()
    # this ONE file, by its path under the member's directory: a member of
    # several files is that many torrents (09 §9.16), all laid out under
    # one `<share>/<snapshot>/<member>/` and served from one save path
    # the file's REAL size, not the snapshot's: a file grown or cut since
    # hashes to another infohash and is refused below like a changed byte
    fs.add_file(f"{member_dir.name}/{torrent.path}",
                (member_dir / torrent.path).stat().st_size)
    t = lt.create_torrent(fs, mem.PIECE_LENGTH, flags=lt.create_torrent_flags_t.v2_only)
    lt.set_piece_hashes(t, str(member_dir.parent))
    gen = t.generate()
    theirs = str(lt.torrent_info(gen).info_hashes().v2)
    if theirs != torrent.infohash[7:]:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{theirs}.torrent"
    out.write_bytes(lt.bencode(gen))
    return out


def publish(collection, snapshot: sn.Snapshot, drag_dir: Path, state_dir: Path, con,
            log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Every member of `snapshot` whose files the drag holds, as the
    entries the worker's seeder serves (app/acquire/seeding.py
    `record_published`'s shape, plus the member, its path, the snapshot).
    The list REPLACES what an earlier snapshot published, and the earlier
    torrents' files go, so nothing of a past snapshot is started again."""
    from app.acquire import seeding
    share_root = Path(state_dir) / SHARE_DIR
    torrents_dir = share_root / TORRENTS_DIR
    entries: list[dict[str, Any]] = []
    refused: list[str] = []
    members = list(mem.members_of(snapshot, drag_dir=drag_dir))
    codex = getattr(collection, "codex", None)
    if codex is not None:
        # the bundles this peer holds for the space (09 §9.7, §9.16): served
        # from state/bundles/, never from the drag
        members += mem.bundle_members(Path(state_dir), codex.space_segment)
    for member in members:
        base = Path(member.base) if member.base is not None else Path(drag_dir)
        if not all((base / f.path).is_file() for f in member.files):
            continue                                     # a slice this copy does not hold
        for t in member.torrents():
            member_dir = _place(share_root, snapshot.snapshot_id, member.name, t.path,
                                base / t.path)
            torrent_path = build_torrent(member_dir, t, torrents_dir)
            if torrent_path is None:
                refused.append(f"{member.name}: {t.path}")
                continue
            # libtorrent reads a one-file tree at its root — `validation`,
            # `terms`, each file of a bundle — as a SINGLE-FILE torrent named
            # after the file, and a seed serves it from `<save path>/<file>`;
            # a nested path stays `<save path>/<member>/<path>`. The seeder
            # takes the save path as the parent of `root`, so a flat file's
            # root is the file itself and a nested one's the member directory
            # (found 2026-10-07: a fetcher got the metadata and no piece ever)
            root = member_dir / t.path if "/" not in t.path else member_dir
            entries.append({
                "name": member.name, "torrent_path": str(torrent_path),
                "root": str(root), "info_hash": t.infohash[7:],
                "magnet": f"magnet:?xt=urn:btmh:1220{t.infohash[7:]}",
                "files": 1, "bytes": t.length, "member": member.name, "path": t.path,
                "member_root": member.root, "snapshot_id": snapshot.snapshot_id,
                "collection_id": snapshot.collection_id,
            })
    keep = {e["torrent_path"] for e in entries}
    for old in torrents_dir.glob("*.torrent") if torrents_dir.is_dir() else []:
        if str(old) not in keep:
            old.unlink()
    for old in share_root.iterdir() if share_root.is_dir() else []:
        if old.is_dir() and old.name not in (TORRENTS_DIR, snapshot.snapshot_id):
            shutil.rmtree(old, ignore_errors=True)
    db.set_control(con, seeding.TORRENT_PUBLISHED_KEY, json.dumps(entries))
    db.set_control(con, CONTROL_KEY, json.dumps({
        "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash,
        "torrents": len(entries), "refused": refused, "at": time.time()}))
    if log is not None:
        log(f"[{collection.name}] share: {len(entries)} torrent(s) of snapshot "
            f"{snapshot.snapshot_id} served" + (f"; {len(refused)} file(s) refused, not "
                                                 f"what the snapshot says: {refused}" if refused else ""))
    return entries


def served(con) -> dict[str, Any] | None:
    raw = db.get_control(con, CONTROL_KEY)
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def keys_for(codex, snapshot: sn.Snapshot, resolve) -> list[bytes]:
    """The collection key and the lineage key of a served snapshot (04 §4.2)."""
    return [mem.collection_key(codex.collection_id, snapshot.snapshot_id),
            mem.lineage_key(_lineage_id(codex, resolve), snapshot.snapshot_id)]


def announce(session, keys: list[bytes], port: int, *, now: float | None = None) -> int:
    """Each key announced through the DHT, once per ANNOUNCE_EVERY_S; how
    many were announced now. A session with the DHT off announces nothing."""
    now = time.time() if now is None else now
    lt = _lt()
    n = 0
    for k in keys:
        if _last_announce.get(k, 0.0) + ANNOUNCE_EVERY_S > now:
            continue
        session.dht_announce(lt.sha1_hash(k), port, 0)
        _last_announce[k] = now
        n += 1
    return n


_last_pointer_put: dict[str, float] = {}


def publish_pointer(collection, session, *, now: float | None = None, key=None, own_key: str | None = None,
                    log: Callable[[str], None] | None = None) -> bool:
    """03 §3.7, 09 §9.11: a certified collection whose certifier is THIS
    peer has the pointer `hold collection certify` wrote put into the DHT
    under its key, once per ANNOUNCE_EVERY_S — a mutable item lives about
    as long as an announcement. Whether a put was made."""
    from app.hold import certify
    from app.share import pointer as pointer_mod
    codex = getattr(collection, "codex", None)
    if session is None or codex is None or codex.publication != "certified" or codex.certifier is None:
        return False
    now = time.time() if now is None else now
    if _last_pointer_put.get(codex.collection_id, 0.0) + ANNOUNCE_EVERY_S > now:
        return False
    if own_key is None:
        try:
            from app import identity
            own_key = identity.own()
        except Exception:   # noqa: BLE001 — no key: not the certifier
            return False
    if own_key != codex.certifier.key:
        return False
    from app import collections as collections_mod
    root = collections_mod.louise_root() / "collections"
    doc = certify.read_pointer(root, collection.name, codex)
    if doc is None:
        return False
    if key is None:
        from app.bundles import keys as node_keys
        key = node_keys.load_signing_key()
    # the attempt is what waits the interval, not the success: a put that
    # failed was retried and logged every beat, every 10 s (2026-10-07)
    _last_pointer_put[codex.collection_id] = now
    pointer_mod.put(session, codex, doc, key)
    if log:
        log(f"[{collection.name}] share: pointer seq {doc['body']['seq']} → snapshot "
            f"{doc['body']['snapshot_id']} put into the DHT under the certifier's key (09 §9.11)")
    return True


def unpublish(collection, state_dir: Path, con, log=None) -> int:
    """Nothing of this collection served: its torrents dropped from the
    seeder, its entries and tree removed. How many torrents were dropped."""
    from app.acquire import seeding
    entries = seeding.read_published(con)
    mine = [e for e in entries if e.get("member")]
    dropped = seeding.get_seeder().drop([e["info_hash"] for e in mine])
    db.set_control(con, seeding.TORRENT_PUBLISHED_KEY,
                   json.dumps([e for e in entries if not e.get("member")]))
    db.set_control(con, CONTROL_KEY, json.dumps({"snapshot_id": None, "torrents": 0,
                                                 "at": time.time()}))
    shutil.rmtree(Path(state_dir) / SHARE_DIR, ignore_errors=True)
    if log is not None and mine:
        log(f"[{collection.name}] share: stopped; {dropped} torrent(s) dropped")
    return dropped


def wants_serving(collection) -> bool:
    """`share = true` in its file, a public Codex, and in use on this peer
    (03 §3.6; 04 §4.3)."""
    from app import copy_state
    codex = getattr(collection, "codex", None)
    if codex is None or not getattr(collection, "share", False):
        return False
    if getattr(codex, "privacy", "public") != "public":
        return False
    return copy_state.of(collection).in_use


def sync(collections, cons: dict, control_con, worker_state: str, *,
         session=None, port: int | None = None, now: float | None = None,
         log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """One beat of the worker's seeding thread, before app/acquire/seeding
    ticks: publish the current snapshot of every collection that wants
    serving and is not yet published at that snapshot; unpublish one that
    stopped wanting it; announce the served keys when the mode seeds."""
    from app import collections as collections_mod, node_state
    from app.hold import collection as col
    root = collections_mod.louise_root() / "collections"
    out: dict[str, Any] = {"published": [], "unpublished": [], "announced": 0, "pointers": 0}
    seeds = node_state.runs(worker_state, node_state.SEED)
    for c in collections:
        con = cons.get(c.name)
        if con is None:
            continue
        state_dir = root / c.name / "state"
        if not wants_serving(c):
            if served(con) and served(con).get("snapshot_id"):
                unpublish(c, state_dir, con, log)
                out["unpublished"].append(c.name)
            continue
        drag_dir = col.drag_of(root, c.name)
        snap = current_snapshot(drag_dir)
        if snap is None:
            continue
        now_served = served(con)
        if now_served is None or now_served.get("snapshot_id") != snap.snapshot_id:
            publish(c, snap, drag_dir, state_dir, con, log)
            out["published"].append(c.name)
        if seeds and session is not None and port:
            keys = keys_for(c.codex, snap, col.resolver(root))
            out["announced"] += announce(session, keys, port, now=now)
            try:
                out["pointers"] += int(publish_pointer(c, session, now=now, log=log))
            except Exception as e:   # noqa: BLE001 — a put that fails never stops the beat
                if log:
                    log(f"[{c.name}] share: pointer not put: {type(e).__name__}: {e}"[:300])
    return out
