"""ONE PARSE: a stage reads the document's FRESH RECORD instead of parsing
its origin (operator, 2026-10-31: "a general rule for any collection that
can benefit from it … one 'backend' for all the collections";
docs/notes/one-parse.md).

The records are the parsed form, built once by `hold records build`. A
record is FRESH for a manifest row when the current snapshot holds its uid,
its `origin_hash` is the row's `txt_hash` (the same origin bytes) and the
plugin's PARSER_VERSION is the Codex's parser version (the same parser):
its sections are then what a parse would give, and the chunks cut from
them by `layers.chunks_for_document` are the same (invariants 1 and 3).

Anything else parses as before: no records, a row newer than the
snapshot, a different parser, a collection whose check found a mismatch.

KEPT HONEST: one row in SAMPLE_EVERY (by uid, so every process samples the
same rows) is parsed anyway and compared section by section. A mismatch
writes MARK under the collection's state directory and every process stops
using records for that collection until a person removes it; the parse is
what the stage gets meanwhile (`sections` returns None).

Per process, cached: the context (CONTEXT_TTL_S), and each part's index
and frames through THE records reader's own caches (`pack.records.
cached_index` / `read_record`, byte-budgeted in `backend.GroupCache`; the
collection backend's S2, docs/notes/collection-backend.md) — the rows a
child gets are contiguous (`parallel._partition`), so neighbouring rows
share a frame.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

#: one row in this many (by uid) is parsed anyway and compared
SAMPLE_EVERY = 1000
MARK = "record-source-mismatch.json"

_lock = threading.Lock()
_stats = {"record": 0, "parsed": 0, "sampled": 0, "mismatch": 0}


def stats() -> dict:
    with _lock:
        return dict(_stats)


def _count(key: str) -> None:
    with _lock:
        _stats[key] += 1


def _mark_path(collection) -> Path:
    return Path(collection.manifest_path).parent / MARK


def stopped(collection) -> bool:
    """A mismatch was found for this collection: records are not used."""
    try:
        return _mark_path(collection).exists()
    except (OSError, TypeError, AttributeError):
        return True


#: how long a process keeps a collection's context before looking again
#: (a new snapshot, a mark): the lookup is a directory listing, not per row
CONTEXT_TTL_S = 60.0
_contexts: dict[str, tuple[float, Any]] = {}


def _context(collection) -> tuple[Path, Any, Any] | None:
    """(the SHARD tree, the current snapshot, the Codex) — or None when the
    collection has no records this process can use. Cached CONTEXT_TTL_S."""
    import time
    key = str(getattr(collection, "manifest_path", ""))
    now = time.monotonic()
    with _lock:
        got = _contexts.get(key)
    if got is not None and now - got[0] < CONTEXT_TTL_S:
        return got[1]
    ctx = _context_now(collection)
    with _lock:
        _contexts[key] = (now, ctx)
    return ctx


def _context_now(collection) -> tuple[Path, Any, Any] | None:
    codex = getattr(collection, "codex", None)
    if codex is None or getattr(collection, "source", "origin") != "origin":
        return None
    try:
        plugin = collection.plugin_instance()
    except Exception:   # noqa: BLE001 — no plugin, no comparison: parse
        return None
    if int(getattr(plugin, "PARSER_VERSION", -1)) != int(getattr(codex.parser, "version", -2)):
        return None
    from . import collections as collections_mod
    from .hold import collection as col
    from .share import possession
    root = collections_mod.louise_root() / "collections"
    snap = possession.current_snapshot(root, collection.name)
    if snap is None:
        return None
    return col.shard_dir_of(root, collection.name), snap, codex


def record_for(collection, row) -> dict | None:
    """The row's FRESH record, or None."""
    return lookup(collection, row)[0]


def lookup(collection, row) -> tuple[dict | None, str]:
    """(the row's fresh record or None, why): "fresh", "stopped" (a mark),
    "no-records" (no Codex, another parser, a fetched copy, no snapshot),
    "no-origin-hash", "not-in-snapshot", "origin-differs"."""
    if stopped(collection):
        return None, "stopped"
    ctx = _context(collection)
    if ctx is None:
        return None, "no-records"
    tree, snap, codex = ctx
    uid = row["uid"]
    want = row["txt_hash"] if "txt_hash" in row.keys() else None
    from .pack import records as pack_records
    order = (getattr(codex, "slicing", None) or {}).get("order", "bytes")
    for shard in snap.shards_of(str(row["native_id"]), order):
        path = Path(tree) / shard.path
        try:
            rec = pack_records.read_record(path, uid)
        except KeyError:
            continue                         # not in this slice's part
        except (OSError, ValueError, IndexError):
            continue
        if rec.get("uid") != uid:
            continue
        if want:
            if rec.get("origin_hash") != want:
                return None, "origin-differs"   # not the same origin bytes: parse
            return rec, "fresh"
        # NO txt_hash (a re-discovered row, 2026-10-31: all 30.9 M of
        # wikipedia's dump layer): the same unit when the record's `origin`
        # locates exactly the row's own — a dated dump's byte range, the
        # same bytes, so the same hash. The sampled byte check stands behind it
        loc = _locator(collection, row)
        if loc is not None and rec.get("origin") == loc:
            return rec, "fresh"
        return None, "no-origin-hash"
    return None, "not-in-snapshot"


def _locator(collection, row) -> str | None:
    """09 §9.5's `origin` for the row's own locator, as the records build
    writes it (`plugin_io.origin_locator`), or None."""
    from . import plugin_io
    try:
        return plugin_io.origin_locator(collection.plugin_instance(), str(row["path"]),
                                        collection.corpus_dir)
    except Exception:   # noqa: BLE001 — no locator, no proof: parse
        return None


def _sampled(uid: str) -> bool:
    return int(hashlib.blake2b(uid.encode(), digest_size=8).hexdigest(), 16) % SAMPLE_EVERY == 0


def sections(collection, row, parse) -> list[tuple]:
    """The row's sections: from its fresh record where there is one, else
    `parse()` (the plugin's, as before). A sampled row is parsed as well and
    compared; a mismatch stops the shortcut for the collection."""
    rec = record_for(collection, row)
    if rec is None:
        _count("parsed")
        return parse()
    got = [tuple(s) for s in rec.get("sections") or []]
    if _sampled(row["uid"]):
        _count("sampled")
        parsed = [tuple(s) for s in parse()]
        if parsed != got:
            _count("mismatch")
            _stop(collection, row["uid"], got, parsed)
            return parsed
    _count("record")
    return got


def _stop(collection, uid: str, from_record, from_parse) -> None:
    """Write the mark (first writer wins) and log: a stale record or a
    parser changed without its version — a defect to fix before records
    are trusted again."""
    path = _mark_path(collection)
    body = {"uid": uid, "record_sections": len(from_record), "parsed_sections": len(from_parse),
            "first_difference": next((i for i, (a, b) in enumerate(zip(from_record, from_parse))
                                      if a != b), min(len(from_record), len(from_parse))),
            "why": "a sampled record's sections differ from the parse: records are not used "
                   "for this collection until a person has looked and removed this file "
                   "(docs/notes/one-parse.md)"}
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump(body, f, indent=1)
    except FileExistsError:
        pass
    except OSError:
        pass
    import sys
    print(f"[record-source] {getattr(collection, 'name', '?')}: {uid}: the record's sections differ "
          f"from the parse; records stop for this collection ({path})", file=sys.stderr, flush=True)
