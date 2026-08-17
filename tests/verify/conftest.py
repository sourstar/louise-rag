"""Shared fixtures for bundle tests. Helpers live in helpers.py so test
modules can import them absolutely (there are no __init__.py files under
tests/, matching tests/acquire/'s layout).
"""
import pytest
from nacl import signing

from app import db, spaces
from tests.bundles.helpers import DIM, make_collection

PRODUCER_SEED = bytes(range(32))
OTHER_SEED = bytes(range(32, 64))


@pytest.fixture(autouse=True)
def confirmed_space(monkeypatch):
    """Pin a CONFIRMED (non-weak) embed-model hash for every bundle test.

    Without this the model hash falls back to hashing the model NAME, which
    importer._check_space refuses outright — correctly, since two nodes running
    different weights behind one Ollama tag produce an identical weak hash. The
    test suite has no embed endpoint, so every import would otherwise refuse
    before reaching the behavior under test. The refusal itself is covered by
    test_weak_model_hash_is_refused_but_retryable, which patches this back.
    """
    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda: ("c0ffee00c0ffee00", False))


@pytest.fixture
def producer_key():
    return signing.SigningKey(PRODUCER_SEED)


@pytest.fixture
def other_key():
    return signing.SigningKey(OTHER_SEED)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "corpus")
    return make_collection(tmp_path)


@pytest.fixture
def con(collection):
    connection = db.connect(collection)
    # The pinned space_id must equal what compute_space_id() derives locally,
    # or export and import disagree about which space this collection is in.
    space_id = spaces.compute_space_id(spaces.gather_components())
    connection.execute(
        "INSERT OR REPLACE INTO space(id,space_id,dim,components,pinned_at) "
        "VALUES(1,?,?,?,0)", (space_id, DIM, "{}"))
    return connection
