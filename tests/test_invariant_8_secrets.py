"""Invariant 8 (secrets), by behaviour: SENTINEL secrets — an admin token
and a deposit token in files at ADMIN_TOKEN_FILE / DEPOSIT_TOKEN_FILE, a
node key at NODE_KEY_PATH — never appear in what the node writes out: its
log, its stderr, its stdout, its HTTP answers. Over the paths where a
leak would happen if it happened anywhere:

- STARTUP, in a fresh process, with the REAL config loading (app/config.py
  reads the files at import; nothing is monkeypatched in): the web app's
  lifespan, the worker's identity check, `hold key show`, `hold config
  show`;
- a BAD TOKEN on every way a token is presented (header, cookie, bearer,
  the two login forms) — including a near miss that STARTS with the real
  token, so an echo of the supplied value would carry the secret;
- a request that is ADMITTED, whose page (the System page shows the
  effective config) must say "(set)", never the value;
- an UNREADABLE or malformed KEY FILE: missing, group-readable, not a seed;
  each error names the path and nothing of the material, and its whole
  traceback (chained context included) carries none either;
- an UNREADABLE TOKEN FILE at startup, with the legacy ADMIN_TOKEN value
  also in the environment: the error names the path, never the value.

Everything runs in child processes, so the real import-time loading is
what is exercised and no test after this one inherits a node identity.
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_CHILD = r'''
import logging, os, sys, traceback
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)
from pathlib import Path
from app import config
token = Path(os.environ["ADMIN_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
assert config.ADMIN_TOKEN == token, "ADMIN_TOKEN was not loaded from ADMIN_TOKEN_FILE"
assert config.DEPOSIT_TOKEN, "DEPOSIT_TOKEN was not loaded from DEPOSIT_TOKEN_FILE"
from fastapi.testclient import TestClient
from app import api, identity, web
from app.bundles import keys
web._ping = lambda url: True                  # no model server here; the page still renders
print("worker start:", identity.check_at_start())
print("own:", identity.own())
with TestClient(api.app, raise_server_exceptions=False) as c:
    cases = [
        ("GET", "/admin/system", {}),
        ("GET", "/admin/system", {"headers": {"X-Admin-Token": "wrong"}}),
        ("GET", "/admin/system", {"headers": {"X-Admin-Token": token + "x"}}),
        ("GET", "/admin/system", {"headers": {"Authorization": "Bearer " + token + "x"}}),
        ("GET", "/admin/system", {"cookies": {"admin_token": token + "x"}}),
        ("POST", "/admin/login", {"json": {"token": token + "x"}}),
        ("POST", "/login", {"json": {"token": token + "x"}}),
        ("POST", "/admin/login", {"content": b"not json"}),
        ("GET", "/admin/system", {"headers": {"X-Admin-Token": token}}),     # admitted
    ]
    for method, path, kw in cases:
        r = c.request(method, path, **kw)
        c.cookies.clear()
        print("HTTP", method, path, r.status_code, dict(r.headers), r.text[:200000])
real = config.NODE_KEY_PATH
for label, path, mode in (("missing", real.with_name("absent.key"), None),
                          ("group-readable", real, 0o644),
                          ("not a seed", Path(os.environ["BAD_KEY"]), 0o600)):
    if mode is not None:
        os.chmod(path, mode)
    try:
        keys.load_signing_key(path)
        print("KEY", label, "loaded?!")
    except Exception as e:
        print("KEY", label, type(e).__name__, e)
        traceback.print_exc()
os.chmod(real, 0o600)
config.NODE_KEY_PATH = Path(os.environ["BAD_KEY"])
print("worker start, bad key:", identity.check_at_start())
try:
    identity.own()
except Exception:
    traceback.print_exc()
print("DONE")
'''


@pytest.fixture
def node(tmp_path):
    """A node's identity on disk, every secret a sentinel."""
    admin = f"adm-{secrets.token_hex(16)}"
    deposit = f"dep-{secrets.token_hex(16)}"
    seed = secrets.token_bytes(32)
    bad = f"SENTINEL-NOT-A-SEED-{secrets.token_hex(8)}"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "admin.token").write_text(admin + "\n", encoding="utf-8")
    (secret_dir / "deposit.token").write_text(deposit + "\n", encoding="utf-8")
    key = secret_dir / "node.key"
    key.write_text(seed.hex(), encoding="ascii")
    key.chmod(0o600)
    bad_key = secret_dir / "bad.key"
    bad_key.write_text(bad, encoding="ascii")
    bad_key.chmod(0o600)
    for d in ("data/collections", "config", "corpus"):
        (tmp_path / d).mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_TOKEN", "ADMIN_TOKEN_FILE", "DEPOSIT_TOKEN_FILE", "NODE_KEY_PATH",
                        "NODE_PUBLIC_KEY_PATH", "LOUISE_CACHE_DIR")}
    env.update(DATA_DIR=str(tmp_path / "data"), LOUISE_ROOT=str(tmp_path / "data"),
               CONFIG_DIR=str(tmp_path / "config"), CORPUS_DIR=str(tmp_path / "corpus"),
               ADMIN_TOKEN_FILE=str(secret_dir / "admin.token"),
               DEPOSIT_TOKEN_FILE=str(secret_dir / "deposit.token"),
               NODE_KEY_PATH=str(key), BAD_KEY=str(bad_key), PYTHONPATH=str(ROOT))
    sentinels = {"admin token": admin, "deposit token": deposit, "key seed (hex)": seed.hex(),
                 "key seed (repr)": repr(seed)[2:-1], "malformed key file's content": bad}
    return env, sentinels


