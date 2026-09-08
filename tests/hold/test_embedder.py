"""tests/hold/test_embedder.py — the calibration gate.

docs/03-checking.md §3.3 (the gate and its quorum), §3.1 (the three
outcomes, and that no number is a threshold); docs/09-formats.md §9.12
(the statement), §9.9 (what the digest carries).
"""
from dataclasses import replace

import pytest
from nacl import signing

from app.hold import calibration as cal, codex as cx, embedder as emb, envelope as env
from app.hold.embedder import GateError
from tests.hold.conftest import pubmed

DIM = 768
NOW = "2026-08-01T00:00:00Z"


def a_key():
    return signing.SigningKey(b"\x05" * 32)


def codex_768(**over):
    """The fixture Codex is dim 768, ctx 512, tolerance "0.95", quorum
    {16, 12} — 09 §9.3's worked example."""
    c = pubmed(**over)
    assert c.space.dim == DIM and c.space.calibration.n == 16
    return c


def reference(n=cal.SIZE, dim=DIM):
    """Deterministic unit-ish vectors, one per text."""
    import numpy as np
    rng = np.random.default_rng(7)
    v = rng.standard_normal((n, dim)).astype("float32")
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).tolist()


def perturbed(ref, *, indices=(), to=0.5):
    """`ref` with the named rows rotated to EXACTLY cosine `to` of
    themselves: `v = to·ref + sqrt(1 - to²)·u`, with `u` a unit vector
    orthogonal to `ref`. Stating the target cosine rather than a noise
    scale is the difference between a test that says what it means and one
    that happens to pass."""
    import numpy as np
    rng = np.random.default_rng(11)
    out = [list(r) for r in ref]
    for i in indices:
        r = np.asarray(out[i], dtype="float32")
        r = r / np.linalg.norm(r)
        u = rng.standard_normal(DIM).astype("float32")
        u -= np.dot(u, r) * r                       # orthogonalise
        u /= np.linalg.norm(u)
        v = to * r + float(np.sqrt(max(0.0, 1 - to * to))) * u
        out[i] = (v / np.linalg.norm(v)).tolist()
    return out


def gate(codex, got, *, seed=b"\x09" * 32, ref=None):
    ref = ref if ref is not None else reference()
    return emb.run_gate(codex, reference=ref, embed=lambda texts: got, seed=seed)


# ---- the cosine -----------------------------------------------------------

