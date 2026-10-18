"""Free space under a collection's vector store: below the floor, nothing
writes vectors (operator, 2026-10-18: "when free space is over refuse further
embeddings and signal the issue").

Vectors are the one store that grows without anyone asking: the embed stage,
the archive import and the bundle import each add rows, and Lance needs room
of its own to commit, prune and compact. A pool that fills stops SQLite and
Lance mid-write — every collection on it, not only the one embedding. So the
writers stop first, while there is still room to stop cleanly.

THE FLOOR is the larger of `VECTORS_MIN_FREE_GB` and `VECTORS_MIN_FREE_FRACTION`
of the filesystem (a ZFS pool slows down long before it is full). Below it:

- the worker's vector writers defer (`worker._vector_writes_allowed`) — a
  deferral, never an error on a document: the documents are fine, the disk is
  not — and say so in the event log, once per `SIGNAL_EVERY` per collection;
- `bundles.importer.import_bundle` answers retryable, nothing written;
- the health report BLOCKS the embed stage with the reason, so `node show`
  (`/api/node/show`) names it, and `node show` lists every filesystem under a
  collection's vectors with its free space and floor (`status`).

Read where it is shown, by whoever shows it (the web computes it itself): the
one function below is every answer.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import config

GIB = 2 ** 30
#: how often one collection's refusal is written to its event log
SIGNAL_EVERY = 600.0

_lock = threading.Lock()
_signalled: dict[str, float] = {}


class LowDisk(RuntimeError):
    """Below the floor: a vector write that would have happened did not."""


def _existing(path: Path) -> Path:
    """The path, or its nearest parent that exists: a table not made yet sits
    on the filesystem of the directory it will be made in."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def floor_bytes(total: int) -> int:
    return max(int(config.VECTORS_MIN_FREE_GB * GIB),
               int(total * config.VECTORS_MIN_FREE_FRACTION))


def usage(path) -> dict[str, Any] | None:
    """`{path, free, total, floor, short}` for the filesystem holding `path`,
    or None when it cannot be read."""
    try:
        where = _existing(Path(path))
        u = shutil.disk_usage(where)
    except (OSError, ValueError):
        return None
    floor = floor_bytes(u.total)
    return {"path": str(where), "free": u.free, "total": u.total, "floor": floor,
            "short": u.free < floor}


def vectors_short(collection) -> str | None:
    """Why this collection's vectors may not grow now, or None. Cannot read
    the disk: None — a gauge that cannot be read never stops the work."""
    lance = getattr(collection, "lance_dir", None)
    if lance is None:
        return None
    u = usage(lance)
    if u is None or not u["short"]:
        return None
    return (f"vector store short of space: {u['free'] / GIB:.1f} GiB free on "
            f"{u['path']}, under its floor of {u['floor'] / GIB:.1f} GiB — embedding and "
            f"vector imports refused until space is freed")


def signal(con, collection, reason: str) -> bool:
    """The refusal in the collection's event log, at most once per
    SIGNAL_EVERY; True when it was written now."""
    now = time.monotonic()
    key = str(getattr(collection, "manifest_path", collection))
    with _lock:
        if now - _signalled.get(key, -SIGNAL_EVERY) < SIGNAL_EVERY:
            return False
        _signalled[key] = now
    if con is not None:
        from . import db
        try:
            db.log_event(con, "warn", "disk", reason)
        except Exception:   # noqa: BLE001 — the refusal stands; the event is the signal's
            print(f"[warn:disk] {getattr(collection, 'name', '?')}: {reason}", flush=True)
    else:
        print(f"[warn:disk] {getattr(collection, 'name', '?')}: {reason}", flush=True)
    return True


def status(collections) -> list[dict[str, Any]]:
    """Every filesystem a collection's vectors live on: its free space, its
    floor, whether it is short, and the collections on it — `node show`'s
    `disk`."""
    by_fs: dict[str, dict[str, Any]] = {}
    for c in collections:
        lance = getattr(c, "lance_dir", None)
        if lance is None:
            continue
        u = usage(lance)
        if u is None:
            continue
        try:
            dev = str(_existing(Path(lance)).stat().st_dev)
        except OSError:
            dev = u["path"]
        row = by_fs.setdefault(dev, {"path": u["path"], "free_gib": round(u["free"] / GIB, 1),
                                     "total_gib": round(u["total"] / GIB, 1),
                                     "floor_gib": round(u["floor"] / GIB, 1),
                                     "short": u["short"], "collections": []})
        row["collections"].append(c.name)
    return sorted(by_fs.values(), key=lambda r: r["path"])
