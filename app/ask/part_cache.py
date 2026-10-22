"""Per-part facts a search needs, read from a part ONCE (app/ask/works.py's
edition facts, app/ask/figures.py's plate placement).

gutenberg's parts are zstd frames, not Parquet: one record cannot be read
by column, and a search that met ten records decompressed and split their
frames every time (München, 2026-10-22: the edition choice 173 ms of 229,
the plates 1.3 s of a 10 s search). A `PartCache` keeps what a kind needs
of every record of a part in one JSON file named by the kind's version and
the part's ROOT — a new version of the part is another file, a change to
the rule that wrote it another name (invariant 3), so a file is never
stale — on the fast drive when one is named (invariant 6, app/derived.py).

A part without its file yet is queued and built by one background thread;
the search does the work for its own records as before and never waits
(CLAUDE.md: no unbounded wait on an optional subsystem). Derived and
re-creatable: a file that cannot be written is built again next time.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

#: records of a part -> {uid: JSON-able value}
Make = Callable[[Iterable[dict[str, Any]]], dict[str, Any]]


class PartCache:
    def __init__(self, kind: str, version: int, *, decode: Callable[[Any], Any] = lambda v: v,
                 max_parts: int = 256) -> None:
        self.kind, self.version, self.decode, self.max_parts = kind, version, decode, max_parts
        self._parts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.queue: "queue.Queue[tuple[Path, Path, Any, Make]]" = queue.Queue(maxsize=1024)
        self._queued: set[str] = set()
        self._builder: threading.Thread | None = None

    def store(self, tree: Path) -> Path:
        """The kind's directory for a collection's SHARD tree: the fast drive
        when named, else `<name>/state/<kind>` beside it."""
        from app import derived
        return derived.place(self.kind, tree) or (Path(tree).parent / "state" / self.kind)

    def file(self, store: Path, shard) -> Path:
        where = re.sub(r"[^\w.=-]", "_", f"{shard.slice}-{shard.n:05d}")
        return Path(store) / f"v{self.version}-{where}-{str(shard.root).split(':')[-1]}.json"

    def clear(self) -> None:
        with self._lock:
            self._parts.clear()

    def get(self, tree: Path, store: Path, shard, make: Make) -> dict[str, Any] | None:
        """{uid: decoded value} of the part, or None while it has no file —
        and then the part is queued for `build`."""
        path = self.file(store, shard)
        key = str(path)
        with self._lock:
            hit = self._parts.get(key)
            if hit is not None:
                self._parts.move_to_end(key)
                return hit
        try:
            got = {u: self.decode(v) for u, v in json.loads(path.read_bytes()).items()}
        except (OSError, ValueError, TypeError, IndexError, KeyError, AttributeError):
            self._enqueue(tree, store, shard, make)
            return None
        with self._lock:
            self._parts[key] = got
            while len(self._parts) > self.max_parts:
                self._parts.popitem(last=False)
        return got

    def build(self, tree: Path, store: Path, shard, make: Make) -> Path | None:
        """Write the part's file: the part read through once, record by
        record, into a sibling renamed whole (invariant 2). None when the
        part cannot be read — nothing is written."""
        from app.hold import shard as shard_mod
        from app.pack import records as pack_records
        path = self.file(store, shard)
        if path.exists():
            return path
        part = Path(tree) / shard_mod.shard_path(shard.slice, shard.n)
        try:
            got = make(pack_records.iter_records(part))
        except (OSError, ValueError, KeyError):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(json.dumps(dict(sorted(got.items()))).encode("utf-8"))
        os.replace(tmp, path)
        return path

    def _enqueue(self, tree: Path, store: Path, shard, make: Make) -> None:
        key = str(self.file(store, shard))
        with self._lock:
            if key in self._queued:
                return
            try:
                self.queue.put_nowait((Path(tree), Path(store), shard, make))
            except queue.Full:
                return                  # met again later; never waited for
            self._queued.add(key)
            if self._builder is None or not self._builder.is_alive():
                self._builder = threading.Thread(target=self._loop, name=self.kind, daemon=True)
                self._builder.start()

    def _loop(self) -> None:
        while True:
            try:
                tree, store, shard, make = self.queue.get(timeout=30)
            except queue.Empty:
                with self._lock:        # an enqueue under the lock sees this thread gone
                    if self.queue.empty():
                        self._builder = None
                        return
                continue
            try:
                self.build(tree, store, shard, make)
            except Exception:   # noqa: BLE001 — a part that cannot be built: per-record work, as before
                pass
            finally:
                with self._lock:
                    self._queued.discard(str(self.file(store, shard)))
                self.queue.task_done()
