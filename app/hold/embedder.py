"""The calibration gate — is this peer's encoder the same function as the
space's? docs/03-checking.md §3.3; docs/09-formats.md §9.12.

    hold embedder check       the gate, and the statement it produces
    hold embedder benchmark   tokens per second, from the same run
    hold embedder calibrate   the tolerance a space can bear, before a
                              collection exists

WHAT THE GATE IS NOT. It is not trust on first use, and it is not a
constant. 03 §3.1: "No number in this paragraph is a threshold:
`tolerance` comes from calibration and from nowhere else." An encoder is
admitted only by reaching the space's tolerance against the publisher's
reference vectors — never by being the first one this peer happened to
see, which would admit anything.

THE WHOLE SET IS EMBEDDED, and the quorum is drawn from it afterwards
(02 §2.4): "the statement it produces reports over all of it, and because
T0's envelope and T1's probe set are the whole set too". One pass serves
three purposes, so sampling the embedding step would leave the three
disagreeing about what was measured.
"""
from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass
from typing import Any, Sequence

from . import calibration as cal, envelope as env
from .codex import Codex, CodexError, sample

#: 03 §3.1's third outcome. An unreachable encoder is not a failure: it is
#: a deferral, and CLAUDE.md forbids waiting on it without a bound.
DEFERRED = "inconclusive"


class GateError(CodexError):
    """The gate could not run, or the encoder is not compliant."""


class GateDeferred(GateError):
    """03 §3.1: the check could not run — the encoder was unreachable or
    past its bounded wait. Neither a failure nor a pass: no attestation, no
    flag, reported with its reason, and retried. Exit 3 (07 §7.7)."""


@dataclass(frozen=True)
class Run:
    """What one pass of the gate found. `passed`, `worst` and `median` are
    over the SAMPLED texts, not all 512, so the statement carries the very
    numbers its verdict was decided by (03 §3.3, 09 §9.12)."""
    space_id: str
    collection_id: str
    set_id: str
    seed: bytes
    sampled: tuple[int, ...]
    cosines: tuple[float, ...]        # over `sampled`, in `sampled` order
    all_cosines: tuple[float, ...]    # over all 512, for the operator's eye
    tolerance: float
    min_pass: int
    max_input_tokens: int
    tokens_per_s: int

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cosines if c >= self.tolerance)

    @property
    def worst(self) -> float:
        return min(self.cosines)

    @property
    def median(self) -> float:
        return statistics.median(self.cosines)

    @property
    def ok(self) -> bool:
        """03 §3.3, both conjuncts, both over the drawn n: "at least
        `min_pass` reach cosine >= `tolerance` … and the median of those n
        does too"."""
        return self.passed >= self.min_pass and self.median >= self.tolerance

    def why(self) -> str:
        if self.ok:
            return (f"{self.passed}/{len(self.cosines)} reached {self.tolerance}, "
                    f"median {self.median:.4f}")
        parts = []
        if self.passed < self.min_pass:
            parts.append(f"only {self.passed} of {len(self.cosines)} reached "
                         f"{self.tolerance} (needs {self.min_pass})")
        if self.median < self.tolerance:
            parts.append(f"median {self.median:.4f} is below {self.tolerance}")
        return "; ".join(parts)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """03 §3.1: "A cosine is computed in f32: both vectors are upcast from
    the space's `dtype`, re-normalised, and multiplied in f32; there is no
    other precision and no storage penalty anywhere."."""
    import numpy as np
    x = np.asarray(a, dtype=np.float32)
    y = np.asarray(b, dtype=np.float32)
    nx, ny = np.linalg.norm(x), np.linalg.norm(y)
    if nx == 0 or ny == 0:
        return 0.0
    return float(np.dot(x / nx, y / ny))


