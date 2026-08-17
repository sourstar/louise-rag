"""Space artifacts: safe loading, signing, and the writer that produces them.

The load path handles attacker-supplied bytes (a .npy sidecar is arbitrary
input), so the defensive assertions here matter more than the happy path.
"""
import json

import numpy as np
import pytest
from nacl import signing

from app.verify import artifact as artifact_mod
from app.verify.artifact import ArtifactError

DIM = 8
KEY = signing.SigningKey(bytes(range(32)))


def _write_artifact(tmp_path, *, sign=False, **overrides):
    directory = tmp_path / "space.artifact"
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    np.save(directory / artifact_mod.ENVELOPE_NAME,
            np.vstack([rng.normal(size=DIM), np.abs(rng.normal(size=DIM)) + 0.1]
                      ).astype("<f4"))
    np.save(directory / artifact_mod.PROBES_NAME,
            rng.normal(size=(4, DIM)).astype("<f4"))
    np.save(directory / artifact_mod.GOLDENS_NAME,
            rng.normal(size=(3, DIM)).astype("<f4"))

    payload = {
        "schema": artifact_mod.SCHEMA,
        "artifact_version": 1,
        "space_id": "a" * 32,
        "created_at": 1_700_000_000,
        "author_pubkey": bytes(KEY.verify_key).hex(),
        "space": {"dim": DIM, "dtype_reference": "f32", "normalization": "l2"},
        "reference_corpus": {"tag": "test", "chunking_profile": "book",
                             "documents": 10, "chunks": 100,
                             "bootstrap_unit": "document",
                             "bootstrap_replicates": 10000},
        "envelope": {"per_dim_ref": artifact_mod.ENVELOPE_NAME},
        "probes": {"count": 4, "vectors_ref": artifact_mod.PROBES_NAME},
        "goldens": {"bank_size": 3, "vectors_ref": artifact_mod.GOLDENS_NAME},
    }
    payload.update(overrides)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    (directory / artifact_mod.ARTIFACT_NAME).write_bytes(raw)

    if sign:
        import hashlib
        digests = [hashlib.sha256((directory / name).read_bytes()).digest()
                   for name in (artifact_mod.PROBES_NAME, artifact_mod.GOLDENS_NAME,
                                 artifact_mod.ENVELOPE_NAME)]
        signed = artifact_mod.signed_payload(raw, digests)
        (directory / artifact_mod.SIGNATURE_NAME).write_bytes(
            KEY.sign(signed).signature)
    return directory


def test_round_trip(tmp_path):
    loaded = artifact_mod.load(_write_artifact(tmp_path))
    assert loaded.space_id == "a" * 32
    assert loaded.dim == DIM
    assert loaded.probes.shape == (4, DIM)
    assert loaded.goldens.shape == (3, DIM)
    assert loaded.envelope.shape == (2, DIM)


def test_unsigned_artifact_loads_as_advisory(tmp_path):
    """An artifact defines what 'honest' means, so it is not trusted on sight —
    but it still loads, because an advisory artifact may tighten checks."""
    loaded = artifact_mod.load(_write_artifact(tmp_path))
    assert loaded.trusted is False


def test_signed_artifact_from_a_configured_author_is_trusted(tmp_path):
    directory = _write_artifact(tmp_path, sign=True)
    author = bytes(KEY.verify_key).hex()
    assert artifact_mod.load(directory, trusted_authors=[author]).trusted is True


def test_signature_from_an_unconfigured_author_is_not_trusted(tmp_path):
    directory = _write_artifact(tmp_path, sign=True)
    assert artifact_mod.load(directory, trusted_authors=["b" * 64]).trusted is False


