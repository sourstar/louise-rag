"""The verification ladder's detection matrix (architecture-v2.md §6).

Two kinds of test, deliberately separated:

  DETERMINISTIC — the decision rules, the SPRT's exact error rates by
  enumeration, the Gram gate's algebra. No randomness, no flake.

  STATISTICAL — the sampler's detection probability, asserted against a
  Clopper-Pearson interval at conf = 1 - 1e-6 so it flakes about once in 10^6
  runs while still failing loudly on a real error.
"""
import numpy as np
import pytest

from app.verify import Tier, t0, t1, t2, t3
from app.verify.verdict import FAILED, PASSED, SKIPPED, TierResult, Verdict

DIM = 16


def unit(n, seed=0, anisotropy=0.6, adjacency=0.75):
    rng = np.random.default_rng(seed)
    out, prev = [], None
    for _ in range(n):
        noise = rng.normal(size=DIM) + anisotropy
        v = noise if prev is None else adjacency * prev + (1 - adjacency) * noise
        v = v / np.linalg.norm(v)
        out.append(v.astype(np.float64))
        prev = v
    return out


def isotropic(n, seed=0):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, DIM))
    return [row / np.linalg.norm(row) for row in v]


def ids(n, uid="gutenberg:1"):
    return [f"{uid}:{i}:1" for i in range(n)]


def uid_of(chunk_id):
    return chunk_id.rsplit(":", 2)[0]


def seq_of(chunk_id):
    return int(chunk_id.rsplit(":", 2)[1])


# ═══════════════════ T0 structural ═══════════════════

def test_structural_accepts_honest_vectors():
    assert t0.check_structural(ids(10), unit(10), DIM, "f32").outcome == PASSED


@pytest.mark.parametrize("mutate,expect", [
    (lambda v: [x * 7 for x in v], "unit-norm"),
    (lambda v: [np.full(DIM, np.nan)] + v[1:], "NaN"),
    (lambda v: [np.zeros(DIM)] + v[1:], "zero vector"),
])
def test_structural_catches_the_obvious(mutate, expect):
    result = t0.check_structural(ids(10), mutate(unit(10)), DIM, "f32")
    assert result.outcome == FAILED and expect in result.detail


def test_f16_rounding_stays_inside_the_norm_band():
    vectors = [v.astype(np.float16).astype(np.float64) for v in unit(20)]
    assert t0.check_structural(ids(20), vectors, DIM, "f16").outcome == PASSED


# ═══════════════════ T0 envelope: one decision, not `dim` ═══════════════════

class _Artifact:
    """Minimal stand-in exposing exactly what the tiers read."""

    def __init__(self, envelope=None, probes=None, goldens=None, raw=None,
                 replicates=100_000, path=None):
        self.envelope = envelope
        self.probes = probes
        self.goldens = goldens
        self.raw = raw or {}
        self.bootstrap_replicates = replicates
        self.space_id = "a" * 32
        self.path = path

    def envelope_block(self):
        return self.raw.get("envelope", {})

    def probes_block(self):
        return self.raw.get("probes", {})

    def overlap_block(self):
        return self.raw.get("overlap", {})

    def calibration(self):
        return self.raw.get("calibration", {})

    def goldens_block(self):
        return self.raw.get("goldens", {})

    def resolvable_alpha(self, alpha):
        return self.bootstrap_replicates >= 1.0 / alpha

    def bucket(self, buckets, n_docs, n_chunks):
        eligible = [b for b in buckets
                    if n_docs >= b.get("min_docs", 0)
                    and n_chunks >= b.get("min_chunks", b.get("min_pairs", 0))]
        return max(eligible, key=lambda b: b.get("min_chunks", 0)) if eligible else None


def _envelope_artifact(reference, crit=1e6):
    matrix = np.stack(reference)
    envelope = np.vstack([matrix.mean(axis=0), matrix.std(axis=0)])
    return _Artifact(
        envelope=envelope,
        raw={"envelope": {"per_dim_ref": "envelope.f32.npy",
                          "moment_statistic": {
                              "null_quantiles_by_bucket": [
                                  {"min_docs": 1, "min_chunks": 4,
                                   "q": {"0.999": crit, "0.9998": crit}}]}}})