def _run(argv, env, timeout=180):
    return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def _leaks(text: str, sentinels: dict[str, str]) -> list[str]:
    return [name for name, value in sentinels.items() if value in text]


def test_no_secret_reaches_any_output_over_startup_and_failure_paths(node):
    env, sentinels = node
    r = _run([sys.executable, "-c", _CHILD], env)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "DONE" in r.stdout, f"the child did not run through:\n{out[-4000:]}"
    # the paths ran, and were refused where they should be
    assert r.stdout.count("HTTP ") == 9
    assert sum(" 401 " in line for line in r.stdout.splitlines() if line.startswith("HTTP")) >= 7
    assert "HTTP GET /admin/system 200" in r.stdout, "the admitted request was not admitted"
    assert "own: ed25519:" in r.stdout, "the node key was not loaded"
    for label in ("missing", "group-readable", "not a seed"):
        assert f"KEY {label} NodeKeyError" in r.stdout, f"{label}: {out[-2000:]}"
    assert str(Path(env["NODE_KEY_PATH"])) in r.stdout, "a key error names the path"
    leaks = _leaks(out, sentinels)
    assert leaks == [], f"invariant 8: {leaks} written out by the node:\n{out[-4000:]}"


@pytest.mark.parametrize("argv", [["hold", "key", "show"], ["hold", "config", "show"]])
def test_the_cli_shows_no_secret(node, argv):
    env, sentinels = node
    for key in (env["NODE_KEY_PATH"], env["BAD_KEY"]):
        r = _run([sys.executable, "-m", "app.louise", *argv], {**env, "NODE_KEY_PATH": key})
        out = r.stdout + r.stderr
        assert r.returncode == 0, out[-2000:]
        assert _leaks(out, sentinels) == [], f"invariant 8: `louise {' '.join(argv)}` wrote a secret:\n{out}"
        if argv[1] == "key":
            assert ("ed25519:" in r.stdout) == (key == env["NODE_KEY_PATH"]), out[-2000:]


def test_an_unreadable_token_file_at_startup_names_the_path_not_any_value(node, tmp_path):
    env, sentinels = node
    legacy = sentinels["admin token"]
    for var in ("ADMIN_TOKEN_FILE", "DEPOSIT_TOKEN_FILE"):
        unreadable = tmp_path / f"{var.lower()}.d"          # a directory: read_text fails
        unreadable.mkdir()
        r = _run([sys.executable, "-c", "import app.api"],
                 {**env, var: str(unreadable), "ADMIN_TOKEN": legacy})
        assert r.returncode != 0, f"{var} unreadable must stop the start"
        assert str(unreadable) in r.stderr, r.stderr[-2000:]
        assert _leaks(r.stdout + r.stderr, sentinels) == [], \
            f"invariant 8: the startup error of {var} carried a secret:\n{r.stderr[-3000:]}"
