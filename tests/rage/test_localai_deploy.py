"""The files RAGE over LocalAI is deployed from, held to the code that reads
them (deploy/rage/Dockerfile.localai, deploy/rage/localai-models/,
deploy/truenas-rage-localai.yaml; migration-plan step 15e), as
test_deploy_files.py holds llama-swap's.
"""
import inspect
import json
import re
import tomllib
from pathlib import Path

from app.rage import loadopts
from app.rage.front_localai import MARKER, unpinned
from app.rage.observer import Observer
from tests.rage.localai_fake import FakeLocalAI

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "rage" / "Dockerfile.localai").read_text(encoding="utf-8")
APP_YAML = (ROOT / "deploy" / "truenas-rage-localai.yaml").read_text(encoding="utf-8")
MODELS = sorted((ROOT / "deploy" / "rage" / "localai-models").glob("*.yaml"))
SERIES = (ROOT / "deploy" / "localai" / "patches" / "series").read_text(encoding="utf-8")


def _arg(name: str) -> str | None:
    m = re.search(rf"^ARG {name}(?:=(\S+))?$", DOCKERFILE, re.M)
    assert m, f"ARG {name}"
    return m.group(1)


def _yaml(path: Path) -> dict:
    """JSON after the header comments: what LocalAI reads as YAML."""
    body = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.startswith("#"))
    return json.loads(body)


def _env(name: str) -> str:
    m = re.search(rf"\b{name}=(\S+)", DOCKERFILE)
    assert m, name
    return m.group(1).strip("'\"")


def test_every_image_the_build_uses_is_pinned_by_digest():
    for name in ("GO_IMAGE", "NODE_IMAGE", "BUSYBOX_IMAGE", "LLAMA_CPP_BACKEND_IMAGE",
                 "LLAMA_CPP_CPU_BACKEND_IMAGE", "PYTHON_IMAGE"):
        assert re.search(r"@sha256:[0-9a-f]{64}$", _arg(name) or ""), name
    assert _arg("LLAMA_CPP_IMAGE") is None, "the base has no default: its digest is passed"
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("PROTOC_SHA256") or "")


def test_localai_is_built_from_the_series_and_the_backend_from_the_same_commit():
    base = re.search(r"^base ([0-9a-f]{40})$", SERIES, re.M).group(1)
    assert "COPY deploy/localai/patches /patches" in DOCKERFILE
    assert "git apply --index" in DOCKERFILE and "sed -n 's/^base //p' /patches/series" in DOCKERFILE
    assert f"sha-{base[:7]}-gpu-intel-sycl-f16-llama-cpp" in DOCKERFILE, \
        "the backend LocalAI's CI built from the series' base"


def test_the_image_carries_what_the_front_imports_and_no_more_of_the_platform():
    final = DOCKERFILE.rsplit("FROM ${LLAMA_CPP_IMAGE}", 1)[1]
    assert "AS " not in final.split("\n", 1)[0], "the image is the last stage: the default target"
    copied = set(re.findall(r"^COPY (app/\S+)", final, re.M))
    assert copied == {"app/__init__.py", "app/gpu_clock.py", "app/gpu_memory.py",
                      "app/hold/__init__.py", "app/hold/codex.py", "app/rage"}


def test_nothing_is_installed_after_the_image_is_built():
    assert _env("RAGE_RUNNER") == "localai"
    assert _env("LOCALAI_GALLERIES") == "[]" and _env("LOCALAI_BACKEND_GALLERIES") == "[]"
    assert _env("LOCALAI_AUTOLOAD_GALLERIES") == "false"
    assert _env("LOCALAI_AUTOLOAD_BACKEND_GALLERIES") == "false"
    system = _env("LOCALAI_BACKENDS_SYSTEM_PATH")
    assert f"/ {system}/llama-cpp/" in DOCKERFILE, "the backend is baked into the system path"
    default = inspect.signature(Observer).parameters["backends_root"].default
    assert str(default) == system, \
        "the path RAGE holds a backend's program to is the one the image fills"
    assert "LOCALAI_API_KEY" not in DOCKERFILE and "LOCALAI_API_KEY" not in APP_YAML


def test_every_model_yaml_pins_what_localai_would_fill_in():
    assert MODELS
    for path in MODELS:
        body = _yaml(path)
        assert not path.read_text(encoding="utf-8").startswith(MARKER), \
            f"{path.name}: the operator's, edited by hand"
        assert unpinned(body) == [], f"{path.name}: {unpinned(body)}"
        assert body["name"] == path.stem and body["backend"] == "llama-cpp"
        assert body["parameters"]["model"].startswith("weights/")


def test_every_model_yaml_states_a_run_once_localai_has_completed_it(tmp_path):
    """What LocalAI sends for each shipped YAML, `env:` included as
    `EnvVars`, is 09 §9.3's options. The smoke model had no `env:`, so §11
    missed that NEO_CACHE_DIR made all six unstatable."""
    fake = FakeLocalAI(tmp_path)
    for path in MODELS:
        body = _yaml(path)
        fake.configs[body["name"]] = body
        sent = loadopts.parse(json.dumps(fake.materialise(body["name"])))
        canon = loadopts.canonical(sent)
        assert canon.env == body.get("env", {}), path.name
        assert "EnvVars" not in canon.options, f"{path.name}: the environment is `env`'s"


def test_the_yamls_cover_every_llama_cpp_model_the_node_serves_today():
    today = tomllib.loads((ROOT / "deploy" / "rage" / "rage.example.toml").read_text())
    llama = {m for m, e in today["models"].items() if e["engine"]["name"] == "llama.cpp"}
    assert {p.stem for p in MODELS} == llama


def test_the_weights_are_mounted_read_only_inside_the_models_directory():
    models_dir = _env("LOCALAI_MODELS_DIR")
    assert models_dir == "/state/localai-models"
    assert re.search(r"^\s+- /mnt/\S+/louise-rage/state:/state$", APP_YAML, re.M), \
        "the models directory is writable: PUT and DELETE write its YAML"
    mounts = re.findall(rf"^\s+- (\S+):{models_dir}/weights/([\w.-]+)(:ro)?$", APP_YAML, re.M)
    assert mounts and all(ro == ":ro" for _, _, ro in mounts), "weights are never written"
    mounted = {name for _, name, _ in mounts}
    for path in MODELS:
        assert _yaml(path)["parameters"]["model"].split("/")[1] in mounted, path.name


def test_the_app_is_deployed_by_digest():
    assert "image: <registry>/rage@sha256:<digest>" in APP_YAML
    assert "RAGE_IMAGE: 'sha256:<digest>'" in APP_YAML
    assert "ADMIN_TOKEN_FILE: /run/rage/admin.token" in APP_YAML
