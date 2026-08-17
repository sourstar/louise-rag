"""Admission checks — the detection matrix.

The premise being defended: T0 alone (dim/dtype/norm/NaN/dupes) is satisfied by
ANY competent embedding model, so a bundle from a different model passes it
while being semantically unrelated to local queries. The first test below
proves that premise rather than asserting it, and the rest prove each gate
catches what it claims to.
"""
import numpy as np
import pytest

from app.bundles import checks
from app.bundles.format import seq_of, uid_of
from tests.bundles.helpers import DIM, isotropic_vectors, unit_vectors

UID = "gutenberg:1"


def _ids(n, uid=UID):
    return [f"{uid}:{i}:1" for i in range(n)]


def _run(chunk_ids, vectors, dim=DIM, dtype="f32", **kw):
    return checks.run_admission_checks(chunk_ids, vectors, dim, dtype,
                                       uid_of=uid_of, seq_of=seq_of, **kw)


def _named(verdict, name):
    return next(r for r in verdict.results if r.name == name)


# ─────────────────── the premise this whole module exists for ───────────────────

def test_a_different_model_passes_every_structural_check():
    """T0 cannot tell a different model from the right one — which is exactly
    why sample re-embed exists. If this test ever fails, the threat model has
    changed and the docstrings need revisiting."""
    wrong_model = unit_vectors(40, seed=999)     # well-formed, just from elsewhere
    structural = checks.check_structural(_ids(40), wrong_model, DIM, "f32")
    assert structural.passed
    assert checks.check_isotropy(wrong_model, DIM).passed
    assert checks.check_degenerate(wrong_model).passed


def test_sample_reembed_is_what_catches_it():
    honest = unit_vectors(40, seed=1)
    wrong_model = unit_vectors(40, seed=999)
    ids = _ids(40)
    texts = {cid: f"text {i}" for i, cid in enumerate(ids)}

    def embed_local(batch):
        # this node's encoder reproduces the honest vectors
        return [honest[texts_index[t]] for t in batch]
    texts_index = {f"text {i}": i for i in range(40)}

    assert _run(ids, honest, texts=texts, embed_fn=embed_local).passed
    verdict = _run(ids, wrong_model, texts=texts, embed_fn=embed_local)
    assert not verdict.passed
    assert _named(verdict, "sample_reembed").detail.count("DIFFERENT MODEL") == 1


# ─────────────────────────── T0 structural ───────────────────────────

def test_structural_accepts_an_honest_bundle():
    assert checks.check_structural(_ids(10), unit_vectors(10), DIM, "f32").passed


def test_missing_normalization_is_caught():
    """§6 calls this 'the most common incompetence bug'."""
    vectors = [v * 7.5 for v in unit_vectors(10)]
    result = checks.check_structural(_ids(10), vectors, DIM, "f32")
    assert not result.passed and "unit-norm" in result.detail


def test_nan_and_inf_are_caught():
    for bad in (np.nan, np.inf):
        vectors = unit_vectors(10)
        vectors[3] = vectors[3].copy()
        vectors[3][0] = bad
        result = checks.check_structural(_ids(10), vectors, DIM, "f32")
        assert not result.passed and "NaN or Inf" in result.detail


def test_zero_vector_is_caught():
    """Reachable from our own pipeline: _embed_batch's `or 1.0` guard preserves
    an all-zero vector rather than rejecting it."""
    vectors = unit_vectors(10)
    vectors[2] = np.zeros(DIM, dtype=np.float32)
    result = checks.check_structural(_ids(10), vectors, DIM, "f32")
    assert not result.passed and "zero vector" in result.detail


def test_wrong_dimension_is_caught():
    vectors = [np.ones(DIM + 1, dtype=np.float32) / np.sqrt(DIM + 1) for _ in range(10)]
    result = checks.check_structural(_ids(10), vectors, DIM, "f32")
    assert not result.passed and "declared dim" in result.detail


def test_duplicate_chunk_ids_are_caught():
    ids = _ids(10)
    ids[4] = ids[3]
    result = checks.check_structural(ids, unit_vectors(10), DIM, "f32")
    assert not result.passed and "duplicate chunk ids" in result.detail


def test_f16_rounding_stays_inside_the_norm_band():
    """The f32->f16 cast perturbs norms by ~1e-3; the band must accommodate it
    or every honest f16 bundle is rejected."""
    vectors = [v.astype(np.float16).astype(np.float32) for v in unit_vectors(20)]
    assert checks.check_structural(_ids(20), vectors, DIM, "f16").passed


def test_structural_failure_short_circuits():
    """Later checks assume finite unit-norm input; running them on garbage
    would produce meaningless statistics."""
    verdict = _run(_ids(10), [v * 99 for v in unit_vectors(10)])
    assert not verdict.passed
    assert [r.name for r in verdict.results] == ["t0_structural"]


# ─────────────────────────── isotropy ───────────────────────────

def test_random_unit_vectors_are_rejected():
    """Structurally flawless and semantically meaningless — what a lazy forger
    produces."""
    result = checks.check_isotropy(isotropic_vectors(200, seed=7), DIM)
    assert not result.passed
    assert "not embeddings" in result.detail


