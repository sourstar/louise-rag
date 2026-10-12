"""tests/test_deploy_flatten_mounts.py — deploy/16f/flatten_mounts.py: every
per-collection origin/shard mount leaves (nested ones too), the shared roots
and the custom origin come in once, the two variables are set, and a second
run changes nothing (operator, 2026-10-12: one mount per kind)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "flatten_mounts", Path(__file__).resolve().parent.parent / "deploy" / "16f" / "flatten_mounts.py")
fm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fm)


def cfg():
    per = ["/mnt/tank/louise-corpora/pubmed:/data/collections/pubmed/origin",
           "/mnt/tank/apps/louise-shard/pubmed:/data/collections/pubmed/shard",
           "/mnt/tank/louise-corpora/wikivec/data:/data/collections/wikipedia/origin/data:ro",
           "/mnt/ssd4/apps/louise-rag/data:/data", "/mnt/tank/apps/louise-exports:/exports"]
    return {"services": {"web": {"volumes": list(per), "environment": {"ADMIN_TOKEN_FILE": "/x"}},
                         "worker": {"volumes": list(per) + [
                             {"type": "bind", "source": "/mnt/tank/apps/louise-shard/gutenberg",
                              "target": "/data/collections/gutenberg/shard"}]},
                         "gpu-probe": {"image": "x"}}}


def test_per_collection_mounts_leave_and_the_shared_roots_come_in():
    c = cfg()
    said = fm.apply(c)
    web, worker = c["services"]["web"], c["services"]["worker"]
    for svc in (web, worker):
        targets = [fm._parts(v)[1] for v in svc["volumes"]]
        assert not any(t.startswith("/data/collections/") for t in targets)
        assert {"/data", "/exports", "/corpora", "/shards", "/corpora-custom/gutenberg"} <= set(targets)
        assert svc["environment"]["SHARDS_ROOT"] == "/shards"
        assert svc["environment"]["ORIGINS_ROOT"] == "/corpora"
    assert "/mnt/tank/louise-corpora:/corpora:ro" in web["volumes"], "web reads origins only"
    assert "/mnt/tank/louise-corpora:/corpora" in worker["volumes"]
    assert web["environment"]["ADMIN_TOKEN_FILE"] == "/x", "nothing else is touched"
    assert "gpu-probe: " not in " ".join(said)
    assert fm.apply(c) == [], "a second run changes nothing"
