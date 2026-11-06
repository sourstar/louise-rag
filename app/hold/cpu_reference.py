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

ONCE PER NODE, NOT ONCE PER COMMAND (operator, 2026-11-06: "I already asked
to save the cpu reference values so that we don't have to repeat that time
consuming part"). `hold embedder calibrate` embedded its CPU-twin lane from
scratch and kept nothing, then `hold embedder cpu-reference` embedded the
same 512 texts again for the new collection (wikidata on Nemotron: stage 2,
then stage 10 of its re-creation). Both now go through `obtain`, over ONE
store per node, `<LOUISE_ROOT>/state/cpu-reference/<key>/`. The key is what
fixes the inputs: the weights, the set, the window they are cut at, the
instruction before them, and the role. Same key, same texts. A twin that
gives back its own bytes (invariant 12) gives the same vectors, so a kept
file is used, never recomputed.
- **A new run of the same weights** (another engine build) is CARRIED when
  its answers to the sixteen fingerprint texts are byte-identical to the
  kept ones (RULE 1: reuse bytes that are provably identical). Otherwise it
  is computed, journalled, then kept beside the old one.
- **The collection keeps its own copy**, which is what its gate reads
  (`find`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
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
    run_object: dict[str, Any] = field(default_factory=dict)


def request_timeout(ctx: int | None, in_flight: int) -> float:
    """How long one request to the CPU twin may take: one text of up to the
    space's window (`ctx` tokens; 1,024 when the space names none), behind at
    most `in_flight` - 1 others at its one-sequence server
    (EMBED_CONCURRENCY), each bounded at CPU_S_PER_KTOKENS a 1,024 tokens."""
    windows = max(1, -(-int(ctx or 1024) // 1024))
    return CPU_S_PER_KTOKENS * windows * max(1, int(in_flight))


def directory(root: Path, name: str, role: str) -> Path:
    return runs_mod.directory(root, name).parent / "cpu-reference" / role


def _file_in(d: Path, run_id: str) -> Path:
    return d / f"{run_id.split(':', 1)[-1]}.json"


def _file(root: Path, name: str, role: str, run_id: str) -> Path:
    return _file_in(directory(root, name, role), run_id)


def write(root: Path, name: str, role: str, *, run_id: str, run: dict[str, Any],
          set_id: str, vectors: Sequence[Sequence[float]]) -> Path:
    """The reference, written whole (a temporary file, then a replace).
    Refused unless `run` is a CPU run: a card's answers are what it checks."""
    return write_in(directory(root, name, role), role, run_id=run_id, run=run,
                    set_id=set_id, vectors=vectors)


def write_in(d: Path, role: str, *, run_id: str, run: dict[str, Any], set_id: str,
             vectors: Sequence[Sequence[float]], extra: dict[str, Any] | None = None) -> Path:
    """`write` into a directory: a collection's, or the node's store."""
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
            "vectors": base64.b64encode(raw).decode("ascii"), **(extra or {})}
    path = _file_in(d, run_id)
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
                        arr.tolist(), Path(path), doc.get("run_object") or {})


def kept(root: Path, name: str, role: str, *, run_id: str, set_id: str,
         count: int) -> Path | None:
    """The reference `run_id` already made of this set, whole and readable,
    or None. A run reproduces itself (invariant 12), so its answers to the
    same set are these bytes: `hold embedder cpu-reference` skips the role."""
    path = _file(root, name, role, run_id)
    if not path.is_file():
        return None
    try:
        ref = read(path)
    except (OSError, ValueError, KeyError, CodexError):
        return None
    if ref.run_id != run_id or ref.set_id != set_id or len(ref.vectors) != count:
        return None
    return path


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


# ---- the pass, resumable (invariant 2) ---------------------------------------
# gutenberg, 2026-11-02: the query role had 352 of 512 answers in memory when
# one request came back after a 5 h `docker pause` with its VeGa receipt
# expired; the pass exited and the 352 answers (~4.6 h of CPU) were lost. A
# role is now journalled a window at a time, and a new start asks only what
# the journal does not hold.

#: TEXTS BETWEEN TWO JOURNAL WRITES: what a stop, a kill or a deferral can
#: cost. The twin answers one text a request, minutes each on a long window,
#: so a window of 8 loses minutes, never hours; and each text is its own
#: request, so the windows change no byte (invariant 12).
WINDOW = 8


class Deferred(Exception):
    """A window's answers came from another run than the pass's: nothing of
    that window is kept, and the pass defers (the journal stays)."""


def journal(root: Path, name: str, role: str, *, run_id: str, set_id: str) -> Path:
    """The role's journal: this run's answers to this set, one line a text."""
    return journal_in(directory(root, name, role), run_id=run_id, set_id=set_id)


def journal_in(d: Path, *, run_id: str, set_id: str) -> Path:
    tag = hashlib.sha256(set_id.encode("utf-8")).hexdigest()[:16]
    return d / f"{run_id.split(':', 1)[-1]}.{tag}.partial.jsonl"


def discard_journal(root: Path, name: str, role: str, *, run_id: str, set_id: str) -> None:
    journal(root, name, role, run_id=run_id, set_id=set_id).unlink(missing_ok=True)


def _digest(text: Any) -> str:
    # a text, or a pair space's item: the journal answers only the same input
    return hashlib.sha256(json.dumps(text, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def _f32(raw: bytes) -> list[float]:
    import numpy as np
    return np.frombuffer(raw, dtype="<f4").astype(float).tolist()


def embed_resumable(root: Path, name: str, role: str, *, run_id: str, set_id: str,
                    texts: Sequence[Any], embed, window: int = WINDOW,
                    say=None) -> list[list[float]]:
    return embed_resumable_in(directory(root, name, role), role, run_id=run_id, set_id=set_id,
                              texts=texts, embed=embed, window=window, say=say)


def embed_resumable_in(d: Path, role: str, *, run_id: str, set_id: str,
                       texts: Sequence[Any], embed, window: int = WINDOW,
                       say=None) -> list[list[float]]:
    """`texts` as `embed` (a `runs.Answers`) answers them, a window at a time,
    each window journalled once its answers are `run_id`'s and before the next
    is asked. A journal line is taken back only for the same input at the
    same index; a line torn by a kill is passed over and its text asked
    again. The vectors are float32, as `write` keeps them, so a resumed pass
    writes the bytes a whole pass would."""
    import numpy as np
    path = journal_in(d, run_id=run_id, set_id=set_id)
    have: dict[int, list[float]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
                i = int(x["i"])
                if 0 <= i < len(texts) and x["t"] == _digest(texts[i]):
                    have[i] = _f32(base64.b64decode(x["v"], validate=True))
            except (ValueError, KeyError, TypeError):
                continue
        if have and say:
            say(f"resumed  {role}  {len(have)} of {len(texts)} answers journalled  {path}")
    todo = [i for i in range(len(texts)) if i not in have]
    path.parent.mkdir(parents=True, exist_ok=True)
    for w in range(0, len(todo), max(1, window)):
        idx = todo[w:w + max(1, window)]
        got = embed([texts[i] for i in idx])
        if embed.runs != {run_id}:
            raise Deferred(f"{role}: answers came from {sorted(embed.runs)}, not "
                           f"{run_id[:23]}…; this window is not kept")
        if len(got) != len(idx):
            raise Deferred(f"{role}: {len(got)} answers for {len(idx)} texts")
        lines = []
        for i, v in zip(idx, got):
            raw = np.asarray(v, dtype="<f4").tobytes()
            lines.append(json.dumps({"i": i, "t": _digest(texts[i]),
                                     "v": base64.b64encode(raw).decode("ascii")}) + "\n")
            have[i] = _f32(raw)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("".join(lines))
            fh.flush()
            os.fsync(fh.fileno())
    return [have[i] for i in range(len(texts))]


# ---- the node's store: made once, used by every command (2026-11-06) ----------

class NotReproducible(Exception):
    """The twin did not give back its own bytes: nothing is kept."""


@dataclass(frozen=True)
class Obtained:
    """The CPU twin's answers to the set, and how they were had: "stored" (a
    kept file of this run), "carried" (another run's file, its sixteen
    fingerprint answers byte-identical to this run's) or "computed"."""
    vectors: list[list[float]]
    run_id: str
    run: dict[str, Any]
    how: str
    path: Path


def node_key(*, weights: str, set_id: str, ctx: int, instruction: str | None, role: str,
             cut: str = "characters") -> dict[str, Any]:
    """What fixes the 512 inputs and the function that answers them.

    THE CUT IS PART OF THE INPUT (2026-11-06, wikidata's gate: worst 0.974831
    against the stored reference). Calibrate cut by characters while the
    space cuts by tokens, and the key, which did not name the cut, handed
    those vectors to `cpu-reference` for a token space: its 54 long items
    were other inputs. A key names its cut when it is not the default
    `characters`, so every characters entry keeps the digest it had."""
    key = {"weights": weights, "set": set_id, "ctx": int(ctx),
           "instruction": instruction or "", "role": role}
    if cut != "characters":
        key["cut"] = cut
    return key


def node_directory(key: dict[str, Any]) -> Path:
    """`<LOUISE_ROOT>/state/cpu-reference/<digest of the key>/`, the key
    written beside the files so a person can read what they answer."""
    from app import config
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    d = config.louise_root() / "state" / "cpu-reference" / digest
    d.mkdir(parents=True, exist_ok=True)
    (d / "key.json").write_text(json.dumps(key, sort_keys=True, indent=1), encoding="utf-8")
    return d


def node_find(d: Path, *, set_id: str, count: int, run_id: str | None = None) -> CpuReference | None:
    """This run's kept file in the store, else the newest readable one."""
    if run_id is not None:
        exact = _file_in(d, run_id)
        if exact.is_file():
            try:
                ref = read(exact)
                if ref.set_id == set_id and len(ref.vectors) == count:
                    return ref
            except (OSError, ValueError, KeyError, CodexError):
                pass
    best: tuple[float, CpuReference] | None = None
    for p in d.glob("*.json"):
        if p.name == "key.json":
            continue
        try:
            ref = read(p)
        except (OSError, ValueError, KeyError, CodexError):
            continue
        if ref.set_id != set_id or len(ref.vectors) != count:
            continue
        if best is None or p.stat().st_mtime > best[0]:
            best = (p.stat().st_mtime, ref)
    return best[1] if best else None


def _f4(vectors: Sequence[Sequence[float]]) -> bytes:
    import numpy as np
    return np.asarray(vectors, dtype="<f4").tobytes()


def adopt_journal(old: Path, d: Path, *, run_id: str, set_id: str) -> None:
    """A collection's journal from before the store (2026-11-06) is taken
    over, never lost: the same run's answers to the same texts."""
    new = journal_in(d, run_id=run_id, set_id=set_id)
    if old.is_file() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)


def obtain(d: Path, role: str, *, set_id: str, texts: Sequence[Any], run_id: str,
           run: dict[str, Any], embed, one, again: bool = False, say=None) -> Obtained:
    """The CPU twin's answers to `texts`, from the node's store `d` when they
    are there, computed once otherwise. `embed` and `one` are `runs.Answers`
    of the twin (normalised, and one text alone); `run_id` is the run that
    answers now, `run` its object (driver, engine, weights). `again` makes
    them anew."""
    from . import fingerprint
    from .embedder import reproduces
    say = say or (lambda line: None)
    if again:
        journal_in(d, run_id=run_id, set_id=set_id).unlink(missing_ok=True)
    else:
        kept_ref = node_find(d, set_id=set_id, count=len(texts), run_id=run_id)
        if kept_ref is not None and kept_ref.run_id == run_id:
            return Obtained(kept_ref.vectors, run_id, kept_ref.run_object or run, "stored",
                            kept_ref.path)
        if kept_ref is not None:
            idx = fingerprint.indices(texts)
            got = embed([texts[i] for i in idx])
            if embed.runs == {run_id} and _f4(got) == _f4([kept_ref.vectors[i] for i in idx]):
                path = write_in(d, role, run_id=run_id, run=run, set_id=set_id,
                                vectors=kept_ref.vectors, extra={"carried_from": kept_ref.run_id})
                say(f"carried  {role}  from run {kept_ref.run_id[:23]}…: its {len(idx)} "
                    f"fingerprint answers are this run's bytes  {path}")
                return Obtained(kept_ref.vectors, run_id, run, "carried", path)
            say(f"not carried  {role}  run {kept_ref.run_id[:23]}…: this run answers the "
                f"fingerprint texts otherwise; computed")
    vectors = embed_resumable_in(d, role, run_id=run_id, set_id=set_id, texts=texts,
                                 embed=embed, say=say)
    sixteen = [texts[i] for i in fingerprint.indices(texts)]
    first = one(sixteen)
    back = list(reversed(one(list(reversed(sixteen)))))
    if sixteen and one.runs != {run_id}:
        raise Deferred(f"{role}: answers came from another run than {run_id[:23]}…; "
                       f"nothing is kept")
    if not reproduces(first, back):
        raise NotReproducible(f"{role}: the CPU twin did not give back its own bytes "
                              f"(invariant 12): nothing is kept")
    path = write_in(d, role, run_id=run_id, run=run, set_id=set_id, vectors=vectors)
    journal_in(d, run_id=run_id, set_id=set_id).unlink(missing_ok=True)
    return Obtained(vectors, run_id, run, "computed", path)
