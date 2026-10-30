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

import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


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


class GroupCache:
    """THE READ CACHE of every template: the units a reader decoded (a row
    group, a zstd frame), most recent kept, BUDGETED IN BYTES — never in
    items, whose size the data decides: wikidata's cache counted 128 groups
    and held 16 times more of a pack written by 1,000 lines a group than by
    64, children of 5.7 GB (lessons-learned.md, 2026-10-30). Five readers
    kept five copies of this LRU, four of them counted in items. The unit
    just read is always kept, whatever the budget. Thread-safe; per process.

    The standard: `cachetools.LRUCache(maxsize, getsizeof=...)`, the same
    policy — not a dependency of ours, and this is its twenty lines."""

    def __init__(self, budget: int = 256 << 20):
        self.budget = int(budget)
        self._units: OrderedDict = OrderedDict()   # key -> (value, bytes)
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key) -> Any:
        """The unit under `key`, or None; a hit is the most recent now."""
        with self._lock:
            hit = self._units.get(key)
            if hit is None:
                return None
            self._units.move_to_end(key)
            return hit[0]

    def put(self, key, value, nbytes: int) -> Any:
        """`value` kept under `key`, `nbytes` counted against the budget, the
        least recent units dropped while over it; returns `value`."""
        with self._lock:
            old = self._units.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._units[key] = (value, int(nbytes))
            self._bytes += int(nbytes)
            while self._bytes > self.budget and len(self._units) > 1:
                self._bytes -= self._units.popitem(last=False)[1][1]
        return value

    def forget(self, match: Callable[[Any], bool]) -> None:
        """Every unit whose key `match`es — a file rebuilt or replaced."""
        with self._lock:
            for k in [k for k in self._units if match(k)]:
                self._bytes -= self._units.pop(k)[1]

    def clear(self) -> None:
        with self._lock:
            self._units.clear()
            self._bytes = 0

    @property
    def nbytes(self) -> int:
        return self._bytes

    def sizes(self) -> list[int]:
        with self._lock:
            return [n for _v, n in self._units.values()]

    def __len__(self) -> int:
        return len(self._units)
