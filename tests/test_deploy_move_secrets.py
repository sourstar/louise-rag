"""tests/test_deploy_move_secrets.py — deploy/16f/move_secrets.py's config
change: ADMIN_TOKEN leaves both services, the web keeps the public half only,
the worker alone mounts the private key, every secret is named by path
(invariant 8), and a second run changes nothing."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "move_secrets", Path(__file__).resolve().parent.parent / "deploy" / "16f" / "move_secrets.py")
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)

SENTINEL = "sentinel-token-not-for-output"


def cfg():
    env = {"ADMIN_TOKEN": SENTINEL, "NODE_KEY_PATH": "/config/node.key", "DATA_DIR": "/data"}
    vols = ["/mnt/ssd4/apps/louise-rag/data:/data", "/mnt/ssd4/apps/louise-rag/config:/config"]
    return {"services": {"web": {"environment": dict(env), "volumes": list(vols)},
                         "worker": {"environment": dict(env), "volumes": list(vols)},
                         "gpu-probe": {"image": "x"}}}


def test_secrets_by_path_and_the_private_key_in_the_worker_alone():
    c = cfg()
    said = ms.edit(c)
    web, worker = c["services"]["web"], c["services"]["worker"]
    for svc in (web, worker):
        assert "ADMIN_TOKEN" not in svc["environment"]
        assert svc["environment"]["ADMIN_TOKEN_FILE"] == "/run/louise/admin/admin-token"
        assert svc["environment"]["NODE_PUBLIC_KEY_PATH"] == "/run/louise/public/node.pub"
        assert svc["environment"]["DATA_DIR"] == "/data"
    assert "NODE_KEY_PATH" not in web["environment"], "the web never holds the seed"
    assert worker["environment"]["NODE_KEY_PATH"] == "/run/louise/private/node.key"
    assert not any("private" in v or v.endswith("/secrets:/run/louise:ro") for v in web["volumes"])
    assert all(v.endswith(":ro") for v in web["volumes"] if "/secrets" in v)
    assert "/mnt/ssd4/apps/louise-rag/secrets:/run/louise:ro" in worker["volumes"]
    assert not any(SENTINEL in line for line in said), "a change line never carries the token"
    assert c["services"]["gpu-probe"] == {"image": "x"}


def test_a_second_run_changes_nothing():
    c = cfg()
    ms.edit(c)
    assert ms.edit(c) == []
