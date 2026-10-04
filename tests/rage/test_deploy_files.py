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
    assert {"bge-m3", "chat", "madlad", "medcpt-rerank"} <= set(cfg.models)
    # DNA collections are frozen and inactive until further development
    # (operator, 2026-09-19): RAGE serves no DNA model, and binds none
    assert "dnabert2" not in cfg.models
    assert not re.search(r"^\s+- \S*dnabert2\S*:/models/", APP_YAML, re.M)
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
    assert copied == {"app/__init__.py", "app/gpu_clock.py", "app/gpu_memory.py",
                      "app/hold/__init__.py",
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
    for source in re.findall(r"^\s+- (\S+?):/gpufreq", APP_YAML, re.M):
        assert ":" not in source, "TrueNAS rewrites binds; a colon in the source breaks the app"
        # A DRM card NUMBER is handed out in load order and swaps across boots.
        # It did, on the node, 2026-09-22: the Arc went card0 -> card1, the old
        # bind then pointed at the processor's graphics, and the app would not
        # start at all. The card's own address is the stable name, and a
        # colon-free symlink to it is how a bind can carry one.
        assert not re.search(r"/drm/card\d", source), \
            "a card number is not a card: bind a symlink to the PCI address"


def test_the_yaml_the_front_writes_is_for_the_llama_swap_the_image_pins():
    pinned = re.search(r"^ARG LLAMA_SWAP_VERSION=(\d+)$", DOCKERFILE, re.M).group(1)
    assert swapyaml.LLAMA_SWAP_VERSION == f"v{pinned}"


def test_a_torch_model_declares_its_runtime_and_a_llama_server_never_does():
    """Both found by building and running the image (2026-09-19). torch's
    wheels bundle a SYCL runtime the base image's LD_LIBRARY_PATH shadows with
    an older one, and `import torch` dies — so a torch model declares its own
    path, inside its run identity. llama-server must keep the base's: it is
    the environment every vector on the node was produced under."""
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    for entry in cfg.models.values():
        programs = {t[0] for t in entry.cmd.values()}
        # The venvs built --system-site-packages, which is what makes them
        # see the system torch. venv-medcpt is deliberately absent: it runs
        # OpenVINO and installs no torch at all, so the base's path is right
        # for it, and a model that imports torch is the only thing this
        # applies to.
        if programs & {"/opt/venv-madlad/bin/python",
                       "/opt/venv-dnabert2/bin/python",
                       "/opt/venv-st/bin/python"}:
            assert entry.env.get("LD_LIBRARY_PATH") == "/usr/local/lib", entry.id
        else:
            assert "LD_LIBRARY_PATH" not in entry.env, entry.id
    assert '- "LD_LIBRARY_PATH=/usr/local/lib"' in swapyaml.render(cfg), \
        "llama-swap is what hands the child its environment"


def test_llama_servers_libraries_are_found_from_any_directory():
    """Its runpath ends in an empty entry — "the current directory" — so it
    loaded only from the base image's WORKDIR, and llama-swap starts children
    from RAGE's. Registered with the loader, never put on LD_LIBRARY_PATH."""
    assert re.search(r"^RUN echo /app > /etc/ld\.so\.conf\.d/\S+ && ldconfig", DOCKERFILE, re.M)
    assert not re.search(r"^ENV .*LD_LIBRARY_PATH", DOCKERFILE, re.M)


def test_nothing_of_rage_depends_on_another_apps_folder():
    """The chat model was first read where Ollama kept it. An app that reads
    another app's folder stops working the day that app is deleted — so RAGE
    has a copy of its own (2026-09-20), and neither a bind nor a weights path
    may name Ollama's. The path is not part of a run: the run id did not move."""
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    for entry in cfg.models.values():
        assert "ollama" not in entry.weights.lower(), entry.id
    binds = re.findall(r"^\s+- (/\S+):(/models/\S+)$", APP_YAML, re.M)
    assert binds and not [src for src, _ in binds if "ollama" in src.lower()]
    assert cfg.models["chat"].weights == "/models/chat/qwen3-8b.gguf"
    assert any(target.startswith("/models/chat:") for _src, target in binds), \
        "the folder the chat model is read from is bound"


def test_every_card_the_example_places_on_says_where_it_is_on_the_bus():
    """No manufacturer in RAGE (operator, 2026-09-20): the driver a run states
    is read from the card's PCI address, never picked from a list of vendors'
    modules — and the reference node loads two of them."""
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    cards = {d for e in cfg.models.values() for d in e.cmd} - {"cpu"}
    assert cards and cards <= set(cfg.pci), "a card with no address: its driver would be a guess"


def test_every_model_on_the_card_keeps_its_compiled_kernels_on_state():
    """Operator, 2026-10-01: the GPU driver's kernel cache on /state, declared
    in rage.toml — so it survives a redeploy and is part of every run. The
    directory must exist before the first start; the deploy steps create it."""
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    for entry in cfg.models.values():
        if set(entry.cmd) - {"cpu"}:
            assert entry.env.get("NEO_CACHE_DIR") == "/state/neo-cache", entry.id
    assert re.search(r"mkdir -p /mnt/ssd5/apps/louise-rage/\{[^}]*state/neo-cache", APP_YAML)
    assert re.search(r"- /mnt/ssd5/apps/louise-rage/state:/state$", APP_YAML, re.M)


def test_the_translator_is_parked_not_unloaded_and_its_server_can_be():
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    park = cfg.models["madlad"].park
    assert park is not None and park.path == "/park" and park.unpark == "/unpark"
    server = (ROOT / "deploy" / "madlad-translate" / "server.py").read_text(encoding="utf-8")
    assert '("/park", "/unpark")' in server
    assert [m for m, e in cfg.models.items() if e.park is not None] == ["madlad"], \
        "only a server that answers /park may declare it"


def test_the_openvino_reranker_keeps_its_compiled_model_on_state():
    cfg = model.load(ROOT / "deploy" / "rage" / "rage.example.toml")
    assert cfg.models["medcpt-rerank"].env.get("OV_CACHE_DIR") == "/state/ov-cache"
    assert re.search(r"mkdir -p /mnt/ssd5/apps/louise-rage/\{[^}]*state/ov-cache", APP_YAML)
