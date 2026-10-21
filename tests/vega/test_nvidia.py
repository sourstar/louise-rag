"""NVIDIA support (2026-10-21, before any NVIDIA peer exists): the card a
backend computes on, its driver and the CUDA libraries it loaded, read from
NVML (nvidia-smi), the kernel driver's /proc listing and the process's own
/proc entries — never declared; and the determinism contract of a CUDA run
(CLAUDE.md invariant 12). nvidia-smi's answers are recorded CSV."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.vega import loadopts, nvidia
from app.vega.procfs import ProcFS
from tests.vega.test_wrapper_logic import LOAD, _config, _load_with, _run_kw, vw

RTX = "0000:01:00.0"
OTHER = "0000:02:00.0"
CARDS = {RTX: "NVIDIA GeForce RTX 4090"}
GPUS = "00000000:01:00.0, NVIDIA GeForce RTX 4090, 560.35.03\n00000000:02:00.0, NVIDIA RTX A4000, 560.35.03\n"


def fake_smi(gpus=GPUS, used=None, mig="[N/A]"):
    used = used if used is not None else ["00000000:01:00.0, 812", "00000000:02:00.0, 4"]

    def run(args):
        if args[0] == "--query-gpu=pci.bus_id,name,driver_version":
            return gpus
        if args[0] == "--query-gpu=pci.bus_id,mig.mode.current":
            return f"00000000:01:00.0, {mig}\n00000000:02:00.0, Disabled\n"
        if args[0] == "--query-gpu=pci.bus_id,memory.used":
            return "\n".join(used) + "\n"
        raise AssertionError(args)
    return run


# ---- nvidia-smi -------------------------------------------------------------------

def test_nvml_bus_ids_are_read_as_lspci_prints_them():
    assert nvidia.pci_of("00000000:01:00.0") == RTX
    assert nvidia.pci_of("0000:0A:00.0") == "0000:0a:00.0"
    with pytest.raises(nvidia.NvidiaUnreadable):
        nvidia.pci_of("GPU-1234")


def test_the_cards_their_driver_and_used_memory_are_read_per_address():
    smi = fake_smi()
    assert nvidia.gpus(smi) == {RTX: {"name": "NVIDIA GeForce RTX 4090", "driver": "560.35.03"},
                                OTHER: {"name": "NVIDIA RTX A4000", "driver": "560.35.03"}}
    assert nvidia.used_vram_mib(smi) == {RTX: 812, OTHER: 4}
    assert nvidia.driver_line(smi, RTX) == "dxg nvidia 560.35.03"
    with pytest.raises(nvidia.NvidiaUnreadable, match="no card"):
        nvidia.driver_line(smi, "0000:09:00.0")


def test_an_answer_that_is_not_its_csv_is_unreadable_never_guessed(tmp_path):
    with pytest.raises(nvidia.NvidiaUnreadable):
        nvidia.gpus(fake_smi(gpus="No devices were found\n"))
    with pytest.raises(nvidia.NvidiaUnreadable):
        nvidia.used_vram_mib(fake_smi(used=["00000000:01:00.0, [N/A]"]))
    with pytest.raises(nvidia.NvidiaUnreadable):
        nvidia.runner(tmp_path / "nvidia-smi")(["-L"])


def test_the_minors_are_the_kernel_drivers_own_listing(tmp_path):
    for pci, minor in ((RTX, 0), (OTHER, 1)):
        d = tmp_path / "driver" / "nvidia" / "gpus" / pci
        d.mkdir(parents=True)
        (d / "information").write_text(f"Model: \t NVIDIA\nIRQ: 130\nDevice Minor: {minor}\nBus Location: {pci}\n")
    assert nvidia.minors(tmp_path) == {0: RTX, 1: OTHER}
    assert nvidia.minors(tmp_path / "absent") == {}


# ---- /proc ------------------------------------------------------------------------

def _process(root: Path, pid: int, fds: list[str], maps: list[str] = ()) -> None:
    d = root / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "fdinfo").mkdir()
    for i, target in enumerate(fds):
        os.symlink(target, d / "fd" / str(i))
        (d / "fdinfo" / str(i)).write_text("pos: 0\nflags: 02\n")
    (d / "maps").write_text("".join(f"7f00-7f01 r-xp 00000000 08:01 1 {m}\n" for m in maps)
                            + "7ffd-7ffe rw-p 00000000 00:00 0 [stack]\n")


def test_a_cuda_context_is_no_drm_client_so_it_is_never_read_as_a_cpu_run(tmp_path):
    """Before 2026-10-21 a backend computing on an NVIDIA card read as holding
    no card at all — a CPU run, the wrong identity for every vector."""
    _process(tmp_path, 10, ["/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia0", "/dev/nvidia0"])
    _process(tmp_path, 11, ["/dev/null"])
    p = ProcFS(tmp_path)
    assert p.nvidia_minors(10) == {0} and p.nvidia_minors(11) == set()
    assert p.card_mib_by_device(10) is None, "unknown, never an empty (CPU) mapping"
    assert p.card_mib_by_device(11) == {}
    with pytest.raises(loadopts.Unstatable, match="nvidia_smi"):
        vw.device_of([11, 10], CARDS, p)
    assert vw.device_of([11, 10], CARDS, p, nvidia_device=lambda pids: ("asked", RTX)) == ("asked", RTX)


def test_the_cuda_libraries_are_the_ones_the_backend_mapped(tmp_path):
    _process(tmp_path, 10, ["/dev/nvidia0"], maps=[
        "/usr/lib/x86_64-linux-gnu/libcuda.so.560.35.03",
        "/backends/cuda12-llama-cpp/lib/libcublas.so.12.4.5.8",
        "/backends/cuda12-llama-cpp/lib/libcublasLt.so.12.4.5.8",
        "/backends/cuda12-llama-cpp/lib/libcudart.so.12.4.127",
        "/usr/lib/x86_64-linux-gnu/libc.so.6",
        "/backends/cuda12-llama-cpp/lib/libggml-cuda.so"])
    _process(tmp_path, 11, [], maps=["/backends/cuda12-llama-cpp/lib/libcublas.so.12.4.5.8 (deleted)"])
    got = [p.name for p in vw.cuda_libraries([10, 11], ProcFS(tmp_path))]
    assert got == ["libcublas.so.12.4.5.8", "libcublasLt.so.12.4.5.8", "libcudart.so.12.4.127",
                   "libcuda.so.560.35.03"], "each once; the driver's and the toolkit's, nothing else"


# ---- which card -------------------------------------------------------------------

def _nv(candidates, moved, declared, weights=2400.0, cards=CARDS):
    return vw.device_on_nvidia(candidates, cards, vram_delta_mib=moved, weights_mib=weights,
                               declared=declared)


def test_a_card_run_moved_the_weights_onto_exactly_one_card():
    assert _nv([RTX], {RTX: 2600}, "card") == ("NVIDIA GeForce RTX 4090", RTX)
    # CUDA opened a context on both cards (~300 MiB each); the weights went to one
    assert _nv([RTX, OTHER], {RTX: 2900, OTHER: 310}, "card",
               cards=CARDS | {OTHER: "A4000"}) == ("NVIDIA GeForce RTX 4090", RTX)


def test_a_split_load_a_card_cards_does_not_name_or_a_load_that_moved_too_little_states_no_run():
    with pytest.raises(loadopts.Unstatable, match="disagree"):
        _nv([RTX, OTHER], {RTX: 1300, OTHER: 1300}, "card", cards=CARDS | {OTHER: "A4000"})
    with pytest.raises(loadopts.Unstatable, match="names no device"):
        _nv([OTHER], {OTHER: 2600}, "card")
    with pytest.raises(loadopts.Unstatable, match="disagree"):
        _nv([RTX], {RTX: 300}, "card")
    with pytest.raises(loadopts.Unstatable, match="disagree"):
        _nv([RTX], {RTX: 2600}, None)
    with pytest.raises(loadopts.Unstatable, match="not read"):
        _nv([RTX], {}, "card")


def test_a_cpu_run_moves_nothing_and_a_context_alone_is_not_a_cpu_run():
    assert _nv([RTX], {RTX: 12}, "cpu") == ("cpu", None)
    with pytest.raises(loadopts.Unstatable, match="CUDA_VISIBLE_DEVICES=-1"):
        _nv([RTX], {RTX: 310}, "cpu")


def test_cuda_visible_devices_minus_one_declares_a_cpu_run():
    load = json.loads(LOAD) | {"EnvVars": {"CUDA_VISIBLE_DEVICES": "-1"}}
    assert vw.declared_placement(vw.canon_from_protojson(json.dumps(load))) == "cpu"


# ---- the determinism contract -----------------------------------------------------

def test_llama_cpp_on_cuda_runs_one_sequence_and_a_card_read_as_nvidia_is_known(tmp_path):
    """cuBLAS disallows atomics by default: llama.cpp on CUDA needs `parallel:1`
    and no vendor variable — and a backend whose NAME names no vendor (the meta
    `llama-cpp`) is accepted on a card the wrapper READ as NVIDIA."""
    vw.state_run(canon=_load_with({}, ("parallel:1",)), environ={}, **_run_kw(tmp_path, "cuda12-llama-cpp"))
    with pytest.raises(loadopts.Unstatable, match="parallel:1"):
        vw.state_run(canon=_load_with({}, ("parallel:4",)), environ={}, **_run_kw(tmp_path, "cuda13-llama-cpp"))
    with pytest.raises(loadopts.Unstatable, match="names no vendor"):
        vw.state_run(canon=_load_with({}, ("parallel:1",)), environ={},
                     **_run_kw(tmp_path / "unread", "llama-cpp"))
    vw.state_run(canon=_load_with({}, ("parallel:1",)), environ={}, vendor="cuda",
                 **_run_kw(tmp_path / "read", "llama-cpp"))


def test_torch_on_cuda_needs_cublass_fixed_workspace_beside_torchs_switch(tmp_path):
    on = {"LOUISE_TORCH_DETERMINISTIC": "1"}
    both = on | {"CUBLAS_WORKSPACE_CONFIG": ":4096:8"}
    for name, vendor in (("cuda12-louise-python-20261021", None), ("louise-python-20261021", "cuda")):
        kw = _run_kw(tmp_path / name, name)
        with pytest.raises(loadopts.Unstatable, match="CUBLAS_WORKSPACE_CONFIG=:4096:8"):
            vw.state_run(canon=_load_with(on, ("task:jina-v3", "device:cuda")), environ=on,
                         vendor=vendor, **kw)
        vw.state_run(canon=_load_with(both, ("task:jina-v3", "device:cuda")), environ=both,
                     vendor=vendor, **kw)
    # on the CPU the workspace is no part of the contract
    kw = _run_kw(tmp_path / "cpu", "cuda12-louise-python-20261021", "cpu")
    vw.state_run(canon=_load_with(on, ("task:jina-v3", "device:cpu")), environ=on, **kw)


# ---- the configuration ------------------------------------------------------------

def test_the_config_names_nvidia_smi_by_absolute_path_and_one_vendors_reader(tmp_path):
    cfg = vw.load_config(_config(tmp_path, nvidia_smi="/usr/bin/nvidia-smi"), tmp_path / "backends")
    assert cfg.nvidia_smi == Path("/usr/bin/nvidia-smi") and cfg.amd_smi is None
    assert vw.load_config(_config(tmp_path), tmp_path / "backends").nvidia_smi is None
    with pytest.raises(vw.ConfigError, match="nvidia_smi"):
        vw.load_config(_config(tmp_path, nvidia_smi="nvidia-smi"), tmp_path / "backends")
    with pytest.raises(vw.ConfigError, match="two vendors"):
        vw.load_config(_config(tmp_path, nvidia_smi="/usr/bin/nvidia-smi",
                               amd_smi="/opt/rocm-wsl/bin/amd-smi"), tmp_path / "backends")


# ---- a whole load, read on a native NVIDIA host -----------------------------------

class _Child:
    pid = 10

    def alive(self) -> bool:
        return True


class _Receipts:
    def __init__(self) -> None:
        self.runs: list = []

    def publish(self, run_id, run) -> None:
        self.runs.append((run_id, run))

    def publish_tree(self, tree, manifest) -> None:
        pass


def _nvidia_host(tmp_path: Path, *, fds=("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia0"),
                 libraries=("libcuda.so.560.35.03", "libcublas.so.12.4.5.8", "libcudart.so.12.4.127")):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    (proc / "sys" / "kernel").mkdir(parents=True)
    (proc / "sys" / "kernel" / "osrelease").write_text("6.8.0-47-generic\n")
    info = proc / "driver" / "nvidia" / "gpus" / RTX
    info.mkdir(parents=True)
    (info / "information").write_text(f"Model: \t NVIDIA GeForce RTX 4090\nDevice Minor: \t 0\nBus Location: \t {RTX}\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in libraries:
        (lib / name).write_bytes(name.encode())
    _process(proc, 10, list(fds), [str(lib / n) for n in libraries] + ["/usr/lib/libc.so.6"])
    (sys_ / "bus" / "pci" / "drivers" / "nvidia").mkdir(parents=True)
    (sys_ / "bus" / "pci" / "devices" / RTX).mkdir(parents=True)
    os.symlink(sys_ / "bus" / "pci" / "drivers" / "nvidia", sys_ / "bus" / "pci" / "devices" / RTX / "driver")
    (sys_ / "module" / "nvidia").mkdir(parents=True)
    (sys_ / "module" / "nvidia" / "version").write_text("560.35.03\n")
    return proc, sys_


def _smi_around_a_load(before: int, after: int, mig="[N/A]"):
    used = iter([before, after])

    def run(args):
        if args[0] == "--query-gpu=pci.bus_id,memory.used":
            return f"00000000:01:00.0, {next(used)}\n"
        return fake_smi(mig=mig)(args)
    return run


def _gate(tmp_path, stubs, smi, *, name="cuda12-llama-cpp", **host):
    from tests.vega.test_wrapper_logic import _backend_dir
    proc, sys_ = _nvidia_host(tmp_path, **host)
    backend = vw.real_backend(_backend_dir(tmp_path, name=name))
    cfg = vw.load_config(_config(tmp_path, backend=name, cards=CARDS,
                                 nvidia_smi="/usr/bin/nvidia-smi"), tmp_path / "backends")
    receipts = _Receipts()
    gate = vw.Gate(cfg, backend, _Child(), receipts, stubs.pb, environ={},
                   hash_weights=lambda p: "sha256:" + "ab" * 32, procfs=ProcFS(proc),
                   proc_root=proc, sys_root=sys_, dpkg_status=tmp_path / "status", nvidia_smi=smi)
    return gate, receipts


def _load(tmp_path, stubs, **options) -> bytes:
    from google.protobuf import json_format
    weights = tmp_path / "models" / "m.gguf"
    weights.parent.mkdir(exist_ok=True)
    with weights.open("wb") as f:
        f.truncate(2400 * 2**20)                     # 2,400 MiB, sparse
    load = {"Model": "m.gguf", "ModelFile": str(weights), "ModelPath": str(weights.parent),
            "ContextSize": 512, "Embeddings": True, "Options": ["parallel:1"]} | options
    return json_format.ParseDict(load, stubs.pb.ModelOptions()).SerializeToString()


def test_a_card_run_on_nvidia_states_the_card_its_bound_driver_and_the_cuda_it_loaded(tmp_path, stubs):
    gate, receipts = _gate(tmp_path, stubs, _smi_around_a_load(300, 2900))
    ok = stubs.pb.Result(success=True).SerializeToString()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert answer.success, answer.message
    (_run_id, run), = receipts.runs
    text = json.dumps(run)
    assert "NVIDIA GeForce RTX 4090" in text
    assert "kernel 6.8.0-47-generic; nvidia 560.35.03; " in text, "the module the card is BOUND to"
    for lib in ("libcublas.so.12.4.5.8", "libcudart.so.12.4.127", "libcuda.so.560.35.03"):
        assert f"{lib} sha256:" in text


def test_a_load_on_nvidia_that_moved_no_weights_onto_the_card_states_no_run(tmp_path, stubs):
    gate, receipts = _gate(tmp_path, stubs, _smi_around_a_load(300, 610))   # a context only
    ok = stubs.pb.Result(success=True).SerializeToString()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert not answer.success and "disagree" in answer.message
    assert receipts.runs == [] and gate.loaded is None


def test_a_torch_task_on_nvidia_carries_cublass_workspace_or_states_no_run(tmp_path, stubs):
    ok = stubs.pb.Result(success=True).SerializeToString()
    on = {"LOUISE_TORCH_DETERMINISTIC": "1"}
    for env, stated in ((on, False), (on | {"CUBLAS_WORKSPACE_CONFIG": ":4096:8"}, True)):
        where = tmp_path / str(stated)
        gate, receipts = _gate(where, stubs, _smi_around_a_load(0, 2700), name="louise-python-20261021")
        gate.environ = env
        raw = _load(where, stubs, Options=["task:jina-v3", "device:cuda"], EnvVars=env)
        answer = stubs.pb.Result.FromString(gate.load_model(raw, lambda raw: ok))
        assert answer.success is stated, answer.message
        if not stated:
            assert "CUBLAS_WORKSPACE_CONFIG=:4096:8" in answer.message


def test_under_wsl2_the_one_named_card_is_read_through_dev_dxg_and_windows_driver(tmp_path, stubs):
    from tests.vega.test_wrapper_logic import _backend_dir
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel").mkdir(parents=True)
    (proc / "sys" / "kernel" / "osrelease").write_text("6.6.87.2-microsoft-standard-WSL2\n")
    lib = tmp_path / "wsl-lib"
    lib.mkdir()
    for name in ("libcuda.so.1.1", "libcublas.so.12.4.5.8"):
        (lib / name).write_bytes(name.encode())
    _process(proc, 10, ["/dev/dxg"], [str(lib / "libcuda.so.1.1"), str(lib / "libcublas.so.12.4.5.8")])
    backend = vw.real_backend(_backend_dir(tmp_path, name="cuda12-llama-cpp"))
    receipts = _Receipts()
    cfg = vw.load_config(_config(tmp_path, backend="cuda12-llama-cpp", cards=CARDS,
                                 nvidia_smi="/usr/lib/wsl/lib/nvidia-smi"), tmp_path / "backends")
    gate = vw.Gate(cfg, backend, _Child(), receipts, stubs.pb, environ={},
                   hash_weights=lambda p: "sha256:" + "ab" * 32, procfs=ProcFS(proc),
                   proc_root=proc, sys_root=tmp_path / "sys", dpkg_status=tmp_path / "status",
                   nvidia_smi=_smi_around_a_load(900, 3500))
    ok = stubs.pb.Result(success=True).SerializeToString()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert answer.success, answer.message
    text = json.dumps(receipts.runs[0][1])
    assert "kernel 6.6.87.2-microsoft-standard-WSL2; dxg nvidia 560.35.03; libcublas.so.12.4.5.8 sha256:" in text


def test_a_card_in_mig_mode_states_no_run_a_slice_is_not_the_card(tmp_path, stubs):
    assert nvidia.mig_enabled(fake_smi(mig="Enabled")) == {RTX}
    assert nvidia.mig_enabled(fake_smi()) == set()
    gate, receipts = _gate(tmp_path, stubs, _smi_around_a_load(300, 2900, mig="Enabled"))
    ok = stubs.pb.Result(success=True).SerializeToString()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert not answer.success and "MIG" in answer.message and receipts.runs == []


def test_a_card_model_that_fell_back_to_the_cpu_states_no_run(tmp_path, stubs):
    """Perugia, 2026-10-21: the card hung; after a restart the same model ran
    on the CPU, a new run nobody measured. A card that is gone leaves the
    backend holding no /dev/nvidia<N>: read as the CPU, refused as such."""
    gate, receipts = _gate(tmp_path, stubs, _smi_around_a_load(0, 0), fds=("/dev/null",))
    ok = stubs.pb.Result(success=True).SerializeToString()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert not answer.success and "missing or has failed" in answer.message
    assert receipts.runs == [] and gate.loaded is None


def test_a_load_answers_without_waiting_for_the_trees_file_list(tmp_path, stubs):
    """The list is informational: a service that drops it cost every load 30 s
    (the node, 2026-10-21: MedCPT 36 s, the load itself 2.5 s). It is told in
    the background, after the answer."""
    import threading
    import time
    gate, receipts = _gate(tmp_path, stubs, _smi_around_a_load(300, 2900))
    release, told = threading.Event(), threading.Event()

    def slow_tree(tree, manifest):
        release.wait(10)
        told.set()
    receipts.publish_tree = slow_tree
    ok = stubs.pb.Result(success=True).SerializeToString()
    t0 = time.monotonic()
    answer = stubs.pb.Result.FromString(gate.load_model(_load(tmp_path, stubs, NGPULayers=999), lambda raw: ok))
    assert answer.success and time.monotonic() - t0 < 5, "answered while the list was still being told"
    assert not told.is_set()
    release.set()
    assert told.wait(5), "and the list is still told"
