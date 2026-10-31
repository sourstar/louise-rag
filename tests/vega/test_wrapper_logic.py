"""deploy/vega/vega_wrapper.py without gRPC: its configuration, the real
backend's identity, the run it states, and its receipts client against the
real intake. The gRPC path is test_wrapper_grpc.py."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.vega import loadopts, runs as runs_mod

from .conftest import ROOT, load_wrapper

vw = load_wrapper()
DIGEST = "sha256:" + "cd" * 32


def _backend_dir(tmp_path, meta=None, name="fake-embed"):
    d = tmp_path / "backends" / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\n")
    meta = {"name": name, "version": "v4.11.0", "uri": "quay.io/x:y", "digest": DIGEST} \
        if meta is None else meta
    (d / "metadata.json").write_text(json.dumps(meta))
    return d


def _config(tmp_path, **kw):
    doc = {"backend": "fake-embed", "receipts_socket": "/vega/r.sock"} | kw
    p = tmp_path / "vega.json"
    p.write_text(json.dumps(doc))
    return p


def test_the_config_names_the_backend_beside_the_wrapper(tmp_path):
    cfg = vw.load_config(_config(tmp_path, cards={"0000:03:00.0": "Arc"}), tmp_path / "backends")
    assert cfg.backend == tmp_path / "backends" / "fake-embed"
    assert cfg.socket == Path("/vega/r.sock") and cfg.cards == {"0000:03:00.0": "Arc"}
    assert cfg.runtime_packages == vw.RUNTIME_PACKAGES
    assert cfg.cuts == vw.TOKENIZER, "the wrapper cuts, unless the backend says it does"


@pytest.mark.parametrize("doc, why", [
    ({"receipts_socket": "/r"}, "`backend`"),
    ({"backend": "b", "receipts_socket": "relative"}, "absolute"),
    ({"backend": "b", "receipts_socket": "/r", "cards": {"3:0": "Arc"}}, "PCI address"),
    ({"backend": "b", "receipts_socket": "/r", "colour": 1}, "unknown key"),
    ({"backend": "b", "receipts_socket": "/r", "cuts": "characters"}, "`cuts` is one of"),
])
def test_a_config_that_is_not_valid_is_refused_with_its_rule(tmp_path, doc, why):
    p = tmp_path / "vega.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(vw.ConfigError, match=why):
        vw.load_config(p, tmp_path)


def test_the_install_record_names_the_backend_and_its_digest_is_only_informational(tmp_path):
    b = vw.real_backend(_backend_dir(tmp_path))
    assert (b.name, b.version, b.uri, b.registry_digest) == \
        ("fake-embed", "v4.11.0", "quay.io/x:y", DIGEST)
    assert b.run_sh.is_absolute() and b.run_sh.name == "run.sh"
    assert not hasattr(b, "image"), "the image is the tree on disk, read at every load"


def test_a_backend_installed_by_hand_is_named_by_its_directory(tmp_path):
    d = _backend_dir(tmp_path)
    (d / "metadata.json").unlink()
    b = vw.real_backend(d)
    assert (b.name, b.version, b.uri, b.registry_digest) == ("fake-embed", None, None, None)


@pytest.mark.parametrize("text, why", [("{not json", "cannot be read"), ("[1]", "not an object")])
def test_an_install_record_that_cannot_be_read_names_nothing(tmp_path, text, why):
    d = _backend_dir(tmp_path)
    (d / "metadata.json").write_text(text)
    with pytest.raises(loadopts.Unstatable, match=why):
        vw.real_backend(d)


def test_a_directory_without_run_sh_is_no_backend(tmp_path):
    d = _backend_dir(tmp_path)
    (d / "run.sh").unlink()
    with pytest.raises(loadopts.Unstatable, match="no run.sh"):
        vw.real_backend(d)


# ---- the tree digest: the engine's image is the bytes installed --------------------------

def _tree(tmp_path):
    d = tmp_path / "tree"
    (d / "lib").mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\nexec ./bin \"$@\"\n")
    (d / "run.sh").chmod(0o755)
    (d / "bin").write_bytes(b"\x7fELF binary")
    (d / "bin").chmod(0o755)
    (d / "lib" / "libx.so.1.2.3").write_bytes(b"library bytes")
    (d / "lib" / "libx.so.1").symlink_to("libx.so.1.2.3")
    (d / "metadata.json").write_text('{"digest": "sha256:' + "cd" * 32 + '"}')
    return d


def test_the_tree_digest_is_its_definition(tmp_path):
    """vega.md §9 and `tree_digest`'s docstring, written out by hand."""
    import hashlib
    from app.hold.codex import canonical_json

    d = _tree(tmp_path)

    def sha(b):
        return hashlib.sha256(b).hexdigest()
    manifest = {
        "bin": {"type": "file", "size": 11, "sha256": sha(b"\x7fELF binary")},
        "lib": {"type": "dir"},
        "lib/libx.so.1": {"type": "link", "target": "libx.so.1.2.3"},
        "lib/libx.so.1.2.3": {"type": "file", "size": 13,
                              "sha256": sha(b"library bytes")},
        "run.sh": {"type": "file", "size": (d / "run.sh").stat().st_size,
                   "sha256": sha((d / "run.sh").read_bytes())},
    }
    assert vw.tree_digest(d) == "sha256:" + sha(canonical_json(manifest))


def test_the_tree_digest_is_deterministic_and_ignores_the_install_record(tmp_path):
    d = _tree(tmp_path)
    first = vw.tree_digest(d)
    assert vw.tree_digest(d) == first == vw.tree_digest(d, vw.FileDigests())
    (d / "metadata.json").write_text('{"installed_at": "later", "digest": "another"}')
    assert vw.tree_digest(d) == first, "a reinstall of the same bytes is the same engine"
    other = tmp_path / "copy"
    import shutil
    shutil.copytree(d, other, symlinks=True)
    assert vw.tree_digest(other) == first, "the bytes, not where they sit"
    (d / "lib" / "metadata.json").write_text("{}")
    assert vw.tree_digest(d) != first, "only the top-level install record is left out"


