"""The files RAGE is deployed from, held to the code that reads them
(deploy/rage/, deploy/truenas-rage.yaml; migration-plan step 13d).

An example nobody loads drifts from the loader the day a rule changes, and
the operator finds out on the node.
"""
import re
from pathlib import Path

from app.rage import model, swapyaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "rage" / "Dockerfile").read_text(encoding="utf-8")
APP_YAML = (ROOT / "deploy" / "truenas-rage.yaml").read_text(encoding="utf-8")


def test_the_example_config_is_one_the_front_would_accept():
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    assert {"bge-m3", "chat", "madlad", "medcpt-rerank", "dnabert2"} <= set(cfg.models)
    for entry in cfg.models.values():
        assert entry.devices == ("cpu", "xpu"), f"{entry.id}: the device is chosen at load"
    assert set(cfg.preload) <= set(cfg.models)
    assert "swap: false" in swapyaml.render(cfg)


def test_every_program_the_example_starts_is_in_the_image():
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    programs = {t[0] for e in cfg.models.values() for t in e.cmd.values()}
    for program in programs:
        if program == "/app/llama-server":
            continue                        # the base image's own (the FROM line)
        venv = re.fullmatch(r"(/opt/venv-\w+)/bin/python", program)
        assert venv and f"python3 -m venv" in DOCKERFILE and venv.group(1) in DOCKERFILE, program
    servers = {t[3] for e in cfg.models.values() for t in e.cmd.values()
               if "servers.launch" in t}
    for server in servers:
        assert f"/opt/rage/servers/{server}.py" in DOCKERFILE, server


def test_the_image_carries_what_the_front_imports_and_no_more_of_the_platform():
    copied = set(re.findall(r"^COPY (app/\S+)", DOCKERFILE, re.M))
    assert copied == {"app/__init__.py", "app/gpu_clock.py", "app/hold/__init__.py",
                      "app/hold/codex.py", "app/rage"}


def test_the_image_cannot_name_its_own_digest_so_the_deployment_does():
    assert not re.search(r"^ENV .*RAGE_IMAGE", DOCKERFILE, re.M)
    assert re.search(r"^ARG LLAMA_SWAP_SHA256$", DOCKERFILE, re.M), "no default: no bytes"
    assert "image: <registry>/rage@sha256:<digest>" in APP_YAML, "by digest, never a tag"
    assert "RAGE_IMAGE: 'sha256:<digest>'" in APP_YAML


def test_the_app_writes_no_model_and_names_its_token_by_path():
    binds = re.findall(r"^\s+- (\S+):(/models/\S+)$", APP_YAML, re.M)
    assert binds and all(target.endswith(":ro") for _src, target in binds)
    assert "ADMIN_TOKEN_FILE: /run/rage/admin.token" in APP_YAML
    assert "ADMIN_TOKEN:" not in APP_YAML, "a path, never the value (invariant 8)"
    for source in re.findall(r"^\s+- (/sys/\S+?):/gpufreq", APP_YAML, re.M):
        assert ":" not in source, "TrueNAS rewrites binds; a colon in the source breaks the app"


def test_the_yaml_the_front_writes_is_for_the_llama_swap_the_image_pins():
    pinned = re.search(r"^ARG LLAMA_SWAP_VERSION=(\d+)$", DOCKERFILE, re.M).group(1)
    assert swapyaml.LLAMA_SWAP_VERSION == f"v{pinned}"
