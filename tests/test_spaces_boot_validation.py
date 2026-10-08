"""Boot validation of a Codex collection: pinned space_id/dim mismatches must refuse to boot
(CLAUDE.md invariant 1) rather than warn or auto-migrate. The embed-model hash
resolution is monkeypatched throughout so these tests never depend on a live
embed endpoint.
"""
import pytest

from app import collections as collections_mod, db, spaces


def _patch_hash(monkeypatch, value="abc123", weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: (value, weak))


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


def test_a_table_pinned_to_another_space_refuses_to_boot(isolated_config, monkeypatch):
    """The Codex collection's pin is `codex.space_id` (09 §9.3): a vector
    table pinned to any other space is refused, never re-pinned."""
    _patch_hash(monkeypatch)
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    con = db.connect(collection)
    con.execute("UPDATE space SET space_id='sha256:' || hex(randomblob(32)) WHERE id=1")
    con.close()
    with pytest.raises(spaces.BootValidationError, match="its Codex says"):
        spaces.validate_boot([collection])


@pytest.mark.parametrize("weak", [False, True])
def test_the_endpoint_model_hash_plays_no_part(isolated_config, monkeypatch, weak):
    """A Codex names its encoder BY DIGEST: what an endpoint reports about
    its weights, confirmed or not, neither refuses nor softens the boot."""
    _patch_hash(monkeypatch, value="model-a")
    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])

    _patch_hash(monkeypatch, value="model-b", weak=weak)
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
