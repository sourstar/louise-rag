"""T0 — algebraic. dim/dtype/norm/NaN/dupes, then the distribution envelope.

architecture-v2.md §6: "per-dim moments and pairwise-cosine distribution inside
the space envelope (embedding spaces are strongly anisotropic — foreign vectors
betray themselves statistically)".

THE MULTIPLE-COMPARISON TRAP, AND WHY THIS MAKES ONE DECISION, NOT 1024.
The obvious implementation gates each dimension at 3 sigma. At dim=1024 that is
1024 * 2 * (1 - Phi(3)) = 2.76 expected alarms per bundle, so
P(at least one) = 1 - 0.9973^1024 = 0.94 — it rejects 94% of HONEST bundles.
Tightening sigma does not fix it; it just destroys the power you wanted.

So T0 makes a single decision about the DISTRIBUTION of 1024 numbers:

    z_j = sqrt(n) * (bundle_mean_j - artifact_mean_j) / artifact_sd_j
    D   = sum_j z_j^2

and compares D against the artifact's bootstrapped null quantile for a bundle
of this size. Per-dimension z-scores are recorded as EVIDENCE, never as
decisions — they explain a rejection without multiplying its false-alarm rate.

D has no usable closed form: embedding dimensions are strongly correlated (that
anisotropy is the tier's whole premise), so chi2_dim would be badly
anticonservative. The artifact publishes empirical quantiles from a
document-block bootstrap instead, which assumes nothing.
"""
import numpy as np

from .verdict import FAILED, PASSED, SKIPPED, Tier, TierResult

NORM_TOLERANCE = {"f32": 1e-3, "f16": 2e-2}
PAIR_SAMPLES = 4000


def _matrix(vectors) -> np.ndarray:
    return np.stack([np.asarray(v, dtype=np.float64) for v in vectors])


def check_structural(chunk_ids: list[str], vectors, dim: int, dtype: str) -> TierResult:
    """Always runs — needs no artifact, no endpoint, no local text."""
    stats = {"n_chunks": len(chunk_ids)}
    if len(chunk_ids) != len(vectors) or not chunk_ids:
        return TierResult(Tier.T0, FAILED, "chunk id / vector count mismatch", stats)
    if len(set(chunk_ids)) != len(chunk_ids):
        return TierResult(Tier.T0, FAILED, "duplicate chunk ids", stats)

    matrix = _matrix(vectors)
    if matrix.shape[1] != dim:
        return TierResult(Tier.T0, FAILED,
                          f"vector width {matrix.shape[1]} != declared dim {dim}", stats)
    if not np.all(np.isfinite(matrix)):
        return TierResult(Tier.T0, FAILED, "vectors contain NaN or Inf", stats)

    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        return TierResult(Tier.T0, FAILED, "bundle contains a zero vector", stats)
    tolerance = NORM_TOLERANCE.get(dtype, NORM_TOLERANCE["f32"])
    worst = float(np.max(np.abs(norms - 1.0)))
    stats["worst_norm_deviation"] = worst
    if worst > tolerance:
        return TierResult(Tier.T0, FAILED,
                          f"vectors are not unit-norm (worst {worst:.4g} > {tolerance:g}) "
                          f"— the classic missing-normalization bug", stats)
    return TierResult(Tier.T0, PASSED, "structure, dim, dtype and norms are sane", stats)


def check_dead_dimensions(vectors, artifact) -> TierResult | None:
    """A bundle dimension that is constant while the artifact says it varies is
    a structural bug — a truncated write, a wrong slice, a broken pooling mode —
    not a fluctuation. Deterministic, so it spends none of the alpha budget."""
    if artifact is None or artifact.envelope is None:
        return None
    matrix = _matrix(vectors)
    if matrix.shape[0] < 4:
        return None
    reference_sd = np.asarray(artifact.envelope[1], dtype=np.float64)
    observed_sd = matrix.std(axis=0)
    dead = np.where((observed_sd == 0) & (reference_sd > 0))[0]
    if dead.size:
        return TierResult(Tier.T0, FAILED,
                          f"{dead.size} dimension(s) are constant across the bundle "
                          f"while the space says they vary — e.g. dims "
                          f"{dead[:8].tolist()}",
                          {"dead_dims": dead[:32].tolist()})
    return None


