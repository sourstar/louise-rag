"""The front over LocalAI (migration-plan step 15d; docs/notes/rage-over-localai.md
§4, §7, §10; 03 §3.3 and 09 §9.12 as amended 2026-10-03).

LocalAI launches, loads and evicts; RAGE states the run of every answer from
what was launched, guards LocalAI's admin routes, holds the clock and keeps
the panel. Every test here runs the front's real code against a LocalAI that
does what LocalAI 58830f7 does (localai_fake.py), over a /proc of files.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.rage import loadopts, localai_config, runs
from app.rage.__main__ import LOCALAI_PINNED_ENV, localai_child, main_localai
from app.rage.clock import Clock
from app.rage.front_localai import MARKER, create_localai_app
from app.rage.localai import LocalAI
from app.rage.model import RageConfigError
from app.rage.observer import NEW_RUN, UNLOADED, Observer
from app.rage.procfs import ProcFS
from app.rage.store import RunStore
from app.rage.wire import RUN_HEADER

from .conftest import IMAGE
from .localai_fake import BACKEND_ARGV, PCI, FakeLocalAI, hand_written, pinned

MIB = 1 << 20
ADMIN = {"Authorization": "Bearer the-token"}
CARD = "Intel(R) Arc(TM) Pro B70 Graphics"
RAGE_TOML = f'[rage]\ndevice_names = {{ xpu = "{CARD}" }}\npci = {{ xpu = "{PCI}" }}\n'


@pytest.fixture
def node(tmp_path):
    fake = FakeLocalAI(tmp_path)
    sys_root = tmp_path / "sys"
    device = sys_root / "bus" / "pci" / "devices" / PCI
    device.mkdir(parents=True)
    (sys_root / "bus" / "pci" / "drivers" / "xe").mkdir(parents=True)
    (device / "driver").symlink_to(sys_root / "bus" / "pci" / "drivers" / "xe")
    (sys_root / "module" / "xe").mkdir(parents=True)
    (sys_root / "module" / "xe" / "srcversion").write_text("9F2C1A\n")
    hostproc = tmp_path / "hostproc"
    (hostproc / "sys" / "kernel").mkdir(parents=True)
    (hostproc / "sys" / "kernel" / "osrelease").write_text("6.18.0-production\n")
    versions = tmp_path / "runtime-versions"
    versions.write_text("libze-intel-gpu1 26.05.1\n")
    config = tmp_path / "rage.toml"
    config.write_text(RAGE_TOML)
    client = LocalAI("http://localai", transport=fake.transport())
    logs: list[str] = []
    used = {"mib": 9_000}
    observer = Observer(localai_config.load(config), client,
                        RunStore(tmp_path / "state" / "rage.sqlite"), image=IMAGE,
                        procfs=ProcFS(fake.proc), sys_root=sys_root, proc_root=hostproc,
                        runtime_versions=versions,
                        card=lambda pci: (32_000 * MIB, used["mib"] * MIB, None),
                        log=logs.append)
    app = create_localai_app(observer, client, Clock(None), "the-token",
                             models_dir=fake.models_dir, watch=False)
    with TestClient(app) as http:
        yield SimpleNamespace(fake=fake, observer=observer, http=http, logs=logs, used=used,
                              config=config)


def _put(node, name="bge-m3", **over):
    r = node.http.put(f"/louise/models/{name}", headers=ADMIN,
                      json=pinned(f"{name}.gguf" if name in ("bge-m3", "reranker")
                                  else "bge-m3.gguf", **over))
    assert r.status_code == 200, r.text
    return r


def _embed(node, name="bge-m3", **body):
    return node.http.post(f"/upstream/{name}/v1/embeddings",
                          json={"model": name, "input": ["a sentence"], **body})


def _run(node, name="bge-m3") -> str:
    r = _embed(node, name)
    assert r.status_code == 200, r.text
    return r.headers[RUN_HEADER]


def _events(node, kind):
    return [e for e in node.observer.events if e["kind"] == kind]


# ---- the run, from what LocalAI launched -----------------------------------------------------

def test_an_answer_states_the_run_its_process_was_loaded_as(node):
    _put(node)
    run_id = _run(node)
    obj = node.http.get(f"/louise/runs/{run_id}").json()
    assert runs.run_id(obj) == run_id, "the id IS the hash of its description (09 §9.12)"
    engine = obj["engine"]
    assert engine["name"] == "localai/llama-cpp" and engine["build"] == "v3.9.0"
    assert engine["image"] == IMAGE and engine["device"] == CARD
    assert engine["args"] == [BACKEND_ARGV, "--addr", "127.0.0.1:${PORT}"], \
        "the port LocalAI chose is a placeholder: a restart is not another run"
    opts = engine["options"]
    assert opts["ModelFile"] == "${WEIGHTS}" and opts["ModelPath"] == "${MODELS}"
    assert opts["RopeFreqBase"] == "10000.5", "a float as its decimal string (09 §9.3)"
    assert opts["NBatch"] == 512 and opts["Threads"] == 8
    weights = (node.fake.models_dir / "bge-m3.gguf").read_bytes()
    assert obj["weights"] == "sha256:" + hashlib.sha256(weights).hexdigest()
    assert obj["driver"].startswith("kernel 6.18.0-production; xe 9F2C1A")


def test_a_reload_with_unchanged_options_states_the_same_run(node):
    _put(node)
    first = _run(node)
    pid = node.fake.loaded["bge-m3"]
    node.fake.replace("bge-m3")                     # LocalAI evicted it and loaded it again
    assert node.fake.loaded["bge-m3"] != pid
    assert _run(node) == first, "another process, the same run: nothing to gate again"


def test_one_changed_option_states_another_run_and_the_event_names_it(node):
    _put(node)
    first = _run(node)
    r = node.http.put("/louise/models/bge-m3", headers=ADMIN,
                      json=pinned("bge-m3.gguf", parameters={"model": "bge-m3.gguf",
                                                             "batch": 1024}))
    assert r.status_code == 200
    assert "bge-m3" not in node.fake.loaded, "a PUT on a loaded model stops it"
    second = _run(node)
    assert second != first
    assert "NBatch 512 → 1024" in _events(node, NEW_RUN)[-1]["detail"]


def test_a_default_localai_filled_in_is_in_the_run(node):
    """§10: a field the YAML leaves unset is filled from LocalAI's settings,
    which its UI changes without anyone editing the model — so it is in the
    run, and a change of setting is a new run, named."""
    hand_written(node.fake.models_dir, "bge-m3",
                 {k: v for k, v in pinned("bge-m3.gguf").items() if k != "threads"})
    node.fake.reload_configs()
    first = _run(node)
    obj = node.http.get(f"/louise/runs/{first}").json()
    assert obj["engine"]["options"]["Threads"] == 8, "LocalAI's, not the YAML's"
    node.fake.settings["threads"] = 16              # the settings page
    node.fake.replace("bge-m3")
    assert _run(node) != first
    assert "Threads 8 → 16" in _events(node, NEW_RUN)[-1]["detail"]


@pytest.mark.parametrize("over,why", [
    ({"mmproj": "projector.gguf"}, "second file"),
    ({"env": {"EXTRA_DATA": "/srv/vocab.txt"}}, "absolute path"),
])
def test_an_option_naming_another_file_states_no_run(node, over, why):
    _put(node, **over)
    r = _embed(node)
    assert r.status_code == 200 and RUN_HEADER not in r.headers, \
        "it answers — search works — and states no run, so nothing is written (03 §3.3)"
    assert why in node.observer.row("bge-m3")["reason"]


def test_a_process_replaced_mid_answer_gets_no_header(node):
    _put(node)
    first = _run(node)
    node.fake.during_request = lambda: node.fake.replace("bge-m3")
    r = _embed(node)
    assert r.status_code == 200 and RUN_HEADER not in r.headers
    node.fake.during_request = None
    assert _run(node) == first, "the next answer, from the new process, states the run"


def test_a_body_naming_another_model_is_refused_and_one_naming_none_is_named(node):
    _put(node)
    asked = len(node.fake.calls)
    r = _embed(node, model="reranker")
    assert r.status_code == 400 and "names the model 'bge-m3'" in r.text
    assert len(node.fake.calls) == asked, "LocalAI never saw it"
    r = node.http.post("/upstream/bge-m3/v1/embeddings", json={"input": ["a"]})
    assert r.status_code == 200 and r.json()["model"] == "bge-m3"


def test_a_model_on_the_cpu_is_a_cpu_run(node):
    _put(node, gpu_layers=0)
    obj = node.http.get(f"/louise/runs/{_run(node)}").json()
    assert obj["engine"]["device"] == "cpu" and obj["driver"] == "cpu"


def test_a_model_that_fell_back_to_the_cpu_is_stated_as_the_cpu_run_it_is(node):
    """Read, never declared: its YAML asks for the card, its process holds
    none of it — the run says cpu, and the gate tests that run."""
    _put(node)
    node.fake.on_card_mib = 0
    obj = node.http.get(f"/louise/runs/{_run(node)}").json()
    assert obj["engine"]["device"] == "cpu"


# ---- the operator's routes -------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("post", "/louise/models/bge-m3/load"), ("post", "/louise/models/bge-m3/unload"),
    ("post", "/louise/models/bge-m3/restart"), ("put", "/louise/models/bge-m3"),
    ("delete", "/louise/models/bge-m3"),
])
def test_the_admin_routes_never_reach_localai_without_the_token(node, method, path):
    kwargs = {"json": pinned("bge-m3.gguf")} if method == "put" else {}
    r = getattr(node.http, method)(path, **kwargs)
    assert r.status_code == 401
    r = getattr(node.http, method)(path, headers={"Authorization": "Bearer wrong"}, **kwargs)
    assert r.status_code == 401
    assert node.fake.admin_calls() == [] and not list(node.fake.models_dir.glob("*.yaml"))


def test_load_and_unload_pass_through_with_the_token(node):
    _put(node)
    row = node.http.post("/louise/models/bge-m3/load", headers=ADMIN).json()
    assert row["state"] == "loaded" and row["run"].startswith("sha256:")
    assert ("POST", "/backend/load", {"model": "bge-m3"}) in node.fake.calls
    assert node.http.post("/louise/models/bge-m3/unload", headers=ADMIN).json()["state"] == \
        "unloaded"
    assert "bge-m3" not in node.fake.loaded
    assert node.http.post("/louise/models/bge-m3/unload", headers=ADMIN).status_code == 200, \
        "a model that is not loaded is already what was asked"


def test_the_restart_reloads_that_model_only_and_states_the_same_run(node):
    _put(node)
    _put(node, "reranker")
    first = node.http.post("/louise/models/bge-m3/load", headers=ADMIN).json()
    node.http.post("/louise/models/reranker/load", headers=ADMIN)
    reranker_pid = node.fake.loaded["reranker"]
    node.fake.calls.clear()
    row = node.http.post("/louise/models/bge-m3/restart", headers=ADMIN).json()
    assert [c for c in node.fake.admin_calls()] == [
        ("POST", "/backend/shutdown", {"model": "bge-m3"}),
        ("POST", "/backend/load", {"model": "bge-m3"})]
    assert node.fake.loaded["reranker"] == reranker_pid, "never another model"
    assert row["pid"] != first["pid"] and row["run"] == first["run"]


def test_put_writes_rages_yaml_and_has_localai_read_it(node):
    r = _put(node)
    path = node.fake.models_dir / "bge-m3.yaml"
    assert r.json()["file"] == "bge-m3.yaml"
    assert path.read_text().split("\n", 1)[0] == MARKER
    assert json.loads(path.read_text().split("\n", 1)[1])["name"] == "bge-m3"
    assert ("POST", "/models/reload", None) in node.fake.calls
    rows = node.http.get("/louise/models").json()
    assert {"id": "bge-m3", "state": "unloaded", "file": "rage"} in rows


@pytest.mark.parametrize("drop,named", [
    ("threads", "threads"), ("context_size", "context_size"), ("gpu_layers", "gpu_layers"),
    ("options", "parallel"),
])
def test_put_refuses_a_yaml_that_leaves_a_10_option_to_localai(node, drop, named):
    config = {k: v for k, v in pinned("bge-m3.gguf").items() if k != drop}
    r = node.http.put("/louise/models/bge-m3", headers=ADMIN, json=config)
    assert r.status_code == 422 and named in r.json()["error"]
    assert not (node.fake.models_dir / "bge-m3.yaml").exists()
    no_batch = dict(pinned("bge-m3.gguf"), parameters={"model": "bge-m3.gguf"})
    r = node.http.put("/louise/models/bge-m3", headers=ADMIN, json=no_batch)
    assert r.status_code == 422 and "parameters.batch" in r.json()["error"]


def test_put_refuses_a_name_that_is_not_its_url(node):
    r = node.http.put("/louise/models/bge-m3", headers=ADMIN,
                      json=pinned("bge-m3.gguf", name="other"))
    assert r.status_code == 400


def test_delete_stops_the_model_removes_its_yaml_and_localai_forgets_it(node):
    _put(node)
    _run(node)
    r = node.http.delete("/louise/models/bge-m3", headers=ADMIN)
    assert r.status_code == 200 and r.json()["state"] == "removed"
    assert "bge-m3" not in node.fake.loaded and not (node.fake.models_dir / "bge-m3.yaml").exists()
    assert all(row["id"] != "bge-m3" for row in node.http.get("/louise/models").json())
    assert node.http.delete("/louise/models/bge-m3", headers=ADMIN).status_code == 404


def test_the_operators_own_yaml_is_edited_by_hand(node):
    hand_written(node.fake.models_dir, "bge-m3", pinned("bge-m3.gguf"))
    node.fake.reload_configs()
    assert node.http.put("/louise/models/bge-m3", headers=ADMIN,
                         json=pinned("bge-m3.gguf")).status_code == 409
    assert node.http.delete("/louise/models/bge-m3", headers=ADMIN).status_code == 409
    assert (node.fake.models_dir / "bge-m3.yaml").exists()
    assert {"id": "bge-m3", "state": "unloaded", "file": "operator"} in \
        node.http.get("/louise/models").json()


# ---- the panel ------------------------------------------------------------------------------

def test_the_panel_splits_the_card_by_model_and_labels_each_process(node):
    _put(node)
    _put(node, "reranker", gpu_layers=0)            # on the CPU: none of the card
    _run(node)
    _run(node, "reranker")
    gpu = node.http.get("/louise/gpu").json()
    memory = gpu["memory"]["xpu"]
    assert memory["by_model_mib"] == {"bge-m3": 1400}
    assert memory["own_mib"] == 1400 and memory["used_mib"] == 9000
    assert memory["foreign_mib"] == 7600, "what is left over is a stranger's"
    assert {"pid": node.fake.loaded["bge-m3"], "model": "bge-m3", "device": "xpu"} in \
        gpu["processes"]
    assert {"pid": node.fake.loaded["reranker"], "model": "reranker", "device": "cpu"} in \
        gpu["processes"]


def test_the_events_are_what_rage_saw_between_two_readings(node):
    _put(node)
    _run(node)
    node.fake.shutdown("bge-m3")                    # LocalAI evicted it, on its own
    node.http.portal.call(node.observer.refresh)
    kinds = [e["kind"] for e in node.http.get("/louise/gpu").json()["events"]
             if e["model"] == "bge-m3"]
    assert kinds[-2:] == ["loaded", UNLOADED]


# ---- the parts -----------------------------------------------------------------------------

def test_the_options_keep_localais_digits_and_name_the_hosts_paths_by_placeholder():
    raw = loadopts.parse(b'{"ModelFile": "/models/m.gguf", "ModelPath": "/models", '
                         b'"Model": "m.gguf", "RopeFreqBase": 1e-05, "YarnExtFactor": 10000.50, '
                         b'"Big": 1E+6, "ContextSize": 8192, "Options": ["parallel:1"]}')
    canon = loadopts.canonical(raw)
    assert canon.weights_path == "/models/m.gguf"
    assert canon.options == {"ModelFile": "${WEIGHTS}", "ModelPath": "${MODELS}",
                             "Model": "m.gguf", "RopeFreqBase": "0.00001",
                             "YarnExtFactor": "10000.5", "Big": "1000000",
                             "ContextSize": 8192, "Options": ["parallel:1"]}


def test_options_read_as_floats_are_refused_rather_than_re_rendered():
    with pytest.raises(TypeError, match="loadopts.parse"):
        loadopts.canonical({"ModelFile": "/m/x.gguf", "RopeFreqBase": 10000.5})
    assert isinstance(loadopts.parse("1.5"), Decimal)


@pytest.mark.parametrize("raw,why", [
    ({}, "no load options"),
    ({"ModelFile": "relative.gguf"}, "no weights file"),
    ({"ModelFile": "/m/x.gguf", "LoraAdapter": "adapter.bin"}, "second file"),
    ({"ModelFile": "/m/x.gguf", "Tokenizer": "/m/tok"}, "second file"),
    ({"ModelFile": "/m/x.gguf", "EngineArgs": "--cache=/tmp/k"}, "absolute path"),
])
def test_a_load_that_cannot_be_written_as_options_states_no_run(raw, why):
    with pytest.raises(loadopts.Unstatable, match=why):
        loadopts.canonical(raw)


def test_rage_toml_with_localai_declares_the_cards_and_nothing_of_the_lifecycle(tmp_path):
    path = tmp_path / "rage.toml"
    path.write_text(RAGE_TOML + '\n[models.bge-m3]\ndevice = "xpu"\n')
    with pytest.raises(RageConfigError, match="LocalAI's YAML"):
        localai_config.load(path)
    path.write_text(f'[rage]\ndevice_names = {{ xpu = "{CARD}" }}\n')
    with pytest.raises(RageConfigError, match="pci.xpu"):
        localai_config.load(path)
    path.write_text(RAGE_TOML)
    assert localai_config.load(path).card_at(PCI) == "xpu"


def test_localai_is_started_keyless_on_loopback_with_the_10_switches_forced(tmp_path):
    argv, env = localai_child({"PATH": "/usr/bin", "LOCALAI_API_KEY": "a-key",
                               "LOCALAI_DISABLE_GUESSING": "false"},
                              state=tmp_path, models_dir=tmp_path / "models", port=8081)
    assert argv == ["/usr/bin/local-ai", "run"]
    assert env["LOCALAI_ADDRESS"] == "127.0.0.1:8081"
    assert "LOCALAI_API_KEY" not in env, "keyless on loopback: RAGE's front is the door"
    assert {k: env[k] for k in LOCALAI_PINNED_ENV} == LOCALAI_PINNED_ENV, \
        "forced on, whatever the deployment says"
    assert env["HF_HOME"] == str(tmp_path / "hf-home"), "outside the read-only models"


def test_the_localai_front_refuses_to_start_without_its_models_directory(tmp_path, capsys):
    config = tmp_path / "rage.toml"
    config.write_text(RAGE_TOML)
    assert main_localai({"RAGE_CONFIG": str(config), "RAGE_STATE_DIR": str(tmp_path)},
                        IMAGE, None) == 2
    assert "LOCALAI_MODELS_DIR" in capsys.readouterr().err
