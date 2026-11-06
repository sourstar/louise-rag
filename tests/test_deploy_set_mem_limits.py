"""tests/test_deploy_set_mem_limits.py — the runbook's step that puts #89's
memory caps on the live app's web and worker (deploy/16f/set_mem_limits.py).
The 2026-10-30 14:14 OOM was global because no container had a cap."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "16f" / "set_mem_limits.py"
_spec = importlib.util.spec_from_file_location("set_mem_limits", _PATH)
sml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sml)


def a_config():
    return {"services": {
        "web": {"environment": {"ADMIN_TOKEN": "s3cret"}, "restart": "unless-stopped"},
        "worker": {"environment": {"ADMIN_TOKEN": "s3cret"}, "restart": "unless-stopped"},
        "gpu-probe": {"command": ["x"]}}}


def test_both_services_are_capped_and_nothing_else_moves():
    cfg = a_config()
    assert sml.apply(cfg) == {"web": (None, None), "worker": (None, None)}
    assert cfg["services"]["web"] == {"environment": {"ADMIN_TOKEN": "s3cret"}, "restart": "unless-stopped",
                                      "mem_limit": "12g", "memswap_limit": "12g"}
    assert cfg["services"]["worker"]["mem_limit"] == cfg["services"]["worker"]["memswap_limit"] == "24g"
    assert cfg["services"]["gpu-probe"] == {"command": ["x"]}


def test_idempotent():
    cfg = a_config()
    sml.apply(cfg)
    assert sml.apply(cfg) == {"web": ("12g", "12g"), "worker": ("24g", "24g")}


def test_the_values_are_the_repository_yaml_s():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "truenas-app.yaml").read_text(encoding="utf-8")
    for cap in sml.LIMITS.values():
        assert f"mem_limit: {cap}" in text and f"memswap_limit: {cap}" in text


def test_a_config_it_does_not_recognise_is_left_untouched():
    with pytest.raises(ValueError, match="services"):
        sml.apply({})
    cfg = a_config()
    del cfg["services"]["worker"]
    with pytest.raises(ValueError, match="worker"):
        sml.apply(cfg)
    assert "mem_limit" not in cfg["services"]["web"]
