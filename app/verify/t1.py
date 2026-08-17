"""T1 — geometric. Probe profile, the Gram gate, and the overlap band.

architecture-v2.md §6: "cosine profile against a signed probe set (~512
reference texts embedded once by the space author); overlap invariant —
consecutive chunks share ~120 tokens, so cos(v_k, v_k+1) must sit in a
predictable band; semantic triangles against probes".

THE GRAM GATE — AND AN HONEST STATEMENT OF ITS SCOPE, WHICH IS NARROWER THAN IT
LOOKS. For unit vectors v, p_k, p_l with a = <v,p_k>, b = <v,p_l>, c = <p_k,p_l>:

    det G = 1 + 2abc - a^2 - b^2 - c^2

Three unit vectors are realizable in an inner-product space if and only if
det G >= 0, so a violation is a mathematical impossibility rather than a
statistical improbability, and the check costs none of the alpha budget.

BUT IT CANNOT DETECT FABRICATION IN *THIS* WIRE FORMAT, and it would be
dangerous to believe otherwise. A bundle carries VECTORS, not similarity
profiles. For any three real vectors the Gram matrix is X^T X, which is
positive semi-definite by construction — so det G >= 0 holds automatically for
whatever float arrays a forger chooses to ship. Measured over 200k random
triples the minimum was +0.12; forced into a 2-D subspace it reached
-7.8e-16, i.e. float rounding, far inside tolerance.

The gate would be the strong anti-fabrication test it first appears to be only
if bundles transmitted per-probe COSINES (where a forger sampling each
similarity independently from a published marginal violates PSD almost at
once). Since they transmit vectors, its real value is as an INTEGRITY CANARY:
it fires when probe vectors have been decoded with the wrong dtype,
endianness or shape, or when our own arithmetic is wrong. That is worth
keeping — it is nearly free and it fails loudly — but it must not be counted
as anti-forgery coverage, and tests assert its true scope rather than the
hoped-for one.
"""
import numpy as np

from .verdict import FAILED, PASSED, SKIPPED, Tier, TierResult

TRIPLES_SAMPLED = 4096
MIN_OVERLAP_PAIRS = 8
DEFAULT_GRAM_TOLERANCE = {"f32": 3.0e-5, "f16": 1.2e-2}


def _unit(vectors) -> np.ndarray:
    matrix = np.stack([np.asarray(v, dtype=np.float64) for v in vectors])
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def check_gram_gate(chunk_ids, vectors, artifact, dtype: str, rng) -> TierResult:
    """Algebraic realizability. Zero alpha, zero false rejects, no corpus needed.

    Scope note, because the name oversells it: this is an INTEGRITY CANARY, not
    an anti-fabrication gate. See the module docstring — a bundle carries
    vectors, and any real vectors satisfy det G >= 0 by construction.
    """
    if artifact is None or artifact.probes is None:
        return TierResult(Tier.T1, SKIPPED, "no space artifact: no probe set")
    block = (artifact.probes_block().get("gram_gate") or {})
    tolerance = (block.get("numeric_tolerance_by_dtype")
                 or DEFAULT_GRAM_TOLERANCE).get(dtype, DEFAULT_GRAM_TOLERANCE["f32"])

    matrix = _unit(vectors)
    probes = _unit(artifact.probes)
    n, k = matrix.shape[0], probes.shape[0]
    if k < 2 or n < 1:
        return TierResult(Tier.T1, SKIPPED, "not enough probes or vectors")

    count = min(TRIPLES_SAMPLED, n * k * k)
    vi = rng.integers(0, n, size=count)
    pk = rng.integers(0, k, size=count)
    pl = rng.integers(0, k, size=count)
    keep = pk != pl
    vi, pk, pl = vi[keep], pk[keep], pl[keep]
    if vi.size == 0:
        return TierResult(Tier.T1, SKIPPED, "no distinct probe pairs")

    a = np.sum(matrix[vi] * probes[pk], axis=1)
    b = np.sum(matrix[vi] * probes[pl], axis=1)
    c = np.sum(probes[pk] * probes[pl], axis=1)
    det = 1.0 + 2.0 * a * b * c - a * a - b * b - c * c

    violations = np.where(det < -abs(float(tolerance)))[0]
    stats = {"triples": int(vi.size), "tolerance": float(tolerance),
             "min_det": float(np.min(det)), "violations": int(violations.size)}
    if violations.size:
        # The single strongest evidence object in the ladder: a third party with
        # the public probe vectors recomputes det G and sees it is negative.
        # That is a proof of fabrication, not a statistical accusation.
        stats["evidence"] = [
            {"chunk_id": chunk_ids[int(vi[i])], "probe_k": int(pk[i]),
             "probe_l": int(pl[i]), "a": float(a[i]), "b": float(b[i]),
             "c": float(c[i]), "det": float(det[i])}
            for i in violations[:32]]
        return TierResult(
            Tier.T1, FAILED,
            f"{violations.size} probe triples are geometrically impossible "
            f"(min det G = {stats['min_det']:.3g}) — these vectors cannot all be "
            f"unit vectors in any inner-product space, so they were synthesised "
            f"rather than embedded", stats)
    return TierResult(Tier.T1, PASSED,
                      f"all {vi.size} sampled probe triples are geometrically realizable",
                      stats)


