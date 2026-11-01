"""THE STORED CPU REFERENCE (operator, 2026-10-31: "The reference is the CPU,
as usual … We may store the CPU golden reference to avoid repeating that
part"; docs/notes/near-exact-runs.md §2).

For one collection and role: the 512 calibration items' vectors as the
encoder's CPU TWIN gave them — the same weights, engine and options on the
CPU (approved-encoders.md "Cross-device bar"). Made once, by `louise hold
embedder cpu-reference`, because the CPU takes hours where the card takes
minutes; then every gate compares the run it validates with it
(`embedder.run_gate`'s `cpu_reference`). A driver, image or wrapper change
is a new run (09 §9.12), so it is gated again against this file, never
recomputing it.

Kept under the CPU twin's RUN, which names the driver ("cpu"), the engine
and the weights: a new CPU build or new weights is a new file, never a file
silently reused. Local evidence, like the statements beside it; the file
carries its own digest, read back before it is used.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import runs as runs_mod
from .codex import CodexError

KIND = "louise-cpu-reference"
VERSION = 1

#: THE BOUND ON ONE REQUEST TO THE CPU TWIN, per 1,024 tokens of the space's
#: window. The node's 300 s flat bound is a card's: wikidata's bge-m3 space
#: (ctx 8,191) timed out three times on its CPU twin (München, 2026-11-01),
#: where wikipedia's (ctx 1,024) finished in 18 min. Generous, never
#: unbounded: a twin that hangs still defers the pass.
CPU_S_PER_KTOKENS = 300.0


@dataclass(frozen=True)
class CpuReference:
    run_id: str
    weights: str
    set_id: str
    role: str
    vectors: list[list[float]]
    path: Path


def request_timeout(ctx: int | None, in_flight: int) -> float:
    """How long one request to the CPU twin may take: one text of up to the
    space's window (`ctx` tokens; 1,024 when the space names none), behind at
    most `in_flight` - 1 others at its one-sequence server
    (EMBED_CONCURRENCY), each bounded at CPU_S_PER_KTOKENS a 1,024 tokens."""
    windows = max(1, -(-int(ctx or 1024) // 1024))
    return CPU_S_PER_KTOKENS * windows * max(1, int(in_flight))


def directory(root: Path, name: str, role: str) -> Path:
    return runs_mod.directory(root, name).parent / "cpu-reference" / role


def _file(root: Path, name: str, role: str, run_id: str) -> Path:
    return directory(root, name, role) / f"{run_id.split(':', 1)[-1]}.json"


def write(root: Path, name: str, role: str, *, run_id: str, run: dict[str, Any],
          set_id: str, vectors: Sequence[Sequence[float]]) -> Path:
    """The reference, written whole (a temporary file, then a replace).
    Refused unless `run` is a CPU run: a card's answers are what it checks."""
    import numpy as np
    if run.get("driver") != "cpu":
        raise CodexError(f"the run {run_id[:23]}… is on {run.get('driver')!r}, not the CPU: "
                         f"a CPU reference is made by the encoder's CPU twin "
                         f"(gpu_layers 0, the device hidden; approved-encoders.md)")
    arr = np.asarray(vectors, dtype="<f4")
    if arr.ndim != 2 or not len(arr):
        raise CodexError("a CPU reference is a non-empty list of vectors of one width")
    raw = arr.tobytes()
    body = {"kind": KIND, "version": VERSION, "role": role, "set": set_id, "run": run_id,
            "weights": run.get("weights"), "run_object": run, "count": int(arr.shape[0]),
            "dim": int(arr.shape[1]), "sha256": hashlib.sha256(raw).hexdigest(),
            "vectors": base64.b64encode(raw).decode("ascii")}
    path = _file(root, name, role, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(path: Path) -> CpuReference:
    import numpy as np
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("kind") != KIND or doc.get("version") != VERSION:
        raise CodexError(f"{path}: not a CPU reference of version {VERSION}")
    raw = base64.b64decode(doc["vectors"])
    if hashlib.sha256(raw).hexdigest() != doc.get("sha256"):
        raise CodexError(f"{path}: its vectors are not the bytes its digest names")
    arr = np.frombuffer(raw, dtype="<f4").reshape(int(doc["count"]), int(doc["dim"]))
    return CpuReference(doc["run"], doc.get("weights") or "", doc["set"], doc["role"],
                        arr.tolist(), Path(path))


def find(root: Path, name: str, role: str, *, weights: str, set_id: str,
         count: int | None = None) -> CpuReference | None:
    """The newest stored reference for these weights and this set, or None.
    A file that cannot be read is passed over (a gate does not fail on it;
    `louise hold embedder cpu-reference` makes it again)."""
    d = directory(root, name, role)
    if not d.is_dir():
        return None
    best: tuple[float, CpuReference] | None = None
    for p in d.glob("*.json"):
        try:
            ref = read(p)
        except (OSError, ValueError, KeyError, CodexError):
            continue
        if ref.weights != weights or ref.set_id != set_id:
            continue
        if count is not None and len(ref.vectors) != count:
            continue
        mtime = p.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, ref)
    return best[1] if best else None
