"""tests/hold/test_node_key.py — `hold key create|trust|show` (07 §7.3):
a key made once at a path and never overwritten, its public half beside
it; the trust list's allow, deny, remove and the lifting of a burn."""
import os

import pytest

from app import identity
from app.bundles import keys as node_keys
from app.hold import node_key, trust_file

K1 = "ed25519:" + "a" * 64
K2 = "ed25519:" + "b" * 64


def test_create_once_never_overwritten_public_half_beside(tmp_path, monkeypatch):
    p = tmp_path / "keys" / "node.key"
    pub = tmp_path / "keys" / "node.pub"
    text = node_key.create(p, public_path=pub)
    assert text.startswith("ed25519:") and len(text) == 72
    assert identity.public_text(node_keys.load_signing_key(p)) == text, "the file loads as the key it names"
    assert identity.read_public(pub) == text
    if os.name == "posix":
        assert (p.stat().st_mode & 0o777) == 0o600
    with pytest.raises(node_key.KeyError_, match="never overwritten"):
        node_key.create(p)
    assert identity.public_text(node_keys.load_signing_key(p)) == text
    from app import config
    monkeypatch.setattr(config, "NODE_KEY_PATH", None)
    with pytest.raises(node_key.KeyError_, match="NODE_KEY_PATH is not set"):
        node_key.create()


def test_trust_allow_deny_remove_and_a_burn_lifted(tmp_path, monkeypatch):
    root = tmp_path / "louise"
    monkeypatch.setattr(identity, "own", lambda: K2)
    assert node_key.trust(K1, root=root) == {"key": K1, "state": "allow"}
    assert node_key.trust(K1, deny=True, root=root)["state"] == "deny"
    assert trust_file.read(root)["allow"] == [] and trust_file.read(root)["deny"] == [K1]
    assert node_key.trust(K1, remove=True, root=root)["state"] == "none"
    trust_file.burn(K1, "sha256:" + "f" * 64, root=root, now=1_800_000_000.0)
    shown = node_key.show(root)
    assert shown["key"] == K2 and shown["trust"][K1]["state"] == "burned" and shown["path"].endswith("trust.json")
    assert node_key.trust(K1, restore=True, root=root) == {"key": K1, "state": "none", "restored": True}
    assert node_key.trust(K1, restore=True, root=root)["restored"] is False
    with pytest.raises(node_key.KeyError_, match="64 hex"):
        node_key.trust("nope", root=root)
    with pytest.raises(node_key.KeyError_, match="one of"):
        node_key.trust(K1, deny=True, remove=True, root=root)
    monkeypatch.setattr(identity, "own", lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    assert node_key.show(root)["key"] is None and "no key" in node_key.show(root)["why"]


def test_the_verbs_are_in_the_tree():
    from app import louise
    p = louise.build_parser()
    a = p.parse_args(["hold", "key", "trust", K1, "--deny"])
    assert a.fn is louise.cmd_key_trust and a.key == K1 and a.deny and not a.restore
    assert p.parse_args(["hold", "key", "create"]).fn is louise.cmd_key_create
    assert p.parse_args(["hold", "key", "show"]).fn is louise.cmd_key_show
    assert p.parse_args(["hold", "key", "anchor", "pubmed"]).fn is louise.cmd_key_anchor
    a = p.parse_args(["hold", "collection", "certify", "pubmed", "2026-08"])
    assert a.fn is louise.cmd_collection_certify and a.snapshot == "2026-08"
    with pytest.raises(SystemExit):
        p.parse_args(["hold", "key", "trust", K1, "--deny", "--restore"])
