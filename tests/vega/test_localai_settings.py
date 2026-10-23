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
