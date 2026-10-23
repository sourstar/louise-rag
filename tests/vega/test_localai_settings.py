"""deploy/vega/stage/localai_settings.py: the WatchDog threshold the operator
set on 2026-10-23 is in the repo, and applying it changes nothing else."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "localai_settings", ROOT / "deploy" / "vega" / "stage" / "localai_settings.py")
settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(settings)


def test_the_reclaimer_threshold_is_the_operators():
    assert settings.DESIRED == {"memory_reclaimer_enabled": True, "memory_reclaimer_threshold": 0.85}


def test_only_the_managed_keys_change():
    current = {"memory_reclaimer_threshold": 0.68, "memory_reclaimer_enabled": True,
               "api_keys": ["k"], "p2p_token": "t", "watchdog_busy_timeout": "10m0s"}
    out = settings.merged(current)
    assert out["memory_reclaimer_threshold"] == 0.85
    assert {k: v for k, v in out.items() if k not in settings.DESIRED} == \
        {k: v for k, v in current.items() if k not in settings.DESIRED}
    assert current["memory_reclaimer_threshold"] == 0.68, "the input is not modified"


#: the encoders processing pins for its duration and restores after (app/model_pins.py;
#: operator, 2026-10-23: "reading the pinning status, in case pinning a model before
#: processing, then restoring the previous pinning status after"). Their resting state is
#: UNPINNED: a static pin here would keep the card from other models while nothing runs,
#: and the scoped pin would find it pinned and never release it.
SCOPED = ("vega-qwen3-embedding-4b", "vega-dar-legal-it", "vega-medcpt-article-ov", "vega-bge-m3", "vega-granite",
          "louise-rerank-medcpt")   # the operator, 2026-10-23: "Remove the reranker pin"


def test_the_scoped_pin_encoders_rest_unpinned():
    import json
    for name in SCOPED:
        text = (ROOT / "deploy" / "vega" / "stage" / "models" / f"{name}.yaml").read_text()
        body = json.loads("\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))
        assert body.get("pinned") is False, f"{name} is staged pinned"


def test_the_lamar_candidate_is_the_bge_reranker_s_strict_configuration():
    """LAMAR-600m has bge-reranker-v2-m3's architecture (reranker-scope §14):
    the candidate runs under the deployed reranker's bit-exact settings
    (invariant 12: GGML_SYCL_ENABLE_DNN=0, parallel:1, cache_ram:0, flash
    attention off), differing only in its name and its weights."""
    import json
    import re
    from pathlib import Path
    d = Path(__file__).resolve().parents[2] / "deploy" / "vega" / "stage" / "models"

    def load(name):
        text = (d / name).read_text(encoding="utf-8")
        return json.loads(re.sub(r"(?m)^#.*$", "", text))
    bge, lamar = load("louise-rerank-bge.yaml"), load("louise-rerank-lamar.yaml")
    assert lamar["name"] == "louise-rerank-lamar"
    assert lamar["parameters"]["model"] == "weights/lamar/LAMAR-600m-f16.gguf"
    assert lamar.get("pinned") is False
    for body in (bge, lamar):
        body.pop("name"); body["parameters"].pop("model"); body.pop("pinned", None)
    assert lamar == bge
