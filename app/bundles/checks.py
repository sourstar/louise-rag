"""Admission checks run before any imported vector reaches the index.

WHY THIS IS MORE THAN T0. The verification ladder's T0 tier (dim, dtype,
unit-norm, NaN, duplicates) is satisfied by ANY competent embedding model — and
1024 dimensions is shared by bge-m3, e5-large and many others. So a bundle
produced with a different model, whether by malice or by someone repointing
EMBED_MODEL and re-exporting, passes T0 with flying colours while being
semantically unrelated to this node's query encoder. Those vectors do not fail
loudly; they surface at random and displace relevant passages.

Four gates, cheapest first. Only the last can catch a different real model, and
only verify-on-use (app/bundles/verify_on_use.py) can catch a handful of
deliberately mismapped vectors inside an otherwise honest bundle.

  T0_STRUCTURAL   dim/dtype/norm/NaN/duplicates            free
  ISOTROPY        vs the uniform-on-the-sphere null        free
  OVERLAP         adjacent-chunk cosine vs random pairs    free
  SAMPLE_REEMBED  n chunks re-embedded locally             ~1 embed call each

RELATIONSHIP TO P1.2. The verification ladder proper (app/verify/) is P1.2's to
build, including T0's per-dim moment envelope, T1's signed probe sets and T3's
calibrated tolerance table. Nothing here reads a space artifact or hardcodes a
calibrated threshold pretending to precision:

  * ISOTROPY and OVERLAP are RELATIVE tests — against a mathematical null and
    against the bundle's own random-pair baseline respectively. No artifact
    needed, and no threshold to calibrate.
  * SAMPLE_REEMBED's floor is deliberately COARSE and is NOT tau. §6 gives the
    numbers: the same model on different hardware lands at >=0.999, a different
    model at 0.3-0.7. A gate at 0.95 sits in the middle of that 0.3-wide chasm,
    so it cannot reject an honest bundle nor admit a different model. P1.2's
    calibrated tau, which detects small perturbations rather than wholesale
    model substitution, is a strictly sharper instrument and supersedes it.
"""
import secrets
from dataclasses import dataclass, field

import numpy as np

from .format import BundleError

# Norm tolerance. f16's cast introduces ~5e-4 relative error per component, so
# the band must accommodate it; anything near "missing normalization" (norms of
# 5-20) is orders of magnitude outside either band.
NORM_TOLERANCE = {"f32": 1e-3, "f16": 2e-2}

# Reject only when the sample is statistically indistinguishable from uniform
# random on the sphere. Pairwise cosine of uniform unit vectors has variance
# exactly 1/dim; real embedding spaces exceed it substantially.
ISOTROPY_VARIANCE_RATIO = 1.5
ISOTROPY_MEAN_SIGMAS = 3.0
ISOTROPY_MIN_VECTORS = 16
PAIR_SAMPLES = 2000

# Adjacent chunks share CHUNK_OVERLAP tokens, so their cosine must exceed the
# bundle's own random-pair baseline. The gap must clear BOTH an absolute floor
# and a significance bar: with only a few dozen adjacent pairs the sampling
# noise on their mean is itself worth several hundredths of a cosine, so a
# fixed floor alone lets shuffled vectors through (measured: a shuffled
# 40-chunk bundle produced a spurious gap of 0.042).
OVERLAP_MIN_GAP = 0.02
OVERLAP_SIGMAS = 3.0
# Below this many adjacent pairs the standard error swamps any real signal —
# the 3-sigma bar climbs above the gap an HONEST bundle produces, so the test
# would reject good data. An underpowered test abstains rather than guesses;
# small bundles are still covered by isotropy and sample re-embed, and sample
# re-embed does not weaken with bundle size the way this does.
OVERLAP_MIN_PAIRS = 12

# NOT tau. See the module docstring.
MODEL_IDENTITY_FLOOR = 0.95
SAMPLE_SIZE = 8

DEGENERATE_DUPLICATE_FRACTION = 0.5


class BundleVerificationError(BundleError):
    """A bundle's vectors failed an admission check. Terminal: retrying imports
    the same bytes and fails identically."""


