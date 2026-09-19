"""The GPU probe (app/gpu_probe.py) against a fake /proc, /sys and /dev/dri.

The fdinfo texts and command lines are the node's own, read on 2026-09-18
(kernel 6.18, xe, Arc Pro B70); the ioctl is the xe uAPI's, faked at the
syscall.
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app import gpu
from app import gpu_probe as gp

PDEV = "0000:03:00.0"
QWEN_ID = "87f3fbd3e86a3e7f566abd0c5da0218aff5ddd1382b93c03a" + "0" * 15
MADLAD_ID = "131f8e039aaa05efe404d44f9a8890756c3f22a547c44b3d2" + "0" * 15
OLLAMA_ID = "9f59901546a15ef271946b3ad14c121a0ded185e70d181b08" + "0" * 15
BLOB = "a3de86cd1c132c822487ededd47a324c5049139" + "5" * 25


def xe_fdinfo(client: int, vram_kib: int, gtt_kib: int = 0, system_kib: int = 0,
              pdev: str = PDEV) -> str:
    """The shape xe prints, 2026-09-18."""
    return (f"pos:\t0\nflags:\t02100002\nmnt_id:\t1234\nino:\t567\n"
            f"drm-driver:\txe\ndrm-client-id:\t{client}\ndrm-pdev:\t{pdev}\n"
            f"drm-total-system:\t{system_kib} KiB\ndrm-shared-system:\t0\n"
            f"drm-active-system:\t0\ndrm-resident-system:\t{system_kib} KiB\n"
            f"drm-purgeable-system:\t0\n"
            f"drm-total-gtt:\t{gtt_kib} KiB\ndrm-shared-gtt:\t0\ndrm-active-gtt:\t0\n"
            f"drm-resident-gtt:\t{gtt_kib} KiB\n"
            f"drm-total-vram0:\t{vram_kib} KiB\ndrm-shared-vram0:\t0\n"
            f"drm-active-vram0:\t0\ndrm-resident-vram0:\t{vram_kib} KiB\n"
            f"drm-cycles-rcs:\t0\ndrm-total-cycles-rcs:\t123456\n")


def _process(proc: Path, pid: int, argv: list[str], cgroup: str,
             fds: dict[str, tuple[str, str | None]], hostname: str | None = None) -> Path:
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "fdinfo").mkdir()
    (d / "cmdline").write_text("\0".join(argv) + "\0")
    (d / "cgroup").write_text(cgroup + "\n")
    for fd, (target, info) in fds.items():
        os.symlink(target, d / "fd" / fd)
        if info is not None:
            (d / "fdinfo" / fd).write_text(info)
    if hostname is not None:
        (d / "root" / "etc").mkdir(parents=True)
        (d / "root" / "etc" / "hostname").write_text(hostname + "\n")
    return d


@pytest.fixture
def proc(tmp_path, monkeypatch) -> Path:
    """Four GPU processes as the node had them, a host one holding nothing,
    and one that is not a GPU client at all."""
    proc = tmp_path / "proc"
    render = "/dev/dri/renderD128"
    _process(proc, 9317,
             ["/app/llama-server", "-m", "/models/Qwen3-Embedding-4B-Q4_K_M.gguf",
              "--embeddings", "--pooling", "last", "-ngl", "999"],
             f"0::/../{QWEN_ID}",           # as the probe's own cgroup namespace sees it
             {"0": ("pipe:[1]", None), "3": (render, xe_fdinfo(7, 9586248, 3974600, 125568)),
              # a dup of fd 3: the same client, counted once
              "4": (render, xe_fdinfo(7, 9586248, 3974600, 125568)),
              "5": (render, xe_fdinfo(52, 144, 4520, 64896)),
              "6": ("/dev/null", "pos:\t0\nflags:\t02\n")},
             hostname=QWEN_ID[:12])
    _process(proc, 978631, ["python3", "/srv/madlad-translate/server.py"],
             f"0::/docker/{MADLAD_ID}",
             {"3": (render, xe_fdinfo(782, 6377712, 79080, 125824)),
              "4": (render, xe_fdinfo(784, 112, 40, 62976))},
             hostname="translate-gpu-madlad")
    ollama = _process(proc, 1034812,
                      ["/usr/lib/ollama/llama-server", "--model",
                       f"/root/.ollama/models/blobs/sha256-{BLOB}", "--port", "40117"],
                      f"0::/system.slice/docker-{OLLAMA_ID}.scope",   # systemd's driver
                      {"3": (render, xe_fdinfo(828, 0)),
                       "4": (render, xe_fdinfo(829, 6329720, 68292))},
                      hostname=OLLAMA_ID[:12])
    manifests = ollama / "root" / "root" / ".ollama" / "models" / "manifests"
    for repo, digest in ((("registry.ollama.ai", "library", "qwen3", "8b"), BLOB),
                         (("registry.ollama.ai", "library", "llama3", "latest"), "f" * 64)):
        path = manifests.joinpath(*repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"layers": [
            {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:" + "e" * 64},
            {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + digest}]}))
    _process(proc, 555, ["/usr/bin/some-monitor"], "0::/system.slice/monitor.service",
             {"3": (render, xe_fdinfo(900, 0))})
    _process(proc, 777, ["/usr/sbin/sshd"], "0::/system.slice/ssh.service",
             {"3": ("socket:[99]", "pos:\t0\n")})
    (proc / "self").mkdir()
    monkeypatch.setattr(gp, "PROC", proc)
    return proc


# ---------------------------------------------------------------- fdinfo

def test_fdinfo_prefers_resident_and_reads_every_unit():
    pdev, client, regions = gp.parse_fdinfo(
        "drm-driver:\txe\ndrm-client-id:\t7\ndrm-pdev:\t0000:03:00.0\n"
        "drm-total-vram0:\t2 GiB\ndrm-resident-vram0:\t1 MiB\n"
        "drm-total-gtt:\t3 KiB\ndrm-total-system:\t0\ndrm-total-cycles-rcs:\t99\n")
    assert (pdev, client) == ("0000:03:00.0", "7")
    assert regions == {"vram0": 1 << 20, "gtt": 3 << 10, "system": 0}


def test_fdinfo_reads_amdgpus_legacy_keys():
    _, _, regions = gp.parse_fdinfo(
        "drm-driver:\tamdgpu\ndrm-pdev:\t0000:13:00.0\ndrm-client-id:\t3\n"
        "drm-memory-vram:\t1024 KiB\ndrm-memory-gtt:\t2 MiB\ndrm-memory-cpu:\t0 KiB\n")
    assert regions == {"vram": 1 << 20, "gtt": 2 << 20, "cpu": 0}


def test_a_file_that_is_no_drm_client_is_none():
    assert gp.parse_fdinfo("pos:\t0\nflags:\t02\n") is None
    assert gp.parse_fdinfo("drm-driver:\txe\ndrm-total-vram0:\t1 KiB\n") is None


# ---------------------------------------------------------------- labels

@pytest.mark.parametrize("argv,expected", [
    (["/app/llama-server", "-m", "/models/bge-m3-FP16.gguf", "--embeddings"], "bge-m3-FP16"),
    (["/app/llama-server", "--model=/models/MedCPT-Query-Encoder-f16.gguf"],
     "MedCPT-Query-Encoder-f16"),
    (["python3", "/srv/madlad-translate/server.py"], "madlad-translate"),
    (["python3", "-u", "/srv/dnabert2/embed_server.py"], "embed_server"),
    (["python3.12", "-X", "dev", "-m", "uvicorn", "app:api"], "uvicorn"),
    (["python", "-c", "import x"], "python"),
    (["/usr/bin/some-monitor", "--interval", "5"], "some-monitor"),
    (["/app/server", "--model", "/data/madlad400-3b-mt-bf16"], "madlad400-3b-mt-bf16"),
    ([], "?"),
])
def test_the_label_names_what_the_process_serves(argv, expected, tmp_path):
    assert gp.label(argv, tmp_path) == expected


def test_an_ollama_blob_is_named_by_its_manifest(proc):
    root = proc / "1034812" / "root"
    blob = f"/root/.ollama/models/blobs/sha256-{BLOB}"
    assert gp.label(["/usr/lib/ollama/llama-server", "--model", blob], root) == "qwen3:8b (ollama)"
    # a blob no manifest names still says what it is
    other = "/root/.ollama/models/blobs/sha256-" + "0" * 64
    assert gp.label(["/usr/lib/ollama/llama-server", "--model", other], root) == \
        "sha256-000000000000… (ollama)"


# ---------------------------------------------------------------- the scan

def test_scan_sums_each_process_once_per_client_largest_first(proc):
    procs = gp.scan()[PDEV]
    assert [p.pid for p in procs] == [9317, 978631, 1034812]
    qwen, madlad, ollama = procs
    assert qwen.vram == (9586248 + 144) * 1024, "fd 4 is a dup of fd 3's client"
    assert qwen.host == (3974600 + 125568 + 4520 + 64896) * 1024
    assert (qwen.label, qwen.command, qwen.container) == \
        ("Qwen3-Embedding-4B-Q4_K_M", "llama-server", QWEN_ID[:12])
    assert (madlad.label, madlad.container) == ("madlad-translate", "translate-gpu-madlad")
    assert (ollama.label, ollama.vram, ollama.container) == \
        ("qwen3:8b (ollama)", 6329720 * 1024, OLLAMA_ID[:12])


def test_scan_leaves_out_clients_that_hold_nothing(proc):
    assert 555 not in {p.pid for p in gp.scan()[PDEV]}


def test_scan_skips_a_process_it_may_not_read(proc, monkeypatch):
    real = os.listdir

    def listdir(path):
        if str(path).endswith(os.path.join("978631", "fd")):
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(gp.os, "listdir", listdir)
    assert [p.pid for p in gp.scan()[PDEV]] == [9317, 1034812]


def test_a_process_outside_any_container_is_the_hosts(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    (d / "cgroup").write_text("0::/system.slice/ssh.service\n")
    assert gp.container(d) == "host"
    (d / "cgroup").write_text("0::/../../init.scope\n")        # from inside a namespace
    assert gp.container(d) == "host"


# ---------------------------------------------------------------- the cards

PCI_IDS = (
    "# pci.ids\n"
    "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
    "\t164e  Raphael [Radeon 610M]\n"
    "\t\t1849 364e  a subsystem\n"
    "1234  Decoy\n"
    "\te223  not the Intel one\n"
    "8086  Intel Corporation\n"
    "\te20b  Battlemage G21 [Arc B580]\n"
    "\t\t8086 1701  a subsystem\n"
    "\te223  Battlemage G31 [Arc Pro B70]\n"
    "\n"
    "C 00  Unclassified device\n")


def test_pci_name_reads_the_device_inside_its_own_vendor(tmp_path, monkeypatch):
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    monkeypatch.setattr(gp, "PCI_IDS_PATHS", (tmp_path / "missing", ids))
    assert gp.pci_name("8086", "e223") == ("Intel Corporation", "Battlemage G31 [Arc Pro B70]")
    assert gp.pci_name("1002", "164e") == ("Advanced Micro Devices, Inc. [AMD/ATI]",
                                           "Raphael [Radeon 610M]")
    assert gp.pci_name("8086", "ffff") == ("Intel Corporation", None)
    assert gp.pci_name("abcd", "e223") == (None, None)


def test_the_ioctl_number_is_xes_device_query():
    assert gp._XE_QUERY.size == 40 and gp._XE_REGION.size == 88
    assert gp.DRM_IOCTL_XE_DEVICE_QUERY == (3 << 30) | (40 << 16) | (ord("d") << 8) | 0x40


def _fake_xe(monkeypatch, regions: list[tuple[int, int, int]]) -> list[int]:
    """The kernel's side of the two-call query: the size, then the data."""
    payload = gp._XE_REGIONS.pack(len(regions), 0) + b"".join(
        gp._XE_REGION.pack(cls, i, 4096, total, used, total, 0)
        for i, (cls, total, used) in enumerate(regions))
    calls: list[int] = []

    def ioctl(fd, request, buf, mutate):
        assert request == gp.DRM_IOCTL_XE_DEVICE_QUERY and mutate
        ext, query, size, data, r0, r1 = gp._XE_QUERY.unpack(buf)
        assert query == gp.XE_QUERY_MEM_REGIONS
        calls.append(size)
        if size == 0:
            buf[:] = gp._XE_QUERY.pack(ext, query, len(payload), data, r0, r1)
        else:
            assert size == len(payload)
            ctypes.memmove(data, payload, size)
        return 0
    monkeypatch.setattr(gp, "_ioctl", ioctl)
    return calls