@pytest.mark.parametrize("change", ["content", "new file", "symlink target",
                                    "empty dir", "rename"])
def test_the_tree_digest_sees_every_change(tmp_path, change):
    d = _tree(tmp_path)
    before = vw.tree_digest(d)
    if change == "content":
        (d / "lib" / "libx.so.1.2.3").write_bytes(b"library BYTES")      # same size
    elif change == "new file":
        (d / "lib" / "liby.so").write_bytes(b"")
    elif change == "symlink target":
        (d / "lib" / "libx.so.1").unlink()
        (d / "lib" / "libx.so.1").symlink_to("./libx.so.1.2.3")
    elif change == "empty dir":
        (d / "lib" / "rocblas").mkdir()
    else:
        (d / "bin").rename(d / "bin2")
    assert vw.tree_digest(d) != before


def test_the_tree_digest_ignores_owner_time_and_other_mode_bits(tmp_path):
    import os
    d = _tree(tmp_path)
    before = vw.tree_digest(d)
    (d / "bin").chmod(0o700)
    # the execute bit too (09 §9.3, amended 2026-10-13): ZFS on an NFSv4-ACL
    # dataset reported it differently across a reboot on untouched files
    (d / "lib" / "libx.so.1.2.3").chmod(0o744)
    (d / "run.sh").chmod(0o644)
    os.utime(d / "lib" / "libx.so.1.2.3", ns=(1, 1))
    assert vw.tree_digest(d) == before


def test_a_tree_holding_a_socket_has_no_digest(tmp_path):
    import socket
    d = _tree(tmp_path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(d / "lib" / "s.sock"))
    try:
        with pytest.raises(loadopts.Unstatable, match="neither a file"):
            vw.tree_digest(d)
    finally:
        s.close()


def test_unchanged_files_are_not_read_again(tmp_path):
    import os
    d = _tree(tmp_path)
    files = vw.FileDigests()
    first = vw.tree_digest(d, files)
    assert sorted(files.reads) == sorted(os.path.realpath(d / p) for p in
                                         ("bin", "run.sh", "lib/libx.so.1.2.3"))
    files.reads.clear()
    assert vw.tree_digest(d, files) == first and files.reads == [], "every file a cache hit"
    (d / "lib" / "liby.so").write_bytes(b"new")
    vw.tree_digest(d, files)
    assert files.reads == [os.path.realpath(d / "lib" / "liby.so")], "only the new file read"


def test_the_cache_holds_to_the_physical_file_not_its_name(tmp_path):
    import hashlib
    import os
    d = _tree(tmp_path)
    files = vw.FileDigests()
    lib = d / "lib" / "libx.so.1.2.3"
    files.read(lib)
    files.reads.clear()
    assert files.read(d / "lib" / "libx.so.1") == files.read(lib), "a link to it is the file"
    assert files.reads == []
    # an upgrade replaces the file: same name, size and mtime, another inode
    st = lib.stat()
    new = d / "lib" / "incoming"
    new.write_bytes(b"LIBRARY bytes")
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(new, lib)
    assert files.read(lib) == (13, hashlib.sha256(b"LIBRARY bytes").hexdigest())
    assert files.reads == [os.path.realpath(lib)], "read again: not the file it read before"
    # a write in place, its mtime set back: the ctime still moves (the
    # kernel's clock for file times is coarse: one tick apart, not less)
    time.sleep(0.05)
    st = lib.stat()
    lib.write_bytes(b"library bytez")
    os.utime(lib, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert files.read(lib) == (13, hashlib.sha256(b"library bytez").hexdigest())


def test_the_build_is_the_declared_version_else_the_trees_short_form():
    assert vw.short_build("sha256:" + "ab" * 32) == "ab" * 6, "12 hex digits, no prefix"


# ---- the driver the backend loads ---------------------------------------------------------

def _proc_environ(root, pid, env):
    p = root / str(pid)
    p.mkdir(parents=True, exist_ok=True)
    (p / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))


def _bundled(tmp_path):
    d = tmp_path / "backend"
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "libze_intel_gpu.so.1.15.39122").write_bytes(b"the carried driver")
    (d / "lib" / "libze_intel_gpu.so.1").symlink_to("libze_intel_gpu.so.1.15.39122")
    (d / "lib" / "libze_loader.so.1").write_bytes(b"the loader")
    return d


def test_the_driver_is_the_one_the_backends_environment_names(tmp_path):
    import hashlib
    d = _bundled(tmp_path)
    proc = tmp_path / "proc"
    named = d / "lib" / "libze_intel_gpu.so.1"
    _proc_environ(proc, 10, {"PATH": "/bin", vw.ALT_DRIVERS_VARIABLE: str(named)})
    libs = vw.driver_libraries(d, [10], {}, proc)
    assert libs == [named]
    assert vw.driver_lines(libs, vw.FileDigests()) == [
        "libze_intel_gpu.so.1.15.39122 sha256:"
        + hashlib.sha256(b"the carried driver").hexdigest()]


def test_a_driver_the_operator_names_outside_the_backend_is_that_one(tmp_path):
    d = _bundled(tmp_path)
    host = tmp_path / "host" / "libze_intel_gpu.so.1.26.0"
    host.parent.mkdir()
    host.write_bytes(b"the host's driver")
    proc = tmp_path / "proc"
    _proc_environ(proc, 10, {})                       # run.sh, which execs …
    _proc_environ(proc, 11, {vw.ALT_DRIVERS_VARIABLE: f"{host},"})   # … or starts it
    assert vw.driver_libraries(d, [10, 11], {}, proc) == [host]