def test_moment_envelope_accepts_vectors_from_the_same_distribution():
    reference = unit(400, seed=1)
    artifact = _envelope_artifact(reference, crit=1e6)
    result = t0.check_moment_envelope(unit(40, seed=2), artifact, n_docs=1, alpha=2e-4)
    assert result.outcome == PASSED


def test_moment_envelope_rejects_a_displaced_bundle():
    reference = unit(400, seed=1)
    artifact = _envelope_artifact(reference, crit=1.0)   # deliberately tight
    foreign = [v + 5.0 for v in unit(40, seed=9)]
    foreign = [v / np.linalg.norm(v) for v in foreign]
    result = t0.check_moment_envelope(foreign, artifact, n_docs=1, alpha=2e-4)
    assert result.outcome == FAILED and "envelope" in result.detail


def test_per_dim_z_is_evidence_not_a_decision():
    """The multiple-comparison trap: gating each of `dim` dimensions at 3 sigma
    rejects ~94% of honest bundles at dim=1024. Per-dim z must appear in the
    evidence and never in the decision."""
    artifact = _envelope_artifact(unit(400, seed=1), crit=1e6)
    result = t0.check_moment_envelope(unit(40, seed=2), artifact, n_docs=1, alpha=2e-4)
    assert result.outcome == PASSED
    assert "top_z" in result.statistics and result.statistics["top_z"]
    assert "n_dims_over_3" in result.statistics


def test_envelope_skips_when_the_bootstrap_cannot_resolve_alpha():
    """B replicates cannot resolve p below 1/(B+1); asking anyway silently
    clips the ladder's strictness."""
    artifact = _envelope_artifact(unit(400, seed=1))
    artifact.bootstrap_replicates = 10
    result = t0.check_moment_envelope(unit(40, seed=2), artifact, n_docs=1, alpha=2e-4)
    assert result.outcome == SKIPPED and "replicates" in result.detail


def test_envelope_skips_without_an_artifact():
    result = t0.check_moment_envelope(unit(20), None, n_docs=1, alpha=1e-4)
    assert result.outcome == SKIPPED


def test_dead_dimension_is_a_deterministic_gate():
    """A constant dimension where the space says it varies is a bug, not a
    fluctuation — so it spends none of the alpha budget."""
    reference = unit(200, seed=1)
    artifact = _envelope_artifact(reference)
    broken = [v.copy() for v in unit(20, seed=3)]
    for v in broken:
        v[7] = 0.0
    result = t0.check_dead_dimensions(broken, artifact)
    assert result is not None and result.outcome == FAILED
    assert 7 in result.statistics["dead_dims"]


# ═══════════════════ T1 Gram gate: exact, zero false rejects ═══════════════════

def _probe_artifact(probes):
    return _Artifact(probes=np.stack(probes),
                     raw={"probes": {"count": len(probes),
                                     "gram_gate": {"numeric_tolerance_by_dtype":
                                                   {"f32": 3e-5, "f16": 1.2e-2}}}})


def test_gram_gate_never_rejects_real_vectors():
    """Its false-reject rate is exactly zero up to float rounding — that is why
    it consumes no alpha. Asserted across many seeds."""
    probes = unit(64, seed=11)
    artifact = _probe_artifact(probes)
    rng = np.random.default_rng(5)
    for seed in range(25):
        result = t1.check_gram_gate(ids(30), unit(30, seed=seed), artifact, "f32", rng)
        assert result.outcome == PASSED, f"seed {seed}: {result.detail}"


