"""The files RAGE over LocalAI is deployed from, held to the code that reads
them (deploy/rage/Dockerfile.localai, deploy/rage/localai-models/,
deploy/truenas-rage-localai.yaml; migration-plan step 15e). llama-swap's,
held by test_deploy_files.py, were retired at 15g.
"""
import inspect
import json
import re
from pathlib import Path

from app.rage import loadopts
from app.rage.front_localai import MARKER, unpinned
from app.rage.observer import Observer
from tests.rage.localai_fake import FakeLocalAI

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "rage" / "Dockerfile.localai").read_text(encoding="utf-8")
APP_YAML = (ROOT / "deploy" / "truenas-rage-localai.yaml").read_text(encoding="utf-8")
MODELS = sorted((ROOT / "deploy" / "rage" / "localai-models").glob("*.yaml"))
#: models whose YAML is kept but not served: copied in at no cut-over and mounted
#: by no app file. DAR-legal-it, suspended by the operator 2026-10-04 (it
#: serves no live collection).
SUSPENDED = sorted((ROOT / "deploy" / "rage" / "localai-models" / "suspended").glob("*.yaml"))
ALL = MODELS + SUSPENDED
BACKENDS = ROOT / "deploy" / "localai" / "backends"
#: the platform's Python backends (15e-2), by the name a YAML's `backend` gives
PYTHON_BACKENDS = sorted(d.name for d in BACKENDS.iterdir() if d.name.startswith("louise-"))
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
    for path in ALL:
        body = _yaml(path)
        assert not path.read_text(encoding="utf-8").startswith(MARKER), \
            f"{path.name}: the operator's, edited by hand"
        assert unpinned(body) == [], f"{path.name}: {unpinned(body)}"
        assert body["name"] == path.stem
        assert body["backend"] in ("llama-cpp", *PYTHON_BACKENDS), path.name
        assert body["parameters"]["model"].startswith("weights/")


def test_a_chat_model_gets_its_own_chat_template():
    """Without `use_tokenizer_template` LocalAI joins the messages itself and
    Qwen3 rambled ("Rome. Answer: Rome. Answer: Rome…") where llama-server's
    --jinja answered one sentence (node, 2026-10-04). Every llama.cpp model
    that is not an encoder or a reranker is a chat model here."""
    chats = [p for p in ALL if _yaml(p)["backend"] == "llama-cpp"
             and not _yaml(p).get("embeddings") and not _yaml(p).get("reranking")]
    assert [p.stem for p in chats] == ["chat"]
    for p in chats:
        assert _yaml(p).get("template", {}).get("use_tokenizer_template") is True, p.name
        assert "use_jinja:true" in _yaml(p)["options"], p.name


def test_each_python_yaml_is_a_load_its_backend_accepts():
    """Every option a Python model's YAML gives is one its backend reads, at
    a value it takes: a misspelt one would load with a default while the run
    named the misspelling."""
    from tests.test_localai_backends import (article_backend, dar_backend, lb, madlad_backend,
                                             medcpt_backend)
    by_name = {"louise-dar-legal-it": dar_backend, "louise-madlad": madlad_backend,
               "louise-medcpt-rerank": medcpt_backend,
               "louise-medcpt-article-ov": article_backend}
    assert sorted(by_name) == PYTHON_BACKENDS
    served = {}
    for path in ALL:
        body = _yaml(path)
        if body["backend"] in by_name:
            module = by_name[body["backend"]]
            lb.options(body.get("options", []), module.KNOWN, module.DEFAULTS)
            assert body["context_size"] > 0 and body["parameters"]["batch"] > 0
            served[body["backend"]] = body
    assert sorted(served) == PYTHON_BACKENDS, "each backend serves a model"
    madlad = served["louise-madlad"]
    for key in ("template", "echo", "cutstrings", "extract_regex", "trimspace", "trimsuffix"):
        assert key not in madlad, \
            f"{key}: LocalAI would change the prompt or the text, the JSON the batch travels in"
    assert served["louise-dar-legal-it"]["embeddings"] is True
    assert served["louise-medcpt-rerank"]["reranking"] is True
    assert served["louise-medcpt-article-ov"]["embeddings"] is True
    assert "pairs:true" in served["louise-medcpt-article-ov"]["options"]


def test_each_backend_is_assembled_with_the_server_it_imports_and_stubs_it_can_run():
    copied = dict((d, m) for _src, d, m in re.findall(
        r"^COPY deploy/([\w-]+)/server\.py\s+/src/(louise-[\w-]+)/(\w+)\.py$", DOCKERFILE, re.M))
    assert sorted(copied) == PYTHON_BACKENDS
    for name, module in copied.items():
        source = (BACKENDS / name / "backend.py").read_text(encoding="utf-8")
        assert re.search(rf"^import {module}$", source, re.M), f"{name} imports {module}"
        assert "-r ../requirements-grpc.txt" in (BACKENDS / name / "requirements.txt").read_text()
    grpc = (BACKENDS / "requirements-grpc.txt").read_text()
    assert f"grpcio=={_arg('GRPCIO_TOOLS_VERSION')}" in grpc, \
        "the stubs are generated by the grpcio the backends run"
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg("TORCH_VERSION") or ""), "one torch, pinned"
    assert "-I/proto" in DOCKERFILE and \
        "COPY --from=localai-source /build/backend/backend.proto" in DOCKERFILE, \
        "the stubs are the series' own protocol"


def test_every_model_yaml_states_a_run_once_localai_has_completed_it(tmp_path):
    """What LocalAI sends for each shipped YAML, `env:` included as
    `EnvVars`, is 09 §9.3's options. The smoke model had no `env:`, so §11
    missed that NEO_CACHE_DIR made all six unstatable."""
    fake = FakeLocalAI(tmp_path)
    for path in ALL:
        body = _yaml(path)
        fake.configs[body["name"]] = body
        sent = loadopts.parse(json.dumps(fake.materialise(body["name"])))
        canon = loadopts.canonical(sent)
        assert canon.env == body.get("env", {}), path.name
        assert "EnvVars" not in canon.options, f"{path.name}: the environment is `env`'s"


def test_the_example_rage_toml_is_the_cards_the_front_accepts():
    from app.rage import localai_config
    cfg = localai_config.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    assert cfg.card_at("0000:03:00.0") == "xpu"


def test_the_yamls_are_the_models_the_node_served_before_the_cut_over():
    """Every model llama-swap served at 15f has its YAML (rage.example.toml
    listed them until 15g); DAR-legal-it's is kept, suspended. Added since:
    granite-embedding-107m-multilingual, the deposit collections' encoder
    (02 §2.9; the operator's word, 2026-10-06, step 16f), and medcpt-article-ov,
    the article encoder in pair form (docs/notes/input-forms.md, 2026-10-08)."""
    assert {p.stem for p in ALL} == {"bge-m3", "bge-reranker", "chat", "madlad",
                                     "medcpt-article", "medcpt-query", "medcpt-rerank",
                                     "medcpt-article-ov", "qwen3-embedding-4b", "dar-legal-it",
                                     "granite-embedding-107m-multilingual"}
    assert {p.stem for p in SUSPENDED} == {"dar-legal-it"}, "suspended: the operator's word"


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
