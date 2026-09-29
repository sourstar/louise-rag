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
from tests.hold.conftest import a_stated_run, fake_embed, pubmed, pubmed_v2

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


NO_RUN = object()


def gate(codex, got, *, seed=b"\x09" * 32, ref=None, stated=None, invariant=True):
    """One pass, as the node runs it against RAGE: the endpoint states its
    run, the sixteen fingerprint texts go one at a time and then the way the
    peer contributes. `stated=NO_RUN` is an endpoint that states none."""
    ref = ref if ref is not None else reference()
    stated = None if stated is NO_RUN else (stated or a_stated_run(codex))
    together = fake_embed if invariant else (lambda texts: fake_embed(texts, tag=b"batched"))
    return emb.run_gate(codex, reference=ref, embed=lambda texts: got, seed=seed,
                        stated=stated, embed_one=fake_embed, contribute=together)


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
        "kind", "version", "space_id", "collection_id", "set", "role",
        "run", "engine", "encoder_weights", "driver", "fingerprint",
        "batch_invariant", "seed", "sampled", "passed",
        "worst", "median", "max_input_tokens", "tokens_per_s", "key", "time"}
    # version 2: "this RUN, on this peer, is compliant with this space"
    assert body["kind"] == "louise-calibration-run" and body["version"] == 2
    stated = a_stated_run(codex)
    assert body["run"] == stated.run_id and body["engine"] == stated.engine
    assert body["driver"] == stated.driver and body["role"] == "document"
    assert body["batch_invariant"] is True and body["fingerprint"].startswith("sha256:")
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
    # §9.9 version 2: the engine of the validated run, by its id
    assert set(entry) == {"space_id", "encoder_engine", "encoder_weights",
                          "tokens_per_s"}
    assert entry["encoder_engine"] == cx.digest(cx.canonical_json(body["engine"]))
    assert entry["space_id"] == codex.space_id
    assert entry["tokens_per_s"] >= 0


# ---- a run is validated, and a run has to be named (dd9bef4) -------------------

def test_an_endpoint_that_states_no_run_gets_a_verdict_and_no_statement():
    """03 §3.3: "An endpoint that states no run identity cannot be held to a
    validated run: a peer may search with it and writes nothing through it."
    The pass is reported; nothing is kept, because there is nothing a later
    answer could be matched against."""
    codex = codex_768()
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=NO_RUN)
    assert run.ok, "the verdict stands on its own"
    with pytest.raises(GateError, match="Louise-Run"):
        emb.statement(run, codex, a_key(), now=NOW)


def test_the_fingerprint_is_the_sixteen_taken_one_at_a_time():
    from app.hold import fingerprint
    codex = codex_768()
    run = gate(codex, reference())
    texts = cal.verify(codex.modality, codex.space.calibration.set)
    sixteen = [texts[i] for i in fingerprint.indices(texts)]
    assert run.fingerprint == fingerprint.of(fake_embed(sixteen))


def test_a_run_that_batches_differently_is_validated_for_single_texts_only():
    """The batching fault that silently corrupted 714 books (2026-07) is a
    property of a run's settings. It is measured — the same sixteen, the way
    this peer contributes — never assumed."""
    codex = codex_768()
    ref = reference()
    run = gate(codex, ref, ref=ref, invariant=False)
    assert run.ok and run.batch_invariant is False
    body = emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))
    assert body["batch_invariant"] is False


def test_an_asymmetric_spaces_query_run_is_gated_against_its_own_reference():
    codex = codex_768()
    seen = {}

    def embed(texts):
        seen["first"] = texts[0]
        return reference()

    run = emb.run_gate(codex, reference=reference(), embed=embed, seed=b"\x09" * 32,
                       role="query", stated=a_stated_run(codex),
                       embed_one=fake_embed, contribute=fake_embed)
    assert run.role == "query" and run.ok
    body = emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))
    assert body["role"] == "query"
    symmetric = replace(codex, space=replace(
        codex.space, query=cx.Query(None, None),
        calibration=replace(codex.space.calibration, query_vectors=None)))
    with pytest.raises(GateError, match="symmetric"):
        emb.run_gate(symmetric, reference=reference(), embed=embed, role="query")


def test_the_query_texts_carry_the_spaces_instruction():
    codex = codex_768()
    instructed = replace(codex, space=replace(
        codex.space, query=replace(codex.space.query, instruction="Query: ")))
    seen = []
    emb.run_gate(instructed, reference=reference(), embed=lambda t: seen.extend(t) or reference(),
                 role="query")
    assert all(t.startswith("Query: ") for t in seen) and len(seen) == 512


def test_a_version_2_collection_keeps_the_statement_it_always_had():
    """Its Codex names an image, so its statement does (09 §9.12: "Version 1
    of this body carried `encoder_image` … in place of the run")."""
    old = pubmed_v2()
    ref = reference()
    body = emb.read_statement(emb.statement(gate(old, ref, ref=ref, stated=NO_RUN),
                                            old, a_key(), now=NOW))
    assert body["version"] == 1 and body["encoder_image"] == old.space.encoder.image
    assert "run" not in body
    assert set(emb.benchmarks_entry(body)) == {"space_id", "encoder_image",
                                               "encoder_weights", "tokens_per_s"}


# ---- the load amendment: statement version 3 (09 §9.12) -------------------

_LOAD = {"Model": "${WEIGHTS}", "ModelFile": "${WEIGHTS}", "Type": "cross-encoder"}


def _with_load(stated):
    engine = dict(stated.engine, load=dict(_LOAD))
    return replace(stated, engine=engine)


def test_a_statement_is_version_3_exactly_when_its_engine_carries_load():
    codex = codex_768()
    ref = reference()
    plain = emb.read_statement(emb.statement(gate(codex, ref, ref=ref), codex,
                                             a_key(), now=NOW))
    assert plain["version"] == 2 and "load" not in plain["engine"]
    stated = _with_load(a_stated_run(codex))
    body = emb.read_statement(emb.statement(gate(codex, ref, ref=ref, stated=stated),
                                            codex, a_key(), now=NOW))
    assert body["version"] == emb.STATEMENT_LOAD_VERSION
    assert body["engine"]["load"] == _LOAD


@pytest.mark.parametrize("change, rule", [
    ({"version": 3}, "lacks the engine's `load`"),
    ({"engine_load": True}, "carries the engine's `load`"),
    ({"version": 4}, "unsupported calibration statement version 4"),
])
def test_a_statement_whose_version_does_not_follow_load_is_refused(change, rule):
    codex = codex_768()
    ref = reference()
    body = emb.read_statement(emb.statement(gate(codex, ref, ref=ref), codex,
                                            a_key(), now=NOW))
    if change.pop("engine_load", False):
        body["engine"] = dict(body["engine"], load=dict(_LOAD))
    body.update(change)
    key = a_key()
    body["key"] = env.key_text(key.verify_key)
    with pytest.raises(GateError, match=rule):
        emb.read_statement(env.sign(env.CALIBRATION, body, key))


def test_the_spec_names_the_statement_versions_it_is_checked_by():
    spec = open("docs/09-formats.md", encoding="utf-8").read()
    assert '{kind: "louise-calibration-run", version: 2 · 3,' in spec
    assert "| `louise-calibration-run` | 2 · 3 |" in spec
