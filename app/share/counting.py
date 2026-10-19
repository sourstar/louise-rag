"""How much of a part is COVERED — the union of its accepted bundles' chunk
ids, never a sum (Perugia, 2026-10-19).

`coverage.shards_view` summed the chunks of every accepted bundle of a part
and capped the sum at the part's size. Two bundles over the same records —
a producer's duplicate, a peer's re-signed copy of landed rows, two
producers' independent embeddings ("duplicates are checks", 04 §4.5) —
summed to the whole part: lang=en/00160 read 6,607 of 6,607 with 280
chunks embedded by nobody, closed, and rendezvous would never assign it
again. A gap that passes every check is the artefact CLAUDE.md warns of.

For a part with ONE accepted bundle its row's `chunks` is exact. For a part
with more, the union of the chunk ids in the held `vectors.parquet` files
(the challenge rows left out) is the count, cached per part on the sorted
list of its accepted bundle ids in `state/share/coverage-union.json`: the
files are read when a part's bundle set changes and never again. A bundle
whose files are not here counts for nothing — a part left open is noise, a
part closed over a gap is a loss.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CACHE = "coverage-union.json"


def _cache_path(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / "share" / CACHE


def _read_cache(p: Path) -> dict[str, Any]:
    try:
        got = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_cache(p: Path, cache: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    tmp.write_bytes(json.dumps(cache, sort_keys=True).encode())
    tmp.replace(p)


def _sid(shard_id: str) -> str:
    return "/".join(shard_id.rsplit("/", 2)[1:])


def chunk_ids_of(bundle_dir: Path) -> list[str] | None:
    """The chunk ids of a held bundle, its challenge rows left out (09 §9.7:
    the first `challenge.n` rows, in file order); None when the files are
    not here or do not read."""
    try:
        import pyarrow.parquet as pq
        header = json.loads((bundle_dir / "header.json").read_bytes())
        n = int((header.get("challenge") or {}).get("n") or 0)
        ids = pq.read_table(bundle_dir / "vectors.parquet", columns=["chunk_id"]).column("chunk_id").to_pylist()
    except Exception:   # noqa: BLE001 — not here, or not a bundle: counts for nothing
        return None
    return [c for c in ids[n:] if isinstance(c, str)]


def union_counts(root: Path, name: str, codex, rows: Sequence[Mapping[str, Any]], *,
                 log: Callable[[str], None] | None = None) -> dict[str, int]:
    """shard id -> chunks covered, for every part with more than one accepted
    bundle; the parts with one are left to the row's count."""
    from app.share import contribute
    root = Path(root)
    by_shard: dict[str, list[str]] = {}
    for r in rows:
        if r.get("accepted_at"):
            by_shard.setdefault(str(r["shard"]), []).append(str(r["bundle_id"]))
    multi = {s: sorted(ids) for s, ids in by_shard.items() if len(ids) > 1}
    if not multi:
        return {}
    path = _cache_path(root, name)
    cache = _read_cache(path)
    out: dict[str, int] = {}
    changed = False
    for shard, ids in multi.items():
        hit = cache.get(shard)
        if isinstance(hit, dict) and hit.get("bundles") == ids and isinstance(hit.get("covered"), int):
            out[shard] = hit["covered"]
            continue
        seen: set[str] = set()
        unreadable = 0
        for b in ids:
            got = chunk_ids_of(contribute.bundle_dir(root, name, codex, _sid(shard), b))
            if got is None:
                unreadable += 1
                continue
            seen.update(got)
        out[shard] = len(seen)
        cache[shard] = {"bundles": ids, "covered": len(seen), "unreadable": unreadable}
        changed = True
        if log and unreadable:
            log(f"[{name}] coverage: {_sid(shard)}: {unreadable} of {len(ids)} accepted bundle(s) not readable here; "
                f"counted as covering nothing")
    if changed or any(s not in multi for s in cache):
        _write_cache(path, {s: v for s, v in cache.items() if s in multi})
    return out


def view_kwargs(root: Path, name: str, codex, con, rows: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """What `coverage.shards_view` needs beyond the rows to count a part
    once: `redundant` (a producer's deprecated copies held beside their
    current one) and `union` (every part with several accepted bundles)."""
    from app.share import coverage as cov, deprecate
    rows = cov.rows(con) if rows is None else rows
    return {"redundant": deprecate.redundant_here(root, name, codex, con),
            "union": union_counts(root, name, codex, rows)}
