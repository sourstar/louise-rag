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
    return key(Path(origin).resolve())


def resolved(path: Path) -> Path:
    """`path` resolved, or — gone — its parent resolved and its name kept."""
    path = Path(path)
    return path.resolve() if path.exists() else path.parent.resolve() / path.name


def key(resolved_path: Path) -> str:
    """`tag` of a path already resolved: a rename keys a whole walked tree
    from one resolve of its root (app/hold/rename.py)."""
    return hashlib.blake2b(str(resolved_path).encode("utf-8"), digest_size=8).hexdigest()


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


def held() -> list[Path]:
    """Every cache the fast drive holds, `<CACHE_DIR>/<kind>/<tag>`, sorted;
    none when no fast drive is named (a rename carries them,
    app/hold/rename.py). A dot-name is a copy in progress (`adopt`)."""
    if config.CACHE_DIR is None or not Path(config.CACHE_DIR).is_dir():
        return []
    return sorted(t for k in Path(config.CACHE_DIR).iterdir() if k.is_dir()
                  for t in k.iterdir() if not t.name.startswith("."))