def test_a_cosine_is_computed_in_f32_and_renormalised():
    """03 §3.1: both vectors upcast, re-normalised, multiplied in f32."""
    assert emb.cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert emb.cosine([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0, abs=1e-6)
    assert emb.cosine([2, 0, 0], [9, 0, 0]) == pytest.approx(1.0), "scale-free"
    assert emb.cosine([0, 0, 0], [1, 0, 0]) == 0.0, "a zero vector is not a crash"


# ---- the pass rule --------------------------------------------------------

def test_an_identical_encoder_passes_everything():
    ref = reference()
    run = gate(codex_768(), ref, ref=ref)
    assert run.ok
    assert run.passed == 16
    assert run.worst == pytest.approx(1.0, abs=1e-5)
    assert run.median == pytest.approx(1.0, abs=1e-5)


def test_the_whole_set_is_embedded_not_only_the_quorum():
    """02 §2.4: the statement reports over all of it, and T0's envelope and
    T1's probe set are the whole set too — so sampling the EMBEDDING step
    would leave the three disagreeing about what was measured."""
    seen = {}

    def embed(texts):
        seen["n"] = len(texts)
        return reference()

    emb.run_gate(codex_768(), reference=reference(), embed=embed,
                 seed=b"\x09" * 32)
    assert seen["n"] == cal.SIZE == 512


def test_both_conjuncts_are_over_the_drawn_n():
    """03 §3.3: "at least `min_pass` reach cosine >= `tolerance` … and the
    median of those n does too"."""
    ref = reference()
    run = gate(codex_768(), ref, ref=ref)
    assert len(run.cosines) == 16
    assert len(run.all_cosines) == 512
    assert run.passed <= len(run.cosines)


def test_a_wholly_different_encoder_fails():
    ref = reference()
    other = perturbed(ref, indices=range(cal.SIZE), to=0.05)
    run = gate(codex_768(), other, ref=ref)
    assert not run.ok
    assert "reached" in run.why()


def test_the_quorum_tolerates_a_few_bad_texts_and_not_many():
    """The reason it is a quorum: an archive is a snapshot of a living
    corpus and a few sampled texts can be stale without the encoder being
    wrong (the same argument genesis.py makes at RE_ENCODE_QUORUM)."""
    ref = reference()
    seed = b"\x09" * 32
    drawn = [int(i) for i in cx.sample(seed, 16, [str(i) for i in range(cal.SIZE)])]
    few = gate(codex_768(), perturbed(ref, indices=drawn[:4], to=0.1),
               seed=seed, ref=ref)
    many = gate(codex_768(), perturbed(ref, indices=drawn[:9], to=0.1),
                seed=seed, ref=ref)
    assert few.ok, "12 of 16 is the quorum; 4 bad is still a pass"
    assert not many.ok, "7 of 16 is below min_pass"


def test_the_median_conjunct_can_fail_alone():
    """A build that is slightly wrong EVERYWHERE passes no median even when
    it never falls far on any single text."""
    ref = reference()
    nearly = perturbed(ref, indices=range(cal.SIZE), to=0.90)
    run = gate(codex_768(), nearly, ref=ref)
    assert run.passed == 0, "0.90 is below the space's 0.95 everywhere"
    assert run.median < run.tolerance
    assert not run.ok


# ---- the tolerance is the Codex's, never a constant -----------------------

def test_the_tolerance_comes_from_the_codex_and_nowhere_else():
    """03 §3.1: "No number in this paragraph is a threshold: `tolerance`
    comes from calibration and from nowhere else." The same vectors pass
    under a loose space and fail under a strict one."""
    ref = reference()
    got = perturbed(ref, indices=range(cal.SIZE), to=0.80)
    loose = codex_768(space=replace(pubmed().space, tolerance="0.50"))
    strict = codex_768(space=replace(pubmed().space, tolerance="0.999"))
    assert gate(loose, got, ref=ref).ok
    assert not gate(strict, got, ref=ref).ok


# ---- what the gate refuses ------------------------------------------------

def test_a_wrong_vector_count_is_refused():
    with pytest.raises(GateError, match="returned 3 vectors"):
        gate(codex_768(), reference(n=3))


def test_a_wrong_dimension_is_refused():
    with pytest.raises(GateError, match="dimensions, the space is 768"):
        gate(codex_768(), reference(dim=384))


def test_a_reference_of_the_wrong_size_is_refused():
    with pytest.raises(GateError, match="reference vectors, the set has 512"):
        emb.run_gate(codex_768(), reference=reference(n=10),
                     embed=lambda t: reference(), seed=b"\x09" * 32)


# ---- the sample is reproducible from the seed -----------------------------

def test_the_same_seed_draws_the_same_texts():
    ref = reference()
    a = gate(codex_768(), ref, seed=b"\x21" * 32, ref=ref)
    b = gate(codex_768(), ref, seed=b"\x21" * 32, ref=ref)
    assert a.sampled == b.sampled
    assert gate(codex_768(), ref, seed=b"\x22" * 32, ref=ref).sampled != a.sampled


def test_the_sampled_indices_are_in_range_and_distinct():
    run = gate(codex_768(), reference())
    assert len(set(run.sampled)) == 16
    assert all(0 <= i < cal.SIZE for i in run.sampled)


# ---- the statement --------------------------------------------------------

def test_the_statement_is_09_9_12s_body_signed_under_its_domain():
    codex = codex_768()
    ref = reference()
    run = gate(codex, ref, ref=ref)
    stmt = emb.statement(run, codex, a_key(), now=NOW)
    body = emb.read_statement(stmt)
    assert set(body) == {
        "kind", "version", "space_id", "collection_id", "set",
        "encoder_image", "encoder_weights", "seed", "sampled", "passed",
        "worst", "median", "max_input_tokens", "tokens_per_s", "key", "time"}
    assert body["kind"] == "louise-calibration-run" and body["version"] == 1
    assert body["space_id"] == codex.space_id
    assert body["set"] == codex.space.calibration.set
    assert len(body["sampled"]) == 16
    assert body["passed"] == run.passed


def test_worst_and_median_are_over_the_sampled_texts():
    """03 §3.3, corrected against 09 §9.12: the statement carries the very
    numbers its verdict was decided by, so anyone holding it can recheck
    from the seed alone."""
    import statistics
    codex = codex_768()
    ref = reference()
    # Everything EXCEPT the drawn texts is rotated away, so the two
    # medians are far apart and the assertion below means something.
    seed = b"\x09" * 32
    drawn = {int(i) for i in cx.sample(seed, 16, [str(i) for i in range(cal.SIZE)])}
    others = [i for i in range(cal.SIZE) if i not in drawn]
    run = gate(codex, perturbed(ref, indices=others, to=0.2), ref=ref, seed=seed)
    body = emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))
    assert float(body["worst"]) == pytest.approx(min(run.cosines), abs=1e-6)
    assert float(body["median"]) == pytest.approx(
        statistics.median(run.cosines), abs=1e-6)
    assert float(body["median"]) != pytest.approx(
        statistics.median(run.all_cosines), abs=1e-6), \
        "over the sampled, not over all 512"