def test_tampered_artifact_loses_trust(tmp_path):
    directory = _write_artifact(tmp_path, sign=True)
    raw = json.loads((directory / artifact_mod.ARTIFACT_NAME).read_bytes())
    raw["space_id"] = "b" * 32
    (directory / artifact_mod.ARTIFACT_NAME).write_bytes(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
    author = bytes(KEY.verify_key).hex()
    assert artifact_mod.load(directory, trusted_authors=[author]).trusted is False


def test_tampered_sidecar_loses_trust(tmp_path):
    """Sidecar digests are inside the signed payload, so swapping probe vectors
    must invalidate the signature."""
    directory = _write_artifact(tmp_path, sign=True)
    np.save(directory / artifact_mod.PROBES_NAME,
            np.zeros((4, DIM), dtype="<f4"))
    author = bytes(KEY.verify_key).hex()
    assert artifact_mod.load(directory, trusted_authors=[author]).trusted is False


def test_chunk_bootstrap_is_refused(tmp_path):
    """A null built by resampling CHUNKS is far too tight and rejects honest
    small bundles en masse. The artifact must declare its unit, and anything
    but 'document' is refused."""
    directory = _write_artifact(tmp_path, reference_corpus={
        "tag": "t", "chunking_profile": "book", "documents": 10, "chunks": 100,
        "bootstrap_unit": "chunk", "bootstrap_replicates": 10000})
    with pytest.raises(ArtifactError, match="bootstrap_unit"):
        artifact_mod.load(directory)


def test_sidecar_shape_mismatch_is_refused(tmp_path):
    directory = _write_artifact(tmp_path)
    np.save(directory / artifact_mod.PROBES_NAME,
            np.zeros((9, DIM), dtype="<f4"))     # declared count is 4
    with pytest.raises(ArtifactError, match="shape"):
        artifact_mod.load(directory)


def test_nan_in_a_sidecar_is_refused(tmp_path):
    directory = _write_artifact(tmp_path)
    bad = np.zeros((4, DIM), dtype="<f4")
    bad[0, 0] = np.nan
    np.save(directory / artifact_mod.PROBES_NAME, bad)
    with pytest.raises(ArtifactError, match="NaN"):
        artifact_mod.load(directory)


def test_duplicate_keys_are_refused(tmp_path):
    directory = _write_artifact(tmp_path)
    raw = (directory / artifact_mod.ARTIFACT_NAME).read_bytes()
    (directory / artifact_mod.ARTIFACT_NAME).write_bytes(
        raw[:1] + b'"space_id":"deadbeef",' + raw[1:])
    with pytest.raises(ArtifactError, match="duplicate key"):
        artifact_mod.load(directory)


def test_non_finite_constants_are_refused(tmp_path):
    directory = _write_artifact(tmp_path)
    raw = (directory / artifact_mod.ARTIFACT_NAME).read_bytes()
    (directory / artifact_mod.ARTIFACT_NAME).write_bytes(
        raw.replace(b'"artifact_version":1', b'"artifact_version":NaN'))
    with pytest.raises(ArtifactError):
        artifact_mod.load(directory)


def test_floats_are_allowed_unlike_bundle_headers(tmp_path):
    """A deliberate divergence from format.py: an artifact is nothing but
    calibrated floats, and float round-tripping cannot affect integrity because
    the signature covers the wire bytes."""
    directory = _write_artifact(tmp_path, calibration={"tau_default": 0.9985})
    loaded = artifact_mod.load(directory)
    assert loaded.calibration()["tau_default"] == pytest.approx(0.9985)


def test_find_locates_by_space_id(tmp_path):
    spaces_dir = tmp_path / "spaces"
    spaces_dir.mkdir()
    directory = _write_artifact(tmp_path)
    directory.rename(spaces_dir / f"{'a' * 32}.artifact")
    found = artifact_mod.find("a" * 32, [spaces_dir])
    assert found is not None and found.space_id == "a" * 32
    assert artifact_mod.find("b" * 32, [spaces_dir]) is None


def test_bucket_selection_rounds_down(tmp_path):
    """Use the widest band whose size requirements the bundle meets — never
    interpolate into a tighter band than the bundle's size supports."""
    loaded = artifact_mod.load(_write_artifact(tmp_path))
    buckets = [{"min_docs": 1, "min_chunks": 8}, {"min_docs": 16, "min_chunks": 128},
               {"min_docs": 64, "min_chunks": 1024}]
    assert loaded.bucket(buckets, 20, 200)["min_docs"] == 16
    assert loaded.bucket(buckets, 2, 20)["min_docs"] == 1
    assert loaded.bucket(buckets, 0, 2) is None


def test_alpha_resolvability(tmp_path):
    loaded = artifact_mod.load(_write_artifact(tmp_path))
    assert loaded.resolvable_alpha(1e-3)      # 10000 replicates
    assert not loaded.resolvable_alpha(1e-6)


def test_artifact_signature_domain_differs_from_bundles(tmp_path):
    """An artifact signature must never be replayable as a bundle signature."""
    from app.bundles.format import signed_payload as bundle_payload
    import hashlib
    raw = b'{"x":1}'
    digest = hashlib.sha256(b"x").digest()
    assert artifact_mod.signed_payload(raw, [digest, digest, digest]) != \
        bundle_payload(raw, digest, digest)
