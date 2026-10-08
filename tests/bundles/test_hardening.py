"""Post-F2 hardening: artifact-trust enforcement (tighten-only), calibrated
verify-on-use, and untruncated evidence.

The security property under test throughout: an UNTRUSTED artifact — anyone
can publish one — must never be able to LOOSEN a check. It defines what
"honest" means, so a hostile one publishing tau=0.1 would otherwise wave
anything through.
"""
import json

import numpy as np
import pytest

from app import db, platform_config
from app.bundles import importer, ledger, verify_on_use
from app.verify import t2, t3
from tests.bundles.helpers import seed_document, unit_vectors


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    """The hand-built `collection` is the only one: CONFIG_DIR names an
    empty folder, so nothing outside the test (the checkout's own
    collections/, a pre-Codex file now refused) becomes the default."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


class _Artifact:
    def __init__(self, calibration, trusted=False):
        self.raw = {"calibration": calibration}
        self.trusted = trusted

    def calibration(self):
        return self.raw["calibration"]


HOSTILE = {"tau_default": 0.1,                      # wave everything through
           "detect_target": {"f1": 1e-9},           # never enough evidence
           "pairs": [{"producer_class": "evil/backend", "tau": 0.05,
                      "p_miss_honest": 0.5,          # tolerate constant misses
                      "storage_dtype_penalty": {"f32": 0.0}}]}


# ─────────────── tighten-only in t3 ───────────────

def test_untrusted_artifact_cannot_lower_t3_tolerances():
    tau, f0, f1, source = t3._tau_for(_Artifact(HOSTILE, trusted=False),
                                       "evil/backend", "f32")
    assert tau >= t3.COARSE_TAU, "a hostile artifact lowered tau below the floor"
    assert f0 <= 1e-4, "a hostile artifact inflated the tolerated miss rate"
    assert f1 >= 0.05, "a hostile artifact weakened the detection target"
    assert "advisory-floor" in source


def test_trusted_artifact_keeps_its_measured_numbers():
    """A signed artifact from a configured author may set what its
    measurements support — including below the coarse floor."""
    tau, f0, _f1, source = t3._tau_for(_Artifact(HOSTILE, trusted=True),
                                        "evil/backend", "f32")
    assert tau == pytest.approx(0.05)
    assert f0 == pytest.approx(0.5)
    assert source == "calibration_pair"


def test_untrusted_artifact_that_only_tightens_is_untouched():
    strict = {"tau_default": 0.9995, "detect_target": {"f1": 0.05}, "pairs": []}
    tau, f0, f1, source = t3._tau_for(_Artifact(strict, trusted=False), "", "f32")
    assert tau == pytest.approx(0.9995)
    assert "advisory-floor" not in source


def test_untrusted_artifact_cannot_lower_t2_tolerance():
    tau, source = t2._tau_for(_Artifact(HOSTILE, trusted=False), "evil/backend", "f32")
    assert tau >= 0.95 and "advisory-floor" in source
    tau_trusted, _ = t2._tau_for(_Artifact(HOSTILE, trusted=True), "evil/backend", "f32")
    assert tau_trusted == pytest.approx(0.05)


def test_hostile_untrusted_tau_does_not_admit_a_wrong_model():
    """End to end: even with the hostile artifact in play, a different model's
    vectors fail T3 at the floored tolerance."""
    honest = unit_vectors(20, seed=1)
    foreign = unit_vectors(20, seed=999)
    chunk_ids = [f"gutenberg:1:{i}:1" for i in range(20)]
    texts = {cid: f"t{i}" for i, cid in enumerate(chunk_ids)}
    index = {f"t{i}": i for i in range(20)}
    result = t3.check_spot_recompute(
        chunk_ids, honest, _Artifact(HOSTILE, trusted=False), texts=texts,
        embed_fn=lambda batch: [foreign[index[t]] for t in batch],
        backend_fingerprint="evil/backend")
    assert result.outcome == "failed"


# ─────────────── [trust] space_authors surface ───────────────

def test_platform_toml_trust_block_parses(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(d))
    (d / "platform.toml").write_text(
        f'[trust]\nspace_authors = ["{"a" * 64}", "{"B" * 64}"]\n')
    pc = platform_config.load()
    assert pc.trust.space_authors == ("a" * 64, "b" * 64)     # normalized lower


def test_trust_block_defaults_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert platform_config.load().trust.space_authors == ()


@pytest.mark.parametrize("bad", ['["short"]', '["z" ]', '"notalist"',
                                  f'[{json.dumps("g" * 64)}]'])
def test_malformed_space_authors_refuse_at_load(tmp_path, monkeypatch, bad):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(d))
    (d / "platform.toml").write_text(f"[trust]\nspace_authors = {bad}\n")
    from app.transfer.schedule import ScheduleError
    with pytest.raises(ScheduleError):
        platform_config.load()


def test_importer_resolves_artifacts_with_the_configured_authors(tmp_path, monkeypatch):
    """The seam F1 taught us to test: the config surface must actually reach
    the lookup, or trust is parsed and then ignored."""
    d = tmp_path / "config"
    (d / "spaces").mkdir(parents=True)
    monkeypatch.setenv("CONFIG_DIR", str(d))
    (d / "platform.toml").write_text(f'[trust]\nspace_authors = ["{"c" * 64}"]\n')

    captured = {}

    def fake_find(space_id, dirs, trusted_authors=None):
        captured["authors"] = trusted_authors
        return None
    from app.verify import artifact as artifact_mod
    monkeypatch.setattr(artifact_mod, "find", fake_find)
    importer.find_space_artifact("a" * 32)
    assert captured["authors"] == ["c" * 64]


# ─────────────── calibrated verify-on-use ───────────────

def test_verify_floor_uses_the_artifact_tau(con, collection, monkeypatch):
    calibrated = {"tau_default": 0.9992, "detect_target": {"f1": 0.05}, "pairs": []}
    monkeypatch.setattr(importer, "find_space_artifact",
                        lambda space_id: _Artifact(calibrated, trusted=True))
    floor, source = verify_on_use._verify_floor(con)
    assert floor == pytest.approx(0.9992)
    assert source == "tau_default"


def test_verify_floor_clamps_a_hostile_untrusted_artifact(con, collection, monkeypatch):
    monkeypatch.setattr(importer, "find_space_artifact",
                        lambda space_id: _Artifact(HOSTILE, trusted=False))
    floor, source = verify_on_use._verify_floor(con)
    assert floor >= verify_on_use.VERIFY_FLOOR
    assert "advisory-floor" in source


def test_verify_floor_degrades_to_coarse_without_an_artifact(con, collection,
                                                              monkeypatch):
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)
    floor, source = verify_on_use._verify_floor(con)
    assert floor == verify_on_use.VERIFY_FLOOR and source == "coarse-fallback"


def test_verify_chunks_reports_which_floor_it_used(con, collection, monkeypatch):
    rows = seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    ledger.mark_imported(con, [r["id"] for r in rows], "gutenberg:1", "a" * 64, "b1")
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)
    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    outcome = verify_on_use.verify_chunks(
        con, collection, [r["id"] for r in rows],
        embed_fn=lambda batch, **kw: [by_text[t] for t in batch])
    assert outcome["floor"] == verify_on_use.VERIFY_FLOOR
    assert outcome["floor_source"] == "coarse-fallback"


# ─────────────── evidence blob ───────────────

def test_evidence_survives_untruncated(con):
    huge = json.dumps({"tiers": [{"statistics": {"vector": list(range(4000))}}]})
    assert len(huge) > 8000
    ledger.record_evidence(con, "b1", huge)
    assert ledger.get_evidence(con, "b1") == huge


def test_verdict_evidence_limit_is_opt_in():
    from app.verify.verdict import PASSED, Tier, TierResult, Verdict
    verdict = Verdict()
    verdict.add(TierResult(Tier.T3, PASSED, "ok",
                            {"vectors": [[float(i)] * 200 for i in range(60)]}))
    full = verdict.evidence()
    assert len(full) > 8000, "premise: this evidence is bigger than the old cap"
    assert len(verdict.evidence(limit=500)) == 500
    json.loads(full)                                        # still valid JSON
