"""tests/test_deploy_set_platform_image.py — the runbook's step that tells
the app's web and worker the image they start in (deploy/16f/
set_platform_image.py). The node declared pubmed's Codex image for two
weeks of later builds (found 2026-10-07)."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "16f" / "set_platform_image.py"
_spec = importlib.util.spec_from_file_location("set_platform_image", _PATH)
spi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spi)

OLD = "sha256:" + "85" * 32
NEW = "sha256:" + "29" * 32


def a_config():
    return {"services": {
        "web": {"environment": {"ADMIN_TOKEN": "s3cret", spi.KEY: OLD}},
        "worker": {"environment": {"ADMIN_TOKEN": "s3cret", spi.KEY: OLD, "NODE_KEY_PATH": "/k"}},
        "gpu-probe": {"command": ["x"]}}}


def test_both_services_are_told_and_nothing_else_moves():
    cfg = a_config()
    assert spi.apply(cfg, NEW) == {"web": OLD, "worker": OLD}
    assert cfg["services"]["web"]["environment"] == {"ADMIN_TOKEN": "s3cret", spi.KEY: NEW}
    assert cfg["services"]["worker"]["environment"] == {"ADMIN_TOKEN": "s3cret", spi.KEY: NEW, "NODE_KEY_PATH": "/k"}
    assert cfg["services"]["gpu-probe"] == {"command": ["x"]}, "the probe carries no environment, by design"


def test_a_config_it_does_not_recognise_is_left_untouched():
    with pytest.raises(ValueError, match="sha256"):
        spi.apply(a_config(), "85fd59f1")
    cfg = a_config()
    cfg["services"]["worker"]["environment"] = ["A=1"]
    with pytest.raises(ValueError, match="not a mapping"):
        spi.apply(cfg, NEW)
    assert cfg["services"]["web"]["environment"][spi.KEY] == OLD, "refused before any change"
    with pytest.raises(ValueError, match="services"):
        spi.apply({}, NEW)


def test_the_output_names_the_variable_and_never_another_value(monkeypatch, capsys):
    import sys
    import types
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args, **kw):
            calls.append(method)
            if method == "app.config":
                return a_config() if calls.count("app.config") == 1 else spi.apply(a_config(), NEW) and \
                    {"services": {s: {"environment": {spi.KEY: NEW}} for s in spi.SERVICES}}
            return None
    monkeypatch.setitem(sys.modules, "truenas_api_client", types.SimpleNamespace(Client=Client))
    assert spi.main(["x", NEW]) == 0
    out = capsys.readouterr()
    assert "s3cret" not in out.out + out.err and "/k" not in out.out + out.err
    assert calls == ["app.config", "app.update", "app.config"] and out.out.strip().endswith("set")
