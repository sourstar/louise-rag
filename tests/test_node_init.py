"""tests/test_node_init.py — a node's first boot on any Docker host
(app/node_init.py; deploy/docker/compose.yaml): what is missing is made
once, nothing is overwritten, no secret is printed, and the platform file
it seeds is one the platform loads."""
import os
import stat

import pytest
from nacl import signing

from app import identity, node_init, platform_config


def env_for(tmp_path, **extra):
    env = {"NODE_KEY_PATH": str(tmp_path / "private/node.key"),
           "NODE_PUBLIC_KEY_PATH": str(tmp_path / "public/node.pub"),
           "ADMIN_TOKEN_FILE": str(tmp_path / "token/admin.token"),
           "CONFIG_DIR": str(tmp_path / "config"),
           "DATA_DIR": str(tmp_path / "data")}
    env.update(extra)
    return env


def test_first_boot_makes_everything_and_prints_no_secret(tmp_path, monkeypatch):
    env = env_for(tmp_path, LOUISE_NODE_ADDRESS="http://192.0.2.7:8742", LOUISE_PEER_PORT="6881")
    lines = []
    node_init.run(env, say=lines.append)
    key = (tmp_path / "private/node.key").read_bytes()
    token = (tmp_path / "token/admin.token").read_text().strip()
    assert len(key) == 32 and len(token) == 64
    pub = identity.read_public(tmp_path / "public/node.pub")
    assert pub == identity.public_text(signing.SigningKey(key))
    out = "\n".join(lines)
    assert token not in out and key.hex() not in out, "a path is printed, never a secret"
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "token/admin.token").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "private/node.key").stat().st_mode) == 0o600
    assert (tmp_path / "config/collections").is_dir() and (tmp_path / "data/collections").is_dir()
    monkeypatch.setenv("CONFIG_DIR", env["CONFIG_DIR"])
    p = platform_config.load()
    assert (p.peers.address, p.peers.peer_port, p.peers.addresses, p.peers.dht) == \
        ("http://192.0.2.7:8742", 6881, (), True), "the seeded file is one the platform loads"


def test_a_second_boot_changes_nothing(tmp_path):
    env = env_for(tmp_path)
    node_init.run(env, say=lambda s: None)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    lines = []
    node_init.run(dict(env, LOUISE_NODE_ADDRESS="http://elsewhere:1"), say=lines.append)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert all(" kept " in line for line in lines), lines
    assert "address =" not in (tmp_path / "config/platform.toml").read_text().replace("# address", ""), \
        "the environment seeds the file once and never rewrites it"


def test_a_missing_public_half_is_written_from_the_existing_key_never_a_new_key(tmp_path):
    env = env_for(tmp_path)
    node_init.run(env, say=lambda s: None)
    key = (tmp_path / "private/node.key").read_bytes()
    (tmp_path / "public/node.pub").unlink()
    node_init.run(env, say=lambda s: None)
    assert (tmp_path / "private/node.key").read_bytes() == key
    assert identity.read_public(tmp_path / "public/node.pub") == identity.public_text(signing.SigningKey(key))


def test_a_public_half_naming_another_key_is_refused(tmp_path):
    env = env_for(tmp_path)
    node_init.run(env, say=lambda s: None)
    (tmp_path / "public/node.pub").unlink()
    identity.write_public(tmp_path / "public/node.pub", signing.SigningKey(bytes([1]) * 32))
    with pytest.raises(identity.IdentityError, match="another key"):
        node_init.run(env, say=lambda s: None)


def test_first_boot_in_a_fresh_process_with_the_token_path_set_but_missing(tmp_path):
    """The first boot failed live (2026-10-07): app/config.py reads
    ADMIN_TOKEN_FILE at import and refuses a set-but-missing path, and the
    in-process tests above import config long before. A fresh interpreter,
    as the `init` service runs it."""
    import subprocess
    import sys
    env = dict(os.environ, **env_for(tmp_path), PYTHONPATH=os.getcwd())
    r = subprocess.run([sys.executable, "-m", "app.node_init"], env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "token/admin.token").exists() and (tmp_path / "private/node.key").exists()
