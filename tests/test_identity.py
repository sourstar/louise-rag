"""This node's identity (app/identity.py): the web process recognises what
this node signed from the PUBLIC key file alone, and never reads the seed
(operator, 2026-09-29; docs/notes/rage.md §13, layer 2)."""
import os

import pytest
from nacl import signing

from app import config, copy_state, identity, run_check
from app.bundles import keys as node_keys
from app.hold import envelope as env

KEY = signing.SigningKey(b"\x01" * 32)
OTHER = signing.SigningKey(b"\x02" * 32)
MINE = env.key_text(KEY.verify_key)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(config, "NODE_KEY_PATH", None)
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", None)
    copy_state.reset_cache()
    run_check.reset_cache()
    yield
    copy_state.reset_cache()
    run_check.reset_cache()


@pytest.fixture
def private(tmp_path, monkeypatch):
    p = tmp_path / "private" / "node.key"
    p.parent.mkdir()
    p.write_bytes(bytes(KEY))
    os.chmod(p, 0o600)
    monkeypatch.setattr(config, "NODE_KEY_PATH", p)
    return p


@pytest.fixture
def public(tmp_path, monkeypatch):
    p = tmp_path / "public" / "node.pub"
    identity.write_public(p, KEY)
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", p)
    return p


@pytest.fixture
def no_seed(monkeypatch):
    """Any read of the private key fails the test."""
    def refuse(*a, **k):
        raise AssertionError("the private key was loaded")
    monkeypatch.setattr(node_keys, "load_signing_key", refuse)


def test_the_public_file_is_the_textual_form(tmp_path):
    p = tmp_path / "node.pub"
    assert identity.write_public(p, KEY) == MINE
    assert p.read_text() == MINE + "\n"
    assert identity.read_public(p) == MINE
    assert identity.write_public(p, KEY) == MINE, "the same key again is nothing"
    with pytest.raises(identity.IdentityError, match="another key"):
        identity.write_public(p, OTHER)


@pytest.mark.parametrize("content", [b"", b"ed25519:zz", b"deadbeef" * 8,
                                     (MINE.upper()).encode(), (MINE + "00").encode(),
                                     MINE.encode() + b"\n" * 40])
def test_a_malformed_public_file_is_refused(tmp_path, content):
    p = tmp_path / "node.pub"
    p.write_bytes(content)
    with pytest.raises(identity.IdentityError, match="64 hex"):
        identity.read_public(p)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_public_file_others_can_write_is_refused(tmp_path):
    """Whoever can replace it decides which signatures this node calls its
    own."""
    p = tmp_path / "node.pub"
    identity.write_public(p, KEY)
    os.chmod(p, 0o664)
    with pytest.raises(identity.IdentityError, match="writable"):
        identity.read_public(p)


def test_the_web_process_recognises_itself_without_the_seed(public, no_seed):
    assert identity.own() == MINE
    assert copy_state.own_key() == MINE
    assert run_check.signer() == MINE


def test_the_worker_takes_the_private_keys_half_and_checks_the_file(private, public):
    assert identity.own() == MINE
    assert identity.check_at_start() == MINE


def test_a_public_file_naming_another_key_refuses_the_worker(private, tmp_path,
                                                            monkeypatch):
    p = tmp_path / "other.pub"
    identity.write_public(p, OTHER)
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", p)
    with pytest.raises(identity.IdentityError, match="not the public half"):
        identity.check_at_start()
    from app import worker
    ran = []
    monkeypatch.setattr(worker, "run_forever", lambda: ran.append(1))
    assert worker.main() == 2 and ran == [], "refused to start, with the reason"


def test_a_missing_public_file_refuses_the_worker(private, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", tmp_path / "absent.pub")
    with pytest.raises(identity.IdentityError, match="no public key"):
        identity.check_at_start()


def test_no_key_at_all_is_a_node_that_cannot_sign():
    """07 §7.8: checks and searches all the same."""
    assert identity.check_at_start() is None
    assert copy_state.own_key() is None and run_check.signer() is None


def test_no_key_is_not_remembered(public, monkeypatch):
    """A key put in place after a first look is found at the next one."""
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", public.parent / "late.pub")
    assert copy_state.own_key() is None
    identity.write_public(public.parent / "late.pub", KEY)
    assert copy_state.own_key() == MINE


def test_key_create_writes_the_public_half_and_never_prints_the_seed(tmp_path,
                                                                    monkeypatch, capsys):
    """`louise hold key create` (was `bundle keygen --public`): the key at
    NODE_KEY_PATH, its public half at NODE_PUBLIC_KEY_PATH, and on stdout the
    PUBLIC key — the producer identity peers see — never the seed
    (invariant 8)."""
    from app import louise
    key = tmp_path / "private" / "node.key"
    pub = tmp_path / "public" / "node.pub"
    monkeypatch.setattr(config, "NODE_KEY_PATH", key)
    monkeypatch.setattr(config, "NODE_PUBLIC_KEY_PATH", pub)
    assert louise.main(["hold", "key", "create"]) == 0
    assert identity.read_public(pub) == identity.public_text(node_keys.load_signing_key())
    out = capsys.readouterr().out
    assert key.read_bytes().hex() not in out, "the seed is never printed"
    assert identity.read_public(pub) in out, "the public key is"


def test_the_web_routes_that_sign_queue_and_never_read_the_seed(
        isolated_config, public, no_seed, monkeypatch):
    """The two Library actions that sign are queued for the worker (app/jobs.py)."""
    from fastapi.testclient import TestClient
    from app.api import app
    from app.bundles import export as bundle_export
    from tests.hold.test_template import recipe
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "ab" * 32)
    monkeypatch.setattr(bundle_export, "export_bundle",
                        lambda *a, **k: pytest.fail("exported in the web process"))
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        r = c.post("/api/hold/collection/create",
                   json={"plugin": "gutenberg", "recipe": recipe(),
                         "encoder": "http://e/upstream/m"})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        r = c.post("/admin/library/gutenberg/bundle/export", json={"uids": ["gutenberg:1"]})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        assert c.get("/admin/library").status_code == 200
