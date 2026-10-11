"""tests/test_deploy_rename_mounts.py — deploy/16f/rename_mounts.py: the
SHARD directory's mounts move host prefix and container segment together,
in both volume shapes, in every service; nothing else is touched, and a
careless prefix is refused."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "rename_mounts", Path(__file__).resolve().parent.parent / "deploy" / "16f" / "rename_mounts.py")
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

OLD, NEW = "/mnt/tank/apps/louise-drag/", "/mnt/tank/apps/louise-shard/"


def cfg():
    return {"services": {
        "web": {"volumes": [f"{OLD}gutenberg:/data/collections/gutenberg/drag",
                            f"{OLD}pubmed:/data/collections/pubmed/drag:ro",
                            "/mnt/ssd4/apps/louise-rag/data:/data"]},
        "worker": {"volumes": [{"type": "bind", "source": f"{OLD}wikipedia",
                                "target": "/data/collections/wikipedia/drag"},
                               "/mnt/tank/media/public/books/project_gutenberg:/data/collections/gutenberg/origin"]},
        "gpu-probe": {"image": "x"}}}


def test_the_shard_mounts_move_and_nothing_else_does():
    c = cfg()
    changed = rm.apply(c, OLD, NEW, "drag", "shard")
    assert len(changed) == 3
    web, worker = c["services"]["web"]["volumes"], c["services"]["worker"]["volumes"]
    assert web[0] == f"{NEW}gutenberg:/data/collections/gutenberg/shard"
    assert web[1] == f"{NEW}pubmed:/data/collections/pubmed/shard:ro", "the mode is kept"
    assert web[2] == "/mnt/ssd4/apps/louise-rag/data:/data"
    assert worker[0] == {"type": "bind", "source": f"{NEW}wikipedia",
                         "target": "/data/collections/wikipedia/shard"}
    assert worker[1].endswith("/gutenberg/origin")
    assert rm.apply(c, OLD, NEW, "drag", "shard") == [], "again: nothing matches"


@pytest.mark.parametrize("args", [("/mnt/tank", NEW, "drag", "shard"),
                                  (OLD, NEW, "a/b", "shard"),
                                  (OLD, "/tmp/x/y/z", "drag", "shard")])
def test_a_careless_argument_is_refused(args):
    with pytest.raises(ValueError):
        rm.apply(cfg(), *args)
