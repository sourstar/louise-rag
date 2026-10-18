"""The run identity (app/vega/runs.py) — docs/09-formats.md §9.3 and §9.12.

What is hashed is never the raw argument vector or environment: those carry
a port, a path and a container's host name, so every restart would be
another run and re-gate every space. The observer writes them with
placeholders (app/vega/loadopts.py); here, the parts every run
is made of — the image, the weights digest, the driver — and the hash.
"""
import hashlib

import pytest

from app.hold.codex import canonical_json, digest
from app.vega import runs
from app.vega.procfs import ProcFS
#: the digest a deployment pins as its image (09 §9.12)
IMAGE = "sha256:" + "ab" * 32


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


# ---- the run -----------------------------------------------------------------------

def _run(*, image=IMAGE, weights="sha256:" + "11" * 32, driver="kernel 6.18; xe 9F",
         args=("./backend", "--addr", "127.0.0.1:${PORT}")):
    engine = {"name": "localai/llama-cpp", "build": "v", "variant": None, "image": image,
              "args": list(args), "env": {}, "device": "a card",
              "options": {"ModelFile": "${WEIGHTS}"}}
    return runs.run_object(driver=driver, engine=engine, weights=weights)


def test_the_run_is_its_three_parts_and_its_id_their_hash():
    run = _run()
    assert sorted(run) == ["driver", "engine", "weights"]
    assert runs.run_id(run) == digest(canonical_json(run))
    assert runs.engine_id(run["engine"]) == digest(canonical_json(run["engine"]))


def test_each_thing_the_spec_names_changes_the_run_and_nothing_else_does():
    same = runs.run_id(_run())
    assert runs.run_id(_run()) == same
    for changed in (_run(image="sha256:" + "cd" * 32), _run(weights="sha256:" + "22" * 32),
                    _run(driver="kernel 6.19; xe 9F"), _run(args=("./backend", "--other"))):
        assert runs.run_id(changed) != same


# ---- procfs ---------------------------------------------------------------------

def test_card_memory_is_counted_once_per_drm_client_and_per_card(tmp_path):
    """One client held through two descriptors is counted once; a process on
    no card is an empty mapping (a CPU process); a gone one is None."""
    fdinfo = tmp_path / "7" / "fdinfo"
    fdinfo.mkdir(parents=True)
    for fd in ("3", "4"):
        (fdinfo / fd).write_text("drm-client-id: 12\ndrm-pdev: 0000:03:00.0\n"
                                 "drm-resident-vram0: 1433600 KiB\n")
    (fdinfo / "5").write_text("pos: 0\n")
    (tmp_path / "8" / "fdinfo").mkdir(parents=True)
    fs = ProcFS(tmp_path)
    assert fs.card_mib_by_device(7) == {"0000:03:00.0": 1400}
    assert fs.card_mib_by_device(8) == {}
    assert fs.card_mib_by_device(99999) is None and fs.cmdline(99999) == [], \
        "a gone process is not an error"


def test_a_process_on_wsls_gpu_is_unreadable_never_a_cpu_process(tmp_path):
    """/dev/dxg (WSL) has no DRM clients: an empty mapping would state a run
    on the card as a CPU run."""
    (tmp_path / "9" / "fdinfo").mkdir(parents=True)
    (tmp_path / "9" / "fdinfo" / "3").write_text("pos: 0\n")
    (tmp_path / "9" / "fd").mkdir()
    try:
        (tmp_path / "9" / "fd" / "3").symlink_to("/dev/dxg")
    except OSError:
        pytest.skip("no symlinks here")
    assert ProcFS(tmp_path).card_mib_by_device(9) is None


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


def test_runtime_lines_given_stand_in_for_the_images_file(tmp_path):
    """VeGa's wrapper runs in LocalAI's image, which nobody here built: it
    reads the runtime from dpkg's status file and hands the lines in."""
    host = _host(tmp_path, {ARC: "xe"}, {"xe": ("srcversion", "9F2C1A")})
    assert runs.driver_string("xpu", pci=ARC, **host, runtime_lines=["libze1  1.21.9", ""]) == \
        "kernel 6.18.0-production; xe 9F2C1A; libze1 1.21.9"


def test_several_drivers_and_no_address_states_no_run_and_says_what_to_declare(tmp_path):
    host = _host(tmp_path, {ARC: "xe", RADEON: "amdgpu"},
                 {"xe": ("srcversion", "9F2C1A"), "amdgpu": ("version", "6.12")})
    with pytest.raises(runs.DriverUnknown, match=r"`cards` does not say which card `xpu`"):
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
