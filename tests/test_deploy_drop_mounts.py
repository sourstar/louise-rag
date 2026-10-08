"""tests/test_deploy_drop_mounts.py — deploy/16f/drop_mounts.py: the bind
mounts under a prefix leave every service, both volume shapes, nothing
else is touched, and a careless prefix is refused."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "drop_mounts", Path(__file__).resolve().parent.parent / "deploy" / "16f" / "drop_mounts.py")
dm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)

G = "/mnt/tank/apps/louise_collections_origin/genome"


def cfg():
    return {"services": {
        "web": {"volumes": [f"{G}-hg38:/corpora/genome-hg38:ro", "/mnt/ssd4/apps/louise-rag/data:/data"]},
        "worker": {"volumes": [{"type": "bind", "source": f"{G}-dm6", "target": "/corpora/genome-dm6"},
                               "/mnt/tank/louise-corpora/pubmed:/data/collections/pubmed/origin"]},
        "gpu-probe": {"image": "x"}}}


def test_the_mounts_under_the_prefix_leave_and_nothing_else_does():
    c = cfg()
    removed = dm.apply(c, G)
    assert removed == [f"web: {G}-hg38 -> /corpora/genome-hg38", f"worker: {G}-dm6 -> /corpora/genome-dm6"]
    assert c["services"]["web"]["volumes"] == ["/mnt/ssd4/apps/louise-rag/data:/data"]
    assert c["services"]["worker"]["volumes"] == ["/mnt/tank/louise-corpora/pubmed:/data/collections/pubmed/origin"]
    assert dm.apply(c, G) == [], "idempotent"


def test_a_careless_prefix_is_refused():
    for bad in ("/", "/mnt", "/mnt/tank", "/data/x"):
        with pytest.raises(ValueError):
            dm.apply(cfg(), bad)
    with pytest.raises(ValueError, match="services"):
        dm.apply({}, G)


def test_a_target_drops_one_mount_of_a_shared_prefix():
    w = "/mnt/tank/louise-corpora/wikivec"
    c = {"services": {"worker": {"volumes": [f"{w}:/corpora/wikivec", f"{w}/data:/data/collections/wikipedia/origin/data"]}}}
    assert dm.apply(c, w, "/corpora/wikivec") == [f"worker: {w} -> /corpora/wikivec"]
    assert c["services"]["worker"]["volumes"] == [f"{w}/data:/data/collections/wikipedia/origin/data"], \
        "wikipedia's mount under the same prefix stays"