def test_real_looking_vectors_pass_isotropy():
    assert checks.check_isotropy(unit_vectors(200, seed=3), DIM).passed


def test_isotropy_skips_a_small_sample_rather_than_guessing():
    result = checks.check_isotropy(unit_vectors(4), DIM)
    assert result.passed and "skipped" in result.detail


# ─────────────────────────── overlap invariant ───────────────────────────

@pytest.mark.parametrize("seed", range(6))
def test_shuffled_vectors_are_caught(seed):
    """Shuffling destroys adjacency while leaving every structural property and
    the whole cosine distribution untouched.

    Run across several shuffles because the first version of this check used a
    fixed 0.02 gap and let a shuffle through on a spurious 0.042 — the noise on
    a 39-pair mean is itself worth several hundredths of a cosine.
    """
    vectors = unit_vectors(40, seed=5)
    ids = _ids(40)
    rng = np.random.default_rng(seed)
    shuffled = list(vectors)
    rng.shuffle(shuffled)
    result = checks.check_overlap(ids, shuffled, uid_of, seq_of)
    assert not result.passed, f"shuffle {seed} slipped through: {result.detail}"
    assert "claimed chunk ordering" in result.detail


def test_adjacent_chunks_pass_overlap():
    assert checks.check_overlap(_ids(40), unit_vectors(40, seed=5),
                                 uid_of, seq_of).passed


def test_overlap_skips_when_there_are_too_few_adjacent_pairs():
    ids = [f"gutenberg:{i}:0:1" for i in range(6)]   # each uid has one chunk
    result = checks.check_overlap(ids, unit_vectors(6), uid_of, seq_of)
    assert result.passed and "skipped" in result.detail


# ─────────────────────────── degenerate ───────────────────────────

def test_mass_duplicated_vectors_are_caught():
    one = unit_vectors(1)[0]
    result = checks.check_degenerate([one] * 20)
    assert not result.passed and "distinct" in result.detail


def test_distinct_vectors_pass():
    assert checks.check_degenerate(unit_vectors(20)).passed


# ─────────────────────────── sample re-embed ───────────────────────────

def test_sample_selection_uses_os_entropy_not_bundle_content():
    """§6: a bundle-derived seed is grindable — corrupt a fraction, then
    re-roll until the sample misses. Two calls on identical input must differ."""
    ids = _ids(200)
    draws = {tuple(checks.choose_sample(ids)) for _ in range(12)}
    assert len(draws) > 1, "sample selection appears deterministic"


def test_sample_selection_returns_everything_when_small():
    ids = _ids(3)
    assert set(checks.choose_sample(ids, size=8)) == set(ids)


def test_unreachable_embed_endpoint_is_transient_not_a_rejection():
    """A check that could not RUN must be retryable, unlike one that ran and
    failed — otherwise a brief endpoint outage permanently rejects good data."""
    ids = _ids(10)
    def boom(_batch):
        raise ConnectionError("endpoint down")
    with pytest.raises(checks.VerificationUnavailable):
        checks.check_sample_reembed(ids, unit_vectors(10),
                                     {cid: "t" for cid in ids}, boom)


def test_reembed_skipped_when_no_local_text_is_available():
    ids = _ids(10)
    result = checks.check_sample_reembed(ids, unit_vectors(10), {}, lambda b: [])
    assert result.passed and "skipped" in result.detail


def test_slightly_perturbed_vectors_still_pass_the_coarse_gate():
    """The floor is a model-identity gate, NOT P1.2's calibrated tau: small
    perturbations are explicitly out of its remit, and claiming otherwise would
    be a false promise."""
    honest = unit_vectors(20, seed=11)
    rng = np.random.default_rng(4)
    nudged = []
    for v in honest:
        w = v + rng.normal(scale=0.02, size=DIM).astype(np.float32)
        nudged.append(w / np.linalg.norm(w))
    ids = _ids(20)
    texts = {cid: f"t{i}" for i, cid in enumerate(ids)}
    index = {f"t{i}": i for i in range(20)}
    result = checks.check_sample_reembed(
        ids, nudged, texts, lambda batch: [honest[index[t]] for t in batch])
    assert result.passed


# ─────────────────────────── verdict plumbing ───────────────────────────

def test_verdict_summary_and_raise():
    verdict = _run(_ids(200), isotropic_vectors(200, seed=2))
    assert not verdict.passed
    assert "isotropy=FAIL" in verdict.summary()
    with pytest.raises(checks.BundleVerificationError, match="isotropy"):
        verdict.raise_if_failed()


def test_passing_verdict_does_not_raise():
    _run(_ids(40), unit_vectors(40, seed=6)).raise_if_failed()


def test_verification_error_is_a_bundle_error():
    """So worker.py's existing `except Exception` keeps working and the
    importer can catch the whole family in one clause."""
    from app.bundles.format import BundleError
    assert issubclass(checks.BundleVerificationError, BundleError)
    assert issubclass(checks.VerificationUnavailable, BundleError)