def check_moment_envelope(vectors, artifact, *, n_docs: int, alpha: float) -> TierResult:
    """One aggregate decision, with per-dim z as evidence only."""
    if artifact is None or artifact.envelope is None:
        return TierResult(Tier.T0, SKIPPED, "no space artifact: no envelope to check")
    block = artifact.envelope_block().get("moment_statistic") or {}
    buckets = block.get("null_quantiles_by_bucket") or []
    matrix = _matrix(vectors)
    n = matrix.shape[0]
    bucket = artifact.bucket(buckets, n_docs, n)
    if bucket is None:
        return TierResult(Tier.T0, SKIPPED,
                          f"bundle too small for any published null "
                          f"({n_docs} docs, {n} chunks)")
    if not artifact.resolvable_alpha(alpha):
        return TierResult(Tier.T0, SKIPPED,
                          f"artifact has {artifact.bootstrap_replicates} bootstrap "
                          f"replicates, too few to resolve alpha={alpha:g}")

    mean = np.asarray(artifact.envelope[0], dtype=np.float64)
    sd = np.asarray(artifact.envelope[1], dtype=np.float64)
    safe_sd = np.where(sd > 0, sd, np.inf)          # dead dims handled separately
    z = np.sqrt(n) * (matrix.mean(axis=0) - mean) / safe_sd
    d_statistic = float(np.sum(z[np.isfinite(z)] ** 2))

    critical = _quantile_at(bucket.get("q") or {}, 1.0 - alpha)
    order = np.argsort(-np.abs(z))[:20]
    stats = {
        "D": d_statistic, "D_crit": critical, "alpha": alpha,
        "n_chunks": n, "n_docs": n_docs,
        "bucket": {"min_docs": bucket.get("min_docs"),
                   "min_chunks": bucket.get("min_chunks")},
        "effective_df": block.get("effective_df"),
        "max_abs_z": float(np.max(np.abs(z[np.isfinite(z)]))) if n else 0.0,
        "n_dims_over_3": int(np.sum(np.abs(z[np.isfinite(z)]) > 3)),
        "top_z": [[int(j), float(z[j])] for j in order],
    }
    if critical is None:
        return TierResult(Tier.T0, SKIPPED, "artifact publishes no quantile at this alpha",
                          stats)
    if d_statistic > critical:
        return TierResult(
            Tier.T0, FAILED,
            f"per-dimension moments sit outside the space envelope "
            f"(D={d_statistic:.1f} > {critical:.1f} at alpha={alpha:g}); worst dims "
            f"{[int(j) for j in order[:5]]}", stats)
    return TierResult(Tier.T0, PASSED,
                      f"moments inside the envelope (D={d_statistic:.1f} <= {critical:.1f})",
                      stats)


def _stratified_pair_cosines(chunk_ids, matrix, uid_of, rng) -> dict[str, np.ndarray]:
    """Sample pairs from OS entropy and split them by stratum.

    A single pooled distribution is a trap: a bundle covering three books has a
    far higher within-bundle pair cosine than a corpus-wide sample, so a pooled
    band either rejects honest narrow bundles or is widened until it detects
    nothing.

    Sampling uses injected randomness rather than a fixed seed because in P1.2
    this GATES rather than describes: a fixed seed tells an adversary exactly
    which pairs are inspected.
    """
    n = matrix.shape[0]
    if n < 4:
        return {}
    uids = [uid_of(c) for c in chunk_ids]
    left = rng.integers(0, n, size=PAIR_SAMPLES)
    right = rng.integers(0, n, size=PAIR_SAMPLES)
    keep = left != right
    left, right = left[keep], right[keep]
    cosines = np.sum(matrix[left] * matrix[right], axis=1)
    same = np.array([uids[a] == uids[b] for a, b in zip(left, right)])
    return {"cross_doc": cosines[~same], "within_doc_nonadjacent": cosines[same]}


def check_pair_cosines(chunk_ids, vectors, artifact, uid_of, *, n_docs: int,
                       alpha: float, rng) -> TierResult:
    if artifact is None:
        return TierResult(Tier.T0, SKIPPED, "no space artifact: no pair-cosine band")
    block = (artifact.envelope_block().get("pair_cosine") or {})
    if not block:
        return TierResult(Tier.T0, SKIPPED, "artifact publishes no pair-cosine band")

    matrix = _matrix(vectors)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    strata = _stratified_pair_cosines(chunk_ids, matrix, uid_of, rng)
    if not strata:
        return TierResult(Tier.T0, SKIPPED, "too few vectors to sample pairs")

    evidence, worst = {}, None
    for name, sample in strata.items():
        reference = block.get(name) or {}
        if sample.size < 32 or not reference:
            evidence[name] = {"n_pairs": int(sample.size), "checked": False}
            continue
        bucket = artifact.bucket(reference.get("null_max_z_by_bucket") or [],
                                 n_docs, matrix.shape[0])
        ref_mean, ref_sd = reference.get("mean"), reference.get("sd")
        if bucket is None or ref_mean is None or not ref_sd:
            evidence[name] = {"n_pairs": int(sample.size), "checked": False}
            continue
        observed = {"mean": float(np.mean(sample)), "sd": float(np.std(sample)),
                    "q99": float(np.quantile(sample, 0.99))}
        z_mean = (observed["mean"] - float(ref_mean)) / float(ref_sd)
        z_sd = (observed["sd"] - float(ref_sd)) / float(ref_sd)
        z_q99 = ((observed["q99"] - float(reference.get("q99", ref_mean)))
                 / float(ref_sd))
        max_z = float(max(abs(z_mean), abs(z_sd), abs(z_q99)))
        critical = _quantile_at(bucket.get("q") or {}, 1.0 - alpha)
        evidence[name] = {"n_pairs": int(sample.size), "checked": True,
                          "observed": observed, "max_z": max_z, "crit": critical}
        if critical is not None and max_z > critical:
            worst = (name, max_z, critical)

    if worst:
        name, max_z, critical = worst
        return TierResult(
            Tier.T0, FAILED,
            f"{name} pairwise-cosine distribution sits outside the space envelope "
            f"(max|z|={max_z:.2f} > {critical:.2f})", evidence)
    if not any(v.get("checked") for v in evidence.values()):
        return TierResult(Tier.T0, SKIPPED, "no stratum had enough pairs to check",
                          evidence)
    return TierResult(Tier.T0, PASSED, "pairwise-cosine distribution inside the envelope",
                      evidence)


def _quantile_at(quantiles: dict, target: float) -> float | None:
    """Pick the published quantile nearest to (and not looser than) `target`."""
    if not quantiles:
        return None
    try:
        parsed = sorted((float(k), float(v)) for k, v in quantiles.items())
    except (TypeError, ValueError):
        return None
    for probability, value in parsed:
        if probability >= target - 1e-12:
            return value
    return parsed[-1][1]
