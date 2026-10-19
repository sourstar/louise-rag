"""tests/test_deploy_add_bundles_mount.py — deploy/16f/add_bundles_mount.py:
`/bundles` comes in once, read-write in the worker and read-only in the web,
nothing else moves, and a second run changes nothing (operator, 2026-10-19:
bundles rest on tank)."""
import importlib.util
import sys
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "deploy" / "16f"
sys.path.insert(0, str(D))
spec = importlib.util.spec_from_file_location("add_bundles_mount", D / "add_bundles_mount.py")
abm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(abm)


def test_bundles_mount_comes_in_once_ro_in_web():
    vols = ["/mnt/ssd4/apps/louise-rag/data:/data", "/mnt/tank/apps/louise-shard:/shards"]
    c = {"services": {"web": {"volumes": list(vols), "environment": {"SHARDS_ROOT": "/shards"}},
                      "worker": {"volumes": list(vols), "environment": {}},
                      "gpu-probe": {"image": "x"}}}
    said = abm.fm.apply(c, shared=abm.BUNDLES, env={})
    assert c["services"]["web"]["volumes"] == vols + ["/mnt/tank/apps/louise-bundles:/bundles:ro"]
    assert c["services"]["worker"]["volumes"] == vols + ["/mnt/tank/apps/louise-bundles:/bundles"]
    assert c["services"]["web"]["environment"] == {"SHARDS_ROOT": "/shards"}, "no variable is touched"
    assert len(said) == 2
    assert abm.fm.apply(c, shared=abm.BUNDLES, env={}) == [], "a second run changes nothing"
