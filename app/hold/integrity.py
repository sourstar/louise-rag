"""03 §3.8, the defence behind the manifest-only gate: in a certified
collection whose content is `manifest-only`, a record shard on disk that no
certifier-signed snapshot of the chain lists — a folder populated by hand,
an old file left behind, a substitute — means the collection is *integrity
violated* on this peer. Migration step 10, owed until 2026-10-07: the gate
refused such a shard at promotion, and nothing noticed one that arrived
another way.

What follows from a violation, each where it is enforced:
  - the shard is never read: `DragSource.discover` skips it, so none of its
    records or chunks enters the manifest (the unit of discard is the record
    and all its chunks);
  - embedding stops for the whole collection and no bundle is published
    (worker `_embedding_allowed`; `contribute.preconditions`) — vectors for
    chunks outside the snapshot would poison everyone's coverage map;
  - search continues over the certified subset, and says what it left out
    (`ask` results' `withheld`, 05 §5.3) — never a silent drop;
  - the mark clears by itself once the shard is gone or replaced by the
    signed one: `share fetch` replaces it, and `hold check roots` proves it.

A file is judged by its PATH and its SIZE against what the signed snapshots
list for that path. Nothing is hashed here: a file of the listed size with
other bytes is what `roots` finds (03 §3.2), and this check runs on every
evaluation of the copy, so it costs a directory listing — cached by the
listing's own stat signature, not by the corpus. A file overwritten IN
PLACE changes no directory's stat, so the full listing is also redone every
RECHECK_S whatever the signature says: a bounded cost on a clock, never one
per round.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

#: drag directory -> (stat signature of the listing, when listed, violations)
_cache: dict[str, tuple[tuple, float, tuple[str, ...]]] = {}
RECHECK_S = 600.0


def applies(codex) -> bool:
    return (codex is not None and getattr(codex, "publication", None) == "certified"
            and getattr(codex, "content", None) == "manifest-only" and codex.certifier is not None)


def _signature(drag: Path) -> tuple:
    """The stats that change when a shard or a snapshot is added, removed or
    replaced: the snapshots directory and every slice directory of records."""
    parts = []
    for d in [drag / "snapshots", drag / "records"] + sorted((drag / "records").glob("*")):
        try:
            st = os.stat(d)
            parts.append((str(d), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(d), None, None))
    return tuple(parts)


def _listed(root: Path, name: str, codex) -> dict[str, int]:
    """path -> bytes, for every file a certifier-signed snapshot of the chain
    lists (any of them: a shard stays listed by every snapshot carrying it)."""
    from app.hold import anchor, snapshot as sn
    from app.share import members as mem
    drag = Path(root) / name / "drag"
    out: dict[str, int] = {}
    d = drag / "snapshots"
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        if p.name.endswith(".sig.json"):
            continue
        try:
            snapshot = sn.load(p)
        except Exception:   # noqa: BLE001 — a file that is no snapshot lists nothing
            continue
        if not anchor.certifier_signatures(root, name, codex, snapshot):
            continue
        for m in mem.members_of(snapshot, drag_dir=drag):
            for f in m.files:
                out[f.path] = f.bytes
    return out


def violations(root, name: str, codex) -> tuple[str, ...]:
    """The record shards on disk (`records/<slice>/<NNNNN>.jsonl.zst`, drag-
    relative) that no certifier-signed snapshot lists at that size. A
    superseded version (`NNNNN.<snapshot id>.jsonl.zst`, 02 §2.2) is not
    read and not counted. Empty where the rule does not apply."""
    if not applies(codex):
        return ()
    drag = Path(root) / name / "drag"
    if not (drag / "records").is_dir():
        return ()
    sig = _signature(drag)
    cached = _cache.get(str(drag))
    if cached is not None and cached[0] == sig and time.monotonic() - cached[1] < RECHECK_S:
        return cached[2]
    listed = _listed(root, name, codex)
    out = []
    for p in sorted((drag / "records").glob("*/*.jsonl.zst")):
        if "." in p.name[: -len(".jsonl.zst")]:
            continue
        rel = p.relative_to(drag).as_posix()
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if listed.get(rel) != size:
            out.append(rel)
    result = tuple(out)
    _cache[str(drag)] = (sig, time.monotonic(), result)
    return result