class VerificationUnavailable(BundleError):
    """A check could not be RUN (e.g. the embed endpoint is unreachable) — as
    opposed to being run and failed. Transient, and therefore retryable."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    statistic: float | None = None


@dataclass(frozen=True)
class Verdict:
    """Evidence payload for the ledger (architecture-v2.md §6's "verdict
    object": tier reached, pass/fail, evidence)."""
    passed: bool
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        return "; ".join(
            f"{r.name}={'ok' if r.passed else 'FAIL'}"
            + (f"({r.statistic:.4f})" if r.statistic is not None else "")
            for r in self.results)

    def raise_if_failed(self) -> None:
        if not self.passed:
            detail = "; ".join(f"{r.name}: {r.detail}" for r in self.failures)
            raise BundleVerificationError(f"bundle failed admission checks — {detail}")


def _matrix(vectors) -> np.ndarray:
    return np.asarray(np.stack([np.asarray(v, dtype=np.float32) for v in vectors]),
                      dtype=np.float32)


# ─────────────────────────── T0: structural ───────────────────────────

def check_structural(chunk_ids: list[str], vectors, dim: int, dtype: str) -> CheckResult:
    if len(chunk_ids) != len(vectors):
        return CheckResult("t0_structural", False,
                           "chunk id count does not match vector count")
    if not chunk_ids:
        return CheckResult("t0_structural", False, "bundle contains no vectors")
    if len(set(chunk_ids)) != len(chunk_ids):
        return CheckResult("t0_structural", False, "duplicate chunk ids")

    matrix = _matrix(vectors)
    if matrix.shape[1] != dim:
        return CheckResult("t0_structural", False,
                           f"vector width {matrix.shape[1]} != declared dim {dim}")
    if not np.all(np.isfinite(matrix)):
        return CheckResult("t0_structural", False, "vectors contain NaN or Inf")

    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        return CheckResult("t0_structural", False, "bundle contains a zero vector")
    tolerance = NORM_TOLERANCE.get(dtype, NORM_TOLERANCE["f32"])
    worst = float(np.max(np.abs(norms - 1.0)))
    if worst > tolerance:
        return CheckResult("t0_structural", False,
                           f"vectors are not unit-norm (worst deviation {worst:.4g}, "
                           f"tolerance {tolerance:g}) — the classic missing-"
                           f"normalization bug", worst)
    return CheckResult("t0_structural", True, "dim, dtype, norms and ids are sane", worst)


# ─────────────────────────── isotropy ───────────────────────────

def _sampled_pair_cosines(matrix: np.ndarray, count: int = PAIR_SAMPLES) -> np.ndarray:
    n = matrix.shape[0]
    rng = np.random.default_rng(12345)     # fixed: a descriptive statistic, not a security sample
    left = rng.integers(0, n, size=count)
    right = rng.integers(0, n, size=count)
    keep = left != right
    if not np.any(keep):
        return np.zeros(0, dtype=np.float32)
    return np.sum(matrix[left[keep]] * matrix[right[keep]], axis=1)


def check_isotropy(vectors, dim: int) -> CheckResult:
    """Reject vectors indistinguishable from uniform random on the sphere.

    Uniform unit vectors have pairwise cosine with mean 0 and variance exactly
    1/dim. Real embedding spaces are strongly anisotropic and exceed both. This
    compares against a MATHEMATICAL null, so it needs no calibration artifact —
    but note it is a noise detector, not a model detector: a different real
    model passes it comfortably.
    """
    matrix = _matrix(vectors)
    n = matrix.shape[0]
    if n < ISOTROPY_MIN_VECTORS:
        return CheckResult("isotropy", True,
                           f"skipped: only {n} vectors (need {ISOTROPY_MIN_VECTORS})")
    cosines = _sampled_pair_cosines(matrix)
    if cosines.size == 0:
        return CheckResult("isotropy", True, "skipped: no distinct pairs")

    null_variance = 1.0 / dim
    null_sigma_of_mean = (null_variance ** 0.5) / (cosines.size ** 0.5)
    mean, variance = float(np.mean(cosines)), float(np.var(cosines))

    mean_is_notable = abs(mean) > ISOTROPY_MEAN_SIGMAS * null_sigma_of_mean
    spread_is_notable = variance > ISOTROPY_VARIANCE_RATIO * null_variance
    if mean_is_notable or spread_is_notable:
        return CheckResult("isotropy", True,
                           f"anisotropic as expected (mean {mean:.4f}, var {variance:.5f} "
                           f"vs null {null_variance:.5f})", mean)
    return CheckResult(
        "isotropy", False,
        f"pairwise cosines are indistinguishable from uniform random vectors "
        f"(mean {mean:.4f}, var {variance:.5f}, isotropic null {null_variance:.5f}) — "
        f"these are not embeddings", mean)


# ─────────────────────────── overlap invariant ───────────────────────────

def check_overlap(chunk_ids: list[str], vectors, uid_of, seq_of) -> CheckResult:
    """Consecutive chunks share CHUNK_OVERLAP tokens of text, so their cosine
    must exceed the bundle's own random-pair baseline.

    A RELATIVE test — the baseline comes from the bundle itself, so no absolute
    threshold is calibrated here. Catches shuffled vectors and vectors computed
    over a different corpus, both of which destroy adjacency while leaving every
    structural property intact.
    """
    matrix = _matrix(vectors)
    index = {cid: i for i, cid in enumerate(chunk_ids)}
    pairs = []
    for cid in chunk_ids:
        nxt = f"{uid_of(cid)}:{seq_of(cid) + 1}:{cid.rsplit(':', 1)[1]}"
        if nxt in index:
            pairs.append((index[cid], index[nxt]))
    if len(pairs) < OVERLAP_MIN_PAIRS:
        return CheckResult("overlap", True,
                           f"skipped: only {len(pairs)} adjacent pairs "
                           f"(need {OVERLAP_MIN_PAIRS})")

    adjacent_cosines = np.array([float(np.dot(matrix[a], matrix[b])) for a, b in pairs])
    baseline_samples = _sampled_pair_cosines(matrix)
    if baseline_samples.size == 0:
        return CheckResult("overlap", True, "skipped: no baseline pairs")
    adjacent = float(np.mean(adjacent_cosines))
    baseline = float(np.mean(baseline_samples))
    gap = adjacent - baseline

    # Standard error of the adjacent-pair mean. Requiring the gap to clear
    # several of these is what distinguishes a real overlap signal from the
    # noise of averaging a few dozen pairs.
    stderr = float(np.std(adjacent_cosines) / max(1.0, len(pairs) ** 0.5))
    required = max(OVERLAP_MIN_GAP, OVERLAP_SIGMAS * stderr)
    if gap >= required:
        return CheckResult("overlap", True,
                           f"adjacent chunks are more similar than random pairs "
                           f"(gap {gap:.4f} >= {required:.4f})", gap)
    return CheckResult(
        "overlap", False,
        f"consecutive chunks are not significantly more similar than random pairs "
        f"(adjacent {adjacent:.4f} vs baseline {baseline:.4f}, gap {gap:.4f}, "
        f"needed {required:.4f}) — vectors do not follow the claimed chunk ordering",
        gap)


# ─────────────────────────── degenerate vectors ───────────────────────────

def check_degenerate(vectors) -> CheckResult:
    matrix = _matrix(vectors)
    n = matrix.shape[0]
    unique = np.unique(matrix, axis=0).shape[0]
    if n >= 4 and unique <= n * DEGENERATE_DUPLICATE_FRACTION:
        return CheckResult("degenerate", False,
                           f"only {unique} distinct vectors among {n} chunks",
                           float(unique) / n)
    return CheckResult("degenerate", True, f"{unique} distinct vectors among {n}",
                       float(unique) / max(1, n))


# ─────────────────────────── sample re-embed ───────────────────────────

def choose_sample(chunk_ids: list[str], size: int = SAMPLE_SIZE) -> list[str]:
    """Pick chunks to re-embed using OS entropy.

    NEVER derive this from bundle content. §6: a bundle-digest seed is
    grindable — corrupt a fraction f, then re-roll the nonce until the derived
    sample misses, at an expected cost of (1-f)^-n tries.
    """
    if len(chunk_ids) <= size:
        return list(chunk_ids)
    return secrets.SystemRandom().sample(list(chunk_ids), size)


def check_sample_reembed(chunk_ids: list[str], vectors, texts: dict[str, str],
                         embed_fn, sample_size: int = SAMPLE_SIZE,
                         inputs: dict | None = None) -> CheckResult:
    """Re-embed a random sample locally and compare against the bundle.

    THE ONLY GATE HERE THAT CATCHES A DIFFERENT REAL MODEL. `texts` comes from
    locally re-derived chunks — the importer has them because possession binding
    requires reading the raw document anyway.

    `inputs`: what each chunk is fed to the encoder as, where that is not its
    text — a pair layer's chunk with its `fields`, re-cut from the record
    (09 §9.3). Absent, the text, as ever.
    """
    fed = inputs if inputs is not None else texts
    by_id = {cid: np.asarray(v, dtype=np.float32) for cid, v in zip(chunk_ids, vectors)}
    sample = [cid for cid in choose_sample(chunk_ids, sample_size) if cid in fed]
    if not sample:
        return CheckResult("sample_reembed", True,
                           "skipped: no local text available for any sampled chunk")
    try:
        recomputed = embed_fn([fed[cid] for cid in sample])
    except Exception as e:
        raise VerificationUnavailable(
            f"could not re-embed a sample for verification "
            f"({type(e).__name__}) — the embed endpoint may be down") from None
    if len(recomputed) != len(sample):
        raise VerificationUnavailable("embed endpoint returned the wrong number of vectors")

    cosines = []
    for cid, fresh_vec in zip(sample, recomputed):
        fresh = np.asarray(fresh_vec, dtype=np.float32)
        claimed = by_id[cid]
        denominator = float(np.linalg.norm(fresh) * np.linalg.norm(claimed))
        cosines.append(0.0 if denominator == 0
                       else float(np.dot(fresh, claimed) / denominator))
    worst = min(cosines)
    if worst >= MODEL_IDENTITY_FLOOR:
        return CheckResult("sample_reembed", True,
                           f"{len(sample)} sampled chunks reproduce locally "
                           f"(worst cosine {worst:.4f})", worst)
    return CheckResult(
        "sample_reembed", False,
        f"sampled chunks do not reproduce against this node's embed endpoint "
        f"(worst cosine {worst:.4f}, floor {MODEL_IDENTITY_FLOOR}) — these vectors "
        f"were almost certainly produced by a DIFFERENT MODEL, which would be "
        f"structurally perfect and semantically unrelated to local queries", worst)


# ─────────────────────────── orchestration ───────────────────────────

def run_admission_checks(chunk_ids: list[str], vectors, dim: int, dtype: str, *,
                         uid_of, seq_of, texts: dict[str, str] | None = None,
                         embed_fn=None, sample_size: int = SAMPLE_SIZE,
                         inputs: dict | None = None) -> Verdict:
    """Run every gate. Structural failure short-circuits: the later checks
    assume finite, unit-norm vectors and would otherwise produce nonsense
    statistics on garbage input."""
    structural = check_structural(chunk_ids, vectors, dim, dtype)
    if not structural.passed:
        return Verdict(passed=False, results=[structural])

    results = [structural,
               check_isotropy(vectors, dim),
               check_overlap(chunk_ids, vectors, uid_of, seq_of),
               check_degenerate(vectors)]
    if embed_fn is not None and texts:
        if inputs is None:
            results.append(check_sample_reembed(chunk_ids, vectors, texts, embed_fn,
                                                sample_size))
        else:
            results.append(check_sample_reembed(chunk_ids, vectors, texts, embed_fn,
                                                sample_size, inputs=inputs))
    return Verdict(passed=all(r.passed for r in results), results=results)