def test_without_the_variable_the_carried_driver_is_the_one(tmp_path):
    d = _bundled(tmp_path)
    proc = tmp_path / "proc"
    _proc_environ(proc, 10, {"PATH": "/bin"})
    assert vw.driver_libraries(d, [10], {}, proc) == [
        (d / "lib" / "libze_intel_gpu.so.1.15.39122").resolve()]


def test_without_either_the_run_reads_the_driver_as_before(tmp_path):
    d = tmp_path / "plain"
    (d / "lib").mkdir(parents=True)
    proc = tmp_path / "proc"
    _proc_environ(proc, 10, {"PATH": "/bin"})
    assert vw.driver_libraries(d, [10], {}, proc) == []
    assert vw.driver_libraries(tmp_path / "no-lib", [10], {}, proc) == []


def test_an_unreadable_environment_falls_back_to_the_one_given(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    given = tmp_path / "given.so.1"
    assert vw.driver_libraries(d, [10], {vw.ALT_DRIVERS_VARIABLE: str(given)},
                               tmp_path / "noproc") == [given]
    proc = tmp_path / "proc"
    _proc_environ(proc, 10, {})
    assert vw.driver_libraries(d, [10], {vw.ALT_DRIVERS_VARIABLE: str(given)}, proc) == [], \
        "what the backend runs with wins over what it was given"


@pytest.mark.parametrize("setup, why", [
    ("relative", "relative path"), ("disagree", "different drivers"), ("two carried", "several")])
def test_a_driver_that_cannot_be_told_states_no_run(tmp_path, setup, why):
    d = _bundled(tmp_path)
    proc = tmp_path / "proc"
    pids = [10]
    if setup == "relative":
        _proc_environ(proc, 10, {vw.ALT_DRIVERS_VARIABLE: "lib/libze_intel_gpu.so.1"})
    elif setup == "disagree":
        _proc_environ(proc, 10, {vw.ALT_DRIVERS_VARIABLE: "/a.so"})
        _proc_environ(proc, 11, {vw.ALT_DRIVERS_VARIABLE: "/b.so"})
        pids = [10, 11]
    else:
        _proc_environ(proc, 10, {})
        (d / "lib" / "libze_intel_gpu.so.1.16.0").write_bytes(b"another")
    with pytest.raises(loadopts.Unstatable, match=why):
        vw.driver_libraries(d, pids, {}, proc)


def test_a_named_driver_that_is_missing_states_no_run(tmp_path):
    with pytest.raises(loadopts.Unstatable, match="cannot be read"):
        vw.driver_lines([tmp_path / "gone.so.1"], vw.FileDigests())


def test_the_runtime_is_read_from_dpkgs_status_without_dpkg(tmp_path):
    status = tmp_path / "status"
    status.write_text("Package: libze1\nStatus: install ok installed\nVersion: 1.21.9\n"
                      "Description: x\n continued\n\n"
                      "Package: intel-opencl-icd\nStatus: deinstall ok config-files\n"
                      "Version: 25.0\n\n"
                      "Package: libze-intel-gpu1\nStatus: install ok installed\n"
                      "Version: 25.35.35096.9-1\n")
    assert vw.runtime_lines(vw.RUNTIME_PACKAGES, status) == [
        "libze-intel-gpu1 25.35.35096.9-1", "libze1 1.21.9"]
    assert vw.runtime_lines(vw.RUNTIME_PACKAGES, tmp_path / "absent") == []


def test_the_family_is_the_process_and_its_descendants(tmp_path):
    for pid, kids in {10: "11 12", 11: "13", 12: "", 13: ""}.items():
        task = tmp_path / str(pid) / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(kids)
    assert sorted(vw.family(10, tmp_path)) == [10, 11, 12, 13]


class FakeProc:
    def __init__(self, held):
        self.held = held

    def card_mib_by_device(self, pid):
        return self.held.get(pid)


def test_the_device_is_read_from_the_cards_the_family_holds():
    cards = {"0000:03:00.0": "Intel(R) Arc(TM) Pro B70 Graphics"}
    assert vw.device_of([1], cards, FakeProc({1: {}})) == ("cpu", None)
    assert vw.device_of([1, 2], cards, FakeProc({1: {}, 2: {"0000:03:00.0": 900}})) == \
        ("Intel(R) Arc(TM) Pro B70 Graphics", "0000:03:00.0")
    assert vw.device_of([1], cards, FakeProc({1: {None: 5}})) == \
        ("Intel(R) Arc(TM) Pro B70 Graphics", "0000:03:00.0")
    with pytest.raises(loadopts.Unstatable, match="names no device"):
        vw.device_of([1], cards, FakeProc({1: {"0000:04:00.0": 5}}))
    with pytest.raises(loadopts.Unstatable, match="several cards"):
        vw.device_of([1], cards | {"0000:04:00.0": "B"},
                     FakeProc({1: {"0000:03:00.0": 5, "0000:04:00.0": 5}}))
    with pytest.raises(loadopts.Unstatable, match="cannot be read"):
        vw.device_of([1], cards, FakeProc({}))


class FakeDxgProc(FakeProc):
    """A family whose first process holds WSL's /dev/dxg: no DRM clients."""
    def __init__(self, on_dxg):
        super().__init__({})
        self.on_dxg = on_dxg

    def holds(self, pid, device):
        return device == "/dev/dxg" and pid in self.on_dxg


AMD = "0000:03:00.0"
DXG_CARDS = {AMD: "AMD Radeon RX 9060 XT"}


def _dxg(pids, clients, moved, declared, weights=1104.0):
    return vw.device_on_dxg(pids, DXG_CARDS, clients, vram_delta_mib={AMD: moved},
                            weights_mib=weights, declared=declared)


def test_on_wsls_gpu_a_card_run_is_listed_and_moved_the_weights_onto_the_card():
    """bge-m3 F16 (1,104 MiB) on the RX 9060 XT, 2026-10-18: +971 MiB."""
    assert _dxg([1, 2], {2: AMD, 77: AMD}, 971, "card") == ("AMD Radeon RX 9060 XT", AMD)


def test_on_wsls_gpu_a_small_model_whose_token_table_stays_on_the_host_is_a_card_run():
    """granite 107M: the 250k x 384 token table is most of its 211 MiB and
    llama.cpp keeps it in host memory — measured +78 MiB."""
    assert _dxg([1], {1: AMD}, 78, "card", weights=211.0) == ("AMD Radeon RX 9060 XT", AMD)
    with pytest.raises(loadopts.Unstatable, match="disagree"):
        _dxg([1], {1: AMD}, 40, "card", weights=211.0)      # below a quarter: not stated


def test_on_wsls_gpu_a_cpu_twin_is_listed_too_and_is_stated_as_cpu_by_what_it_did_not_move():
    """The measured trap: a backend shown no HIP device (gpu_layers 0) is a
    driver client like a card run. It moved nothing, and its options say CPU."""
    assert _dxg([1], {1: AMD}, 3, "cpu") == ("cpu", None)


@pytest.mark.parametrize("moved, declared, why", [
    (3, "card", "disagree"),         # asked for the card, nothing went there (HIP found no card)
    (971, "cpu", "disagree"),        # said CPU, the weights went to the card
    (300, None, "disagree"),         # gpu_layers 0 with op offload: split
    (-2900, "card", "disagree"),     # another model evicted during the load
    (400, "cpu", "disagree"),        # something else loaded meanwhile
])
def test_on_wsls_gpu_anything_the_two_readings_do_not_agree_on_states_no_run(moved, declared,
                                                                              why):
    with pytest.raises(loadopts.Unstatable, match=why):
        _dxg([1], {1: AMD}, moved, declared)


def test_on_wsls_gpu_the_driver_must_list_the_backend_on_one_named_card():
    with pytest.raises(loadopts.Unstatable, match="lists none of its processes"):
        _dxg([1], {77: AMD}, 971, "card")
    with pytest.raises(loadopts.Unstatable, match="several cards"):
        _dxg([1], {1: "several"}, 971, "card")
    with pytest.raises(loadopts.Unstatable, match="names no device"):
        _dxg([1], {1: "0000:04:00.0"}, 971, "card")
    with pytest.raises(loadopts.Unstatable, match="not read around the load"):
        vw.device_on_dxg([1], DXG_CARDS, {1: AMD}, vram_delta_mib={}, weights_mib=1.0,
                         declared="card")


def test_device_of_asks_the_dxg_reader_only_for_a_backend_on_dev_dxg():
    seen = []

    def reader(pids):
        seen.append(pids)
        return DXG_CARDS[AMD], AMD
    assert vw.device_of([1, 2], DXG_CARDS, FakeDxgProc({1}), reader) == (DXG_CARDS[AMD], AMD)
    assert seen == [[1, 2]]
    with pytest.raises(loadopts.Unstatable, match="cannot be read"):
        vw.device_of([1], DXG_CARDS, FakeDxgProc({1}))                 # no amd_smi: as before
    with pytest.raises(loadopts.Unstatable, match="cannot be read"):
        vw.device_of([1], DXG_CARDS, FakeDxgProc(set()), reader)       # not on /dev/dxg


@pytest.mark.parametrize("layers, options, env, placement", [
    (999, [], {}, "card"),
    (0, ["no_op_offload:true"], {}, "cpu"),
    (0, [], {}, None),                                   # op offload still uses the card
    (999, [], {"HIP_VISIBLE_DEVICES": "-1"}, "cpu"),     # no device to use
    (None, [], {}, None),
    (0, ["task:jina-v3", "device:cuda"], {}, "card"),    # a torch task names its device
    (0, ["task:jina-v3", "device:cuda:1"], {}, "card"),
    (0, ["task:jina-v3", "device:xpu"], {}, "card"),
    (0, ["task:jina-v3", "device:cpu"], {}, "cpu"),
    (0, ["task:jina-v3", "device:cuda"], {"CUDA_VISIBLE_DEVICES": "-1"}, "cpu"),
    (0, ["task:jina-v3", "device:openvino"], {}, None),
    (0, ["device:cuda"], {}, None),                      # llama.cpp reads no `device:`
])
def test_the_options_say_where_the_work_was_asked_to_go(layers, options, env, placement):
    load = {"Model": "m.gguf", "ModelFile": "/models/m.gguf", "Options": options,
            "EnvVars": env}
    if layers is not None:
        load["NGPULayers"] = layers
    assert vw.declared_placement(vw.canon_from_protojson(json.dumps(load))) == placement


@pytest.mark.parametrize("declared, device, refused", [
    ("card", "cpu", "missing or has failed"),       # a card that fell back to the CPU
    ("cpu", "Intel(R) Arc(TM) Pro B70 Graphics", "computes on"),
    ("card", "Intel(R) Arc(TM) Pro B70 Graphics", None),
    ("cpu", "cpu", None),
    (None, "cpu", None),                            # split options: read, not checked
    (None, "Intel(R) Arc(TM) Pro B70 Graphics", None),
])
def test_the_device_read_is_the_one_the_options_asked_for(declared, device, refused):
    if refused is None:
        vw.placement_holds(declared, device)
    else:
        with pytest.raises(loadopts.Unstatable, match=refused):
            vw.placement_holds(declared, device)


def test_the_config_names_amd_smi_by_absolute_path(tmp_path):
    cfg = vw.load_config(_config(tmp_path, amd_smi="/opt/rocm-wsl/bin/amd-smi"),
                         tmp_path / "backends")
    assert cfg.amd_smi == Path("/opt/rocm-wsl/bin/amd-smi")
    assert vw.load_config(_config(tmp_path), tmp_path / "backends").amd_smi is None
    with pytest.raises(vw.ConfigError, match="amd_smi"):
        vw.load_config(_config(tmp_path, amd_smi="amd-smi"), tmp_path / "backends")


#: ModelOptions as protojson with proto names, unset fields omitted: P1's form
LOAD = json.dumps({"Model": "bge-m3.gguf", "ModelFile": "/models/bge-m3.gguf",
                   "ModelPath": "/models", "ContextSize": 8192, "Embeddings": True,
                   "RopeFreqBase": 10000.5, "Options": ["pairs:true"],
                   "EnvVars": {"NEO_CACHE_DIR": "/state/neo-cache"}})
#: a load that holds llama.cpp's determinism contract (one sequence at a time;
#: invariant 12 fail-closed, 2026-10-20) — what the run-stating tests load
LOAD_RUN = json.dumps(json.loads(LOAD) | {"Options": ["pairs:true", "parallel:1"]})


def test_the_options_are_canonical_as_rage_writes_them():
    canon = vw.canon_from_protojson(LOAD)
    assert canon.options == {"Model": "bge-m3.gguf", "ModelFile": "${WEIGHTS}",
                             "ModelPath": "${MODELS}", "ContextSize": 8192,
                             "Embeddings": True, "RopeFreqBase": "10000.5",
                             "Options": ["pairs:true"]}
    assert canon.env == {"NEO_CACHE_DIR": "/state/neo-cache"}
    assert canon.weights_path == "/models/bge-m3.gguf"


def test_the_run_is_what_the_wrapper_launched(tmp_path):
    backend = vw.real_backend(_backend_dir(tmp_path, name="fake-llama-cpp"))
    canon = vw.canon_from_protojson(LOAD_RUN)
    tree = vw.tree_digest(backend.directory)
    rid, run = vw.state_run(backend=backend, canon=canon,
                            environ={"NEO_CACHE_DIR": "/state/neo-cache"}, device="cpu",
                            pci=None, weights="sha256:" + "11" * 32, tree=tree, runtime=[])
    assert rid == runs_mod.run_id(run)
    assert run["driver"] == "cpu" and run["weights"] == "sha256:" + "11" * 32
    assert run["engine"] == {
        "name": "localai/fake-llama-cpp", "build": "v4.11.0", "variant": None, "image": tree,
        "args": ["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"],
        "env": {"NEO_CACHE_DIR": "/state/neo-cache"}, "device": "cpu",
        "options": canon.options}
    assert tree != DIGEST, "never the registry's digest"
    with pytest.raises(loadopts.Unstatable, match="NEO_CACHE_DIR"):
        vw.state_run(backend=backend, canon=canon, environ={}, device="cpu", pci=None,
                     weights="sha256:" + "11" * 32, tree=tree, runtime=[])


def test_the_run_is_a_valid_codex_engine(tmp_path):
    """The run's engine is signed into a calibration statement, whose
    validator reads `image` as `sha256:<64 hex>` (app/hold/codex.py)."""
    from app.hold.codex import _validate_engine, engine_from_dict
    backend = vw.real_backend(_backend_dir(tmp_path, name="fake-llama-cpp"))
    _rid, run = vw.state_run(backend=backend, canon=vw.canon_from_protojson(LOAD_RUN),
                             environ={"NEO_CACHE_DIR": "/state/neo-cache"}, device="cpu",
                             pci=None, weights="sha256:" + "11" * 32,
                             tree=vw.tree_digest(backend.directory), runtime=[])
    _validate_engine(engine_from_dict(run["engine"], "engine"), "engine")


def test_an_upgrade_is_a_new_run_and_a_reinstall_of_the_same_bytes_is_not(tmp_path):
    d = _backend_dir(tmp_path, {"name": "fake-llama-cpp", "uri": "quay.io/x:latest-y",
                                "digest": DIGEST})
    backend, canon = vw.real_backend(d), vw.canon_from_protojson(LOAD_RUN)
    env = {"NEO_CACHE_DIR": "/state/neo-cache"}

    def stated():
        tree = vw.tree_digest(d)
        rid, run = vw.state_run(backend=backend, canon=canon, environ=env, device="cpu",
                                pci=None, weights="sha256:" + "11" * 32, tree=tree, runtime=[])
        return rid, run
    rid, run = stated()
    assert run["engine"]["build"] == vw.short_build(run["engine"]["image"]), \
        "no declared version: the tree's short form, never the uri's moving tag"
    (d / "metadata.json").write_text(json.dumps({"name": "fake-llama-cpp", "uri": "quay.io/x:z",
                                                 "digest": "sha256:" + "ef" * 32}))
    assert stated()[0] == rid
    (d / "run.sh").write_text("#!/bin/sh\n# upgraded\n")
    assert stated()[0] != rid


def test_the_driver_lines_are_the_runs_runtime(tmp_path):
    backend = vw.real_backend(_backend_dir(tmp_path, name="fake-sycl-llama-cpp"))
    sys_root, proc_root = tmp_path / "sys", tmp_path / "proc"
    (sys_root / "module" / "xe").mkdir(parents=True)
    (proc_root / "sys" / "kernel").mkdir(parents=True)
    (proc_root / "sys" / "kernel" / "osrelease").write_text("6.18.0\n")
    _rid, run = vw.state_run(backend=backend, canon=vw.canon_from_protojson(json.dumps(
                                 json.loads(LOAD_RUN) | {"EnvVars": {"NEO_CACHE_DIR": "/state/neo-cache",
                                                                     "GGML_SYCL_ENABLE_DNN": "0",
                                                                     "GGML_SYCL_FA_ONEDNN": "0"}})),
                             environ={"NEO_CACHE_DIR": "/state/neo-cache", "GGML_SYCL_ENABLE_DNN": "0",
                                      "GGML_SYCL_FA_ONEDNN": "0"},
                             device="Arc",
                             pci=None, weights="sha256:" + "11" * 32,
                             tree=vw.tree_digest(backend.directory),
                             runtime=["libze_intel_gpu.so.1.15.39122 sha256:" + "aa" * 32],
                             sys_root=sys_root, proc_root=proc_root)
    assert run["driver"] == "kernel 6.18.0; xe; libze_intel_gpu.so.1.15.39122 sha256:" + "aa" * 32


@pytest.mark.parametrize("name, env", [
    ("rocm-llama-cpp", {"ROCBLAS_DEFAULT_ATOMICS_MODE": "0"}),
    ("intel-sycl-f16-llama-cpp", {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0"}),
    ("cuda12-llama-cpp", {}),              # cuBLAS disallows atomics by default
    ("vulkan-llama-cpp", {}),              # no mode offered
    ("llama-cpp", {}),
])
def test_each_stack_names_its_vendors_deterministic_mode(name, env):
    assert vw.determinism_env(name) == env


def _load_with(env, options=("pairs:true", "parallel:1")):
    return vw.canon_from_protojson(json.dumps(json.loads(LOAD) | {"EnvVars": env, "Options": list(options)}))


@pytest.mark.parametrize("name, env", [
    ("rocm-llama-cpp", {"ROCBLAS_DEFAULT_ATOMICS_MODE": "0"}),
    ("intel-sycl-f16-llama-cpp", {"GGML_SYCL_ENABLE_DNN": "0"}),
    ("intel-sycl-f16-llama-cpp", {"GGML_SYCL_FA_ONEDNN": "0"}),
])
def test_a_card_run_without_its_vendors_deterministic_mode_is_never_stated(tmp_path, name, env):
    """CLAUDE.md invariant 12: in the MODEL's env, so the run states it."""
    backend = vw.real_backend(_backend_dir(tmp_path, name=name))
    kw = dict(backend=backend, device="the card", pci=None, weights="sha256:" + "11" * 32,
              tree=vw.tree_digest(backend.directory), runtime=[],
              card_driver_line="dxg 1")
    (key, value), = env.items()
    rest = {k: v for k, v in vw.determinism_env(name).items() if k != key}
    with pytest.raises(loadopts.Unstatable, match=f"invariant 12.*{key}={value}"):
        vw.state_run(canon=_load_with(rest), environ=rest, **kw)
    with pytest.raises(loadopts.Unstatable, match="invariant 12"):
        vw.state_run(canon=_load_with({key: "1"}), environ={key: "1"}, **kw)
    full = vw.determinism_env(name)
    _rid, run = vw.state_run(canon=_load_with(full), environ=full, **kw)
    assert run["engine"]["env"] == full, "the run says it ran deterministic"
    # the same backend on the CPU uses no card library: nothing is required
    vw.state_run(canon=_load_with({}), environ={}, **(kw | {"device": "cpu"}))


@pytest.mark.parametrize("left, timeout", [(9.223372035063293e18, None), (None, None),
                                           (30.0, 30.0), (0.0, 0.0)])
def test_no_deadline_is_forwarded_as_none(left, timeout):
    """grpcio says "no deadline" as 9.2e18 s left (measured on the node with
    grpcio 1.84.0); passed on as a timeout it is a deadline already past."""
    class Context:
        def time_remaining(self):
            return left
    assert vw.remaining(Context()) == timeout


def test_pairs_are_declared_by_the_option_alone():
    for options, says in (((["pairs:true"]), True), (["device:xpu", "pairs : true"], True),
                          (["pairs:false"], False), ([], False), (None, False)):
        assert vw.declares_pairs(options) is says, options


def test_the_wrapper_venv_pins_the_backends_grpc():
    def pins(path):
        return sorted(line for line in path.read_text().splitlines()
                      if line and not line.startswith("#"))
    assert pins(ROOT / "deploy/vega/requirements.txt") == \
        pins(ROOT / "deploy/localai/backends/requirements-grpc.txt")


# ---- the receipts client against the real intake -------------------------------------

RUN = {"driver": "cpu", "weights": "sha256:" + "11" * 32,
       "engine": {"name": "localai/fake", "build": "v1", "variant": None, "image": DIGEST,
                  "args": [], "env": {}, "device": "cpu", "options": {"M": 1}}}
RUN_ID = runs_mod.run_id(RUN)


def _receipt(i):
    return {"id": f"{i:032x}", "run_id": RUN_ID, "input_digest": "sha256:" + "33" * 32,
            "vector_digest": "sha256:" + "44" * 32}


def test_a_receipt_is_delivered_with_its_run_told_first(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.deliver(_receipt(1), RUN_ID, RUN)
    c.deliver(_receipt(2), RUN_ID, RUN)
    assert vega.runs.get(RUN_ID) == RUN
    assert len(vega.receipts.get([_receipt(1)["id"], _receipt(2)["id"]])) == 2
    c.close()


def test_a_service_that_forgot_the_run_is_told_again(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.publish(RUN_ID, RUN)
    from app.vega.store import RunStore
    vega.runs._known.clear()                 # a service with a fresh state directory
    vega.runs.store = RunStore(vega.socket.parent / "fresh.sqlite")
    c.deliver(_receipt(5), RUN_ID, RUN)
    assert vega.receipts.get([_receipt(5)["id"]]) and vega.runs.known(RUN_ID)


def test_no_service_is_unavailable_within_the_bound(tmp_path):
    c = vw.ReceiptClient(tmp_path / "nobody.sock", timeout_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(vw.Unavailable, match="no acknowledgement within 0.3 s"):
        c.deliver(_receipt(1), RUN_ID, RUN)
    assert time.monotonic() - t0 < 1.5


def test_a_conflicting_receipt_is_refused_not_retried(vega):
    c = vw.ReceiptClient(vega.socket, timeout_s=2)
    c.deliver(_receipt(1), RUN_ID, RUN)
    with pytest.raises(vw.ReceiptRefused, match="one-time"):
        c.deliver(_receipt(1) | {"vector_digest": "sha256:" + "55" * 32}, RUN_ID, RUN)


def test_a_service_that_hangs_is_unavailable_within_the_bound(tmp_path):
    import socket
    path = tmp_path / "hang.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    c = vw.ReceiptClient(path, timeout_s=0.4)
    t0 = time.monotonic()
    with pytest.raises(vw.Unavailable):
        c.deliver(_receipt(1), RUN_ID, RUN)
    assert time.monotonic() - t0 < 1.5
    server.close()


# ---- the tree's per-file list, told to VeGa beside the run (vega.md §12) --------------

def test_the_tree_manifest_is_what_the_digest_hashes(tmp_path):
    """`tree_digest` is H of `tree_manifest`'s canonical JSON, byte for byte,
    so the list VeGa keeps under an image is the list that image names."""
    from app.hold.codex import canonical_json, digest
    d = _tree(tmp_path)
    manifest = vw.tree_manifest(d)
    assert set(manifest) == {"bin", "lib", "lib/libx.so.1", "lib/libx.so.1.2.3", "run.sh"}
    assert manifest["lib"] == {"type": "dir"}
    assert manifest["lib/libx.so.1"] == {"type": "link", "target": "libx.so.1.2.3"}
    assert manifest["bin"]["type"] == "file" and "exec" not in manifest["bin"]
    assert digest(canonical_json(manifest)) == vw.tree_digest(d) == vw.manifest_digest(manifest)
    (d / "lib" / "libnew.so").write_bytes(b"an upgrade")
    after = vw.tree_manifest(d)
    assert set(after) - set(manifest) == {"lib/libnew.so"}
    assert vw.manifest_digest(after) != vw.manifest_digest(manifest)


def test_the_receipts_client_tells_vega_the_tree_once_and_vega_keeps_it(tmp_path, vega):
    """`publish_tree` against the real intake: kept under its digest, readable
    back; told once per tree and process; a list that is not what the digest
    names is refused, never kept."""
    d = _tree(tmp_path)
    manifest = vw.tree_manifest(d)
    image = vw.manifest_digest(manifest)
    c = vw.ReceiptClient(vega.socket)
    c.publish_tree(image, manifest)
    assert vega.runs.get_tree(image) == manifest
    sent = []
    original = c._exchange_line
    c._exchange_line = lambda out, deadline: sent.append(out) or original(out, deadline)
    c.publish_tree(image, manifest)
    assert sent == [], "the same tree again is not sent again"
    with pytest.raises(vw.ReceiptRefused, match="not H of the tree"):
        c.publish_tree("sha256:" + "00" * 32, manifest)
    assert vega.runs.get_tree("sha256:" + "00" * 32) is None
    c.close()


def test_a_tree_retried_never_holds_the_receipts_lock_while_it_waits(tmp_path, monkeypatch):
    """The receipts' lock is the answers' too: an older service that drops the
    list must not hold every receipt back for the tree's whole bound."""
    d = _tree(tmp_path)
    manifest = vw.tree_manifest(d)
    c = vw.ReceiptClient(tmp_path / "nobody.sock")
    held: list = []
    real_sleep = vw.time.sleep
    monkeypatch.setattr(vw.time, "sleep", lambda s: held.append(c._lock.locked()) or real_sleep(min(s, 0.01)))
    with pytest.raises(vw.Unavailable):
        c.publish_tree(vw.manifest_digest(manifest), manifest, timeout_s=0.2)
    assert held and not any(held), "never asleep with the lock"


def test_without_a_receipts_service_the_tree_is_a_deferral_of_its_own(tmp_path):
    d = _tree(tmp_path)
    manifest = vw.tree_manifest(d)
    c = vw.ReceiptClient(tmp_path / "nobody.sock")
    with pytest.raises(vw.Unavailable, match="tree was not acknowledged"):
        c.publish_tree(vw.manifest_digest(manifest), manifest, timeout_s=0.3)


# ---- invariant 12 fail-closed (operator, 2026-10-20): ANY backend ----------------

def _run_kw(tmp_path, name, device="the card"):
    backend = vw.real_backend(_backend_dir(tmp_path, name=name))
    return dict(backend=backend, device=device, pci=None, weights="sha256:" + "11" * 32,
                tree=vw.tree_digest(backend.directory), runtime=[], card_driver_line="dxg 1")


@pytest.mark.parametrize("device", ["the card", "cpu"])
def test_llama_cpp_runs_one_sequence_at_a_time_on_any_device(tmp_path, device):
    """Measured: louise-rerank-bge at parallel:4 moved 16 of 20 scores with
    the documents' order; at parallel:1 none moved."""
    env = {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0"} if device != "cpu" else {}
    kw = _run_kw(tmp_path, "intel-sycl-f16-llama-cpp", device)
    for options in (("parallel:4",), ()):
        with pytest.raises(loadopts.Unstatable, match="invariant 12.*parallel:1"):
            vw.state_run(canon=_load_with(env, options), environ=env, **kw)
    vw.state_run(canon=_load_with(env, ("parallel:1",)), environ=env, **kw)


@pytest.mark.parametrize("name, why", [
    ("vulkan-llama-cpp", "offers no deterministic mode"),
    ("llama-cpp", "names no vendor"),
    ("fake-embed", "no determinism contract"),
    ("stablediffusion-ggml", "no determinism contract"),
])
def test_a_backend_without_a_known_contract_is_refused(tmp_path, name, why):
    with pytest.raises(loadopts.Unstatable, match=why):
        vw.state_run(canon=_load_with({}, ("parallel:1",)), environ={}, **_run_kw(tmp_path, name))


def test_a_cuda_card_run_needs_no_vendor_env(tmp_path):
    vw.state_run(canon=_load_with({}, ("parallel:1",)), environ={},
                 **_run_kw(tmp_path, "cuda12-llama-cpp"))


@pytest.mark.parametrize("device", ["the card", "cpu"])
def test_louise_python_torch_tasks_need_torchs_switch_and_openvino_is_refused(tmp_path, device):
    kw = _run_kw(tmp_path, "louise-python-20261020c", device)
    on = {"LOUISE_TORCH_DETERMINISTIC": "1"}
    for task in ("dar-legal-it", "madlad", "jina-v3"):
        with pytest.raises(loadopts.Unstatable, match="invariant 12.*LOUISE_TORCH_DETERMINISTIC=1"):
            vw.state_run(canon=_load_with({}, (f"task:{task}", "device:xpu")), environ={}, **kw)
        _rid, run = vw.state_run(canon=_load_with(on, (f"task:{task}", "device:xpu")), environ=on, **kw)
        assert run["engine"]["env"] == on
    for task in ("medcpt-article", "medcpt-rerank"):
        with pytest.raises(loadopts.Unstatable, match="no deterministic mode"):
            vw.state_run(canon=_load_with(on, (f"task:{task}", "device:xpu")), environ=on, **kw)
    with pytest.raises(loadopts.Unstatable, match="no deterministic mode"):
        vw.state_run(canon=_load_with(on, ("device:xpu",)), environ=on, **kw)


@pytest.mark.parametrize("task", ["medcpt-article", "medcpt-rerank"])
def test_an_openvino_task_is_stated_only_measured_on_this_very_tree(tmp_path, task):
    """No mode: measured instead (invariant 12). The env names the tree the
    measurement ran on; another tree's name, or none, is refused."""
    kw = _run_kw(tmp_path, "louise-python-20261021a", "the card")
    opts = (f"task:{task}", "device:xpu")
    for env in ({}, {vw.MEASURED_ENV: "louise-python-20261020c"}):
        with pytest.raises(loadopts.Unstatable, match="not measured bit-exact on this tree"):
            vw.state_run(canon=_load_with(env, opts), environ=env, **kw)
    env = {vw.MEASURED_ENV: "louise-python-20261021a"}
    _rid, run = vw.state_run(canon=_load_with(env, opts), environ=env, **kw)
    assert run["engine"]["env"] == env, "the run says what it was measured on"


def test_the_wrapper_is_part_of_the_engine(tmp_path):
    """Operator, 2026-10-31 (choice A): the wrapper decides what the backend is
    given (the cut, the pair's form), and #110 changed it with every run's
    identity unchanged. A backend engine's `image` is the digest of the
    backend's tree AND of the package's code, under `/vega/`, and of `cuts`;
    not of the host's settings, the install record, the kept digests or
    what Python compiles beside the code."""
    backend, pkg = tmp_path / "real", tmp_path / "vega-real"
    (backend / "lib").mkdir(parents=True)
    (backend / "run.sh").write_text("#!/bin/sh\n")
    (pkg / "app" / "vega").mkdir(parents=True)
    (pkg / "vega_wrapper.py").write_text("# the wrapper\n")
    (pkg / "app" / "vega" / "tag.py").write_text("# the tag\n")
    (pkg / "vega.json").write_text('{"receipts_socket": "/run/a.sock"}')
    (pkg / "metadata.json").write_text("{}")
    (pkg / vw.DIGEST_STORE).write_text("{}")
    (pkg / f".{vw.DIGEST_STORE}.123.tmp").write_text("{}")
    (pkg / "venv" / "lib").mkdir(parents=True)
    (pkg / "venv" / "lib" / "grpc.py").write_text("#")
    (pkg / "app" / "__pycache__").mkdir()
    (pkg / "app" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (pkg / "run.sh").write_text("#!/bin/sh\n")
    (pkg / "REVISION").write_text("0ec2239\n")
    (pkg / "neo-cache").mkdir()                  # Intel's kernel cache, where an env names none
    (pkg / "neo-cache" / "kernel.bin").write_bytes(b"\1")
    (pkg / "run").mkdir()                        # the receipts socket's directory
    (pkg / "run" / "stray.log").write_text("x")
    m = vw.engine_manifest(backend, pkg, vw.TOKENIZER)
    wrapper = sorted(k for k in m if k.startswith(vw.WRAPPER_PREFIX))
    assert wrapper == ["/vega/app/vega/tag.py", "/vega/run.sh", "/vega/vega.json#cuts",
                       "/vega/vega_wrapper.py"], "the code alone, by an allow-list"
    assert m["/vega/vega.json#cuts"] == {"type": "setting", "value": vw.TOKENIZER}
    assert {k for k in m if not k.startswith("/")} == set(vw.tree_manifest(backend))
    image = vw.manifest_digest(m)
    assert image != vw.tree_digest(backend)
    # the host's settings are not the engine
    (pkg / "vega.json").write_text('{"receipts_socket": "/run/b.sock"}')
    (pkg / "venv" / "lib" / "grpc.py").write_text("# upgraded")
    (pkg / "neo-cache" / "kernel2.bin").write_bytes(b"\2")     # compiled under load
    (pkg / "run" / "other.txt").write_text("y")
    (pkg / "REVISION").write_text("f70dbf9\n")
    assert vw.manifest_digest(vw.engine_manifest(backend, pkg, vw.TOKENIZER)) == image, \
        "a derived cache, a stray file or a revision note never moves the run"
    # the wrapper's code and its cut are
    assert vw.manifest_digest(vw.engine_manifest(backend, pkg, vw.CONTEXT_SIZE)) != image
    (pkg / "app" / "vega" / "tag.py").write_text("# the tag, changed\n")
    assert vw.manifest_digest(vw.engine_manifest(backend, pkg, vw.TOKENIZER)) != image
    # without a package (a test's Config), the backend's tree alone
    assert vw.engine_manifest(backend, None, vw.TOKENIZER) == vw.tree_manifest(backend)
