"""Members over the wire (04 §4.3; 02 §2.6; 09 §9.16), migration step 9d,
second brick: every file of every wanted member this peer lacks, fetched
by its torrent — the infohash computed from the snapshot alone, so no one
is asked what to fetch — into `state/staging/`, verified against its root
and promoted into the SHARD (app/share/holdings.py).

Run by the WORKER in a thread of its own (`loop`), on the session it
already runs for seeding, under the budget: the DOWN bucket, DELTA class
for the current snapshot's members (04 §4.3's classes), a rate lease
handed to libtorrent as its ceiling and the bytes that landed credited
after the fact, as for every transport that cannot be metered per byte
(invariant 5). A stop turns the file off and leaves staging as it is: the
next beat resumes from the pieces staging holds (07 §7.7). The ack gate
(04 §4.4): until this peer's acknowledgement exists, the only member
fetched of a yellow or red collection is `terms`. The swarm is reached
through the peers whose digests name a `peer_port` for the collection, and,
when the session has the DHT, through those announced under the
snapshot's swarm key (04 §4.2) — never by a member's own infohash, which
no seeder announces.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app.acquire.spec import TransferAborted as AcquireAborted, TransportError
from app.hold import collection as col
from app.share import holdings, members as mem, peers, seeder
from app.transfer.bucket import Priority, TransferAborted

POLL_S = 0.5
METADATA_TIMEOUT_S = 120.0       # as the acquisition transport's: no peer holding it answered
#: a swarm that answered — a peer connected, or bytes came — and then sent
#: nothing for this long is stalled: the Perugia peer waited fifteen minutes
#: and more on a part the node did not serve, its one connection to the
#: node's swarm counting as "answered" and the metadata bound never firing
#: (2026-10-18 21:53; p2p-review-open-issues.md §6)
STALL_TIMEOUT_S = 300.0
LANDED_TIMEOUT_S = 60.0          # a file received whole, until the disk holds what was verified
RATE_REFRESH_S = 5.0
RECONNECT_EVERY = 20             # polls between peer-hint connects while waiting for metadata
BACKOFF_S = 15 * 60.0            # a collection nobody served waits this long before the next try
CONTROL_KEY = "share_fetch"      # per collection: the last beat's account
#: (collection_id, snapshot_id) -> (the swarms found under its swarm key, when)
_swarms: dict[tuple[str, str], tuple[list[tuple[str, int]], float]] = {}


#: the words a fetch that found nobody ends in (`fetch_file`), read by the
#: beats that back off on them
NOBODY_SERVED = ("no metadata", "stalled")


def nobody_served(text: str) -> bool:
    """Whether a transport error says that nobody served the file — the
    metadata wait ran out, or the swarm answered and then sent nothing."""
    return any(w in str(text) for w in NOBODY_SERVED)


def offered_by_neighbours(node_con, collection_id: str, *, now: float) -> set[tuple[str, str]] | None:
    """`{(member name, member root)}` over every live neighbour's digest
    entry for the collection (09 §9.9 `members`: what each one SERVES), or
    None when no neighbour lists the collection at all — then nothing is
    known about who serves what, and a fetch is bounded by its timeouts
    alone. A wanted member in no neighbour's list is deferred and said, not
    waited on (04 §4.3, amended 2026-10-19)."""
    listed = False
    out: set[tuple[str, str]] = set()
    for n in peers.neighbours(node_con, now=now):
        d = peers.digest_of(node_con, n["key"]) or {}
        for e in d.get("collections", []) or []:
            if not isinstance(e, dict) or e.get("collection_id") != collection_id:
                continue
            listed = True
            for m in e.get("members", []) or []:
                if isinstance(m, dict) and isinstance(m.get("name"), str) and isinstance(m.get("root"), str):
                    out.add((m["name"], m["root"]))
    return out if listed else None


def _has_dht(session) -> bool:
    try:
        return bool(session.get_settings().get("enable_dht"))
    except Exception:   # noqa: BLE001 — a session that cannot say has none to probe
        return False


def swarm_hints(session, collection_id: str, snapshot_id: str, *, now: float) -> list[tuple[str, int]]:
    """The swarms announced under the snapshot's swarm key, probed at most
    once per announcement interval — the probe waits up to the DHT's bound,
    and a beat that found nobody does not wait again until announcements
    could have changed."""
    if session is None or not _has_dht(session):
        return []
    held = _swarms.get((collection_id, snapshot_id))
    if held is not None and held[1] + seeder.ANNOUNCE_EVERY_S > now:
        return held[0]
    from app.share import fetch
    found = fetch.find_swarm(session, collection_id, snapshot_id)
    _swarms[(collection_id, snapshot_id)] = (found, now)
    return found
BEAT_S = 15.0


def magnet(torrent: mem.Torrent) -> str:
    return f"magnet:?xt=urn:btmh:1220{torrent.infohash[7:]}"


def hints_for(node_con, collection_id: str, *, now: float) -> list[tuple[str, int]]:
    """`(host, peer_port)` of every neighbour whose digest lists the collection
    and names the port its swarm listens on — and, when none lists it, of
    every neighbour that names a port at all. A digest is hourly: a
    collection shared after the last one is in no digest yet, and the
    second peer, its DHT off, waited for the next hour beside the node that
    served it (2026-10-07). Asking a swarm that does not hold the torrent
    costs one connection, refused."""
    from urllib.parse import urlsplit
    listing: list[tuple[str, int]] = []
    naming: list[tuple[str, int]] = []
    for n in peers.neighbours(node_con, now=now):
        d = peers.digest_of(node_con, n["key"]) or {}
        port = d.get("peer_port")
        if not isinstance(port, int) or isinstance(port, bool):
            continue
        host = urlsplit(n["address"]).hostname
        if not host:
            continue
        lists = any(isinstance(e, dict) and e.get("collection_id") == collection_id
                    for e in d.get("collections", []))
        for out in ((listing, naming) if lists else (naming,)):
            if (host, port) not in out:
                out.append((host, port))
    return listing or naming


def newest_hint_digest(node_con, collection_id: str, *, now: float) -> float:
    """When the newest digest naming a swarm for the collection arrived (0.0
    when none did): a digest after a failed attempt is news even from the
    same peer — it restarted, it now serves (2026-10-07: the node restarted
    with the fix and the second peer's backoff held for its address)."""
    newest = 0.0
    for n in peers.neighbours(node_con, now=now):
        d = peers.digest_of(node_con, n["key"]) or {}
        if not isinstance(d.get("peer_port"), int):
            continue
        if any(isinstance(e, dict) and e.get("collection_id") == collection_id for e in d.get("collections", [])):
            row = node_con.execute("SELECT last_digest FROM peers WHERE key=?", (n["key"],)).fetchone()
            if row and row[0]:
                newest = max(newest, float(row[0]))
    return newest


def _locate(info, member: str, path: str) -> tuple[int, str]:
    """`(torrent index, the path libtorrent stores the file under)` for the
    member's file `path` — `<member>/<path>`, or `<member>` alone when the
    tree held one file at its root (libtorrent's single-file layout)."""
    from app.acquire import transport_torrent as tt
    want = (f"{member}/{path}", member, path)
    for index, name, _ in tt.content_files(info):
        if name in want or name.endswith("/" + path):
            return index, name
    raise TransportError(f"{member}/{path}: no such file in the torrent")


def landed_whole(path: Path, length: int, root: str) -> bool:
    """Whether the file on disk IS the one the torrent names: its length
    and its pieces root. Never the length alone — libtorrent sizes a file
    before every block of it is written, so a staged file at its full
    length can still hold holes (2026-10-08)."""
    from app.hold import shard as shard_mod
    try:
        if path.stat().st_size != length:
            return False
        return shard_mod.pieces_root_of(path) == root
    except OSError:
        return False


def _connect(handle, hints) -> None:
    for h in hints:
        try:
            handle.connect_peer(h)
        except Exception:   # noqa: BLE001 — a hint that cannot be dialled is no hint
            continue


def fetch_file(session, *, torrent: mem.Torrent, member: str, staging: Path, hints=(),
               stop=None, log: Callable[[str], None] | None = None, poll_s: float = POLL_S,
               metadata_timeout_s: float | None = None, stall_timeout_s: float | None = None) -> Path:
    """One member file into `staging/<member>/<path>`, whole: added with the
    metadata its snapshot names, this one file on, paced by the lease, credited as it
    lands, stopped on `stop` with staging left for the next beat. The
    download handle leaves the session afterwards; serving is the seeder's,
    from the SHARD, when the collection's file says so.

    Two bounds, never none: no peer answers within `metadata_timeout_s`
    ("no metadata"), or the swarm answered and no byte landed for
    `stall_timeout_s` ("stalled") — a connected peer that does not hold
    the torrent is an answer with nothing in it, and until 2026-10-19 it
    turned the wait unbounded."""
    from app.acquire import transport_torrent as tt
    from app.transfer.manager import get_manager
    lt = seeder._lt()
    if metadata_timeout_s is None:
        metadata_timeout_s = METADATA_TIMEOUT_S
    if stall_timeout_s is None:
        stall_timeout_s = STALL_TIMEOUT_S
    rel = f"{member}/{torrent.path}"
    key = f"share:{torrent.infohash}"
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    # ADDED WITH ITS METADATA, this one file at priority 4 and every other at
    # 0, paused and not auto-managed until the lease is its ceiling: the
    # info dict is the snapshot's own (the infohash was computed from it),
    # so nothing is asked of the swarm before the first byte is wanted. Added
    # as a magnet, the auto-manager started it, and its metadata arrived with
    # every file wanted, ahead of the priorities and the lease (2026-10-08).
    info = lt.torrent_info(lt.bdecode(mem.bencode({"info": torrent.info})))
    # libtorrent lays a file tree of ONE file at the tree's root out as a
    # single-file torrent named after the file — `validation.json` alone,
    # not `validation/validation.json` — while a nested path keeps
    # `<name>/<path>`; the file is found by either spelling and staged
    # where libtorrent puts it (the caller takes the path returned)
    index, rel = _locate(info, member, torrent.path)
    fs = info.files()
    total = fs.file_size(index)
    priorities = [0] * fs.num_files()
    priorities[index] = 4
    params = lt.add_torrent_params()
    params.ti = info
    params.save_path = str(staging)
    params.file_priorities = priorities
    try:
        params.flags |= lt.torrent_flags.paused
        params.flags &= ~lt.torrent_flags.auto_managed
    except Exception:   # noqa: BLE001 — older bindings name flags differently
        pass
    handle = None
    try:
        with get_manager().rsync_lease(None) as leased_bps:
            handle = session.add_torrent(params)
            tt._apply_rate(handle, leased_bps)
            last_refresh = time.monotonic()
            handle.resume()
            _connect(handle, hints)
            # no peer holding it answered: bounded as the metadata wait was,
            # in the words the beat's backoff reads ("no metadata")
            deadline = time.monotonic() + metadata_timeout_s
            answered = False
            polls = 0
            last_done, progressed_at = 0, time.monotonic()
            while True:
                done = handle.file_progress()[index]
                tt._credit_download(key, handle)
                if done >= total:
                    break
                if done > last_done:
                    last_done, progressed_at = done, time.monotonic()
                st = handle.status()
                if stop is not None and stop.is_set():
                    # the file off before leaving, so nothing lands outside the lease
                    priorities[index] = 0
                    handle.prioritize_files(priorities)
                    tt._credit_download(key, handle)
                    raise TransferAborted(f"stopped with {done}/{total} bytes of {rel}")
                error = getattr(st, "errc", None)
                if error is not None and getattr(error, "value", lambda: 0)():
                    raise TransportError(f"{rel}: {error.message()}")
                num_peers = int(getattr(st, "num_peers", 0) or 0)
                answered = answered or done > 0 or num_peers > 0
                if not answered:
                    if time.monotonic() > deadline:
                        raise TransportError(f"{rel}: no metadata after {metadata_timeout_s:.0f}s — "
                                             f"no peer holding it answered")
                    polls += 1
                    if polls % RECONNECT_EVERY == 0:
                        _connect(handle, hints)
                elif time.monotonic() - progressed_at > stall_timeout_s:
                    # a peer connected and nothing came: it does not hold this
                    # torrent, or stopped serving it. Staging keeps the pieces
                    # for the next beat; the wait ends here
                    raise TransportError(f"{rel}: stalled — {done}/{total} bytes and nothing for "
                                         f"{stall_timeout_s:.0f}s from {num_peers} peer(s); nobody "
                                         f"serving it answered")
                now = time.monotonic()
                if now - last_refresh > RATE_REFRESH_S:
                    tt._apply_rate(handle, leased_bps)
                    last_refresh = now
                time.sleep(poll_s)
        # `file_progress` counts pieces received, not pieces WRITTEN: the disk
        # thread is behind it, and a small file's one piece was not on disk
        # when the first promotion looked (2026-10-07). Nor is the SIZE the
        # sign: libtorrent sizes the file before its last blocks are written
        # (`flush_cache` only queues the flush), so a file at its full length
        # still held zeros where a block was due, and the promotion refused
        # bytes the swarm had served right — the survivor test's calibration
        # member, 2026-10-08. What libtorrent verified is the torrent's root;
        # the wait is for the file on disk to hash to it.
        landed = staging / rel
        try:
            handle.flush_cache()
        except Exception:   # noqa: BLE001 — older bindings; the wait below still holds
            pass
        deadline = time.monotonic() + LANDED_TIMEOUT_S
        while not landed_whole(landed, torrent.length, torrent.root):
            if time.monotonic() > deadline:
                raise TransportError(f"{rel}: received whole and not on disk as received after "
                                     f"{LANDED_TIMEOUT_S:.0f}s")
            time.sleep(poll_s)
        return landed
    finally:
        if handle is not None:
            try:
                session.remove_torrent(handle)
            except Exception:   # noqa: BLE001 — a handle already gone
                pass


def fetch_members(root: Path, name: str, codex, *, slices, kinds, ack_missing: bool, session,
                  con, hints=(), stop=None, log: Callable[[str], None] | None = None,
                  now: float | None = None, max_files: int | None = None,
                  patcher: Callable[..., Path | None] | None = None,
                  offered: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """One beat for one collection: every missing file of every wanted
    member of the current snapshot, terms first; with no acknowledgement,
    terms alone (04 §4.4). What moved, and why nothing did.

    `offered` is what the neighbours' digests serve for the collection
    (`offered_by_neighbours`): a wanted member in it is fetched; one in no
    digest, when the swarm key found no swarm either, is DEFERRED and said
    — `deferred` lists its files — not waited on (04 §4.3, amended
    2026-10-19: the node served two slices and a peer wanting every slice
    hung on the first part nobody served). None: nothing is known, every
    member is tried under the bounds."""
    root = Path(root)
    shard_dir = col.shard_dir_of(root, name)
    state = root / name / "state"
    staging = state / holdings.STAGING
    out: dict[str, Any] = {"snapshot_id": None, "promoted": [], "refused": [], "errors": [],
                           "waiting": None, "stopped": False, "wanted": 0, "missing": 0,
                           "patched": [], "deferred": []}
    snap = seeder.current_snapshot(shard_dir)
    if snap is None:
        return out
    out["snapshot_id"] = snap.snapshot_id
    want = holdings.wanted(snap, slices=slices, kinds=kinds)
    out["wanted"] = len(want)
    if ack_missing and any(m.name != "terms" for m in want):
        want = [m for m in want if m.name == "terms"]
        out["waiting"] = "acknowledgement: `hold collection ack` first (04 §4.4)"
    holdings.sweep_marks(con, shard_dir)
    todo = holdings.missing(shard_dir, want, holdings.marks(con))
    out["missing"] = len(todo)
    if not todo:
        return out
    need = sum(f.bytes for _, f in todo)
    staging.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(staging).free
    if need > free:                       # 04 §4.3: sizes are known before the first byte
        out["waiting"] = f"space: {need} bytes wanted, {free} free"
        if log:
            log(f"[{name}] share fetch: waiting for space — {need} bytes wanted, {free} free")
        return out
    # 03 §3.8: in a certified collection a file enters the copy only if its
    # root is listed by a snapshot a certifier key signed — computed once
    # per beat from the chain held, before anything is fetched
    allowed = None
    if getattr(codex, "publication", "open") == "certified":
        from app.hold import anchor as anchor_mod
        allowed = anchor_mod.signed_roots(root, name, codex)
        if not allowed:
            out["waiting"] = "manifest-only: no held snapshot of the chain carries a certifier's signature (03 §3.8)"
            if log:
                log(f"[{name}] share fetch: {out['waiting']}")
            return out
    hints = list(hints)
    swarms = swarm_hints(session, snap.collection_id, snap.snapshot_id,
                         now=time.time() if now is None else now)
    for h in swarms:
        if h not in hints:
            hints.append(h)
    deferred_members: set[str] = set()
    from app.transfer.manager import Direction, get_manager
    with get_manager().bind(Priority.DELTA, Direction.DOWN, stop=stop):
        for m, f in (todo if max_files is None else todo[:max_files]):
            if offered is not None and not swarms and (m.name, m.root) not in offered:
                # no neighbour's digest offers this member and no swarm was
                # found under the swarm key: nobody is known to serve it.
                # Deferred — the next digest may offer it — never waited on
                out["deferred"].append(f.path)
                if m.name not in deferred_members:
                    deferred_members.add(m.name)
                    if log:
                        log(f"[{name}] share fetch: {m.name}: no neighbour offers it; deferred, not "
                            f"waited on (04 §4.3)")
                continue
            t = next(t for t in m.torrents() if t.path == f.path)
            # A NEW VERSION OF A PART THIS PEER HOLDS: first a patch from a
            # peer that holds it (app/share/patch.py) — the changed lines,
            # not the part; the root decides as for a download, and a patch
            # that misses costs the torrent below and nothing else
            if patcher is not None and (shard_dir / f.path).is_file():
                try:
                    staged = patcher(f, staging / f.path, snap.snapshot_id)
                except Exception as e:   # noqa: BLE001 — a patch is an optimisation
                    staged = None
                    if log:
                        log(f"[{name}] share fetch: patch for {f.path}: {type(e).__name__}"[:300])
                if staged is not None:
                    try:
                        holdings.promote(con, staged, shard_dir, snapshot_id=snap.snapshot_id,
                                         member=m.name, f=f, now=now, allowed_roots=allowed)
                        out["promoted"].append(f.path)
                        out["patched"].append(f.path)
                        if log:
                            log(f"[{name}] share fetch: {f.path} rebuilt from a patch "
                                f"({f.bytes} bytes not downloaded)")
                        continue
                    except holdings.Refused as e:
                        if log:
                            log(f"[{name}] share fetch: a patch for {f.path} missed the root ({e}); "
                                f"fetching the part")
            try:
                staged = fetch_file(session, torrent=t, member=m.name, staging=staging,
                                    hints=hints, stop=stop, log=log)
                holdings.promote(con, staged, shard_dir, snapshot_id=snap.snapshot_id,
                                 member=m.name, f=f, now=now, allowed_roots=allowed)
                out["promoted"].append(f.path)
                if log:
                    log(f"[{name}] share fetch: {f.path} ({f.bytes} bytes) promoted")
            except holdings.Refused as e:
                out["refused"].append(str(e))
                if log:
                    log(f"[{name}] share fetch: refused — {e}")
            except (TransferAborted, AcquireAborted):
                out["stopped"] = True
                break
            except (TransportError, OSError) as e:
                out["errors"].append(f"{f.path}: {e}"[:300])
                if log:
                    log(f"[{name}] share fetch: {f.path}: {e}"[:400])
                if nobody_served(str(e)):
                    break                 # nobody serves this collection now: the rest can wait
    if out["deferred"] and not out["promoted"] and not out["errors"] and out["waiting"] is None:
        out["waiting"] = (f"{len(out['deferred'])} file(s) of {len(deferred_members)} member(s) no "
                          f"neighbour offers: deferred until a digest offers them (04 §4.3)")
    return out


def _patcher(root: Path, c, shard_dir: Path, node_con, *, stop=None, now: float,
             log: Callable[[str], None] | None = None):
    """`(file, out, snapshot_id) -> rebuilt path | None` for one collection:
    a record part's or a plates container's new version from a patch
    (app/share/patch.py), asked of the neighbours whose digest lists the
    collection. None — every file is fetched whole — without a peer
    database or the node's key. A record part whose snapshot entry names
    another `writer` than this platform's is not asked for: its rebuild
    could not be sure to match."""
    if node_con is None:
        return None
    try:
        from app import identity
        from app.bundles import keys as node_keys
        key, own = node_keys.load_signing_key(), identity.own()
    except Exception:   # noqa: BLE001 — no key: no signed pull, the torrent alone
        return None
    from app.pack import records as pack_records
    from app.share import fetch as fetch_mod, patch as patch_mod
    puller = fetch_mod.Puller(key=key, own_key=own, stop=stop, con=node_con, log=log, now=now)

    writers: dict[str, dict[str, str]] = {}

    def writer_of(path: str, snapshot_id: str) -> str:
        if snapshot_id not in writers:
            snap = seeder.current_snapshot(shard_dir)
            writers.clear()
            writers[snapshot_id] = {s.path: s.writer for s in snap.shards}                 if snap is not None and snap.snapshot_id == snapshot_id else {}
        return writers[snapshot_id].get(path, "")

    def patcher(f, out: Path, snapshot_id: str) -> Path | None:
        part, kind = patch_mod.part_of(f.path), "records"
        if part is None:
            part, kind = patch_mod.figures_part_of(f.path), "figures"
        if part is None:
            return None
        if kind == "records" and writer_of(f.path, snapshot_id) not in ("", pack_records.WRITER):
            return None
        sources = fetch_mod.sources(node_con, c.codex.collection_id, now=now)
        return patch_mod.ask(puller, sources, c.codex.collection_id, snapshot_id, part,
                             shard_dir / f.path, out, kind=kind, log=log)
    return patcher


# ---- the worker's thread -----------------------------------------------------------------

_next_try: dict[str, float] = {}
#: per collection: the swarm addresses the backed-off attempt was given. A
#: hint outside them — a digest naming the swarm arrived meanwhile — ends
#: the backoff: the second peer's first attempt ran before the node's digest
#: and then waited fifteen minutes beside a swarm that answered in one
#: second (2026-10-07)
_tried_hints: dict[str, tuple[frozenset, float]] = {}
#: per collection: the (snapshot file, selection) found complete, and when —
#: a beat costs one directory listing per collection, never one stat per
#: shard of a corpus that has nothing to fetch (CLAUDE.md: a per-round cost
#: scales with the work available, never with the size of the corpus)
_complete: dict[str, tuple[tuple, float]] = {}
RECHECK_S = 3600.0


def _snapshot_key(shard_dir: Path) -> tuple | None:
    """The newest snapshot file's identity — (id, mtime, size) — without
    reading it; None when the SHARD has none."""
    d = Path(shard_dir) / "snapshots"
    if not d.is_dir():
        return None
    ids = sorted(p.name[:-5] for p in d.glob("*.json") if not p.name.endswith(".sig.json"))
    if not ids:
        return None
    try:
        st = (d / f"{ids[-1]}.json").stat()
    except OSError:
        return None
    return (ids[-1], st.st_mtime_ns, st.st_size)


def wants_fetching(collection) -> bool:
    """A Codex collection whose file asks for slices — `"all"` or a list —
    and is not a deposit collection (never-share: nothing to fetch)."""
    codex = getattr(collection, "codex", None)
    if codex is None or getattr(collection, "slices", None) in (None, ()):
        return False
    from app.hold import deposit
    return not deposit.is_deposit(codex)


def beat(collections, cons: dict, node_con, *, worker_state: str, session_factory,
         root: Path | None = None, stop=None, now: float | None = None,
         log: Callable[[str], None] | None = None,
         on_promoted: Callable[[str], None] | None = None) -> dict[str, Any]:
    """One beat over every collection that wants members: skipped when the
    mode does not fetch, when the collection is in its backoff after a
    beat nobody served, or when nothing is missing."""
    from app import collections as collections_mod, db, licensing, node_state
    now = time.time() if now is None else now
    out: dict[str, Any] = {}
    if not node_state.runs(worker_state, node_state.FETCH):
        return out
    # a collection the operator paused takes nothing in — no member, no
    # bundle, no withdrawal or expiry: "runs nothing for it" (07 §7.8).
    # München, 2026-10-20: the paused, emptied gutenberg was refilled by
    # twelve "taken again" bundles between the re-pin's carry and rename
    try:
        paused = node_state.paused_collections(node_con) if node_con is not None else node_state.paused_collections()
    except Exception:   # noqa: BLE001 — unreadable: nothing is taken this beat
        return out
    if paused:
        collections = [c for c in collections if getattr(c, "name", None) not in paused]
    root = Path(root) if root is not None else collections_mod.louise_root() / "collections"
    for c in collections:
        if not wants_fetching(c):
            continue
        if _next_try.get(c.name, 0.0) > now:
            tried, tried_at = _tried_hints.get(c.name, (frozenset(), now))
            fresh = set(hints_for(node_con, c.codex.collection_id, now=now)) if node_con is not None else set()
            heard = newest_hint_digest(node_con, c.codex.collection_id, now=now) if node_con is not None else 0.0
            if not fresh - tried and heard <= tried_at:
                continue
        con = cons.get(c.name)
        if con is None:
            continue
        try:
            ack_missing = licensing.state_for(c) == licensing.MISSING
        except Exception:   # noqa: BLE001 — a gate error withholds, never fetches
            ack_missing = True
        shard_dir = col.shard_dir_of(root, c.name)
        skey = _snapshot_key(shard_dir)
        if skey is None:
            continue
        selection = (skey, c.slices if c.slices == "all" else tuple(c.slices or ()),
                     tuple(c.kinds or ()), ack_missing)
        done = _complete.get(c.name)
        if done is not None and done[0] == selection and now - done[1] < RECHECK_S:
            continue                      # complete at this snapshot and selection: nothing to stat
        snap = seeder.current_snapshot(shard_dir)
        if snap is None:
            continue
        want = holdings.wanted(snap, slices=c.slices, kinds=c.kinds)
        if ack_missing:
            want = [m for m in want if m.name == "terms"]
        todo = holdings.missing(shard_dir, want, holdings.marks(con))
        if not todo:
            _complete[c.name] = (selection, now)
            continue                      # the session is opened only for a byte to fetch
        _complete.pop(c.name, None)
        hints = hints_for(node_con, c.codex.collection_id, now=now) if node_con is not None else []
        offered = offered_by_neighbours(node_con, c.codex.collection_id, now=now) if node_con is not None else None
        try:
            # the beat's account is written when it STARTS as well as when it
            # ends: a beat that never returned left the previous one's row
            # standing, and nothing said it was stuck (Perugia, 2026-10-18)
            db.set_control(con, CONTROL_KEY, json.dumps({"at": now, "running": True, "wanted": len(want),
                                                         "missing": len(todo), "hints": len(hints),
                                                         "offered": None if offered is None else len(offered)}))
        except Exception:   # noqa: BLE001 — the account is a courtesy
            pass
        got = fetch_members(root, c.name, c.codex, slices=c.slices, kinds=c.kinds,
                            ack_missing=ack_missing, session=session_factory(), con=con,
                            hints=hints, stop=stop, log=log, now=now,
                            patcher=_patcher(root, c, shard_dir, node_con, stop=stop, now=now, log=log),
                            offered=offered)
        nobody = any(nobody_served(e) for e in got["errors"]) or bool(
            got.get("deferred") and not got["promoted"] and not got["errors"] and not got["stopped"])
        if nobody:
            # nobody serves what is wanted: wait for news — a digest naming a
            # swarm or offering the members — before asking again
            _next_try[c.name] = now + BACKOFF_S
            _tried_hints[c.name] = (frozenset(hints), now)
        else:
            _next_try.pop(c.name, None)
            _tried_hints.pop(c.name, None)
        try:
            db.set_control(con, CONTROL_KEY, json.dumps({"at": now, **got}))
        except Exception:   # noqa: BLE001 — the account is a courtesy
            pass
        out[c.name] = got
        landed = [p for p in got.get("promoted", ()) if str(p).startswith("records/")]
        if landed:
            # 03 §3.6 test 1 (2026-10-20): the copy's own roots, records and
            # chunks over the parts that just landed, attested — a taken
            # copy otherwise sat in quarantine until someone typed `hold
            # check` (Perugia, gutenberg-book-editions, 2.5 h)
            _attest_landed(root, c, snap, landed, log=log)
        if on_promoted is not None and landed:
            # 9f: a fetched collection reads its shards (app/plugins/shard.py),
            # and the shards that just landed wait for no six-hour interval
            # — the second peer held its records with an empty manifest
            # until a restart (2026-10-07)
            on_promoted(c.name)
        if stop is not None and stop.is_set():
            break
    # the bundles other peers announced (04 §4.5), taken in through the ladder
    if node_con is not None and not (stop is not None and stop.is_set()):
        from app.share import accept
        try:
            # bundles, flags and forgeries are about vectors: an annotations
            # collection has none (02 §2.10)
            vectored = [c for c in collections
                        if getattr(getattr(c, "codex", None), "annotates", None) is None]
            taken = accept.beat(vectored, cons, node_con, session_factory=session_factory,
                                root=root, stop=stop, now=now, log=log)
            if any(taken.get(k) for k in ("accepted", "refused", "ignored", "held")):
                out["bundles"] = taken
        except Exception as e:   # noqa: BLE001 — one beat, not the thread
            if log:
                log(f"[warn:share] bundles: {type(e).__name__}: {e}"[:400])
    return out


def _attest_landed(root: Path, c, snap, landed: list, *, log: Callable[[str], None] | None = None) -> dict | None:
    """`check.attest_landed` for one beat's record parts, with this peer's
    key and image where it has them; one log line; never the beat's error."""
    from app.hold import check as chk, equivalence
    key = None
    try:
        from app.bundles import keys as node_keys
        key = node_keys.load_signing_key()
    except Exception:   # noqa: BLE001 — no key: checked, not attested
        key = None
    try:
        got = chk.attest_landed(root, c.name, c.codex, snap, landed, key=key,
                                platform_image=equivalence.running_image())
    except Exception as e:   # noqa: BLE001 — the parts are held; `hold check` runs the same by hand
        if log:
            log(f"[{c.name}] share fetch: checks over the landed part(s) failed to run — "
                f"{type(e).__name__}: {e}"[:400])
        return None
    if log:
        verdicts = ", ".join(f"{k} {v.upper()}" for k, v in got["results"].items())
        tail = f"attested {len(got['attestations'])}" if got["attestations"] else got["why"]
        log(f"[{c.name}] share fetch: {len(got['parts'])} part(s) landed — {verdicts}; {tail}")
    return got


def loop(stop, *, beat_s: float = BEAT_S, log: Callable[[str], None] = print,
         on_promoted: Callable[[str], None] | None = None) -> None:
    """The worker's fetch thread (07 §7.7: resumable per file, stoppable
    between polls). Per-collection connections are this thread's own."""
    from app import collections as collections_mod, db, node_state
    from app.acquire import transport_torrent as tt
    cons: dict[str, Any] = {}
    node_con = None
    while not stop.wait(beat_s):
        try:
            collections = [c for c in collections_mod.load_all() if getattr(c, "codex", None) is not None]
            # an annotations collection is fetched like any (02 §2.10)
            collections += collections_mod.load_annotations()
            live = {c.name for c in collections}
            for stale in [n for n in cons if n not in live]:
                try:
                    cons.pop(stale).close()
                except Exception:   # noqa: BLE001
                    cons.pop(stale, None)
            for c in collections:
                if c.name not in cons:
                    cons[c.name] = db.connect(c)
            if node_con is None:
                node_con = node_state.connect()
            beat(collections, cons, node_con, worker_state=node_state.worker_state(node_con),
                 session_factory=tt.get_session, stop=stop, log=log, on_promoted=on_promoted)
        except Exception as e:   # noqa: BLE001 — the thread outlives any one beat
            try:
                log(f"[warn:share] fetch beat: {type(e).__name__}: {e}"[:400])
            except Exception:   # noqa: BLE001
                pass
    for c_con in cons.values():
        try:
            c_con.close()
        except Exception:   # noqa: BLE001
            pass
