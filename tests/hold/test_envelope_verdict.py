"""tests/hold/test_envelope_verdict.py — T0's envelope test, 03 §3.5.

The literals and the SHAPE of this test were both measured on 2026-09-09
against the encoder a real collection pins, and both were wrong before
that. The old rule — cosine ≥ 0.9, and EVERY per-dimension standard
deviation within half and twice the envelope's — refused honest bundles
on two of three runs.

The diagnosis is the reason these tests exist in the form they do: the
same honest vectors passed at 2560 dimensions and failed at 4096, which
no property of the encoder or the corpus explains. `min` over 4096
samples is smaller than `min` over 2560 for no reason but the count, so
"every dimension" is a multiple-comparison problem and widening the band
only postpones it. A percentile does not drift with sample size.

These tests are synthetic on purpose — a unit test cannot embed anything
(invariant 7) — so they assert the PROPERTIES the measurement established
rather than replaying its numbers: that an in-space bundle passes, that
the failure modes T0 names are refused, that the verdict says which
number failed, and above all that the answer does not change when the
dimension does. `tools/measure_t0_envelope.py` is where the real numbers
come from.
"""
import numpy as np
import pytest

from app.hold import calibration as cal
from app.hold.calibration import CalibrationError


def a_space(dim: int, n: int = 512, seed: int = 7):
    """An anisotropic space: a shared direction plus per-dimension scales
    that span two orders of magnitude. Real embedding spaces are strongly
    anisotropic — that is T0's whole premise — and an isotropic fixture
    would make every ratio 1.0 and prove nothing."""
    rng = np.random.default_rng(seed)
    scale = np.exp(rng.normal(0, 1.2, dim)).astype("float32")
    centre = rng.standard_normal(dim).astype("float32")
    centre /= np.linalg.norm(centre)
    v = centre + rng.standard_normal((n, dim)).astype("float32") * scale * 0.15
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype("float32")


def a_bundle(reference, n=300, tighten=0.8, sigma=0.22, seed=11):
    """A plausible honest bundle: the same space, but LESS varied than the
    calibration set — one language, one genre, one chunk size against a
    set that spans every script and five to eight thousand tokens.

    Two properties matter and both come from the real measurement. The
    ratios sit UNDER 1 rather than straddling it (measured 0.36-1.54,
    median 0.80), so `tighten` is 0.8. And they SPREAD, per dimension,
    which is the property a uniform tightening does not have and without
    which none of the multiple-comparison behaviour appears at all:
    `sigma` is chosen so the distribution lands on the measured shape —
    p2 near 0.6, median near 0.8, and a minimum that falls under the band
    once there are thousands of dimensions to draw it from.
    """
    rng = np.random.default_rng(seed)
    mean = reference.mean(axis=0)
    per_dim = tighten * np.exp(rng.normal(0, sigma, reference.shape[1]))
    spread = reference.std(axis=0) * per_dim.astype("float32")
    v = mean + rng.standard_normal((n, reference.shape[1])).astype("float32") * spread
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype("float32")


# ---- an honest bundle passes, at every dimension --------------------------

@pytest.mark.parametrize("dim", [256, 1024, 2560, 4096])
def test_an_honest_bundle_passes_at_every_dimension(dim):
    """THE regression this test file exists for. The old rule passed at
    2560 and failed at 4096 on the same vectors; a rule that changes its
    mind with the dimension is not measuring the bundle."""
    ref = a_space(dim)
    v = cal.envelope_verdict(a_bundle(ref), cal.envelope_of(ref))
    assert v["passed"], v["reasons"]


def test_the_calibration_vectors_pass_against_their_own_envelope():
    ref = a_space(1024)
    v = cal.envelope_verdict(ref, cal.envelope_of(ref))
    assert v["passed"]
    assert v["cosine"] == pytest.approx(1.0, abs=1e-6)
    assert v["p_lo"] == pytest.approx(1.0, abs=1e-6)


def test_the_extremum_form_is_what_used_to_fail():
    """Not a test of the code — a test of the claim the code rests on.
    At 4096 dimensions an honest bundle puts SOMETHING outside the band,
    which is exactly why the rule is a percentile; at 256 it does not,
    which is why the defect hid for so long."""
    ref = a_space(4096)
    bundle = a_bundle(ref)
    sd = ref.std(axis=0)
    ratio = bundle.std(axis=0) / sd
    lo, hi = cal.ENVELOPE_BAND
    assert ((ratio < lo) | (ratio > hi)).sum() > 0, \
        "the fixture no longer reproduces the multiple-comparison effect"
    v = cal.envelope_verdict(bundle, cal.envelope_of(ref))
    assert v["passed"], "and the percentile rule is unmoved by it"


# ---- what T0 exists to catch ---------------------------------------------

def test_random_vectors_are_refused():
    ref = a_space(1024)
    rng = np.random.default_rng(3)
    r = rng.standard_normal((300, 1024)).astype("float32")
    r /= np.linalg.norm(r, axis=1, keepdims=True)
    v = cal.envelope_verdict(r, cal.envelope_of(ref))
    assert not v["passed"]
    assert any("cosine" in r_ for r_ in v["reasons"])


def test_a_foreign_model_is_refused():
    """Modelled as the same vectors with their dimensions permuted: the
    marginals are identical and only the geometry is wrong, which is the
    hardest version of this case and the one a per-dimension marginal
    test would sail through."""
    ref = a_space(1024)
    rng = np.random.default_rng(5)
    bundle = a_bundle(ref)
    v = cal.envelope_verdict(bundle[:, rng.permutation(1024)],
                             cal.envelope_of(ref))
    assert not v["passed"]


