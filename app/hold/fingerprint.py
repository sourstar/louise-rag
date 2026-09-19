"""A run's fingerprint — docs/09-formats.md §9.12; docs/03-checking.md §3.3.

    Its sixteen texts are fixed by the set's bytes alone. The set is ordered
    by the UTF-8 byte length of `text`, ties broken by `i`, and the texts at
    ranks ⌊k × 511 / 15⌋ for k = 0 … 15 are taken: the shortest, the longest
    and fourteen spaced between. They are embedded one at a time, truncated
    as a chunk is. A run's fingerprint is `H` over the sixteen vectors, in
    that order, each as the endpoint returned it, converted to little-endian
    f32 and concatenated.

WHY THOSE SIXTEEN. Four of them are near or past a full context window, on
purpose: `-c`, `-b`, `-ub` and rope settings change only long inputs, and a
build that is right on a sentence and wrong on a full window passes every
short check while the corpus takes damage — which is what happened here.

EVIDENCE, NOT A REPEATED CHECK (dd9bef4). The fingerprint goes into a run's
calibration statement; it is not taken after every batch — that measured
12.5-17 s per commit on gutenberg (docs/notes/fingerprint-implications.md).
A peer that runs the same run on the same kind of device reproduces it bit
for bit, which is stronger evidence than any tolerance.
"""
from __future__ import annotations

from typing import Sequence

from .codex import digest

COUNT = 16


def indices(texts: Sequence[str]) -> list[int]:
    """The positions, in the set, of the sixteen texts — in rank order, so
    the shortest first and the longest last. A text taken twice (a set far
    smaller than 512) stays taken twice: the rule is the spec's, not ours."""
    if not texts:
        raise ValueError("an empty set has no fingerprint texts")
    order = sorted(range(len(texts)), key=lambda i: (len(texts[i].encode("utf-8")), i))
    last = len(order) - 1
    return [order[(k * last) // (COUNT - 1)] for k in range(COUNT)]


def of(vectors: Sequence[Sequence[float]]) -> str:
    """`H` over the vectors, in the order given, each as little-endian f32."""
    import numpy as np
    if len(vectors) != COUNT:
        raise ValueError(f"a fingerprint is over {COUNT} vectors, got {len(vectors)}")
    return digest(b"".join(np.asarray(v, dtype="<f4").tobytes() for v in vectors))