@pytest.fixture
def sysfs(tmp_path, monkeypatch) -> Path:
    """card0 the Arc (xe) with its connector, card1 the iGPU (amdgpu)."""
    sys_ = tmp_path / "sys"
    drm = sys_ / "class" / "drm"
    drm.mkdir(parents=True)
    for card, pdev, driver, vendor, device in (("card0", PDEV, "xe", "0x8086", "0xe223"),
                                              ("card1", "0000:13:00.0", "amdgpu", "0x1002", "0x164e")):
        dev = sys_ / "devices" / "pci0000:00" / pdev
        (dev / "drm").mkdir(parents=True)
        drivers = sys_ / "bus" / "pci" / "drivers" / driver
        drivers.mkdir(parents=True)
        os.symlink(drivers, dev / "driver")
        (dev / "vendor").write_text(vendor + "\n")
        (dev / "device").write_text(device + "\n")
        (drm / card).mkdir()
        os.symlink(dev, drm / card / "device")
    (sys_ / "devices" / "pci0000:00" / PDEV / "drm" / "renderD128").mkdir()
    (sys_ / "devices" / "pci0000:00" / "0000:13:00.0" / "mem_info_vram_total").write_text("536870912\n")
    (sys_ / "devices" / "pci0000:00" / "0000:13:00.0" / "mem_info_vram_used").write_text("41943040\n")
    (drm / "card0-DP-1").mkdir()
    (drm / "renderD128").mkdir()
    dri = tmp_path / "dev" / "dri"
    dri.mkdir(parents=True)
    (dri / "renderD128").write_bytes(b"")
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    monkeypatch.setattr(gp, "SYS_DRM", drm)
    monkeypatch.setattr(gp, "DEV_DRI", dri)
    monkeypatch.setattr(gp, "PCI_IDS_PATHS", (ids,))
    return sys_


