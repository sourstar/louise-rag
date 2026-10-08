"""A torrent file is moved into place only once the bytes ON DISK are the
ones libtorrent verified (2026-10-08).

libtorrent 2.x counts a piece in `file_progress` once its hash passed,
and its disk thread writes the blocks after that; the file stands at its
full length meanwhile (`flush_cache` only queues a flush). Moved and
hashed at once, a file can hold a block of zeros the swarm served right:
the digest recorded is wrong, a sidecar check blames the origin, and a
resumed pass records the file as done with a hole in it. The share
transport found it first (app/share/transport.py, `landed_whole`).

What is checked is what libtorrent verified, read back from the disk:
  - the v1 piece hashes, piece by piece, wherever the torrent has them (a
    v1 or hybrid torrent). A piece is our file's bytes, the zeros of pad
    files, and — in a v1 torrent without padding — the bytes of a
    neighbouring file at either end, which this node may not hold at all
    (a file nobody asked for is never written). Those boundary pieces are
    covered by libtorrent's own fence instead: `flush_cache` is a fence job
    on the torrent's storage, run after every write queued before it, and
    its `cache_flushed_alert` says the writes are done;
  - the v2 pieces root otherwise (a v2-only torrent): the whole file,
    hashed a block at a time.
A wait that never comes right is a deferral — a TransportError whose text
the engine reads as transient (`timed out after`) — never an error on a
document, never a verdict on the origin's bytes.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from pathlib import Path

from .spec import TransferAborted, TransportError

#: waiting for the disk, not hashing: the time a check spends reading the
#: file is added to the deadline, so a 50 GB file is not timed out by its size
LANDED_TIMEOUT_S = 60.0
POLL_S = 0.25
#: a flush issued this long ago without its alert was lost (a reader that
#: popped the session's alerts without passing them through `note_alerts`):
#: dropped, so it never holds back the flushes queued behind it
FLUSH_LOST_S = 2 * LANDED_TIMEOUT_S
_READ = 1 << 20


# ---- the flush fence -------------------------------------------------------

_flush_lock = threading.Lock()
#: per torrent: the flushes issued and not yet confirmed, oldest first. A
#: storage runs its fence jobs in order, so the n-th alert for a torrent
#: confirms the n-th flush issued for it
_flushes: dict[str, deque] = {}


def torrent_key(handle) -> str:
    try:
        return str(handle.info_hashes().get_best())
    except Exception:   # noqa: BLE001 — a binding or a fake without info_hashes
        return str(id(handle))


def note_alerts(alerts) -> list:
    """Every alert popped from the process session passes through here, so
    a `cache_flushed_alert` reaches the flush waiting for it whoever popped
    it. Returns the alerts unchanged."""
    for a in alerts:
        if type(a).__name__ != "cache_flushed_alert":
            continue
        try:
            key = torrent_key(a.handle)
        except Exception:   # noqa: BLE001
            continue
        with _flush_lock:
            q = _flushes.get(key)
            if q:
                q.popleft()
    return alerts


def flushed(session, handle, *, until: float, stop=None, poll_s: float = POLL_S) -> bool:
    """Ask libtorrent to flush the torrent and wait, until `until`, for
    the alert saying every write queued before the flush is on disk."""
    from .transport_torrent import pop_alerts
    key = torrent_key(handle)
    token = (object(), time.monotonic())
    with _flush_lock:
        q = _flushes.setdefault(key, deque())
        while q and time.monotonic() - q[0][1] > FLUSH_LOST_S:
            q.popleft()
        q.append(token)
        handle.flush_cache()           # under the lock: issued in the order queued
    while True:
        with _flush_lock:
            if token not in _flushes.get(key, ()):
                return True
        if stop is not None and stop.is_set():
            raise TransferAborted("stopped waiting for the disk")
        if time.monotonic() > until:
            return False
        pop_alerts(session)
        time.sleep(poll_s)


# ---- the bytes on disk -----------------------------------------------------

def v2_root(fs, index: int) -> str | None:
    """`sha256:<hex>` of the file's pieces root, None when the torrent has none."""
    try:
        if not fs.v2():
            return None
        root = fs.root(index)
        if root.is_all_zeros():
            return None
        return "sha256:" + root.to_bytes().hex()
    except Exception:   # noqa: BLE001 — a binding without v2
        return None