def test_gram_gate_cannot_detect_fabrication_in_this_wire_format():
    """The gate's TRUE scope, asserted rather than hoped for.

    The design this was built from called it "the strongest test in the ladder"
    and "a mathematical proof of fabrication". That is wrong for a wire format
    that carries VECTORS: for any three real vectors the Gram matrix is X^T X,
    positive semi-definite by construction, so det G >= 0 holds automatically
    for whatever float arrays a forger ships. It would be the strong test it
    appears to be only if bundles transmitted per-probe COSINES.

    This test exists so nobody re-reads the formula and re-inflates the claim.
    """
    rng = np.random.default_rng(0)
    worst = 1.0
    for _ in range(20_000):
        X = rng.normal(size=(3, DIM))
        X = X / np.linalg.norm(X, axis=1, keepdims=True)
        a, b, c = float(X[0] @ X[1]), float(X[0] @ X[2]), float(X[1] @ X[2])
        worst = min(worst, 1 + 2 * a * b * c - a * a - b * b - c * c)
    assert worst >= -1e-12, "det G went negative for genuinely real vectors"

    # ...and the gate therefore passes even deliberately adversarial input
    probes = unit(32, seed=11)
    artifact = _probe_artifact(probes)
    adversarial = [np.clip(v, -0.05, 0.05) for v in unit(30, seed=3)]
    adversarial = [v / np.linalg.norm(v) for v in adversarial]
    result = t1.check_gram_gate(ids(30), adversarial, artifact, "f32",
                                 np.random.default_rng(1))
    assert result.outcome == PASSED


def test_gram_gate_normalizes_defensively_rather_than_misfiring():
    """Probes that arrive un-normalized (a byteswapped or wrongly-scaled
    sidecar) must not produce spurious violations — the gate renormalizes both
    sides before computing det G. Without that, a scaling bug in the artifact
    would read as a forgery accusation against an honest producer."""
    scaled = [v * 40.0 for v in unit(32, seed=11)]
    result = t1.check_gram_gate(ids(20), unit(20, seed=2), _probe_artifact(scaled),
                                 "f32", np.random.default_rng(1))
    assert result.outcome == PASSED
    assert result.statistics["min_det"] >= -1e-9


def test_gram_gate_skips_without_probes():
    rng = np.random.default_rng(1)
    assert t1.check_gram_gate(ids(5), unit(5), None, "f32", rng).outcome == SKIPPED


# ═══════════════════ T2 beacon ═══════════════════

def test_challenge_indices_are_deterministic_per_epoch():
    root = bytes(range(32))
    first = t2.challenge_indices(root, 100, 8, 256)
    assert first == t2.challenge_indices(root, 100, 8, 256)
    assert first != t2.challenge_indices(root, 101, 8, 256)
    assert len(set(first)) == 8


def test_challenge_indices_stay_in_range():
    root = bytes(range(32))
    for epoch in range(50):
        for index in t2.challenge_indices(root, epoch, 8, 64):
            assert 0 <= index < 64


class _Header:
    def __init__(self, challenge=None, created_at=None, dtype="f32",
                 backend_fingerprint=""):
        self.challenge = challenge
        self.created_at = int(created_at if created_at is not None else 1_700_000_000)
        self.dtype = dtype
        self.backend_fingerprint = backend_fingerprint


def _goldens_artifact(goldens, root=b"\x01" * 32):
    return _Artifact(goldens=np.stack(goldens),
                     raw={"goldens": {"bank_size": len(goldens),
                                      "beacon": {"root": root.hex(), "epoch_seconds": 3600,
                                                 "challenge_count": 4,
                                                 "max_bundle_age_seconds": 10**9,
                                                 "max_clock_skew_seconds": 900}},
                          "calibration": {"tau_default": 0.99}})


