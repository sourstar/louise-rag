"""Post-F2 hardening: artifact-trust enforcement (tighten-only), calibrated
verify-on-use, the resweep verb, and untruncated evidence.

The security property under test throughout: an UNTRUSTED artifact — anyone
can publish one — must never be able to LOOSEN a check. It defines what
"honest" means, so a hostile one publishing tau=0.1 would otherwise wave
anything through.
"""
import json

import numpy as np
import pytest

from app import db, embeddings, platform_config
from app.bundles import export as exp, importer, ledger, verify_on_use
from app.verify import t2, t3
from tests.bundles.helpers import seed_document, unit_vectors


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
        embed_fn=lambda batch: [by_text[t] for t in batch])
    assert outcome["floor"] == verify_on_use.VERIFY_FLOOR
    assert outcome["floor_source"] == "coarse-fallback"


# ─────────────── resweep ───────────────

@pytest.fixture
def under_verified(con, collection, producer_key, monkeypatch, tmp_path):
    """A bundle imported at tier < 3 (no artifact -> the ladder never ran)."""
    rows = seed_document(con, collection, "gutenberg:1", 1, n_chunks=4, seed=1)
    con.execute("UPDATE documents SET embed_done=0 WHERE uid='gutenberg:1'")
    exp.export_bundle(con, collection, ["gutenberg:1"], tmp_path / "b",
                      signing_key=producer_key)
    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda batch: [by_text[t] for t in batch])
    result = importer.import_bundle(con, collection, tmp_path / "b")
    assert result["ok"]
    assert ledger.bundles_below_tier(con, 3), "premise: bundle is under-verified"
    return con, collection, rows, by_text


def test_resweep_promotes_a_clean_bundle_to_tier_3(under_verified, monkeypatch):
    con, collection, rows, by_text = under_verified
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)
    outcome = verify_on_use.resweep(
        con, collection, embed_fn=lambda batch: [by_text[t] for t in batch])
    assert outcome == {"swept": 1, "promoted": 1, "failed": []}
    assert ledger.bundles_below_tier(con, 3) == []
    states = ledger.verification_states(con, [r["id"] for r in rows])
    assert all(s["state"] == "verified" for s in states.values())


def test_resweep_fails_a_dirty_bundle_and_quarantines(under_verified, producer_key,
                                                       monkeypatch):
    con, collection, rows, by_text = under_verified
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)
    corrupted = dict(by_text)
    corrupted[rows[0]["text"]] = unit_vectors(1, seed=7777)[0]
    outcome = verify_on_use.resweep(
        con, collection, embed_fn=lambda batch: [corrupted[t] for t in batch])
    assert outcome["failed"], "the poisoned bundle was not caught"
    from app.bundles import trust
    with pytest.raises(trust.BundleUntrusted):
        trust.check_admission(con, bytes(producer_key.verify_key).hex())


def test_resweep_leaves_endpoint_down_bundles_for_next_time(under_verified,
                                                             monkeypatch):
    con, collection, _, _ = under_verified
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)

    def down(_batch):
        raise ConnectionError("endpoint down")
    outcome = verify_on_use.resweep(con, collection, embed_fn=down)
    assert outcome["swept"] == 0 and outcome["promoted"] == 0
    assert ledger.bundles_below_tier(con, 3), "the bundle must remain sweepable"


def test_resweep_second_pass_is_free(under_verified, monkeypatch):
    con, collection, _, by_text = under_verified
    monkeypatch.setattr(importer, "find_space_artifact", lambda space_id: None)
    embed = lambda batch: [by_text[t] for t in batch]      # noqa: E731
    verify_on_use.resweep(con, collection, embed_fn=embed)
    second = verify_on_use.resweep(con, collection, embed_fn=embed)
    assert second == {"swept": 0, "promoted": 0, "failed": []}


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
