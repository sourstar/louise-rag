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
    actually loaded, the device's driver. RAGE computes all of it from what
    it launched; nothing here is this peer's declaration."""
    run_id: str
    engine: dict[str, Any]
    weights: str
    driver: str


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


#: what f32 rounding alone moves a cosine of two unit vectors by, so two
#: answers whose cosines differ by less are the same answer to the gate
COSINE_ROUNDING = 1e-6


def within_band(single: Sequence[Sequence[float]], batched: Sequence[Sequence[float]],
                again) -> bool:
    """03 §3.3's batch check (amended 2026-10-12, operator: amendment b).
    Equal bytes pass at once. Otherwise the batched answers are judged
    against the run's OWN run-to-run variation: `again()` embeds the same
    texts one at a time a second time, and every batched vector must be as
    close to its single-text answer as the closest-to-worst single-text
    repeat is — the lowest cosine between the two single-text passes,
    less f32 rounding. A batching fault like 2026-07's (a vector for
    another text) is far outside any such band; last-bit noise is not."""
    from . import fingerprint
    if fingerprint.of(batched) == fingerprint.of(single):
        return True
    if len(batched) != len(single):
        return False
    repeat = again()
    if len(repeat) != len(single):
        return False
    band = min(cosine(a, b) for a, b in zip(single, repeat))
    return min(cosine(a, b) for a, b in zip(single, batched)) >= band - COSINE_ROUNDING


def run_gate(codex: Codex, *, reference: Sequence[Sequence[float]],
             embed, seed: bytes | None = None,
             collection_id: str | None = None,
             role: str = "document",
             stated: StatedRun | None = None,
             embed_one=None, contribute=None,
             items: Sequence[cal.Item] | None = None) -> Run:
    """One pass: embed all 512, draw the quorum, compare.

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
    tokens = sum(max(1, cal.item_chars(t) // 4) for t in texts)

    taken = invariant = None
    if embed_one is not None:
        from . import fingerprint
        sixteen = [texts[i] for i in fingerprint.indices(texts)]
        single = embed_one(sixteen)
        taken = fingerprint.of(single)
        if contribute is not None:
            invariant = within_band(single, contribute(sixteen), lambda: embed_one(sixteen))

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
STATEMENT_VERSIONS = (1, 2, 3, 4)


def statement_version(engine: Any) -> int:
    """The version a statement of a run with this engine is written at: 4
    for a backend engine's run and only for it, 3 for every other — so no
    statement, `engine_id` or run already stated moves (09 §9.12)."""
    args = engine.get("args") if isinstance(engine, dict) else None
    return BACKEND_STATEMENT_VERSION if is_backend_args(args) else STATEMENT_VERSION


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
            "writing needs the node's model container, RAGE")
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
    return env.sign(env.CALIBRATION, body, key)


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
                f"{BACKEND_STATEMENT_VERSION} (09 §9.3, §9.12)")
        if version == BACKEND_STATEMENT_VERSION and not backend:
            raise GateError(
                f"a version {version} calibration statement is written for a backend "
                f"engine's run alone ({BACKEND_PLACEHOLDER}/ in its program); every "
                f"other run is stated at {STATEMENT_VERSION} (09 §9.12, §9.18)")
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
