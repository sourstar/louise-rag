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
through the DHT when the session has it, and through the peers whose
digests name a `peer_port` for the collection.
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
LANDED_TIMEOUT_S = 60.0          # a file received whole, until the disk holds what was verified
RATE_REFRESH_S = 5.0
RECONNECT_EVERY = 20             # polls between peer-hint connects while waiting for metadata
BACKOFF_S = 15 * 60.0            # a collection nobody served waits this long before the next try
CONTROL_KEY = "share_fetch"      # per collection: the last beat's account
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
               metadata_timeout_s: float | None = None) -> Path:
    """One member file into `staging/<member>/<path>`, whole: added with the
    metadata its snapshot names, this one file on, paced by the lease, credited as it
    lands, stopped on `stop` with staging left for the next beat. The
    download handle leaves the session afterwards; serving is the seeder's,
    from the SHARD, when the collection's file says so."""
    from app.acquire import transport_torrent as tt
    from app.transfer.manager import get_manager
    lt = seeder._lt()
    if metadata_timeout_s is None:
        metadata_timeout_s = METADATA_TIMEOUT_S
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
            while True:
                done = handle.file_progress()[index]
                tt._credit_download(key, handle)
                if done >= total:
                    break
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
                answered = answered or done > 0 or int(getattr(st, "num_peers", 0) or 0) > 0
                if not answered:
                    if time.monotonic() > deadline:
                        raise TransportError(f"{rel}: no metadata after {metadata_timeout_s:.0f}s — "
                                             f"no peer holding it answered")
                    polls += 1
                    if polls % RECONNECT_EVERY == 0:
                        _connect(handle, hints)
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
                  now: float | None = None, max_files: int | None = None) -> dict[str, Any]:
    """One beat for one collection: every missing file of every wanted
    member of the current snapshot, terms first; with no acknowledgement,
    terms alone (04 §4.4). What moved, and why nothing did."""
    root = Path(root)
    shard_dir = col.shard_dir_of(root, name)
    state = root / name / "state"
    staging = state / holdings.STAGING
    out: dict[str, Any] = {"snapshot_id": None, "promoted": [], "refused": [], "errors": [],
                           "waiting": None, "stopped": False, "wanted": 0, "missing": 0}
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
    from app.transfer.manager import Direction, get_manager
    with get_manager().bind(Priority.DELTA, Direction.DOWN, stop=stop):
        for m, f in (todo if max_files is None else todo[:max_files]):
            t = next(t for t in m.torrents() if t.path == f.path)
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
                if "no metadata" in str(e):
                    break                 # nobody serves this collection now: the rest can wait
    return out


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
        if not holdings.missing(shard_dir, want, holdings.marks(con)):
            _complete[c.name] = (selection, now)
            continue                      # the session is opened only for a byte to fetch
        _complete.pop(c.name, None)
        hints = hints_for(node_con, c.codex.collection_id, now=now) if node_con is not None else []
        got = fetch_members(root, c.name, c.codex, slices=c.slices, kinds=c.kinds,
                            ack_missing=ack_missing, session=session_factory(), con=con,
                            hints=hints, stop=stop, log=log, now=now)
        if any("no metadata" in e for e in got["errors"]):
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
        if on_promoted is not None and any(str(p).startswith("records/") for p in got.get("promoted", ())):
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
