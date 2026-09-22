"""F2's calibration harness, against the COMMITTED recorded fixtures.

The constraint under test everywhere: reproducible from recorded vectors, no
live endpoint anywhere. The one test that would touch a network asserts the
opposite — that nothing here can.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from app.verify import t3
from tools import calibrate_space

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "calibration"


@pytest.fixture(scope="module")
def recorded():
    return calibrate_space.load_recorded(FIXTURES)


@pytest.fixture(scope="module")
def result(recorded):
    backends, meta = recorded
    return calibrate_space.calibrate(backends, meta)


def test_live_recordings_classify_by_endpoint_order(tmp_path):
    """Live recordings key lanes by URL, not the synthetic prefix names —
    the first endpoint is the reference, the rest are same-weights lanes.
    Regression for the first real run, which recorded both endpoints and
    then crashed in classify(); the report writer must also survive the
    absence of a different-model lane."""
    rng = np.random.default_rng(7)
    base = rng.normal(size=(64, 32))
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    jitter = base + rng.normal(scale=1e-5, size=base.shape)
    jitter /= np.linalg.norm(jitter, axis=1, keepdims=True)
    backends = {"http://gpu:1": base, "http://cpu:2": jitter}

    ref, same, quant, other = calibrate_space.classify(backends)
    assert ref == "http://gpu:1"
    assert list(same) == ["http://cpu:2"] and not quant and not other

    result = calibrate_space.calibrate(backends, {"synthetic": False})
    assert result["recommendation"]["tau"] > 0.999
    report = tmp_path / "r.md"
    calibrate_space.write_report(result, backends, report)
    assert "Different model" in report.read_text(encoding="utf-8")


# ─────────────── reproducibility: the constraint itself ───────────────

def test_fixtures_regenerate_byte_identically(tmp_path, recorded):
    """The committed recording carries its seed; --synth with that seed must
    reproduce it exactly, or 'reproducible from recorded vectors' is a claim
    rather than a property."""
    calibrate_space.synthesize(tmp_path)
    fresh, _ = calibrate_space.load_recorded(tmp_path)
    committed, meta = recorded
    assert meta["synthetic"] is True and meta["seed"] == calibrate_space.SEED
    assert set(fresh) == set(committed)
    for key in fresh:
        np.testing.assert_array_equal(fresh[key], committed[key])


def test_calibration_is_deterministic(recorded):
    backends, meta = recorded
    first = calibrate_space.calibrate(backends, meta)
    second = calibrate_space.calibrate(backends, meta)
    assert first["recommendation"] == second["recommendation"]
    assert first["epsilon"] == second["epsilon"]


def test_fixtures_declare_themselves_synthetic(recorded):
    """The honesty label is data, not prose: the meta file carries it, and the
    report generator keys its disclaimer off it."""
    _, meta = recorded
    assert meta["synthetic"] is True


# ─────────────── the measurements the doc's numbers rest on ───────────────

def test_tau_separates_same_model_from_different_model(result, recorded):
    backends, _ = recorded
    tau = result["recommendation"]["tau"]
    reference = backends["reference/cuda/f16"]

    same = calibrate_space.rowwise_cosine(reference, backends["same/sycl/f16"])
    other = calibrate_space.rowwise_cosine(reference, backends["othermodel/cuda/f16"])
    assert np.all(same >= tau - 1e-9), "honest backends must sit above tau"
    assert np.all(other < tau), "a different model must sit far below tau"
    assert result["recommendation"]["margin_to_other_model_p99"] > 0.2


def test_false_reject_uses_the_exact_lattice_not_wald(result):
    """Same honesty rule P1.2 established: report delivered error rates."""
    rec = result["recommendation"]
    sprt = t3.Sprt(f0=max(rec["p_miss_honest"], 1e-9), f1=0.05,
                   alpha=1e-2, beta=0.05, n_max=rec["n_t3"])
    assert rec["false_reject_at_n"] == pytest.approx(
        sprt.delivered_error_rates(max(rec["p_miss_honest"], 1e-9)))
    # and §6's per-importer weakness is preserved, not inflated
    assert 0.5 < rec["detect_5pct_at_n"] < 0.62


def test_more_noise_means_lower_cosine_floor(recorded):
    """Monotonicity — the property that makes the harness a measurement
    instrument rather than a formatter: coarser lanes must measurably reach
    further down."""
    backends, _ = recorded
    reference = backends["reference/cuda/f16"]
    floors = {k: float(np.min(calibrate_space.rowwise_cosine(reference, v)))
              for k, v in backends.items() if k != "reference/cuda/f16"}
    assert floors["same/cpu/f32"] > floors["same/sycl/f16"]
    assert floors["same/sycl/f16"] > floors["quant/cuda/q4"]
    assert floors["quant/cuda/q4"] > floors["othermodel/cuda/f16"]


def test_epsilon_ladder_is_monotone_and_bounds_the_budget(result):
    ladder = result["epsilon"]["ladder"]
    p01 = [step["cos_p01"] for step in ladder]
    assert all(a >= b - 1e-12 for a, b in zip(p01, p01[1:])), \
        "cosine must fall as perturbation grows"
    budget = result["epsilon"]["adversary_epsilon_budget"]
    assert budget > 0
    tau = result["recommendation"]["tau"]
    for step in ladder:
        if step["epsilon"] <= budget:
            assert step["passes_at_tau"] or step["epsilon"] == budget
        if step["epsilon"] > budget * 3:
            assert not step["passes_at_tau"]


def test_quantization_verdicts_are_measured_not_assumed(result):
    """The interesting §6 question: does quantization drift stay inside tau
    (indistinguishable from honest) or reach below it? Whatever the answer on
    these fixtures, it must be MEASURED — present, per-lane, and consistent
    with the recorded vectors."""
    floors = result["quantization_floor"]
    assert set(floors) == {"quant/cuda/q8", "quant/cuda/q4"}
    tau = result["recommendation"]["tau"]
    # q4's coarser noise was recorded well below the same-model lanes; the
    # harness must report it as distinguishable rather than smoothing it over
    assert floors["quant/cuda/q4"] < tau


# ─────────────── wiring into t3 (deliverable 4) ───────────────

class _Artifact:
    def __init__(self, calibration):
        self.raw = {"calibration": calibration}

    def calibration(self):
        return self.raw["calibration"]


def test_calibration_block_drives_t3_directly(result, recorded):
    """The output format IS t3's expectation: a producer whose fingerprint
    matches a measured pair gets the measured tau, labeled calibration_pair —
    no translation layer, no re-keying."""
    backends, _ = recorded
    artifact = _Artifact(result["calibration"])
    tau, f0, f1, source = t3._tau_for(
        artifact, backend_fingerprint="same/sycl/f16", dtype="f32")
    assert source == "calibration_pair"
    assert tau == pytest.approx(result["recommendation"]["tau"])

    reference = backends["reference/cuda/f16"]
    chunk_ids = [f"gutenberg:1:{i}:1" for i in range(24)]
    vectors = [reference[i] for i in range(24)]
    texts = {cid: f"text {i}" for i, cid in enumerate(chunk_ids)}
    sycl = backends["same/sycl/f16"]
    index = {f"text {i}": i for i in range(24)}

    honest = t3.check_spot_recompute(
        chunk_ids, vectors, artifact, texts=texts,
        embed_fn=lambda batch: [sycl[index[t]] for t in batch],
        backend_fingerprint="same/sycl/f16", rng=__import__("random").Random(7))
    assert honest.outcome == "passed", honest.detail
    assert honest.statistics["tau_source"] == "calibration_pair"

    other = backends["othermodel/cuda/f16"]
    wrong = t3.check_spot_recompute(
        chunk_ids, vectors, artifact, texts=texts,
        embed_fn=lambda batch: [other[index[t]] for t in batch],
        backend_fingerprint="same/sycl/f16", rng=__import__("random").Random(7))
    assert wrong.outcome == "failed"


def test_artifact_writer_accepts_the_calibration_block(tmp_path, result):
    from tools import build_space_artifact
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"calibration": result["calibration"]}))
    block = build_space_artifact.load_calibration(path)
    assert block["pairs"] and block["tau_default"] == pytest.approx(
        result["recommendation"]["tau"])


def test_committed_calibration_json_matches_the_fixtures(result):
    """The checked-in calibration.json is the harness's own output over the
    checked-in vectors — assert they have not drifted apart."""
    committed = json.loads((FIXTURES / "calibration.json").read_text())
    assert committed["calibration"]["tau_default"] == pytest.approx(
        result["calibration"]["tau_default"])
    assert committed["recommendation"]["p_miss_honest"] == pytest.approx(
        result["recommendation"]["p_miss_honest"])


# ─────────────── the constraint, structurally ───────────────

def test_nothing_here_touches_a_network(monkeypatch, recorded):
    """Live runs are a flag, never a test dependency — enforced, not hoped."""
    import httpx

    from app import httpc

    def forbidden(*a, **kw):
        raise AssertionError("calibration tests must not touch the network")
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    # the model clients go through the pooled client (app/httpc.py); barring
    # httpx alone would leave a door this test exists to keep shut
    monkeypatch.setattr(httpc, "post", forbidden)
    monkeypatch.setattr(httpc, "get", forbidden)
    monkeypatch.setattr(httpc, "client", forbidden)
    backends, meta = recorded
    calibrate_space.calibrate(backends, meta)      # must complete untouched
