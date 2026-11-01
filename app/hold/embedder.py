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
import re
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import calibration as cal, envelope as env
from .codex import (BACKEND_PLACEHOLDER, ENGINE_VERSION, Codex, CodexError,
                    engine_from_dict, is_backend_args, sample)

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
class StatedRun:
    """The run the platform said gave the answers (09 §9.12): its identity,
    from the `Louise-Run` header, and what that identity stands for, from
    `GET /louise/runs/<run_id>` — the engine as launched, the weights
    actually loaded, the device's driver. VeGa's wrapper computes all of it
    from what LocalAI launched; nothing here is this peer's declaration."""
    run_id: str
    engine: dict[str, Any]
    weights: str
    driver: str


#: THE CROSS-DEVICE BAR (approved-encoders.md, operator 2026-10-18): a run
#: whose answer to any calibration item is below this cosine to the stored
#: CPU reference fails — "sends that engine to further review"
CPU_REVIEW_LINE = 0.995
#: THE REVIEW LINE FOR 4-BIT WEIGHTS (operator, 2026-11-01, choice A). A 4-bit
#: model's CPU and card kernels differ by path, not by fault — the CPU
#: quantizes activations to 8 bits, SYCL does not — so its card stands at
#: ~0.998 against its CPU twin everywhere and lower on the shortest inputs:
#: gutenberg's Qwen3-Embedding-4B Q4_K_M, run 0a80405f, worst 0.989292 (39
#: tokens), p1 0.996898, median 0.998358 over 512, exact over 3 fresh loads,
#: and two CPU engines agreeing 1.000000 on 19 of 19 (München, m6); bge-m3 at
#: F16 against its twin, worst 0.999930. Still far above what a broken
#: engine gives (NaN, 0.0865, 0.126). Every other weights keeps 0.995.
CPU_REVIEW_LINE_4BIT = 0.985
#: llama.cpp's 4-bit types as a GGUF file is named after them
#: (Q4_0, Q4_1, Q4_K_S, Q4_K_M, IQ4_NL, IQ4_XS, MXFP4)
_FOUR_BIT = re.compile(r"(?i)(?:^|[-_.])(?:I?Q4(?:_[A-Z0-9]+)*|MXFP4)(?=[-_.]|$)")
#: what a NEAR-EXACT run must reach against it (operator, 2026-10-31)
CPU_NEAR_LINE = 0.999