def run_gate(codex: Codex, *, reference: Sequence[Sequence[float]],
             embed, seed: bytes | None = None,
             collection_id: str | None = None) -> Run:
    """One pass: embed all 512, draw the quorum, compare.

    `embed(texts) -> list[list[float]]` is injected rather than imported so
    the gate stays a pure function of what the endpoint returned — the
    caller owns the HTTP client, its timeout and its bounded wait
    (CLAUDE.md: a pipeline thread never waits without a bound on an
    optional subsystem).
    """
    texts = cal.verify(codex.modality, codex.space.calibration.set)
    if len(reference) != cal.SIZE:
        raise GateError(f"the calibration member carries {len(reference)} "
                        f"reference vectors, the set has {cal.SIZE}")

    started = time.monotonic()
    got = embed(texts)
    elapsed = max(time.monotonic() - started, 1e-9)

    if len(got) != cal.SIZE:
        raise GateError(f"the encoder returned {len(got)} vectors for "
                        f"{cal.SIZE} texts")
    for i, v in enumerate(got):
        if len(v) != codex.space.dim:
            raise GateError(f"text {i}: the encoder returned {len(v)} "
                            f"dimensions, the space is {codex.space.dim}")

    all_cos = tuple(cosine(got[i], reference[i]) for i in range(cal.SIZE))

    seed = seed if seed is not None else os.urandom(32)
    universe = [str(i) for i in range(cal.SIZE)]
    drawn = sample(seed, codex.space.calibration.n, universe)
    sampled = tuple(int(i) for i in drawn)

    # The benchmark is measured on the same pass (03 §3.3). `tokens` is the
    # encoder's own count where the endpoint reports one; absent that, the
    # honest denominator is the set's own token estimate, so this is a
    # LOWER bound on throughput rather than an invented number.
    tokens = sum(max(1, len(t) // 4) for t in texts)

    return Run(
        space_id=codex.space_id,
        collection_id=collection_id or codex.collection_id,
        set_id=codex.space.calibration.set,
        seed=seed, sampled=sampled,
        cosines=tuple(all_cos[i] for i in sampled),
        all_cosines=all_cos,
        tolerance=float(codex.space.tolerance),
        min_pass=codex.space.calibration.min_pass,
        max_input_tokens=codex.space.ctx,
        tokens_per_s=int(tokens / elapsed),
    )


def statement(run: Run, codex: Codex, key, *, now: str) -> dict[str, Any]:
    """09 §9.12's *peer's calibration statement* — an envelope under
    `louise/calibration/v1`, kept in `state/` and published in this peer's
    digest. This is what every other document means by "a compliant
    encoder"; the space author's calibration MEMBER is a different object
    defined in the same section."""
    body = {
        "kind": "louise-calibration-run", "version": 1,
        "space_id": run.space_id,
        "collection_id": run.collection_id,
        "set": run.set_id,
        "encoder_image": codex.space.encoder.image,
        "encoder_weights": codex.space.encoder.weights,
        "seed": run.seed.hex(),
        "sampled": list(run.sampled),
        "passed": run.passed,
        "worst": f"{run.worst:.6f}",
        "median": f"{run.median:.6f}",
        "max_input_tokens": run.max_input_tokens,
        "tokens_per_s": run.tokens_per_s,
        "key": env.key_text(key.verify_key),
        "time": now,
    }
    return env.sign(env.CALIBRATION, body, key)


def read_statement(envelope_: dict[str, Any]) -> dict[str, Any]:
    """The body of a calibration statement, verified. A statement that does
    not verify under `louise/calibration/v1` is not one."""
    body = env.verify(env.CALIBRATION, envelope_)
    if body.get("kind") != "louise-calibration-run":
        raise GateError(f"not a calibration statement: kind="
                        f"{body.get('kind')!r}")
    return body


def benchmarks_entry(body: dict[str, Any]) -> dict[str, Any]:
    """The digest's `benchmarks` row (09 §9.9), keyed by `space_id`.

    Keyed by space because that is how 04 §4.5 reads it: "a peer with no
    such entry being unscored here, since a rate measured on another
    encoder says nothing about this one"."""
    return {"space_id": body["space_id"],
            "encoder_image": body["encoder_image"],
            "encoder_weights": body["encoder_weights"],
            "tokens_per_s": body["tokens_per_s"]}