def test_correct_challenge_answers_pass():
    goldens = unit(64, seed=21)
    artifact = _goldens_artifact(goldens)
    created = 1_700_000_000
    expected = t2.challenge_indices(b"\x01" * 32, created // 3600, 4, 64)
    header = _Header(challenge={"indices": expected,
                                 "vectors": [goldens[i].tolist() for i in expected]},
                      created_at=created)
    result = t2.check_challenges(header, artifact, now=created + 60)
    assert result.outcome == PASSED


def test_answering_a_different_question_fails():
    goldens = unit(64, seed=21)
    artifact = _goldens_artifact(goldens)
    created = 1_700_000_000
    wrong = [0, 1, 2, 3]
    header = _Header(challenge={"indices": wrong,
                                 "vectors": [goldens[i].tolist() for i in wrong]},
                      created_at=created)
    result = t2.check_challenges(header, artifact, now=created + 60)
    assert result.outcome == FAILED and "beacon" in result.detail


def test_wrong_oven_fails_even_with_the_right_indices():
    """T2's actual job: a systematic displacement that leaves the bundle's
    internal geometry perfectly plausible."""
    goldens = unit(64, seed=21)
    artifact = _goldens_artifact(goldens)
    created = 1_700_000_000
    expected = t2.challenge_indices(b"\x01" * 32, created // 3600, 4, 64)
    other = unit(64, seed=99)
    header = _Header(challenge={"indices": expected,
                                 "vectors": [other[i].tolist() for i in expected]},
                      created_at=created)
    result = t2.check_challenges(header, artifact, now=created + 60)
    assert result.outcome == FAILED and "displaced" in result.detail


def test_future_dated_bundle_is_refused_before_the_epoch_is_derived():
    """created_at is producer-controlled; without a freshness gate
    'beacon-derived' reduces to 'producer-derived'."""
    goldens = unit(16, seed=21)
    artifact = _goldens_artifact(goldens)
    header = _Header(challenge={"indices": [0], "vectors": [goldens[0].tolist()]},
                      created_at=2_000_000_000)
    result = t2.check_challenges(header, artifact, now=1_700_000_000)
    assert result.outcome == FAILED and "future" in result.detail


def test_v1_bundle_without_a_challenge_skips_rather_than_passes():
    artifact = _goldens_artifact(unit(16, seed=21))
    result = t2.check_challenges(_Header(challenge=None), artifact)
    assert result.outcome == SKIPPED


# ═══════════════════ T3 SPRT: exact, then statistical ═══════════════════

def test_sprt_rejects_on_first_miss_in_the_shipped_regime():
    """With f0 tiny, ln(f1/f0) exceeds the upper boundary, so one miss decides —
    which is exactly §6's implied rule."""
    sprt = t3.Sprt(f0=1e-4, f1=0.05, alpha=1e-2, beta=0.05, n_max=64)
    assert sprt.decide(misses=1, observed=1) == "fail"
    assert sprt.decide(misses=0, observed=1) is None


def test_sprt_accepts_a_clean_run():
    sprt = t3.Sprt(f0=1e-4, f1=0.05, alpha=1e-2, beta=0.05, n_max=200)
    decision = None
    for m in range(1, 201):
        decision = sprt.decide(0, m)
        if decision:
            break
    assert decision == "pass"


def test_delivered_error_rates_are_computed_not_assumed():
    """Wald's A and B ignore overshoot and truncation, so the DESIGNED alpha and
    beta are not the DELIVERED ones. The exact lattice enumeration is the only
    honest statement of what the test achieves."""
    sprt = t3.Sprt(f0=1e-4, f1=0.05, alpha=1e-2, beta=0.05, n_max=16)
    false_reject = sprt.delivered_error_rates(1e-4)
    detect = sprt.delivered_error_rates(0.05)
    assert false_reject < 2e-3, "an honest bundle must almost never be rejected"
    # §6's own arithmetic depends on this being ~0.56, NOT ~0.95
    assert 0.5 < detect < 0.62, f"expected ~0.56 at n=16, f=5%, got {detect}"


def test_n16_beta_matches_section_6s_network_argument():
    """§6: '10 independent importers at f=5% -> 3x10^-4'. That requires a
    single importer's miss probability to be ~0.44, i.e. 0.95^16."""
    single_miss = 0.95 ** 16
    assert abs(single_miss - 0.44) < 0.01
    assert abs(single_miss ** 10 - 2.7e-4) < 1e-4


def test_detection_probability_matches_the_closed_form():
    for f in (0.01, 0.05, 0.2):
        for n in (4, 16, 32):
            expected = 1 - (1 - f) ** n
            assert abs(t3.detection_probability(10_000, f, n) - expected) < 1e-9


def test_hypergeometric_matches_binomial_for_large_corpora():
    """Sampling is without replacement; the binomial form §6 quotes is the
    large-N limit."""
    exact = t3.hypergeometric_detection(100_000, 5_000, 16)
    approx = 1 - 0.95 ** 16
    assert abs(exact - approx) < 5e-3


def test_sampler_detection_rate_matches_theory():
    """Statistical, with a stated flake budget: conf = 1 - 1e-6 means this
    flakes about once in 10^6 runs while still catching a real off-by-one."""
    import random
    total, corrupt, n, trials = 400, 20, 16, 4000
    corrupted = set(range(corrupt))
    rng = random.Random(4242)
    chunk_ids = list(range(total))
    detections = sum(
        1 for _ in range(trials)
        if corrupted & set(t3.choose_sample(chunk_ids, n, rng=rng)))
    observed = detections / trials
    expected = t3.hypergeometric_detection(total, corrupt, n)
    # Clopper-Pearson half-width at conf 1-1e-6 is ~2.4/sqrt(R)
    assert abs(observed - expected) < 0.045, f"{observed} vs {expected}"


def test_sampler_default_is_not_deterministic():
    """§6's grindability argument: a content-derived seed lets an attacker
    re-roll until the sample misses the corrupted chunks."""
    chunk_ids = [f"c{i}" for i in range(200)]
    draws = {tuple(t3.choose_sample(chunk_ids, 8)) for _ in range(12)}
    assert len(draws) > 1


def test_spot_recompute_passes_for_the_same_model():
    chunk_ids = ids(20)
    vectors = unit(20, seed=31)
    texts = {c: f"text {i}" for i, c in enumerate(chunk_ids)}
    index = {f"text {i}": i for i in range(20)}
    result = t3.check_spot_recompute(
        chunk_ids, vectors, None, texts=texts,
        embed_fn=lambda batch: [vectors[index[t]] for t in batch])
    assert result.outcome == PASSED


def test_spot_recompute_fails_for_a_different_model():
    chunk_ids = ids(20)
    vectors = unit(20, seed=31)
    foreign = unit(20, seed=777)
    texts = {c: f"text {i}" for i, c in enumerate(chunk_ids)}
    index = {f"text {i}": i for i in range(20)}
    result = t3.check_spot_recompute(
        chunk_ids, vectors, None, texts=texts,
        embed_fn=lambda batch: [foreign[index[t]] for t in batch])
    assert result.outcome == FAILED
    assert result.statistics["tau_source"] == "coarse-fallback"


def test_spot_recompute_skips_when_the_endpoint_is_down():
    """Could-not-run is not the same as failed."""
    chunk_ids = ids(10)
    texts = {c: "t" for c in chunk_ids}

    def down(_batch):
        raise ConnectionError("endpoint down")
    result = t3.check_spot_recompute(chunk_ids, unit(10), None, texts=texts,
                                      embed_fn=down)
    assert result.outcome == SKIPPED


def test_spot_recompute_skips_without_an_endpoint():
    assert t3.check_spot_recompute(ids(5), unit(5), None, texts={"a": "b"},
                                    embed_fn=None).outcome == SKIPPED


# ═══════════════════ Verdict semantics ═══════════════════

def test_all_skipped_is_not_a_pass():
    """An absence of evidence must never read as approval."""
    verdict = Verdict()
    verdict.add(TierResult(Tier.T0, SKIPPED, "no artifact"))
    verdict.add(TierResult(Tier.T1, SKIPPED, "no artifact"))
    assert not verdict.passed and not verdict.failed
    assert verdict.tier_reached is None


def test_tier_reached_records_honest_degradation():
    verdict = Verdict()
    verdict.add(TierResult(Tier.T0, PASSED, "ok"))
    verdict.add(TierResult(Tier.T1, PASSED, "ok"))
    verdict.add(TierResult(Tier.T2, PASSED, "ok"))
    verdict.add(TierResult(Tier.T3, SKIPPED, "endpoint down"))
    assert verdict.passed and verdict.tier_reached == Tier.T2


def test_any_failure_fails_the_verdict():
    verdict = Verdict()
    verdict.add(TierResult(Tier.T0, PASSED, "ok"))
    verdict.add(TierResult(Tier.T1, FAILED, "gram violation"))
    assert verdict.failed and not verdict.passed


def test_evidence_is_serializable_and_carries_statistics():
    verdict = Verdict(space_id="a" * 32)
    verdict.add(TierResult(Tier.T0, PASSED, "ok", {"D": 12.5, "D_crit": 40.0}))
    import json
    payload = json.loads(verdict.evidence())
    assert payload["tiers"][0]["statistics"]["D"] == 12.5
    assert payload["passed"] is True