def test_xe_vram_sums_the_vram_regions_and_skips_system_memory(sysfs, monkeypatch, tmp_path):
    calls = _fake_xe(monkeypatch, [(0, 64 << 30, 1 << 30),                # system memory
                                   (1, 34242297856, 25730179072)])       # vram0
    assert gp.xe_vram(tmp_path / "dev" / "dri" / "renderD128") == (34242297856, 25730179072)
    assert calls[0] == 0 and calls[1] == 8 + 2 * 88


def test_perfmon_is_read_from_the_effective_set():
    assert not gp.perfmon_capable("CapEff:\t00000000a80425fb\n")        # docker's default set
    assert gp.perfmon_capable("CapEff:\t00000040a80425fb\n")            # + PERFMON
    assert gp.perfmon_capable("CapEff:\t00000000a82425fb\n")            # + SYS_ADMIN
    assert not gp.perfmon_capable("Name:\tpython\n")


def test_snapshot_is_every_card_with_its_processes(sysfs, proc, monkeypatch):
    _fake_xe(monkeypatch, [(1, 34242297856, 25730179072)])
    monkeypatch.setattr(gp, "perfmon_capable", lambda status=None: True)
    snap = gp.snapshot()
    assert [d.card for d in snap.devices] == ["card0", "card1"], "the connector is no card"
    arc, igpu = snap.devices
    assert (arc.pdev, arc.driver, arc.vendor_id, arc.device_id) == (PDEV, "xe", "8086", "e223")
    assert arc.name == "Battlemage G31 [Arc Pro B70]"
    assert (arc.vram_total, arc.vram_used) == (34242297856, 25730179072)
    assert [p.pid for p in arc.processes] == [9317, 978631, 1034812]
    assert (igpu.driver, igpu.vram_total, igpu.vram_used, igpu.processes) == \
        ("amdgpu", 536870912, 41943040, ())
    assert snap.notes == ()
    assert gpu.from_json(json.loads(json.dumps(gpu.to_json(snap)))) == snap


