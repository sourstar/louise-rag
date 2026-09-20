"""The run identity (app/rage/runs.py) — docs/09-formats.md §9.3 and §9.12
as amended 8bbc66a.

The first design hashed "the exact argv and env" of the process. Those carry
the port llama-swap picked, the weights' path and the container's host name,
so every restart would have been another run and re-gated every space on
the node. What is hashed is the ENTRY'S template and declared environment;
what keeps that honest is that the process is checked against them first.
"""
import hashlib

import pytest

from app.hold.codex import canonical_json, digest
from app.rage import model, runs
from app.rage.procfs import ProcFS
from tests.rage.conftest import IMAGE


# ---- the image ---------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "rage:latest", "ghcr.io/x/rage:v1",
                                   "sha256:abc", "SHA256:" + "ab" * 32])
def test_a_tag_or_nothing_refuses_to_start_and_names_the_variable(value):
    env = {} if value is None else {"RAGE_IMAGE": value}
    with pytest.raises(runs.BootError, match="RAGE_IMAGE") as e:
        runs.boot_image(env)
    assert "09-formats.md" in str(e.value)


def test_a_digest_is_the_image():
    assert runs.boot_image({"RAGE_IMAGE": IMAGE}) == IMAGE


# ---- the weights digest (09 §9.3) ---------------------------------------------

def test_one_file_is_the_hash_of_its_bytes(tmp_path):
    f = tmp_path / "w.gguf"
    f.write_bytes(b"GGUF bytes")
    assert runs.weights_digest(f) == "sha256:" + hashlib.sha256(b"GGUF bytes").hexdigest()


def test_a_directory_is_the_manifest_of_its_files_code_included(tmp_path):
    d = tmp_path / "dnabert2"
    (d / "sub").mkdir(parents=True)
    (d / "model.safetensors").write_bytes(b"tensors")
    (d / "bert_layers.py").write_text("def forward(): ...")      # the model's remote code
    (d / "sub" / "tokenizer.json").write_text("{}")
    manifest = {"bert_layers.py": digest(b"def forward(): ..."),
                "model.safetensors": digest(b"tensors"),
                "sub/tokenizer.json": digest(b"{}")}
    assert runs.weights_digest(d) == digest(canonical_json(manifest))

    before = runs.weights_digest(d)
    (d / "bert_layers.py").write_text("def forward(): return 1")
    assert runs.weights_digest(d) != before, "the code is part of the function"


def test_a_symbolic_link_is_refused(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "real.bin").write_bytes(b"x")
    try:
        (d / "link.bin").symlink_to(d / "real.bin")
    except OSError:
        pytest.skip("no symlinks here")
    with pytest.raises(runs.WeightsError, match="symbolic link"):
        runs.weights_digest(d)


def test_an_empty_directory_names_no_model(tmp_path):
    (tmp_path / "m").mkdir()
    with pytest.raises(runs.WeightsError, match="holds no file"):
        runs.weights_digest(tmp_path / "m")


# ---- the engine and the run -----------------------------------------------------

def _run(rage, *, image=IMAGE, device="xpu", weights="sha256:" + "11" * 32,
         driver="kernel 6.18; xe 9F"):
    entry = model.load(rage.config).models["bge-m3"]
    engine = runs.engine_object(entry, device, image=image,
                                device_name="Intel(R) Arc(TM) Pro B70 Graphics")
    return engine, runs.run_object(driver=driver, engine=engine, weights=weights)


def test_the_engine_is_09_9_3s_object_with_its_placeholders_unsubstituted(rage):
    engine, run = _run(rage)
    assert sorted(engine) == ["args", "build", "device", "env", "image", "name", "variant"]
    assert engine["args"] == ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--embedding",
                              "-ngl", "99", "--port", "${PORT}"]
    assert engine["env"] == {"LD_LIBRARY_PATH": "/opt/llama/lib"}, "declared, nothing more"
    assert engine["image"] == IMAGE
    assert sorted(run) == ["driver", "engine", "weights"]
    assert runs.run_id(run) == digest(canonical_json(run))
    assert runs.engine_id(engine) == digest(canonical_json(engine))


def test_each_thing_the_spec_names_changes_the_run_and_nothing_else_does(rage):
    _, base = _run(rage)
    same = runs.run_id(_run(rage)[1])
    assert runs.run_id(base) == same
    for changed in (_run(rage, image="sha256:" + "cd" * 32),
                    _run(rage, device="cpu"),                     # another template
                    _run(rage, weights="sha256:" + "22" * 32),
                    _run(rage, driver="kernel 6.19; xe 9F")):
        assert runs.run_id(changed[1]) != same


