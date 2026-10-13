"""`hold collection forget` — stop holding a collection on this peer (07 §7.3,
§7.7; 02 §2.9).

What goes: the held file (`collections/<name>.toml`, 07 §7.8 — the node
stops serving the collection at its next round and starts no thread for
it); then, for a never-share collection, every deposit erased leaving no
trace (02 §2.9: "erasing every deposit followed by removing the
collection's directory"); then the directory whole — SHARD, state, indexes,
vector table, origin. With `--kinds vectors` only the vector-side members
of the SHARD and the vector table go, and the collection stays held. The
Codex as published, this peer's attestations as published and what other
peers hold are untouched: a peer may stop holding (01, promise 7 is about
the network). Nothing is announced yet, so nothing is withdrawn.

On a running node this runs IN THE WORKER (app/jobs.py, the DEPOSIT kind's
op "forget"): the worker is the one writer of a collection's indexes, and
it retires the collection's threads first (`worker.retire_collection`).
Idempotent: a member is gone entire or still there (07 §7.7), and
forgetting what is not held is nothing to do.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from .codex import CodexError

#: 09 §9.16's vector-side members of a SHARD, by their prefixes
VECTOR_MEMBER_PREFIXES = ("vectors-", "buckets-", "bundle-", "centres-")
#: the `--kinds` taken apart today; everything else goes with the copy whole
KINDS = ("vectors",)


def _linked_by_others(root: Path, name: str, target: Path) -> list[str]:
    from .rename import _origin_shared
    return _origin_shared(Path(root), name, target)


def forget(root: Path, name: str, *, kinds=None, dry_run: bool = False,
           retire: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Remove the copy at `<root>/<name>` and its held file — or, with
    `kinds`, only those members — and say what went. `retire` is the
    worker's hook that stops the collection's threads before anything under
    its directory is removed; None outside the worker."""
    from . import collection as col, deposit, shard as shard_mod
    root = Path(root)
    base = root / name
    held = deposit.held_file(root, name)
    kinds = tuple(kinds or ())
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise CodexError(f"--kinds {','.join(unknown)}: forget removes the copy whole, or "
                         f"its vectors (`--kinds vectors`); no other member is taken "
                         f"apart yet (07 §7.3)")
    if not base.exists() and not held.exists():
        return {"collection": name, "gone": True, "removed": []}
    try:
        codex = col.read(root, name)
    except (CodexError, FileNotFoundError, OSError):
        codex = None                        # half gone already: finish the job
    if kinds:
        shard_tree = shard_mod.shard_dir(base)
        plan = [p for p in (sorted(shard_tree.iterdir()) if shard_tree.is_dir() else [])
                if p.name.startswith(VECTOR_MEMBER_PREFIXES)]
        if (base / "lance").exists():
            plan.append(base / "lance")
    else:
        plan = [p for p in (held, base) if p.exists()]
    if dry_run:
        return {"collection": name, "dry_run": True,
                "would_remove": [str(p) for p in plan], "removed": []}
    removed: list[str] = []
    if kinds:
        for p in plan:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        return {"collection": name, "kinds": list(kinds), "removed": removed}
    # 1. the held file: not held from here on, and no round starts a thread
    #    for it (07 §7.8)
    if held.exists():
        held.unlink()
        removed.append(str(held))
    # 2. its threads, when this runs in the worker
    if retire is not None:
        retire(name)
    # 3. a never-share collection: every deposit erased, no trace (02 §2.9)
    erased = 0
    if codex is not None and deposit.is_deposit(codex) and base.exists():
        erased = deposit.erase_all(root, name, codex)
    # 4. its folders under the shared roots (one mount per kind, a folder per
    #    collection, 2026-10-12): the directory's links name them and rmtree
    #    removes only the links, so the SHARD folder was left behind under
    #    the name — and a collection renamed to that name found it taken
    #    (2026-10-13). The SHARD folder is this collection's copy and goes;
    #    an origin folder goes too unless another held collection links it
    #    (both Normattiva collections read /corpora/normattiva)
    for link_name, env in ((shard_mod.SHARD_DIR if hasattr(shard_mod, "SHARD_DIR") else "shard",
                            "SHARDS_ROOT"), ("origin", "ORIGINS_ROOT")):
        link = base / link_name
        shared_root = os.getenv(env, "").strip()
        if not (shared_root and link.is_symlink()):
            continue
        target = Path(os.readlink(link))
        if target.name != name or os.path.normpath(target.parent) != os.path.normpath(shared_root):
            continue                         # a custom origin, linked by hand: not ours
        if link_name == "origin" and _linked_by_others(root, name, target):
            continue
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(str(target))
    # 5. the directory, whole
    if base.exists():
        shutil.rmtree(base)
        removed.append(str(base))
    return {"collection": name, "erased_deposits": erased, "removed": removed}