def test_without_perfmon_used_is_left_to_the_processes_sum(sysfs, proc, monkeypatch):
    _fake_xe(monkeypatch, [(1, 34242297856, 0)])
    monkeypatch.setattr(gp, "perfmon_capable", lambda status=None: False)
    arc = gp.snapshot().devices[0]
    assert arc.vram_total == 34242297856 and arc.vram_used is None
    assert arc.used == arc.attributed
    assert any("CAP_PERFMON" in n for n in gp.snapshot().notes)


def test_a_failed_query_is_a_note_not_a_failure(sysfs, proc, monkeypatch):
    def ioctl(*a):
        raise OSError(25, "Inappropriate ioctl for device")
    monkeypatch.setattr(gp, "_ioctl", ioctl)
    snap = gp.snapshot()
    assert snap.devices[0].vram_total is None and snap.devices[0].processes
    assert any("memory query failed" in n for n in snap.notes)


# ---------------------------------------------------------------- the service

@pytest.fixture
def served(monkeypatch):
    snap = gpu.GpuSnapshot(devices=(gpu.GpuDevice(
        card="card0", pdev=PDEV, driver="xe", vendor_id="8086", device_id="e223",
        vendor="Intel Corporation", name="Battlemage G31 [Arc Pro B70]",
        vram_total=100, vram_used=40,
        processes=(gpu.GpuProcess(1, "bge-m3", "llama-server", "abc", 30, 5),)),))
    state = {"snapshot": snap}

    def snapshot():
        if isinstance(state["snapshot"], Exception):
            raise state["snapshot"]
        return state["snapshot"]
    monkeypatch.setattr(gp, "snapshot", snapshot)
    server = gp.ThreadingHTTPServer(("127.0.0.1", 0), gp._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


def test_the_service_answers_gpu_and_health(served):
    url, state = served
    with urllib.request.urlopen(url + "/gpu", timeout=5) as r:
        assert r.headers["Content-Type"] == "application/json"
        assert gpu.from_json(json.load(r)) == state["snapshot"]
    with urllib.request.urlopen(url + "/health", timeout=5) as r:
        assert r.read() == b"ok"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/nope", timeout=5)
    assert e.value.code == 404


def test_a_failing_read_is_a_500_that_says_why(served):
    url, state = served
    state["snapshot"] = RuntimeError("proc vanished")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/gpu", timeout=5)
    assert e.value.code == 500 and "proc vanished" in json.load(e.value)["error"]


def test_once_prints_one_snapshot(monkeypatch, capsys):
    snap = gpu.GpuSnapshot(devices=(), notes=("n",))
    monkeypatch.setattr(gp, "snapshot", lambda: snap)
    assert gp.main(["--once"]) == 0
    assert gpu.from_json(json.loads(capsys.readouterr().out)) == snap


def test_the_service_stops_cleanly_on_sigterm():
    """`docker stop` sends SIGTERM: a stateless reader leaves at once, 0."""
    import signal
    import subprocess
    import sys
    child =subprocess.Popen([sys.executable, "-m", "app.gpu_probe", "--port", "0"],
                             stderr=subprocess.PIPE, text=True)
    try:
        assert "serving" in child.stderr.readline()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()


def test_the_region_struct_matches_the_uapi_layout():
    raw = struct.pack("<HHIQQQQ", 1, 0, 65536, 10, 4, 10, 2) + bytes(48)
    assert gp._XE_REGION.unpack(raw) == (1, 0, 65536, 10, 4, 10, 2)


def test_a_python_server_that_names_its_weights_is_labelled_by_them(tmp_path):
    """After RAGE took the models over, the panel showed two rows called
    "servers.launch" — 6.2 GB and 432 MB of the card — because RAGE starts its
    torch and OpenVINO servers through one launcher and a python process was
    labelled by its module (operator, 2026-09-19: "check what models are
    servers.launch")."""
    from app import gpu_probe
    madlad = ["/opt/venv-madlad/bin/python", "-m", "servers.launch", "madlad", "--weights",
              "/models/madlad/madlad400-3b-mt-bf16", "--device", "xpu", "--port", "10008"]
    rerank = ["/opt/venv-medcpt/bin/python", "-m", "servers.launch", "medcpt",
              "--weights=/models/medcpt/medcpt-cross-ov", "--device", "xpu"]
    assert gpu_probe.label(madlad, tmp_path) == "madlad400-3b-mt-bf16"
    assert gpu_probe.label(rerank, tmp_path) == "medcpt-cross-ov"
    # a python process that names no weights keeps the label it had
    assert gpu_probe.label(["python3", "-m", "app.worker"], tmp_path) == "app.worker"
    # and `-m` is never read as a model for python: it is the module
    assert gpu_probe.label(["python", "-m", "uvicorn", "app.api:app"], tmp_path) == "uvicorn"
