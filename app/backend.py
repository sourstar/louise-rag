"""THE COLLECTION BACKEND — how a collection's origin is held, addressed and
read, the same kind of machinery for every collection (operator,
2026-10-30: "All collections must share the same kind of backend.
Depending on the peculiarity of each collection the backend may use a
different template, but any collection must fall in a 'template'.
Depending on the future needs we can add cases to the backend."). Not
LocalAI's model backends (docs/notes/backend-consolidation.md): this one is
the corpus side.

A plugin declares, per ORIGIN FORM it reads (the form its "THE ORIGIN FORM,
and why" note names), the TEMPLATE that form falls in. A template is a
contract, and each contract has ONE implementation of its machinery,
shared by every plugin in it (docs/notes/collection-backend.md):

- MEMBER: the origin addresses each record itself (a file, a zip member, a
  bz2 stream of a multistream dump, a row range of a Parquet archive, a
  zstd frame) and is read where it lies. Nothing is derived from it but
  small indexes; what a reader caches is budgeted in bytes.
- CONTAINER_PACK: records are members of a container that cannot be read
  at a record (a gzip XML, a SQL dump): one Parquet pack per container,
  built in one pass under `derived.building` (one builder across
  processes, a writer's own temporary file, an atomic replace), fresh by
  its Parquet metadata (version, the container's size and mtime), placed by
  `derived.place`; a record is read from its row group.
- DUMP_PACK: one origin too large for one pass (Wikidata's 100 GB JSON): a
  pack directory of chunk files and side tables, built by resumable passes
  under `derived.writing` (one writer, never waited for), `meta.json` the
  checkpoint; a record is read from a row group of its chunk file.

A plugin without a declaration, or with a form outside the templates, is
refused by tests/test_backend_templates.py before a collection is made
from it. A new case is a new template here, with its contract and its one
implementation — never a plugin's private variant of an existing one.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app import config


class Template(str, Enum):
    MEMBER = "member"
    CONTAINER_PACK = "container-pack"
    DUMP_PACK = "dump-pack"


@dataclass(frozen=True)
class OriginForm:
    """One origin form a plugin reads, and its template. `derived`: the
    kinds of derived data it builds (each placed by `derived.place`), none
    for a MEMBER form read where it lies."""
    name: str
    template: Template
    derived: tuple[str, ...] = ()


def forms_of(plugin) -> tuple[OriginForm, ...]:
    """The forms `plugin` declares (its `backend` attribute); TypeError when
    it declares none or something that is not an `OriginForm` of a `Template`."""
    forms = getattr(plugin, "backend", None)
    if not forms:
        raise TypeError(f"{type(plugin).__name__} declares no backend template (app/backend.py)")
    forms = tuple(forms)
    for f in forms:
        if not isinstance(f, OriginForm) or not isinstance(f.template, Template):
            raise TypeError(f"{type(plugin).__name__}: {f!r} is not a backend OriginForm of a Template")
    return forms


def release_arrow_memory() -> bool:
    """pyarrow's own call to hand the memory its pool has freed back to the
    OS: `pa.default_memory_pool().release_unused()` (mimalloc's `mi_collect`,
    jemalloc's purge, glibc's `malloc_trim`, by backend). True when it ran.

    NEVER RAISES, and never imports pyarrow: a process that has not loaded it
    holds no Arrow memory, and a pyarrow without the call (or a pool whose
    call fails) leaves the memory where it was, as before. Measured here
    (pyarrow 25.0.1, mimalloc; three row groups of ~24 MB kept, the oldest
    dropped per read, 200 reads): RssAnon 180 MB without it, 104 MB with it
    after every drop; 37 against 184 MB with groups of ~0.5 MB. A call costs
    0.24 ms (median) after a 24 MB drop, 0.035 ms after a 0.5 MB one, ~2 µs
    when there is nothing to give back — and the next read faults its pages
    in again (a 24 MB group read in 18.8 ms against 9.6–10.1 ms)."""
    pa = sys.modules.get("pyarrow")
    if pa is None:
        return False
    try:
        release = getattr(pa.default_memory_pool(), "release_unused", None)
        if release is None:
            return False
        release()
        return True
    except Exception:   # noqa: BLE001 — giving memory back is never a reason to fail a read
        return False


class GroupCache:
    """THE READ CACHE of every template: the units a reader decoded (a row
    group, a zstd frame), most recent kept, BUDGETED IN BYTES — never in
    items, whose size the data decides: wikidata's cache counted 128 groups
    and held 16 times more of a pack written by 1,000 lines a group than by
    64, children of 5.7 GB (lessons-learned.md, 2026-10-30). The unit just
    read is always kept, whatever the budget. Thread-safe; per process.

    ONE PER PROCESS, shared by every reader (`READ_CACHE`, `reader`). Five
    readers kept five of these, 704 MiB of possible cache a process, and a
    parse child serves several collections (wikipedia's rdf/fts reads the
    archive, wikidata's embed stage cuts its short candidates in the pool
    from its pack, both through the records' frames): eleven children at
    ~1.05 GB RssAnon each, and the worker OOM-killed at its 24 GiB cap
    (München, 2026-11-07). Each reader holds a `CacheReader`, a namespaced
    handle: its keys carry its name, so its `forget`, `clear`, `nbytes`
    and `len` are its own, while its units count against the one budget
    and the least recent unit of ANY reader goes first.

    `release`: called (outside the lock) after a `put` that EVICTED — never
    on a get, nor on a put that dropped nothing; `READ_CACHE` hands the
    Arrow memory the evicted units held back to the OS
    (`release_arrow_memory`).

    The standard: `cachetools.LRUCache(maxsize, getsizeof=...)`, the same
    policy — not a dependency of ours, and this is its twenty lines. A
    namespaced view over one LRU is what Django's cache `KEY_PREFIX` and
    memcached's key prefixes do."""

    def __init__(self, budget: int = 256 << 20, *, release: Callable[[], Any] | None = None):
        self.budget = int(budget)
        self._units: OrderedDict = OrderedDict()   # key -> (value, bytes, owner)
        self._bytes = 0
        self._lock = threading.Lock()
        self._release = release
        #: owner (a reader's name; None for a direct put) -> [units, bytes, hits, misses]
        self._owners: dict[str | None, list[int]] = {}
        self.evictions = 0
        self.releases = 0

    def _tally(self, owner: str | None) -> list[int]:
        t = self._owners.get(owner)
        if t is None:
            t = self._owners[owner] = [0, 0, 0, 0]
        return t

    def _get(self, key, owner: str | None) -> Any:
        with self._lock:
            hit = self._units.get(key)
            if hit is None:
                self._tally(owner)[3] += 1
                return None
            self._units.move_to_end(key)
            self._tally(owner)[2] += 1
            return hit[0]

    def _put(self, key, value, nbytes: int, owner: str | None) -> Any:
        nbytes = int(nbytes)
        evicted = 0
        with self._lock:
            old = self._units.pop(key, None)
            if old is not None:
                self._drop(old)
            self._units[key] = (value, nbytes, owner)
            self._bytes += nbytes
            t = self._tally(owner)
            t[0] += 1
            t[1] += nbytes
            while self._bytes > self.budget and len(self._units) > 1:
                self._drop(self._units.popitem(last=False)[1])
                evicted += 1
            self.evictions += evicted
        if evicted and self._release is not None:
            try:
                if self._release():
                    with self._lock:
                        self.releases += 1
            except Exception:   # noqa: BLE001 — the unit is read and kept either way
                pass
        return value

    def _drop(self, unit: tuple) -> None:
        """Caller holds the lock."""
        _v, n, owner = unit
        self._bytes -= n
        t = self._tally(owner)
        t[0] -= 1
        t[1] -= n

    def _forget(self, match: Callable[[Any], bool], owner: str | None, unwrap: bool) -> None:
        with self._lock:
            gone = [k for k, (_v, _n, o) in self._units.items()
                    if o == owner and match(k[1] if unwrap else k)]
            for k in gone:
                self._drop(self._units.pop(k))

    def get(self, key) -> Any:
        """The unit under `key`, or None; a hit is the most recent now."""
        return self._get(key, None)

    def put(self, key, value, nbytes: int) -> Any:
        """`value` kept under `key`, `nbytes` counted against the budget, the
        least recent units dropped while over it; returns `value`."""
        return self._put(key, value, nbytes, None)

    def forget(self, match: Callable[[Any], bool]) -> None:
        """Every unit put directly (not by a reader) whose key `match`es — a
        file rebuilt or replaced."""
        self._forget(match, None, False)

    def clear(self) -> None:
        """Every unit, of every reader."""
        with self._lock:
            self._units.clear()
            self._bytes = 0
            for t in self._owners.values():
                t[0] = t[1] = 0

    def reader(self, name: str) -> "CacheReader":
        """A namespaced handle on this cache for the reader called `name`."""
        return CacheReader(self, name)

    def stats(self) -> dict:
        """What is held now, and each reader's hits and misses since start —
        for a periodic reporter, never the reading thread."""
        with self._lock:
            return {"bytes": self._bytes, "units": len(self._units), "budget": self.budget,
                    "evictions": self.evictions, "releases": self.releases,
                    "readers": {o: {"bytes": t[1], "units": t[0], "hits": t[2], "misses": t[3]}
                                for o, t in self._owners.items() if o is not None}}

    @property
    def nbytes(self) -> int:
        return self._bytes

    def sizes(self) -> list[int]:
        with self._lock:
            return [n for _v, n, _o in self._units.values()]

    def __len__(self) -> int:
        return len(self._units)


class CacheReader:
    """One reader's handle on a shared `GroupCache`: the same calls, under
    keys `(name, key)`. `forget` and `clear` touch this reader's units only,
    `nbytes`, `sizes()` and `len()` count them; `budget` IS the shared
    budget (setting it sets everyone's), since that is the one there is."""

    def __init__(self, cache: GroupCache, name: str):
        self.cache = cache
        self.name = name

    def get(self, key) -> Any:
        return self.cache._get((self.name, key), self.name)

    def put(self, key, value, nbytes: int) -> Any:
        return self.cache._put((self.name, key), value, nbytes, self.name)

    def forget(self, match: Callable[[Any], bool]) -> None:
        """Every unit of this reader whose key (the reader's own) `match`es."""
        self.cache._forget(match, self.name, True)

    def clear(self) -> None:
        self.cache._forget(lambda _k: True, self.name, True)

    @property
    def budget(self) -> int:
        return self.cache.budget

    @budget.setter
    def budget(self, value: int) -> None:
        self.cache.budget = int(value)

    @property
    def nbytes(self) -> int:
        with self.cache._lock:
            return self.cache._owners.get(self.name, (0, 0))[1]

    def sizes(self) -> list[int]:
        with self.cache._lock:
            return [n for _v, n, o in self.cache._units.values() if o == self.name]

    def __len__(self) -> int:
        with self.cache._lock:
            return self.cache._owners.get(self.name, (0,))[0]


#: THE process's read cache: one byte budget (config.READ_CACHE_BYTES) for
#: every reader, each through its own `READ_CACHE.reader(name)`
READ_CACHE = GroupCache(config.READ_CACHE_BYTES, release=release_arrow_memory)


def read_cache(name: str) -> CacheReader:
    """Reader `name`'s handle on this process's one read cache."""
    return READ_CACHE.reader(name)


#: how long a per-process answer about the FILES — which directory holds a
#: pack, a meta file's stamp, a chunk file's stat — is taken as standing
RECHECK_S = 1.0
_rechecked: dict = {}
_rechecked_lock = threading.Lock()


def rechecked(key, compute: Callable[[], Any], ttl: float = RECHECK_S) -> Any:
    """`compute()`, asked again at most every `ttl` seconds per process. For
    a check that is cheap once and dear per record: wikidata's reader paid
    ~6 stats a line (which place holds the pack, meta.json's stamp, the
    chunk file's) — 3 % of a records-build child after #95 (München,
    2026-10-30). A file replaced meanwhile is still read whole through the
    handle already open (a rename never changes an open file), and the
    writer of a structure forgets its keys when it writes (`forget_rechecked`).
    `cachetools.TTLCache` is the standard; not a dependency of ours."""
    now = time.monotonic()
    with _rechecked_lock:
        hit = _rechecked.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = compute()
    with _rechecked_lock:
        _rechecked[key] = (now, value)
    return value


def forget_rechecked(match: Callable[[Any], bool] | None = None) -> None:
    """Every answer whose key `match`es — all with None."""
    with _rechecked_lock:
        for k in [k for k in _rechecked if match is None or match(k)]:
            _rechecked.pop(k, None)
