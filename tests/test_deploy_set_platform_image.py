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


def test_the_worker_takes_the_webs_probe_address_in_the_same_update():
    """#164: the worker's card recorder asks the GPU probe the web's panel
    asks; the node's config gave the address to the web alone."""
    cfg = a_config()
    cfg["services"]["web"]["environment"]["GPU_PROBE_URL"] = "http://gpu-probe:8080"
    spi.apply(cfg, NEW)
    assert spi.carry(cfg) == ["GPU_PROBE_URL None -> http://gpu-probe:8080"]
    assert cfg["services"]["worker"]["environment"]["GPU_PROBE_URL"] == "http://gpu-probe:8080"
    assert spi.carry(cfg) == [], "already there: nothing to change"
    cfg["services"]["worker"]["environment"]["GPU_PROBE_URL"] = "http://old:1"
    assert spi.carry(cfg) == ["GPU_PROBE_URL http://old:1 -> http://gpu-probe:8080"], \
        "the web's is the configured truth"
    assert "ADMIN_TOKEN" not in spi.CARRIED, "addresses only, never secrets"


def test_a_web_without_a_probe_leaves_the_worker_as_it_is():
    cfg = a_config()
    spi.apply(cfg, NEW)
    assert spi.carry(cfg) == []
    assert "GPU_PROBE_URL" not in cfg["services"]["worker"]["environment"]


def test_an_image_already_set_still_updates_to_carry_the_probe(monkeypatch, capsys):
    import sys
    import types
    calls = []

    def configured():
        cfg = a_config()
        for s in spi.SERVICES:
            cfg["services"][s]["environment"][spi.KEY] = NEW
        cfg["services"]["web"]["environment"]["GPU_PROBE_URL"] = "http://gpu-probe:8080"
        return cfg

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args, **kw):
            calls.append(method)
            if method == "app.update":
                state["cfg"] = args[1]["custom_compose_config"]
                return None
            return state["cfg"]
    state = {"cfg": configured()}
    monkeypatch.setitem(sys.modules, "truenas_api_client", types.SimpleNamespace(Client=Client))
    assert spi.main(["x", NEW]) == 0
    out = capsys.readouterr().out
    assert "worker  GPU_PROBE_URL None -> http://gpu-probe:8080" in out
    assert calls[:2] == ["app.config", "app.update"]


def test_an_update_the_worker_did_not_take_is_said(monkeypatch, capsys):
    import sys
    import types

    def configured():
        cfg = a_config()
        for s in spi.SERVICES:
            cfg["services"][s]["environment"][spi.KEY] = NEW
        cfg["services"]["web"]["environment"]["GPU_PROBE_URL"] = "http://gpu-probe:8080"
        return cfg

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, *args, **kw):
            return configured() if method == "app.config" else None     # the update is lost
    monkeypatch.setitem(sys.modules, "truenas_api_client", types.SimpleNamespace(Client=Client))
    assert spi.main(["x", NEW]) == 1
    assert "did not take ['GPU_PROBE_URL']" in capsys.readouterr().err