def test_the_driver_is_read_not_declared(rage, tmp_path):
    p = rage.placement()
    assert p._driver("cpu") == "cpu"
    assert p._driver("xpu") == ("kernel 6.18.0-production; xe 9F2C1A; "
                                "libze-intel-gpu1 26.05.1; intel-opencl-icd 26.05.1")


# ---- is the process the engine we are about to name -----------------------------

def test_the_process_is_matched_whatever_port_and_host_name_it_got(rage):
    entry = model.load(rage.config).models["bge-m3"]
    rage.hostname = "container-A"
    rage.launch("bge-m3@xpu")
    found = runs.match(ProcFS(rage.proc), entry, "xpu")
    assert found.ok and found.port == 10001 and found.pid == rage.running["bge-m3@xpu"]

    rage.kill("bge-m3@xpu")
    rage.hostname = "container-B"                   # a restarted container
    rage.launch("bge-m3@xpu")                       # and another port
    again = runs.match(ProcFS(rage.proc), entry, "xpu")
    assert again.ok and again.port == 10002


def test_another_flag_is_another_engine_so_no_run_is_stated(rage):
    entry = model.load(rage.config).models["bge-m3"]
    rage.launch("bge-m3@cpu")                       # -ngl 0 is running
    found = runs.match(ProcFS(rage.proc), entry, "xpu")     # -ngl 99 is asked about
    assert not found.ok and "no process" in found.reason


def test_a_declared_variable_the_process_lacks_states_no_run(rage):
    entry = model.load(rage.config).models["bge-m3"]
    argv = [entry.weights if a == "${WEIGHTS}" else "10001" if a == "${PORT}" else a
            for a in entry.template("xpu")]
    rage.spawn(argv, {"LD_LIBRARY_PATH": "/somewhere/else"})
    found = runs.match(ProcFS(rage.proc), entry, "xpu")
    assert not found.ok and "LD_LIBRARY_PATH" in found.reason


def test_two_processes_are_not_one_run(rage):
    entry = model.load(rage.config).models["bge-m3"]
    rage.launch("bge-m3@xpu")
    rage.running.pop("bge-m3@xpu")
    rage.launch("bge-m3@xpu")
    found = runs.match(ProcFS(rage.proc), entry, "xpu")
    assert not found.ok and "2 processes" in found.reason


# ---- procfs ---------------------------------------------------------------------

def test_card_memory_is_counted_once_per_drm_client(rage):
    """The harness gives every child ONE client through two descriptors, as
    llama-server holds them; summing per descriptor would double it."""
    pid = rage.launch("bge-m3@xpu")
    fs = ProcFS(rage.proc)
    assert fs.vram_mib(pid) == 1400 and fs.rss_mib(pid) == 500
    assert fs.set_oom_score_adj(pid, 800)
    assert (rage.proc / str(pid) / "oom_score_adj").read_text() == "800"
    assert fs.exe_digest(pid) == digest(b"the program")
    assert fs.cmdline(99999) == [] and fs.rss_mib(99999) is None, "a gone process is not an error"


# ---- the driver is the one the card is bound to ------------------------------------
#
# Operator, 2026-09-20: "RAGE should be independent from a specific
# manufacturer." The driver in a run used to be "the first GPU module loaded"
# from a fixed list. The reference node loads TWO — `xe` for its Arc and
# `amdgpu` for the processor's own graphics — and read `xe` by luck of order.

def _host(tmp_path, cards: dict[str, str], versions: dict[str, tuple[str, str]]):
    """A /sys with these PCI cards bound to these drivers, and these modules."""
    sys_root, proc_root = tmp_path / "sys", tmp_path / "proc"
    (proc_root / "sys" / "kernel").mkdir(parents=True)
    (proc_root / "sys" / "kernel" / "osrelease").write_text("6.18.0-production\n")
    for module, (fname, value) in versions.items():
        (sys_root / "module" / module).mkdir(parents=True)
        if fname:
            (sys_root / "module" / module / fname).write_text(value + "\n")
        (sys_root / "bus" / "pci" / "drivers" / module).mkdir(parents=True)
    for address, driver in cards.items():
        device = sys_root / "bus" / "pci" / "devices" / address
        device.mkdir(parents=True)
        if driver:
            (device / "driver").symlink_to(sys_root / "bus" / "pci" / "drivers" / driver)
    runtime = tmp_path / "runtime-versions"
    runtime.write_text("libfoo 1.2\n")
    return {"sys_root": sys_root, "proc_root": proc_root, "runtime_versions": runtime}