def _has_v1(info) -> bool:
    try:
        return bool(info.info_hashes().has_v1())
    except Exception:   # noqa: BLE001 — a binding without info_hashes: v1 is all it knows
        return True


def _is_pad(fs, i: int) -> bool:
    from .transport_torrent import _is_pad
    return _is_pad(fs, i)


def file_pieces(info, index: int) -> range:
    fs = info.files()
    size = fs.file_size(index)
    if size == 0:
        return range(0)
    plen = info.piece_length()
    off = fs.file_offset(index)
    return range(off // plen, (off + size - 1) // plen + 1)


def check_pieces(info, index: int, path: Path, pieces) -> tuple[set[int], set[int]]:
    """`(bad, foreign)` among `pieces`: those whose bytes on disk do not hash
    to the torrent's v1 hash, and those holding a neighbour's bytes that
    are not ours to read."""
    fs = info.files()
    bad: set[int] = set()
    foreign: set[int] = set()
    try:
        f = open(path, "rb")
    except OSError:
        return set(pieces), set()
    with f:
        for p in pieces:
            h = hashlib.sha1()
            ok = True
            for s in info.map_block(p, 0, info.piece_size(p)):
                if s.file_index == index:
                    f.seek(s.offset)
                    left = s.size
                    while left > 0:
                        chunk = f.read(min(left, _READ))
                        if not chunk:
                            ok = False
                            break
                        h.update(chunk)
                        left -= len(chunk)
                elif _is_pad(fs, s.file_index):
                    h.update(bytes(s.size))
                else:
                    foreign.add(p)
                    ok = None
                    break
                if not ok:
                    break
            if ok is False or (ok and h.digest() != bytes(info.hash_for_piece(p))):
                bad.add(p)
    return bad, foreign


def wait_landed(session, handle, info, index: int, path: Path, *, stop=None,
                timeout: float = LANDED_TIMEOUT_S, poll_s: float = POLL_S) -> None:
    """Return once `path` holds the bytes libtorrent verified for file
    `index`; TransportError (a deferral) after `timeout` of waiting."""
    fs = info.files()
    size = fs.file_size(index)
    deadline = time.monotonic() + timeout
    # no flush asked for here: one whose alert nobody waits on would confirm
    # the flush queued behind it for another file of the torrent. libtorrent
    # writes on its own; the checks below are the proof, and `flushed` the
    # fence where no check can be made
    root = None if _has_v1(info) else v2_root(fs, index)
    if not _has_v1(info) and root is None:
        raise TransportError(f"{path.name}: the torrent carries neither v1 hashes nor a v2 root")
    todo = set(file_pieces(info, index))
    while True:
        started = time.monotonic()
        try:
            right_size = path.stat().st_size == size
        except OSError:
            right_size = False
        if root is not None:
            from app.hold import drag as drag_mod
            ok = right_size and drag_mod.pieces_root_of(path) == root
            foreign: set[int] = set()
        else:
            bad, foreign = check_pieces(info, index, path, todo) if right_size else (todo, set())
            todo = bad | foreign
            ok = not bad
        deadline += time.monotonic() - started
        if ok and not foreign:
            return
        if ok and flushed(session, handle, until=deadline, stop=stop, poll_s=poll_s):
            # the fence ran after every write queued before it: the ends this
            # file shares with a neighbour are on disk; the rest just hashed
            return
        if stop is not None and stop.is_set():
            raise TransferAborted(f"stopped waiting for {path.name} to land")
        if time.monotonic() > deadline:
            raise TransportError(
                f"{path.name}: received whole, not yet on disk as verified — "
                f"timed out after {timeout:.0f}s (retried next pass)")
        time.sleep(poll_s)
