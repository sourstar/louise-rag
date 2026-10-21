"""deploy/localai-nvidia (2026-10-21): every model YAML holds the
determinism contract VeGa's wrapper requires of a run on an NVIDIA card
(CLAUDE.md invariant 12) — checked by the wrapper's own function, so a YAML
that would be refused at its first load fails here first."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.vega.conftest import load_wrapper

vw = load_wrapper()
ROOT = Path(__file__).resolve().parents[1]
MODELS = sorted((ROOT / "deploy" / "localai-nvidia" / "models").glob("*.yaml"))
#: the real backend behind each YAML's `backend` (README.md, steps 2, 4, 5)
REAL = {"vega-llama-cpp": "cuda13-llama-cpp", "cuda13-llama-cpp": "cuda13-llama-cpp",
        "vega-louise-python": "louise-python"}


def _canon(doc: dict) -> object:
    load = {"Model": "m", "ModelFile": "/models/m", "Options": doc.get("options") or [],
            "EnvVars": doc.get("env") or {}}
    if "gpu_layers" in doc:
        load["NGPULayers"] = doc["gpu_layers"]
    return vw.canon_from_protojson(json.dumps(load))


def test_there_are_models_and_none_carries_another_vendors_mode():
    assert len(MODELS) >= 6
    for path in MODELS:
        text = path.read_text()
        for other in ("ROCBLAS_", "HIP_VISIBLE_DEVICES", "GGML_SYCL_", "NEO_CACHE_DIR", "rocm"):
            assert other not in text, f"{path.name}: {other}"


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_every_model_holds_the_contract_of_the_device_it_declares(path):
    doc = yaml.safe_load(path.read_text())
    canon = _canon(doc)
    placement = vw.declared_placement(canon)
    assert placement in ("card", "cpu"), f"{path.name} leaves the work split"
    if path.stem.endswith("-cpu"):
        assert placement == "cpu" and doc["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
    real = REAL[doc["backend"]]
    device = "cpu" if placement == "cpu" else "NVIDIA GeForce RTX 4090"
    assert vw.determinism_lacks(real, canon, device, "cuda" if placement == "card" else None) == []