def _profile_for(artifact, corpus_tag: str | None, chunking_profile: str | None) -> dict | None:
    """Probe reference statistics are CORPUS-DEPENDENT: per-probe means measured
    on Wikipedia do not describe a PubMed bundle, and that shift is legitimate.
    With no matching profile the corpus-sensitive sub-tests must skip rather
    than borrow a nearby corpus's numbers."""
    for profile in artifact.probes_block().get("profiles") or []:
        if (profile.get("corpus_tag") == corpus_tag
                and profile.get("chunking_profile") == chunking_profile):
            return profile
    return None


def check_probe_profile(vectors, artifact, *, corpus_tag, chunking_profile,
                        n_docs: int, alpha: float) -> TierResult:
    """The bundle's position in PROBE coordinates.

        a_k = mean_i <v_i, p_k>
        D_P = sum_k (sqrt(n) * (a_k - mean_k) / sd_k)^2

    Same aggregate-decision machinery as T0, but the axes are semantically
    meaningful and model-specific rather than basis-arbitrary. A different model
    can reproduce a marginal cosine distribution by accident — models trained on
    similar data have similar anisotropy — but it will not reproduce the
    ORDERING of 512 probes.
    """
    if artifact is None or artifact.probes is None:
        return TierResult(Tier.T1, SKIPPED, "no space artifact: no probe set")
    profile = _profile_for(artifact, corpus_tag, chunking_profile)
    if profile is None:
        return TierResult(Tier.T1, SKIPPED,
                          f"artifact has no probe profile for corpus "
                          f"{corpus_tag!r}/{chunking_profile!r}")
    reference_path = artifact.path / (profile.get("per_probe_ref") or "")
    if not reference_path.is_file():
        return TierResult(Tier.T1, SKIPPED, "probe profile sidecar is missing")

    try:
        reference = np.load(reference_path, allow_pickle=False)
    except Exception:
        return TierResult(Tier.T1, SKIPPED, "probe profile sidecar is unreadable")
    if reference.ndim != 2 or reference.shape != (2, artifact.probes.shape[0]):
        return TierResult(Tier.T1, SKIPPED, "probe profile sidecar has the wrong shape")

    matrix = _unit(vectors)
    probes = _unit(artifact.probes)
    n = matrix.shape[0]
    a = (matrix @ probes.T).mean(axis=0)
    mean = np.asarray(reference[0], dtype=np.float64)
    sd = np.asarray(reference[1], dtype=np.float64)
    safe_sd = np.where(sd > 0, sd, np.inf)
    z = np.sqrt(n) * (a - mean) / safe_sd
    d_p = float(np.sum(z[np.isfinite(z)] ** 2))

    aggregate = profile.get("per_probe_aggregate") or {}
    bucket = artifact.bucket(aggregate.get("null_quantiles_by_bucket") or [], n_docs, n)
    stats = {"D_P": d_p, "n_probes": int(probes.shape[0]), "n_chunks": n}
    if bucket is None:
        return TierResult(Tier.T1, SKIPPED, "bundle too small for any published probe null",
                          stats)
    critical = _quantile_at(bucket.get("q") or {}, 1.0 - alpha)
    stats["D_P_crit"] = critical
    if critical is None:
        return TierResult(Tier.T1, SKIPPED, "no probe quantile at this alpha", stats)

    # Spearman rank correlation is the corpus-robust companion: a topical shift
    # moves the MAGNITUDES of a_k far more than their ORDER.
    rank_a = np.argsort(np.argsort(a))
    rank_ref = np.argsort(np.argsort(mean))
    rho = float(np.corrcoef(rank_a, rank_ref)[0, 1]) if a.size > 2 else 1.0
    stats["spearman_rho"] = rho

    if d_p > critical:
        return TierResult(
            Tier.T1, FAILED,
            f"probe-coordinate profile is outside the space's published band "
            f"(D_P={d_p:.1f} > {critical:.1f}, Spearman rho={rho:.3f}) — the bundle "
            f"sits somewhere this model does not put text", stats)
    return TierResult(Tier.T1, PASSED,
                      f"probe profile inside the band (D_P={d_p:.1f} <= {critical:.1f})",
                      stats)


