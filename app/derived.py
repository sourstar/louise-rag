"""app/derived.py — where DERIVED data lives: the caches a plugin builds from
its origin and seeks in while it parses (pubmed's per-container pack,
wikipedia's page-props pack, wikidata's pack and label table), re-creatable
from the origin, never exchanged.

Operator, 2026-10-20: "a cache folder on a fast drive, like nvme or ssd, to
be used for such operations". Measured on the node (München): wikidata's
parse is ~95 % label lookups in labels.sqlite — 133–179 entities/s a core
with the table on the HDD pool while the scan writes it, 430 warmed in the
ARC, 1,763 from RAM. `LOUISE_CACHE_DIR` (config.CACHE_DIR, identity tier,
07 §7.8) names the fast drive; unset, every cache stays beside its origin,
as before, so a node that did not ask sees nothing move. A cache is keyed
on the ORIGIN'S PHYSICAL IDENTITY — its resolved path — never a name
(CLAUDE.md: two objects can share a name and point at different files).
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from app import config


def tag(origin: Path) -> str:
    """16 hex of the origin's resolved path — the key page_props has used
    for its temporary pack since 2026-09."""
    return hashlib.blake2b(str(Path(origin).resolve()).encode("utf-8"), digest_size=8).hexdigest()


def place(kind: str, origin: Path) -> Path | None:
    """`<CACHE_DIR>/<kind>/<tag of origin>/` — the directory for the caches
    derived from `origin` (a file or a folder) — or None when no fast drive
    is named. The directory is not created here."""
    if config.CACHE_DIR is None:
        return None
    return Path(config.CACHE_DIR) / kind / tag(origin)


def adopt(src: Path, dst: Path) -> bool:
    """An existing cache beside its origin, carried into the fast drive once:
    `src` (a file or a directory) copied to `dst` when `dst` does not exist
    — into a sibling first, renamed whole (invariant 2), sizes and times
    kept — and left where it was for the operator to delete. A stat when
    `dst` exists; False when nothing was copied. RULE 1: the pubmed pack of
    a 40 M-record corpus costs hours to rebuild and is identical either
    way. A directory's `*.tmp` files — a pass's uncommitted work — are not
    copied; a copy stopped half-way leaves only the sibling, which the next
    call removes and redoes: `dst` is whole or absent, never half (the
    node's wikidata pack is ~60 GB, minutes of copying — München)."""
    src, dst = Path(src), Path(dst)
    if dst.exists() or not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.adopting")
    if tmp.is_dir():
        shutil.rmtree(tmp, ignore_errors=True)
    elif tmp.exists():
        tmp.unlink()
    if src.is_dir():
        shutil.copytree(src, tmp, copy_function=shutil.copy2, ignore=shutil.ignore_patterns("*.tmp"))
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def _entries(folder: Path, depth: int):
    """`folder` and its entries to `depth` levels (1: its children), as found."""
    yield folder
    if depth <= 0 or not folder.is_dir():
        return
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return
    for child in children:
        yield from _entries(child, depth - 1)


def carried(pairs, depth: int = 1) -> list[dict]:
    """The caches a rename must carry: for each folder moved `a -> b`, every
    cache keyed on `a` or on an entry of it (to `depth`; every kind in use
    keys on the origin or the SHARD tree, a file or folder directly in it, or
    the tree itself), with the key it will have once moved. A cache is keyed
    on its origin's resolved path (`tag`), so a moved origin would otherwise
    find nothing and rebuild for hours (wikidata's pack: München,
    2026-10-27). One listing per moved folder; nothing when no fast drive is
    named or it holds no cache."""
    if config.CACHE_DIR is None:
        return []
    root = Path(config.CACHE_DIR)
    if not root.is_dir():
        return []
    held = {(k.name, t.name) for k in root.iterdir() if k.is_dir()
            for t in k.iterdir() if t.is_dir() and not t.name.startswith(".")}
    if not held:
        return []
    keys: dict[str, tuple[Path, Path]] = {}
    for a, b in pairs:
        ra = Path(os.path.realpath(a))
        rb = Path(os.path.realpath(Path(b).parent)) / Path(b).name
        if not ra.exists():
            continue
        for p in _entries(ra, depth):
            keys[tag(p)] = (p, rb / p.relative_to(ra))
    out = []
    for kind, t in sorted(held):
        if t in keys:
            old, new = keys[t]
            out.append({"kind": kind, "from": t, "to": tag(new), "key": str(old), "new_key": str(new)})
    return out


def carry(moves: list[dict]) -> list[str]:
    """Move each cache of `carried` to its new key: renamed whole within its
    kind's directory. Idempotent — a cache already under its new key is done;
    one under both is left as it is and said (a resumed rename never deletes
    a cache)."""
    if config.CACHE_DIR is None:
        return []
    root, said = Path(config.CACHE_DIR), []
    for m in moves:
        src, dst = root / m["kind"] / m["from"], root / m["kind"] / m["to"]
        if src.exists() and not dst.exists():
            os.rename(src, dst)
            said.append(f"{m['kind']}: {m['from']} -> {m['to']}")
        elif src.exists() and dst.exists():
            said.append(f"{m['kind']}: both {m['from']} and {m['to']} exist; left as they are")
    return said
