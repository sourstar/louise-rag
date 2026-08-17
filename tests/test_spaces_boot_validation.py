"""Boot validation: pinned space_id/dim mismatches must refuse to boot
(CLAUDE.md invariant 1) rather than warn or auto-migrate. The embed-model hash
resolution is monkeypatched throughout so these tests never depend on a live
embed endpoint.
"""
import pytest

from app import collections as collections_mod, config, db, spaces


def _patch_hash(monkeypatch, value="abc123", weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: (value, weak))


def test_first_boot_pins(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])  # must not raise

    con = db.connect(collection)
    row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    assert row is not None and row["space_id"]


def test_unchanged_config_boots_silently(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])
    spaces.validate_boot([collection])  # second boot, identical config: no raise


def test_confirmed_model_hash_mismatch_refuses_to_boot(isolated_config, monkeypatch):
    _patch_hash(monkeypatch, value="model-a")
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    _patch_hash(monkeypatch, value="model-b")  # confirmed (non-weak) change
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])


def test_weak_hash_mismatch_does_not_refuse(isolated_config, monkeypatch):
    _patch_hash(monkeypatch, value="model-a")
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    _patch_hash(monkeypatch, value="model-b", weak=True)
    spaces.validate_boot([collection])  # unconfirmable: warn, don't refuse


def test_chunk_version_change_refuses_to_boot(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    monkeypatch.setattr(config, "CHUNK_VERSION", config.CHUNK_VERSION + 1)
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])


def test_config_change_refuses_even_with_weak_hash(isolated_config, monkeypatch):
    """A weak (unconfirmed) embed-model hash must never mask a real,
    fully-local config mismatch — e.g. an unreachable embed endpoint must not
    let a CHUNK_VERSION bump silently through."""
    _patch_hash(monkeypatch, weak=True)  # simulates an unreachable embed endpoint
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    monkeypatch.setattr(config, "CHUNK_VERSION", config.CHUNK_VERSION + 1)
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])


def test_dim_mismatch_refuses_to_boot(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])  # pins with dim=None (no Lance table yet)

    monkeypatch.setattr(spaces, "known_lance_dim", lambda lance_dir: 768)
    spaces.validate_boot([collection])  # dim observed for the first time: adopted

    monkeypatch.setattr(spaces, "known_lance_dim", lambda lance_dir: 512)
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])