def check_overlap_band(chunk_ids, vectors, artifact, *, texts, uid_of, seq_of,
                       n_docs: int, alpha: float) -> TierResult:
    """The overlap invariant, keyed on MEASURED overlap rather than config.

    CHUNK_TOKENS/CHUNK_OVERLAP are not part of space_id, so a band keyed on a
    producer's declared chunk settings is silently wrong for a peer with
    different ones — and trusts the producer to declare them honestly. Instead
    the band is keyed on the overlap actually observed between the two chunks in
    front of us:

        phi = shared_chars(c_k, c_k+1) / min(len(c_k), len(c_k+1))

    which is self-keying, config-independent, and survives a producer lying.
    It needs local text, which the import path always has.

    BOTH TAILS MEAN SOMETHING. Low = shuffled, foreign or wrong-corpus vectors,
    the attack direction. High, with cosines near 1.0 = the producer emitted the
    same vector for adjacent chunks.
    """
    if artifact is None:
        return TierResult(Tier.T1, SKIPPED, "no space artifact: no overlap band")
    block = (artifact.overlap_block().get("by_phi") or {})
    bins = block.get("bins") or []
    if not bins:
        return TierResult(Tier.T1, SKIPPED, "artifact publishes no measured-overlap band")
    if not texts:
        return TierResult(Tier.T1, SKIPPED, "no local text: cannot measure real overlap")

    matrix = _unit(vectors)
    index = {cid: i for i, cid in enumerate(chunk_ids)}
    z_scores, pairs = [], 0
    for cid in chunk_ids:
        successor = f"{uid_of(cid)}:{seq_of(cid) + 1}:{cid.rsplit(':', 1)[1]}"
        if successor not in index or cid not in texts or successor not in texts:
            continue
        phi = _measured_overlap(texts[cid], texts[successor])
        band = _bin_for(bins, phi)
        if band is None or not band.get("sd"):
            continue
        cosine = float(np.dot(matrix[index[cid]], matrix[index[successor]]))
        z_scores.append((cosine - float(band["mean"])) / float(band["sd"]))
        pairs += 1

    if pairs < MIN_OVERLAP_PAIRS:
        # chunk_abstract emits one chunk per document, so for PubMed the overlap
        # invariant does not exist at all. Skipping is correct; failing is not.
        return TierResult(Tier.T1, SKIPPED,
                          f"only {pairs} adjacent pairs with a published band")

    z = np.asarray(z_scores)
    aggregate = float(np.sqrt(pairs) * np.mean(z))
    bucket = artifact.bucket(block.get("aggregate_null_by_bucket") or [], n_docs, pairs)
    tail_threshold = float(block.get("z_tail_threshold", -3.0))
    tail_count = int(np.sum(z < tail_threshold))
    stats = {"pairs": pairs, "Z": aggregate, "tail_count": tail_count,
             "mean_z": float(np.mean(z))}
    if bucket is None:
        return TierResult(Tier.T1, SKIPPED, "bundle too small for any published overlap null",
                          stats)
    band = bucket.get("Z") or {}
    low = _quantile_at(band, alpha / 2)
    high = _quantile_at(band, 1.0 - alpha / 2)
    stats["Z_low"], stats["Z_high"] = low, high
    if low is not None and aggregate < low:
        return TierResult(
            Tier.T1, FAILED,
            f"consecutive chunks are far less similar than the space's overlap band "
            f"(Z={aggregate:.2f} < {low:.2f}) — vectors do not follow the claimed "
            f"chunk ordering", stats)
    if high is not None and aggregate > high:
        return TierResult(
            Tier.T1, FAILED,
            f"consecutive chunks are implausibly similar (Z={aggregate:.2f} > "
            f"{high:.2f}) — the producer may be emitting one vector for several "
            f"chunks", stats)
    return TierResult(Tier.T1, PASSED,
                      f"overlap band satisfied over {pairs} adjacent pairs "
                      f"(Z={aggregate:.2f})", stats)


def _measured_overlap(first: str, second: str) -> float:
    """Longest shared boundary run, normalized. chunking.py builds a successor
    from the tail of its predecessor, so the shared region is a suffix/prefix
    pair — no general LCS needed."""
    if not first or not second:
        return 0.0
    limit = min(len(first), len(second))
    best = 0
    # binary search on the suffix/prefix match length
    low, high = 0, limit
    while low <= high:
        mid = (low + high) // 2
        if mid and first[-mid:] == second[:mid]:
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best / limit if limit else 0.0


def _bin_for(bins: list[dict], phi: float) -> dict | None:
    for band in bins:
        if float(band.get("phi_lo", 0)) <= phi < float(band.get("phi_hi", 1)):
            return band
    return bins[-1] if bins and phi >= float(bins[-1].get("phi_hi", 1)) else None


def _quantile_at(quantiles: dict, target: float) -> float | None:
    if not quantiles:
        return None
    try:
        parsed = sorted((float(k), float(v)) for k, v in quantiles.items())
    except (TypeError, ValueError):
        return None
    if target <= 0.5:
        for probability, value in parsed:
            if probability >= target - 1e-12:
                return value
        return parsed[0][1]
    for probability, value in parsed:
        if probability >= target - 1e-12:
            return value
    return parsed[-1][1]
