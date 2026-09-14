"""04 §4.4: "an acknowledgement is of a text, identified by its hash … A
collection whose Codex names a `terms_sha256` this peer has already
acknowledged … is acknowledged by that record."

The gate compared a collection's OWN record only, so wikipedia's new Codex
— pinning the CC BY-SA text the operator acknowledged on 2026-08-17 for the
legacy wikipedia — read as unacknowledged (found 2026-09-14).
"""
import json
from types import SimpleNamespace

import pytest

from app import licensing

H = "3a10093a1d5ea1f78419ae5f953795e6987e72701f33fc9e041e0567cb714f22"


def _codex_collection(terms: str, ack=None):
    return SimpleNamespace(
        name="wikipedia", plugin="wikipedia", license_ack=ack,
        codex=SimpleNamespace(license=SimpleNamespace(
            cls="yellow", terms_sha256=f"sha256:{terms}")))


@pytest.fixture
def peer(tmp_path, monkeypatch):
    root, cfg = tmp_path / "root", tmp_path / "config"
    (cfg / "collections").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    licensing.reset_peer_acks()
    yield root, cfg
    licensing.reset_peer_acks()


def test_another_collections_record_of_the_same_text_acknowledges_it(peer):
    root, _ = peer
    d = root / "collections" / "sister" / "state"
    d.mkdir(parents=True)
    (d / "ack.json").write_text(json.dumps(
        {"time": "2026-09-10T20:31:08Z", "terms_sha256": f"sha256:{H}"}))
    assert licensing.state_for(_codex_collection(H)) == licensing.ACKNOWLEDGED


def test_a_parked_legacy_tomls_record_still_counts(peer):
    _, cfg = peer
    legacy = cfg / "collections-legacy"
    legacy.mkdir()
    (legacy / "wikipedia.toml").write_text(
        f'plugin = "wikipedia"\nlicense_ack = "2026-08-17T18:47:45Z/{H}"\n')
    assert licensing.state_for(_codex_collection(H)) == licensing.ACKNOWLEDGED


def test_a_record_of_another_text_does_not(peer):
    root, cfg = peer
    (cfg / "collections" / "pubmed.toml").write_text(
        f'license_ack = "2026-08-17T18:47:45Z/{"0" * 64}"\n')
    d = root / "collections" / "pubmed" / "state"
    d.mkdir(parents=True)
    (d / "ack.json").write_text(json.dumps(
        {"time": "2026-09-10T20:31:08Z", "terms_sha256": "sha256:" + "1" * 64}))
    assert licensing.state_for(_codex_collection(H)) == licensing.MISSING


def test_the_collections_own_record_still_suffices(peer):
    assert licensing.state_for(
        _codex_collection(H, ack=f"2026-09-14T19:00:00Z/{H}")) == licensing.ACKNOWLEDGED