def four_bit(engine: Any) -> bool:
    """Whether the run's weights are a 4-bit quantisation: the weights FILE
    LocalAI loaded, as its load options name it (`options.Model`, kept by
    app/vega/loadopts.py, in the stated run and so in its statement), read
    for llama.cpp's type tag. A name, not the file's own header: VeGa states
    the weights by digest, and the wrapper — whose code is part of every run
    — would have to read GGUF's `general.file_type` and state it, a new run
    for every model. That is the standard way and is written down here to
    be taken at the wrapper's next change; a file renamed against its type
    would get the wrong line until then, and either line still fails a
    broken engine."""
    options = (engine or {}).get("options") if isinstance(engine, dict) else None
    model = (options or {}).get("Model") if isinstance(options, dict) else None
    if not isinstance(model, str) or not model:
        return False
    stem = os.path.basename(model)
    for ext in (".gguf", ".bin"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
    return bool(_FOUR_BIT.search(stem))


def cpu_review_line(engine: Any) -> float:
    """The CPU review line a run is held to: CPU_REVIEW_LINE_4BIT for 4-bit
    weights, CPU_REVIEW_LINE for every other."""
    return CPU_REVIEW_LINE_4BIT if four_bit(engine) else CPU_REVIEW_LINE
EXACT, NEAR = "exact", "near"
#: THE MULTI-LOAD GATE (operator, 2026-10-31: "the gate may include multiple
#: runs to ensure that the tolerance is not just a lucky run"): fresh loads
#: measured, and the prefixes of the set's longest item added to the sixteen
#: so every length band is asked (a shape-dependent kernel is found only by an
#: input of that shape, 2026-10-30)
NEAR_LOADS = 3
SWEEP = 32
#: how far apart a near-exact run's answers to one text may be
NEAR_VARIATION = 0.99999


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
    # What a version-2 statement adds (dd9bef4). `stated`: the run that
    # answered, None where the endpoint states none — and then nothing can be
    # held to it (03 §3.3). `fingerprint`: H over the sixteen vectors taken
    # one at a time, as evidence. `batch_invariant`: whether the same sixteen,
    # embedded the way this peer contributes, came back bit for bit.
    stated: StatedRun | None = None
    fingerprint: str | None = None
    batch_invariant: bool | None = None
    role: str = "document"            # "document" | "query"
    # CLAUDE.md invariant 12 (operator, 2026-10-18; measured by the gate since
    # 2026-10-25, the operator's choice A): the sixteen embedded one at a
    # time a second time, in reverse order, came back byte for byte. False
    # fails the run whatever its cosines: a vector its own run cannot
    # reproduce cannot be checked by anyone. None where no single-text pass
    # was asked for.
    reproducible: bool | None = None
    # THE STORED CPU REFERENCE (operator, 2026-10-31; docs/notes/near-exact-
    # runs.md §2): the CPU twin's run that made it, and every one of the 512
    # answers' cosine to it. None where none is stored for the run's weights.
    cpu_run: str | None = None
    cpu_cosines: tuple[float, ...] | None = None
    # how exact the run is (§3): "exact", one load, until the multi-load gate
    exactness: str = EXACT
    loads: int = 1
    load_variation: float = 1.0
    #: the CPU review line this run is held to (`cpu_review_line`)
    cpu_line: float = CPU_REVIEW_LINE

    @property
    def cpu_worst(self) -> float | None:
        return min(self.cpu_cosines) if self.cpu_cosines else None

    @property
    def cpu_median(self) -> float | None:
        return statistics.median(self.cpu_cosines) if self.cpu_cosines else None

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
        does too"; and how exact the run is (`runs.passing` reads the same
        from the statement)."""
        if not (self.passed >= self.min_pass and self.median >= self.tolerance):
            return False
        if self.cpu_worst is not None and self.cpu_worst < self.cpu_line:
            return False
        if self.exactness == EXACT:
            return self.reproducible is not False and self.load_variation >= 1.0
        return (self.loads >= NEAR_LOADS and self.load_variation >= NEAR_VARIATION
                and self.cpu_worst is not None and self.cpu_worst >= CPU_NEAR_LINE)

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
        if self.exactness == NEAR:
            if self.loads < NEAR_LOADS or self.load_variation < NEAR_VARIATION:
                parts.append(f"near-exact: over {self.loads} fresh load(s) its answers to one "
                             f"text came within {self.load_variation:.7f}, and a near-exact run "
                             f"needs {NEAR_LOADS} loads within {NEAR_VARIATION}")
            if self.cpu_worst is None or self.cpu_worst < CPU_NEAR_LINE:
                parts.append(f"near-exact: needs a stored CPU reference reached at "
                             f"{CPU_NEAR_LINE} ({'none is stored' if self.cpu_worst is None else f'worst {self.cpu_worst:.6f}'}; "
                             f"`louise hold embedder cpu-reference`)")
        elif self.load_variation < 1.0 and self.reproducible is not False:
            parts.append(f"the run did not give back its own bytes across {self.loads} fresh "
                         f"loads (closest {self.load_variation:.7f}), and its collection asks "
                         f"`exactness = \"exact\"`: bytes or nothing. Without that key a run "
                         f"is held to the near-exact bound (CLAUDE.md invariant 12, operator "
                         f"2026-11-01; docs/notes/near-exact-runs.md)")
        if self.reproducible is False and self.exactness == EXACT:
            parts.append("the run did not give back its own bytes: the sixteen fingerprint texts, "
                         "embedded one at a time again in reverse order, came back different, "
                         "and its collection asks `exactness = \"exact\"` (CLAUDE.md invariant "
                         "12; set the vendor's reproducibility mode and one request at a time, "
                         "docs/notes/approved-encoders.md)")
        if self.cpu_worst is not None and self.cpu_worst < self.cpu_line:
            parts.append(f"against the stored CPU reference the worst of the 512 is "
                         f"{self.cpu_worst:.6f}, below the review line {self.cpu_line} "
                         f"(operator, 2026-10-18; 4-bit weights {CPU_REVIEW_LINE_4BIT}, "
                         f"2026-11-01): nothing is written from this run until a "
                         f"person has reviewed it (docs/notes/near-exact-runs.md)")
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


#: what f32 rounding alone moves a cosine of two unit vectors by, so two
#: answers whose cosines differ by less are the same answer to the gate
COSINE_ROUNDING = 1e-6


def batch_invariant(single: Sequence[Sequence[float]], batched: Sequence[Sequence[float]]) -> bool:
    """03 §3.3's batch check (*amended 2026-10-25*, operator's choice A,
    CLAUDE.md invariant 12): the batched answers are the single-text ones
    byte for byte, or the run is validated for one-at-a-time work only. The
    band of 2026-10-12 (batched answers within the single-text passes'
    own variation) is gone: under invariant 12 a run's single-text passes
    are equal, so the only band left was f32 rounding, and a batched vector
    that a checker embedding one text cannot reproduce is not admitted."""
    from . import fingerprint
    return len(batched) == len(single) and fingerprint.of(batched) == fingerprint.of(single)


def reproduces(single: Sequence[Sequence[float]], again: Sequence[Sequence[float]]) -> bool:
    """Invariant 12 measured: the same texts one at a time, a second pass
    (the caller sends it in reverse order), give the same bytes."""
    from . import fingerprint
    return len(again) == len(single) and fingerprint.of(again) == fingerprint.of(single)


def sweep(texts: Sequence, count: int, window: int | None) -> list:
    """`count` prefixes of the longest item (a pair's second segment, its
    first kept whole), from one word up to `window` words, spread evenly:
    every length band, as tools/vega_repro_check's `--sweep` asks."""
    if count <= 0 or not texts:
        return []
    longest = max(texts, key=lambda t: len(t[1]) if isinstance(t, tuple) else len(t))
    first, body = longest if isinstance(longest, tuple) else (None, longest)
    words = body.split()
    top = min(len(words), window) if window else len(words)
    if top <= 0:
        return []
    n = min(count, top)
    sizes = sorted({1 + (k * (top - 1)) // max(1, n - 1) for k in range(n)})
    return [(first, " ".join(words[:m])) if first is not None else " ".join(words[:m])
            for m in sizes]


def fresh_loads(texts: Sequence, embed_one, reload: Callable[[], None],
                seed: bytes, loads: int = NEAR_LOADS) -> tuple[int, float]:
    """(loads, the closest two answers of any one text — 1.0 when every text
    came back as one byte string). Each load is fresh (`reload`) and asks the
    texts one at a time in its own order."""
    import random
    import numpy as np
    answers: list[dict[bytes, list[float]]] = [{} for _ in texts]
    rng = random.Random(seed)
    for _ in range(loads):
        reload()
        order = list(range(len(texts)))
        rng.shuffle(order)
        got = embed_one([texts[k] for k in order])
        for k, v in zip(order, got):
            arr = np.asarray(v, dtype="<f4")
            if not np.all(np.isfinite(arr)):
                # A NaN is a wrong answer, not a variation: the run cannot be
                # exact or near, however often it comes back the same (P5 with
                # FA on gave one, 2026-10-31)
                return loads, 0.0
            answers[k].setdefault(arr.tobytes(), list(v))
    closest = 1.0
    for seen in answers:
        vs = list(seen.values())
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                closest = min(closest, min(cosine(vs[i], vs[j]), 1.0 - 1e-12))
    return loads, closest


def gate_texts(codex: Codex, role: str, items: Sequence | None = None) -> list:
    """The 512 items as the gate sends them for `role`: the space's set (a
    pair space's from its member, `items`); on the query side each as one
    text, with the space's instruction before it. One definition, so the
    stored CPU reference is made of exactly what the gate compares."""
    if role not in ("document", "query"):
        raise GateError(f"role is document or query, got {role!r}")
    if items is None:
        if cal.form(codex) != "text":
            raise GateError("a pair space's calibration set travels in its member "
                            "(09 §9.12); the gate is given it, and was not")
        items = cal.verify(codex.modality, codex.space.calibration.set)
    texts = list(items)
    if role == "query":
        if codex.space.query.encoder is None:
            raise GateError("this space is symmetric: it has no query run to gate — "
                            "queries and documents come from one run (03 §3.3)")
        texts = [cal.query_text(t) for t in texts]
        instruction = codex.space.query.instruction
        if instruction:
            texts = [f"{instruction}{t}" for t in texts]
    return texts


def run_gate(codex: Codex, *, reference: Sequence[Sequence[float]],
             embed, seed: bytes | None = None,
             collection_id: str | None = None,
             role: str = "document",
             stated: StatedRun | None = None,
             embed_one=None, contribute=None,
             items: Sequence[cal.Item] | None = None,
             cpu_reference: tuple[str, Sequence[Sequence[float]]] | None = None,
             reload: Callable[[], None] | None = None,
             exactness: str = EXACT) -> Run:
    """One pass: embed all 512, draw the quorum, compare.

    `cpu_reference`: (the CPU twin's run, its 512 vectors) — the stored CPU
    reference (docs/notes/near-exact-runs.md §2): every answer's cosine to it
    is kept, and a worst below CPU_REVIEW_LINE fails the run.

    `reload`: loads the model afresh. Given it, the fresh-load half of
    invariant 12 is measured here (§3): NEAR_LOADS loads, each asking the
    sixteen and SWEEP prefixes of the longest item one at a time in its own
    order. Every text one byte string: exact. Otherwise the closest two
    answers of a text are `load_variation`, and the run is near-exact only
    where `exactness` is NEAR — never downgraded unasked.

    `embed(texts) -> list[list[float]]` is injected rather than imported so
    the gate stays a pure function of what the endpoint returned — the
    caller owns the HTTP client, its timeout and its bounded wait
    (CLAUDE.md: a pipeline thread never waits without a bound on an
    optional subsystem).

    `role="query"` gates an asymmetric space's QUERY run: the same texts
    with `space.query.instruction` prefixed, against the member's
    `query_vectors` (09 §9.12) — the caller passes those as `reference` and
    the query encoder as `embed`.

    WHAT THE ANSWERS DEPEND ON BESIDES THE RUN (03 §3.3). `embed_one` embeds
    the sixteen fingerprint texts one at a time, as the endpoint returned
    them: their hash is the run's FINGERPRINT, evidence in the statement and
    never a check the worker repeats (dd9bef4). `contribute` embeds the same
    sixteen the way this peer really contributes — its batching, its
    concurrency; equal bytes mean the run is batch-invariant at those
    settings, and a run that is not is validated for single-text work only.
    The batching fault that silently corrupted 714 books (2026-07) is a
    property of a run's settings: measured here, never assumed.

    THE FORM THE PIPELINE WRITES (09 §9.12, amended 2026-10-08). `items` is
    the space's set as its encoder is fed it (`calibration.items`): a text
    space's texts, read from the platform when not given; a PAIR space's
    pairs, which travel in its member, so the caller that holds the member
    passes them. On the document side a pair is embedded as a pair — every
    pass, the fingerprint and the batches alike — and the query side gets
    each item as one text (`calibration.query_text`).
    """
    texts = gate_texts(codex, role, items)
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
    cpu_run = cpu_cos = None
    if cpu_reference is not None:
        cpu_run, cpu_vectors = cpu_reference
        if len(cpu_vectors) != cal.SIZE:
            raise GateError(f"the stored CPU reference holds {len(cpu_vectors)} vectors, "
                            f"the set has {cal.SIZE}")
        cpu_cos = tuple(cosine(got[i], cpu_vectors[i]) for i in range(cal.SIZE))

    seed = seed if seed is not None else os.urandom(32)
    universe = [str(i) for i in range(cal.SIZE)]
    drawn = sample(seed, codex.space.calibration.n, universe)
    sampled = tuple(int(i) for i in drawn)

    # The benchmark is measured on the same pass (03 §3.3). `tokens` is the
    # encoder's own count where the endpoint reports one; absent that, the
    # honest denominator is the set's own token estimate, so this is a
    # LOWER bound on throughput rather than an invented number.
    tokens = sum(max(1, cal.item_chars(t) // 4) for t in texts)

    taken = invariant = reproducible = None
    variation = 1.0
    loads = 1
    if embed_one is not None:
        from . import fingerprint
        sixteen = [texts[i] for i in fingerprint.indices(texts)]
        single = embed_one(sixteen)
        taken = fingerprint.of(single)
        # invariant 12, in another order (the fresh-load half stays the
        # measurement harness's: llama-intel-arc-b70 capture.py)
        again = list(reversed(embed_one(list(reversed(sixteen)))))
        reproducible = reproduces(single, again)
        # how far apart the two passes are: 1 when byte-equal; carried in the
        # statement, which is what a write is admitted by (`runs.passing`)
        variation = 1.0 if reproducible else min(
            (cosine(a, b) for a, b in zip(single, again)), default=0.0)
        if contribute is not None:
            batched = contribute(sixteen)
            invariant = batch_invariant(single, batched)
            if not invariant and exactness == NEAR and len(batched) == len(single):
                # BATCHES HELD TO THE RUN'S OWN BOUND (operator, 2026-11-01,
                # choice B): a near-exact run's batched answers may differ in
                # their last bits from its single-text ones, as its loads may;
                # within NEAR_VARIATION they are the same run's answers, and
                # the difference counts toward the run's variation. Further
                # apart, the batching is refused as before — and the run stays
                # what its single-text answers make it, for one text a request
                apart = min((cosine(a, b) for a, b in zip(single, batched)), default=0.0)
                invariant = apart >= NEAR_VARIATION
                if invariant:
                    variation = min(variation, apart)
        if reload is not None:
            loads, across = fresh_loads(sixteen + sweep(texts, SWEEP, codex.space.ctx),
                                        embed_one, reload, seed or b"")
            variation = min(variation, across)

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
        stated=stated, fingerprint=taken, batch_invariant=invariant, role=role,
        reproducible=reproducible, cpu_run=cpu_run, cpu_cosines=cpu_cos,
        load_variation=variation, loads=loads,
        exactness=NEAR if (exactness == NEAR and variation < 1.0) else EXACT,
        cpu_line=cpu_review_line(stated.engine if stated is not None else None),
    )


def statement(run: Run, codex: Codex, key, *, now: str) -> dict[str, Any]:
    """09 §9.12's *peer's calibration statement* — an envelope under
    `louise/calibration/v1`, kept in `state/` and published in this peer's
    digest. This is what every other document means by "a compliant
    encoder"; the space author's calibration MEMBER is a different object
    defined in the same section."""
    if codex.version >= ENGINE_VERSION:
        return _statement_run(run, key, now=now)
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


#: 09 §9.12's statement versions: what is WRITTEN, and what is READ. Version
#: 3 is the options amendment's (operator, 2026-10-03): its `engine` may carry
#: `options`. "A version-2 statement stays valid as written and is read
#: beside version 3: its run is unchanged, and gating it again would
#: recompute a verdict already held" — so 2 is read for ever, and 1 with it.
STATEMENT_VERSION = 3
#: Version 4 is the VeGa amendment's (2026-10-08; 09 §9.12, §9.18): "a
#: backend engine's run; every other run is stated at 3". No key was added;
#: a backend engine is told by `${BACKEND}/` in its program (09 §9.3), and
#: a reader of version 3 would take its tree digest for an image.
BACKEND_STATEMENT_VERSION = 4
#: Version 5 (2026-10-31, operator's choice C; docs/notes/near-exact-runs.md
#: §4): every run's statement — a backend engine's or not, told as before by
#: `${BACKEND}/` in its program — with `exactness`, `loads`, `load_variation`
#: and `cpu_reference`. Versions 3 and 4 stay read: "exact, one load, no CPU
#: reference".
NEAR_EXACT_VERSION = 5
NEAR_EXACT_KEYS = ("exactness", "loads", "load_variation", "cpu_reference")
STATEMENT_VERSIONS = (1, 2, 3, 4, 5)


def statement_version(engine: Any) -> int:
    """The version a statement is written at: 5 for every run since
    2026-10-31 (NEAR_EXACT_VERSION: how exact it is and how it stands against
    the stored CPU reference are part of every verdict). Before it, 4 for a
    backend engine's run and 3 for every other; those stay read as written."""
    return NEAR_EXACT_VERSION


def _statement_run(run: Run, key, *, now: str) -> dict[str, Any]:
    """09 §9.12's body at version 3 — at 4 for a backend engine's run
    (`statement_version`): "this RUN, on this peer, is compliant
    with this space" (dd9bef4). A peer keeps one per run it has validated,
    and a vector is written only from an answer naming a run with a passing
    one. Version 2 is this body with an engine that cannot carry `options`;
    "Version 1 of this body carried `encoder_image` … in place of the run,
    and had no fingerprint."

    THE ENGINE IS CHECKED BEFORE IT IS SIGNED, by the rules a Codex holds
    an engine to (09 §9.3): a statement is published, and a peer that
    adopted one naming a malformed engine — `options` naming a file no
    weights digest covers, say — could not repair it.

    IT NEEDS A RUN TO NAME. 03 §3.3: "An endpoint that states no run
    identity cannot be held to a validated run: a peer may search with it
    and writes nothing through it" — so for such an endpoint the gate's
    verdict is reported and no statement is kept, because there is nothing
    a later answer could be matched against."""
    if run.stated is None:
        raise GateError(
            "the endpoint states no run identity (no `Louise-Run` on its answers), "
            "so this pass cannot be kept: a calibration statement says WHICH run "
            "is compliant, and a vector is written only from an answer naming a "
            "validated one (03 §3.3). Search works through such an endpoint; "
            "writing needs an encoder that states the run of every answer: "
            "`api = \"vega\"`, whose receipts name it (docs/notes/vega.md)")
    if run.fingerprint is None or run.batch_invariant is None:
        raise GateError("a version-2 statement carries the run's fingerprint and "
                        "whether it is batch-invariant; this pass measured neither")
    if run.role not in ("document", "query"):
        raise GateError(f"role is document or query, got {run.role!r}")
    try:
        _validate_stated_engine(run.stated.engine)
    except CodexError as e:
        raise GateError(f"the run's engine cannot be signed into a statement: {e}") from e
    body = {
        "kind": "louise-calibration-run",
        "version": statement_version(run.stated.engine),
        "space_id": run.space_id,
        "collection_id": run.collection_id,
        "set": run.set_id,
        "role": run.role,
        "run": run.stated.run_id,
        "engine": run.stated.engine,
        "encoder_weights": run.stated.weights,
        "driver": run.stated.driver,
        "fingerprint": run.fingerprint,
        "batch_invariant": run.batch_invariant,
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
    if body["version"] >= 5:
        # how exact the run is, and how it stands against the stored CPU
        # reference (docs/notes/near-exact-runs.md §4)
        body["exactness"] = run.exactness
        body["loads"] = run.loads
        body["load_variation"] = _decimal(run.load_variation)
        body["cpu_reference"] = (None if run.cpu_run is None else
                                 {"run": run.cpu_run, "worst": f"{run.cpu_worst:.6f}",
                                  "median": f"{run.cpu_median:.6f}"})
    return env.sign(env.CALIBRATION, body, key)


def _decimal(x: float) -> str:
    """A load variation as 09 §9.1's decimal string: "1" when the answers
    were byte-equal, and never a string that reads as 1 when they were not
    (0.99999999 is not exact, and rounded to six places it would say so)."""
    if x >= 1.0:
        return "1"
    out = f"{x:.9f}"
    return "0.999999999" if out.startswith("1") else out


def _validate_stated_engine(engine: Any) -> None:
    from .codex import _validate_engine
    _validate_engine(engine_from_dict(engine, "engine"), "engine")


def read_statement(envelope_: dict[str, Any]) -> dict[str, Any]:
    """The body of a calibration statement, verified. A statement that does
    not verify under `louise/calibration/v1` is not one, and a version this
    build does not implement is refused, never half-read (09 §9.18)."""
    body = env.verify(env.CALIBRATION, envelope_)
    if body.get("kind") != "louise-calibration-run":
        raise GateError(f"not a calibration statement: kind="
                        f"{body.get('kind')!r}")
    version = body.get("version")
    if isinstance(version, bool) or version not in STATEMENT_VERSIONS:
        raise GateError(f"calibration statement version {version!r}; this build reads "
                        f"{', '.join(str(v) for v in STATEMENT_VERSIONS)} (09 §9.18)")
    if version == 2 and isinstance(body.get("engine"), dict) and "options" in body["engine"]:
        raise GateError("a version-2 calibration statement names an engine without "
                        "`options`; one that carries them is a version-3 body (09 §9.12)")
    if version >= 2:
        # 09 §9.12, §9.18 (the VeGa amendment): version 4 is written for a
        # backend engine's run ALONE, and a backend engine is admitted from
        # it — below it `${BACKEND}` is a placeholder the schema does not know
        engine = body.get("engine")
        args = engine.get("args") if isinstance(engine, dict) else None
        backend = is_backend_args(args)
        carries = isinstance(args, list) and any(
            isinstance(a, str) and BACKEND_PLACEHOLDER in a for a in args)
        if version < BACKEND_STATEMENT_VERSION and carries:
            raise GateError(
                f"a version-{version} calibration statement names an engine carrying "
                f"{BACKEND_PLACEHOLDER}; a backend engine's run is stated at version "
                f"{BACKEND_STATEMENT_VERSION} or later (09 §9.3, §9.12)")
        if version == BACKEND_STATEMENT_VERSION and not backend:
            raise GateError(
                f"a version {version} calibration statement is written for a backend "
                f"engine's run alone ({BACKEND_PLACEHOLDER}/ in its program); every "
                f"other run is stated at {STATEMENT_VERSION} (09 §9.12, §9.18)")
        missing = [k for k in NEAR_EXACT_KEYS if k not in body]
        if version >= 5 and missing:
            raise GateError(f"a version-5 calibration statement carries "
                            f"{', '.join(NEAR_EXACT_KEYS)}; this one lacks {', '.join(missing)}")
        if version >= 5 and body.get("exactness") not in (EXACT, NEAR):
            raise GateError(f"exactness is {EXACT!r} or {NEAR!r}, got {body.get('exactness')!r}")
    return body


def benchmarks_entry(body: dict[str, Any]) -> dict[str, Any]:
    """The digest's `benchmarks` row (09 §9.9), keyed by `space_id`.

    Keyed by space because that is how 04 §4.5 reads it: "a peer with no
    such entry being unscored here, since a rate measured on another
    encoder says nothing about this one"."""
    if body.get("version", 1) >= 2:
        # §9.9 version 2: the engine of "the validated run this peer
        # contributes with", by its id
        from .codex import canonical_json, digest
        return {"space_id": body["space_id"],
                "encoder_engine": digest(canonical_json(body["engine"])),
                "encoder_weights": body["encoder_weights"],
                "tokens_per_s": body["tokens_per_s"]}
    return {"space_id": body["space_id"],
            "encoder_image": body["encoder_image"],
            "encoder_weights": body["encoder_weights"],
            "tokens_per_s": body["tokens_per_s"]}