def test_the_cosines_are_decimal_strings_because_a_body_forbids_floats():
    codex = codex_768()
    run = gate(codex, reference())
    body = emb.statement(run, codex, a_key(), now=NOW)["body"]
    assert isinstance(body["worst"], str) and isinstance(body["median"], str)


def test_a_statement_does_not_verify_under_another_domain():
    codex = codex_768()
    stmt = emb.statement(gate(codex, reference()), codex, a_key(), now=NOW)
    with pytest.raises(env.SignatureError, match="does not verify"):
        env.verify(env.ATTESTATION, stmt)


def test_a_tampered_benchmark_does_not_verify():
    """The number 04 §4.5 weights the rendezvous by is inside the
    signature, so inflating it invalidates the statement."""
    codex = codex_768()
    stmt = emb.statement(gate(codex, reference()), codex, a_key(), now=NOW)
    stmt["body"]["tokens_per_s"] = 10 ** 9
    with pytest.raises(env.SignatureError, match="does not verify"):
        emb.read_statement(stmt)


def test_something_that_is_not_a_calibration_statement_is_refused():
    k = a_key()
    body = {"kind": "louise-digest", "version": 1,
            "key": env.key_text(k.verify_key), "time": NOW}
    with pytest.raises(GateError, match="not a calibration statement"):
        emb.read_statement(env.sign(env.CALIBRATION, body, k))


# ---- the digest entry -----------------------------------------------------

def test_the_digest_entry_is_keyed_by_space():
    """09 §9.9, and 04 §4.5's reason: a peer with no entry for a
    collection's space is unscored, since a rate measured on another
    encoder says nothing about this one."""
    codex = codex_768()
    body = emb.read_statement(
        emb.statement(gate(codex, reference()), codex, a_key(), now=NOW))
    entry = emb.benchmarks_entry(body)
    assert set(entry) == {"space_id", "encoder_image", "encoder_weights",
                          "tokens_per_s"}
    assert entry["space_id"] == codex.space_id
    assert entry["tokens_per_s"] >= 0