def test_an_un_normalised_bundle_is_refused_by_the_deviation_test_alone():
    """The cosine does not notice scaling at all — it is scale-invariant —
    so this case is the deviation test's to catch, and it is why the two
    statistics are not redundant."""
    ref = a_space(1024)
    rng = np.random.default_rng(9)
    bundle = a_bundle(ref)
    scaled = bundle * rng.uniform(0.2, 5.0, (bundle.shape[0], 1)).astype("float32")
    v = cal.envelope_verdict(scaled, cal.envelope_of(ref))
    assert not v["passed"]
    assert v["cosine"] >= cal.ENVELOPE_COS_MIN, "the cosine is blind to scale"
    assert any("deviation" in r for r in v["reasons"])


def test_a_degenerate_bundle_is_refused():
    ref = a_space(1024)
    one = np.repeat(a_bundle(ref)[:1], 300, axis=0)
    v = cal.envelope_verdict(one, cal.envelope_of(ref))
    assert not v["passed"]


def test_a_half_random_bundle_is_refused():
    ref = a_space(1024)
    rng = np.random.default_rng(13)
    bundle = a_bundle(ref)
    r = rng.standard_normal((150, 1024)).astype("float32")
    r /= np.linalg.norm(r, axis=1, keepdims=True)
    v = cal.envelope_verdict(np.vstack([bundle[:150], r]), cal.envelope_of(ref))
    assert not v["passed"]


def test_IN_SPACE_substitution_is_invisible_to_this_test_at_ANY_fraction():
    """The honest limit of the tier, and the sharpest form of it.

    A producer who ships REAL vectors from this space, correctly computed,
    but attached to the wrong chunks, produces a bundle that is perfectly
    in-distribution. Every statistic here is a property of the vectors as
    a SET — a mean direction and per-dimension deviations — and that set
    is unchanged when the labels on it are wrong. So the envelope cannot
    see this attack at 10 %, and it cannot see it at 100 % either. That is
    not a threshold to tune; it is what the test measures.

    What catches it is elsewhere and is exact rather than statistical: T0's
    OWN requirement that the bundle's set of chunk ids equals the set
    `chunks` derives locally, per record; and T3, which re-embeds sixteen
    chunks and compares each against ITS OWN text. On real data a bundle a
    tenth random also passed both statistics — see
    `tools/measure_t0_envelope.py`, which reproduces that — and the
    extremum form that caught it was the same statistic that refuses
    honest bundles.
    """
    ref = a_space(1024)
    bundle = a_bundle(ref)
    env = cal.envelope_of(ref)
    assert cal.envelope_verdict(bundle, env)["passed"]

    # Shuffled rows: every vector correct, every label wrong. Identical
    # statistics, by construction — this is an equality, not a threshold.
    rng = np.random.default_rng(17)
    shuffled = bundle[rng.permutation(bundle.shape[0])]
    v = cal.envelope_verdict(shuffled, env)
    assert v["passed"]
    assert v["cosine"] == pytest.approx(
        cal.envelope_verdict(bundle, env)["cosine"], abs=1e-12)

    # And vectors from the same space that were never in this bundle at
    # all — a producer substituting another shard's work wholesale.
    v = cal.envelope_verdict(a_bundle(ref, seed=99), env)
    assert v["passed"], "in-space vectors pass whatever chunks they claim"


# ---- the verdict is usable ------------------------------------------------

def test_a_refusal_names_the_number_that_failed():
    ref = a_space(512)
    rng = np.random.default_rng(21)
    r = rng.standard_normal((300, 512)).astype("float32")
    r /= np.linalg.norm(r, axis=1, keepdims=True)
    v = cal.envelope_verdict(r, cal.envelope_of(ref))
    assert v["reasons"] and all(isinstance(s, str) and s for s in v["reasons"])
    for key in ("cosine", "p_lo", "p_hi", "ratio_min", "ratio_max", "dead_dims"):
        assert key in v, f"{key} is evidence a reader needs"


def test_a_dead_dimension_is_not_a_division_by_zero():
    """No measured space had one — the smallest deviation seen was
    2.3e-06 — but a space that did must not turn the ratio into a NaN
    that compares false against every bound and refuses silently."""
    ref = a_space(1024).copy()
    ref[:, 5] = 0.0                            # a dimension that never varies
    env = cal.envelope_of(ref)
    assert env[1][5] == 0.0
    v = cal.envelope_verdict(a_bundle(ref), env)
    assert v["dead_dims"] == 1
    assert np.isfinite(v["p_lo"]) and np.isfinite(v["p_hi"])


def test_a_mis_shaped_envelope_is_refused_rather_than_broadcast():
    ref = a_space(256)
    with pytest.raises(CalibrationError, match=r"\[2, dim\]"):
        cal.envelope_verdict(a_bundle(ref), ref)
    with pytest.raises(CalibrationError, match="envelope is for dim"):
        cal.envelope_verdict(a_bundle(ref)[:, :128], cal.envelope_of(ref))


def test_the_literals_are_the_ones_the_specification_names():
    """03 §3.5 fixes these, and a peer that quietly used others would
    admit or refuse bundles no other peer does."""
    assert cal.ENVELOPE_COS_MIN == 0.5
    assert cal.ENVELOPE_BAND == (0.4, 2.0)
    assert cal.ENVELOPE_QUANTILE == 2