ARC, RADEON, GEFORCE = "0000:03:00.0", "0000:0d:00.0", "0000:01:00.0"


def test_the_driver_is_the_one_the_declared_card_is_bound_to(tmp_path):
    """The reference node itself: an Intel card and AMD graphics, both loaded."""
    host = _host(tmp_path, {ARC: "xe", RADEON: "amdgpu"},
                 {"xe": ("srcversion", "9F2C1A"), "amdgpu": ("version", "6.12")})
    assert runs.driver_string("xpu", pci=ARC, **host) == \
        "kernel 6.18.0-production; xe 9F2C1A; libfoo 1.2"
    assert runs.driver_string("igpu", pci=RADEON, **host) == \
        "kernel 6.18.0-production; amdgpu 6.12; libfoo 1.2"


def test_an_nvidia_card_beside_amd_graphics_is_stated_as_nvidia(tmp_path):
    """The case the fixed list got WRONG: `amdgpu` comes before `nvidia` in
    it, so the NVIDIA card's runs would have named the AMD driver."""
    host = _host(tmp_path, {GEFORCE: "nvidia", RADEON: "amdgpu"},
                 {"nvidia": ("version", "575.64.03"), "amdgpu": ("version", "6.12")})
    assert runs.driver_string("cuda", pci=GEFORCE, **host) == \
        "kernel 6.18.0-production; nvidia 575.64.03; libfoo 1.2"


def test_a_module_with_no_version_file_is_named_alone(tmp_path):
    """The reference node's `xe` has neither `version` nor `srcversion`."""
    host = _host(tmp_path, {ARC: "xe"}, {"xe": ("", "")})
    assert runs.driver_string("xpu", pci=ARC, **host) == \
        "kernel 6.18.0-production; xe; libfoo 1.2"


def test_several_drivers_and_no_address_states_no_run_and_says_what_to_declare(tmp_path):
    host = _host(tmp_path, {ARC: "xe", RADEON: "amdgpu"},
                 {"xe": ("srcversion", "9F2C1A"), "amdgpu": ("version", "6.12")})
    with pytest.raises(runs.DriverUnknown, match=r"declare \[rage\] pci\.xpu"):
        runs.driver_string("xpu", **host)


def test_one_driver_and_no_address_is_read_as_it_always_was(tmp_path):
    host = _host(tmp_path, {ARC: "xe"}, {"xe": ("srcversion", "9F2C1A")})
    assert runs.driver_string("xpu", **host) == \
        "kernel 6.18.0-production; xe 9F2C1A; libfoo 1.2"


def test_an_address_with_no_card_or_no_driver_is_not_guessed(tmp_path):
    host = _host(tmp_path, {ARC: ""}, {"xe": ("srcversion", "9F2C1A")})
    with pytest.raises(runs.DriverUnknown, match="bound to no kernel driver"):
        runs.driver_string("xpu", pci=ARC, **host)
    with pytest.raises(runs.DriverUnknown, match="no PCI device 0000:99:00.0"):
        runs.driver_string("xpu", pci="0000:99:00.0", **host)
    assert runs.driver_string("cpu", pci=None, **host) == "cpu"


def test_the_cards_address_is_declared_in_rage_toml_and_checked(rage):
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[rage]\n", '[rage]\npci = { xpu = "0000:03:00.0" }\n', 1),
                           encoding="utf-8")
    assert model.load(rage.config).pci == {"xpu": "0000:03:00.0"}
    for bad, why in (('"03:00.0"', "PCI address"), ('"renderD128"', "PCI address"), ("3", "PCI address")):
        rage.config.write_text(text.replace("[rage]\n", f"[rage]\npci = {{ xpu = {bad} }}\n", 1),
                               encoding="utf-8")
        with pytest.raises(model.RageConfigError, match=why):
            model.load(rage.config)
    rage.config.write_text(text.replace("[rage]\n", '[rage]\npci = { cpu = "0000:03:00.0" }\n', 1),
                           encoding="utf-8")
    with pytest.raises(model.RageConfigError, match="not a card"):
        model.load(rage.config)
