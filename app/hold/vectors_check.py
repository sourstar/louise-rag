"""`hold vectors check [--drop]` — 03 §3.2's `vectors-sample`, locally.

Migration plan step 4h, the last of step 4. 07 §7.3: "`vectors check` is
`hold check --checks vectors-sample` over this peer's own vectors;
`--drop` turns a failing row into an empty one (02 §2.4) so the coverage
map stops claiming it." So the CHECK lives here and step 5's plan runner
calls it; the verb is the same check with one plan entry.

WHAT IT IS, from 03 §3.2's row: "in a seeded sample of shards, sixteen
chunks each, re-embedded with this peer's compliant encoder, reach cosine
>= `tolerance` on the calibration quorum (§3.3)". Two numbers with two
different owners, and the migration plan is explicit about the split:
**the plan sets how many SHARDS and never how many CHUNKS**. Sixteen is
the Codex's `space.calibration.n` and travels with the space, so two
peers checking the same collection draw the same size of sample whatever
their local plans say. A plan that could set it would let a peer publish
an attestation that looked like everyone else's and rested on four
chunks.

THE SEED IS THIS PEER'S. `codex.sample` is 09 §9.1's one draw and the
seed is published in the attestation, so the sample is reproducible by
anyone afterwards and predictable by no one before: a verifier cannot be
steered toward the shards a producer prepared, and cannot be accused of
having chosen them either.

WHAT A FAILURE MEANS HERE, and it is not what T3's means. T3 judges a
bundle ARRIVING: a producer's vectors against this peer's encoder, and a
failure refuses the bundle. This judges what is ALREADY STORED, whoever
put it there — this peer's own encoder included — so a failure is not a
verdict about a sender. It is a row that no longer reproduces, and 02
§2.4's remedy is to empty it so that nothing claims it: `--drop`.
Without `--drop` the check reports and changes nothing, because deleting
a stranger's vectors on a cosine is a decision an operator makes.

INCONCLUSIVE IS A RESULT (03 §3.1). No compliant encoder, or a sample
that turned up no stored vectors at all, is "neither a failure nor a
pass" — and reporting it as a pass is the one outcome that would let an
empty table certify itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import codex as cx
from . import snapshot as snap
from .codex import CodexError

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"

#: 03 §3.2's plan entry this implements.
CHECK = "vectors-sample"


class VectorsCheckError(CodexError):
    """The check could not be set up — not a verdict about the vectors."""


@dataclass(frozen=True)
class ShardResult:
    shard: str
    verdict: str                    # pass | fail | inconclusive, per shard
    sampled: int                    # chunk ids drawn
    checked: int                    # of those, held here with text AND a vector
    passed: int
    median: float
    worst: float
    failing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    verdict: str
    seed: bytes
    tolerance: float
    min_pass: int
    n: int
    shards: tuple[ShardResult, ...]
    dropped: tuple[str, ...] = ()
    why: str = ""

    @property
    def failing(self) -> tuple[str, ...]:
        return tuple(c for s in self.shards for c in s.failing)


def plan_fraction(codex) -> str:
    """The plan's shard fraction for this check (03 §3.2).

    `validation.sample` is keyed by the check's own name where it has
    one, and `vectors-sample` is spelled `chunks` in the recipe shape 09
    §9.3 fixes — the same universe, the chunks of a shard. A plan with
    neither key falls back to every shard, which is the honest reading of
    a plan that declined to sample: it did not ask for less.
    """
    s = codex.validation.sample or {}
    for key in (CHECK, "chunks"):
        entry = s.get(key)
        if isinstance(entry, dict) and entry.get("shards"):
            return str(entry["shards"])
    return "1"


def shards_for(codex, snapshot: snap.Snapshot, *, seed: bytes,
               fraction: str | None = None) -> list[snap.Shard]:
    """The seeded sample of shards, at the plan's fraction or above it.

    07 §7.3: "`--sample F` raises the plan's sample for this run and
    cannot lower it" — enforced by the caller passing a fraction, and by
    this taking the LARGER of the two.
    """
    from decimal import Decimal
    want = Decimal(plan_fraction(codex))
    if fraction is not None:
        want = max(want, Decimal(str(fraction)))
    names = {f"{s.slice}/{s.n:05d}": s for s in snapshot.shards}
    if not names:
        raise VectorsCheckError(
            f"snapshot {snapshot.snapshot_id} holds no shard to sample")
    n = cx.sample_size(str(want), len(names))
    return [names[k] for k in cx.sample(seed, n, iter(sorted(names)))]


def picked_inputs(shard_tree: str, codex, path: str, seed: bytes, n: int
                  ) -> tuple[list[str], dict[str, Any]]:
    """One sampled part, cut and sampled: `(the picked chunk ids, what is
    re-embedded for each)`. Module-level, so the pool's children run it
    (app/parallel.py `each_part`): cutting a part is the expensive half of
    this check, and only sixteen ids and their inputs come back."""
    from pathlib import Path
    from app.pack import records as pack_records
    from . import land
    cut = land.cut(codex, list(pack_records.iter_records(Path(shard_tree) / path)))
    ids = sorted(cut)
    picked = cx.sample(seed, min(n, len(ids)), iter(ids)) if ids else []
    # a pair layer's chunk with its `fields`, re-cut from the record; a text
    # layer's text, as ever
    return picked, {cid: land.input_of(cut, cid) for cid in picked}


def check(codex, snapshot: snap.Snapshot, *, seed: bytes,
          vectors_for: Callable[[list[str]], dict[str, list]],
          embed: Callable[[list[str]], list[list[float]]] | None,
          shard_tree=None,
          chunk_ids_for: Callable[[snap.Shard], Sequence[str]] | None = None,
          text_of: Callable[[str], str | None] | None = None,
          fraction: str | None = None,
          drop: Callable[[list[str]], Any] | None = None) -> Report:
    """Run `vectors-sample` over this peer's own vectors.

    Everything about this peer arrives as a callable, as everywhere else
    under `app/hold/`: the module is a pure function of the Codex, the
    snapshot and what the caller can look up. The sampled parts are read
    from `shard_tree`, each cut in the parse pool's processes while this
    one embeds (`picked_inputs`); or, given `chunk_ids_for` and `text_of`,
    here.
    """
    import numpy as np

    tol = float(codex.space.tolerance)
    quorum = codex.space.calibration
    n = quorum.n                      # SIXTEEN, and from the space (see above)
    sampled = shards_for(codex, snapshot, seed=seed, fraction=fraction)

    if embed is None:
        return Report(INCONCLUSIVE, seed, tol, quorum.min_pass, n, (),
                      why="no compliant encoder on this peer: 03 §3.1's third "
                          "outcome, neither a failure nor a pass")

    if shard_tree is not None:
        from app import parallel
        prepared = parallel.each_part(picked_inputs, [(str(shard_tree), codex, e.path, seed, n)
                                                      for e in sampled])
    else:
        def here(entry):
            ids = list(chunk_ids_for(entry))
            picked = cx.sample(seed, min(n, len(ids)), iter(ids)) if ids else []
            return picked, {cid: text_of(cid) for cid in picked}
        prepared = (here(e) for e in sampled)
    try:
        return _check(codex, seed, tol, quorum, n, sampled, prepared, vectors_for, embed, drop)
    finally:
        prepared.close()


def _check(codex, seed, tol, quorum, n, sampled, prepared, vectors_for, embed, drop) -> Report:
    import numpy as np
    results: list[ShardResult] = []
    failing_all: list[str] = []
    checked_total = 0
    for entry, (picked, inputs) in zip(sampled, prepared):
        name = f"{entry.slice}/{entry.n:05d}"
        held = vectors_for(picked) if picked else {}
        pairs = [(cid, inputs.get(cid)) for cid in picked if cid in held]
        pairs = [(cid, t) for cid, t in pairs if t]
        if not pairs:
            results.append(ShardResult(
                shard=name, verdict=INCONCLUSIVE, sampled=len(picked),
                checked=0, passed=0, median=float("nan"), worst=float("nan")))
            continue

        mine = np.asarray(embed([t for _, t in pairs]), dtype=np.float64)
        mine = mine / np.clip(np.linalg.norm(mine, axis=1, keepdims=True), 1e-12, None)
        stored = np.asarray([held[cid] for cid, _ in pairs], dtype=np.float64)
        stored = stored / np.clip(np.linalg.norm(stored, axis=1, keepdims=True),
                                  1e-12, None)
        cos = np.sum(mine * stored, axis=1)
        bad = tuple(cid for (cid, _), c in zip(pairs, cos) if c < tol)
        passed = int((cos >= tol).sum())
        # The quorum is over the chunks actually checkable here, scaled:
        # a shard that yielded four of sixteen must not be judged against
        # a threshold written for sixteen.
        need = max(1, round(quorum.min_pass * len(pairs) / n))
        results.append(ShardResult(
            shard=name, verdict=PASS if passed >= need else FAIL,
            sampled=len(picked), checked=len(pairs), passed=passed,
            median=float(np.median(cos)), worst=float(cos.min()), failing=bad))
        failing_all.extend(bad)
        checked_total += len(pairs)

    if not checked_total:
        return Report(INCONCLUSIVE, seed, tol, quorum.min_pass, n,
                      tuple(results),
                      why=f"{len(sampled)} shard(s) sampled and not one chunk "
                          f"is held here with both its text and a vector: "
                          f"there is nothing to reproduce, which is not a pass")

    dropped: tuple[str, ...] = ()
    if drop is not None and failing_all:
        drop(list(failing_all))
        dropped = tuple(failing_all)

    failed = [r for r in results if r.verdict == FAIL]
    verdict = FAIL if failed else PASS
    why = (f"{len(failed)} of {len(results)} sampled shard(s) missed the "
           f"quorum at tolerance {tol}" if failed else
           f"{checked_total} chunk(s) over {len(results)} sampled shard(s) "
           f"reproduce at tolerance {tol}")
    if dropped:
        why += f"; {len(dropped)} row(s) emptied (02 §2.4)"
    return Report(verdict, seed, tol, quorum.min_pass, n, tuple(results),
                  dropped, why)
