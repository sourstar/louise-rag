"""The CONTAINER_PACK template's one implementation (app/backend.py;
docs/notes/collection-backend.md): records that are members of a container
which cannot be read at a record — a gzip XML, a SQL dump — are read from
one Parquet pack per container, built in one pass.

What every pack of the template shares, written once here (it was written
twice, in pubmed's pack and wikipedia's page_props, and fixed twice on
2026-10-30):
- FRESH by the pack's own Parquet key/value metadata: the format version,
  the container's size and mtime. A pack whose footer cannot be read is
  stale. The answer is memoised per container while its size and mtime
  stand: a footer read per record cost ~3 ms, a stat microseconds.
- ONE BUILDER across threads and processes: a thread lock, then
  `derived.building` (an advisory flock beside the pack); the others wait
  and find it fresh.
- WRITTEN WHOLE: into the writer's own temporary file, then an atomic
  replace; on any failure the temporary file goes and the error is the
  caller's.
- PLACES tried in order: a place that cannot be written (OSError) hands
  over to the next, the last one's error is raised.

A plugin gives its schema, its rows as Arrow tables, its version and its
metadata key for the version (each pack's key is kept as it was, so a pack
already built stays fresh — RULE 1: pubmed's packs cost hours).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

SIZE_KEY = b"louise.source_size"
MTIME_KEY = b"louise.source_mtime_ns"


def signature(origin: Path) -> tuple[int, int]:
    st = Path(origin).stat()
    return st.st_size, st.st_mtime_ns


class ContainerPack:
    """`label` names the pack in its log line ("pubmed pack"), `unit` what
    it counts ("records"), `version_key` the metadata key of its version."""

    def __init__(self, label: str, unit: str, version_key: bytes):
        self.label, self.unit, self.version_key = label, unit, version_key
        #: (str(origin), where) -> (signature, version, pack path) of the pack found fresh
        self.fresh: dict[tuple, tuple] = {}
        self._locks: dict[str, threading.Lock] = {}       # str(origin) -> its builders' lock
        self._guard = threading.Lock()

    def metadata(self, origin: Path, version: int) -> dict[bytes, bytes]:
        size, mtime = signature(origin)
        return {self.version_key: str(version).encode(), SIZE_KEY: str(size).encode(),
                MTIME_KEY: str(mtime).encode()}

    def is_fresh(self, origin: Path, path: Path, version: int) -> bool:
        if not Path(path).is_file():
            return False
        try:
            meta = pq.read_metadata(path).metadata or {}
        except Exception:   # noqa: BLE001 — a truncated pack is simply stale
            return False
        want = self.metadata(origin, version)
        return all(meta.get(k) == v for k, v in want.items())

    def write(self, origin: Path, dest: Path, schema: pa.Schema, tables: Iterable[pa.Table],
              version: int) -> int:
        """One pass: `tables` (in `schema`) into `dest`, written whole. The
        rows written; what `tables` raises (a corrupt container) is raised
        after the temporary file is gone."""
        dest = Path(dest)
        schema = schema.with_metadata(self.metadata(origin, version))
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")    # a writer's own (derived.building)
        writer = pq.ParquetWriter(tmp, schema, compression="zstd", compression_level=3)
        n = 0
        try:
            for t in tables:
                writer.write_table(t.replace_schema_metadata(schema.metadata))
                n += t.num_rows
            writer.close()
            os.replace(tmp, dest)
        except BaseException:
            try:
                writer.close()
            except Exception:   # noqa: BLE001
                pass
            tmp.unlink(missing_ok=True)
            raise
        return n

    def ensure(self, origin: Path, places: Callable[[], list[Path]], *, version: int,
               build: Callable[[Path, Path], int], where: object = None,
               ready: Callable[[Path], None] | None = None) -> Path:
        """The pack of `origin`: the first of `places()` that holds it fresh,
        else built — by `build(origin, place)` — in the first place that can
        be written. `places` is asked only when the memo has no answer: per
        record it would cost a realpath. `where` is what decides the places
        (the fast drive's place, a setting), cheap to compute, so an answer
        given under another `where` is not reused. `ready(pack)` is told of
        a pack newly confirmed or built (the plugin's caches of an older one
        go)."""
        from app import derived
        key = (str(origin), where)
        sig = signature(origin)
        with self._guard:
            hit = self.fresh.get(key)
        if hit is not None and hit[0] == sig and hit[1] == version:
            return Path(hit[2])
        places = list(places())
        for pack in places:
            if self.is_fresh(origin, pack, version):
                return self._remember(key, sig, version, pack, ready)
        with self._guard:
            lock = self._locks.setdefault(str(origin), threading.Lock())
        err: OSError | None = None
        with lock:                                    # threads here, then every process
            for pack in places:
                if self.is_fresh(origin, pack, version):
                    return self._remember(key, sig, version, pack, ready)
            for pack in places:
                t0 = time.monotonic()
                try:
                    with derived.building(pack):
                        if self.is_fresh(origin, pack, version):
                            return self._remember(key, sig, version, pack, ready)
                        n = build(origin, pack)
                except OSError as e:                  # cannot be written here: the next place
                    err = e
                    continue
                print(f"[{self.label}] built {pack.name} ({n} {self.unit}, "
                      f"{pack.stat().st_size >> 20} MiB) in {time.monotonic() - t0:.1f}s",
                      file=sys.stderr, flush=True)
                return self._remember(key, signature(origin), version, pack, ready)
        raise err if err is not None else OSError(f"no place to build the {self.label} of {origin}")

    def _remember(self, key: tuple, sig, version: int, pack: Path, ready) -> Path:
        with self._guard:
            known = self.fresh.get(key) == (sig, version, str(pack))
            self.fresh[key] = (sig, version, str(pack))
        if not known and ready is not None:
            ready(pack)
        return pack
