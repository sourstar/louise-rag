"""deploy/vega/retired_backends.py — the backends nothing names, listed and
removed (operator, 2026-11-07: "It is important to delete the retired backends
as a practice, to avoid reusing a wrong one"; approved-encoders.md, "Retiring
an engine after a swap").

The fake node: two model files, two wrappers, three engines — the live SYCL
engine the card wrapper names, its CPU twin the -cpu wrapper names, and the
engine a swap retired (16acc89, the one a measurement loaded by mistake).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import ROOT

LIVE = "louise-sycl-f16-llama-cpp-ea2417d91dcc"
TWIN = "louise-cpu-llama-cpp-ea2417d91dcc"
OLD = "louise-sycl-f16-llama-cpp-16acc89"
CARD, CPU = "vega-louise-llama-cpp", "vega-louise-llama-cpp-cpu"


def _load():
    name = "retired_backends_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "deploy" / "vega" / "retired_backends.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module                  # its dataclasses look themselves up
        spec.loader.exec_module(module)
    return sys.modules[name]


rb = _load()


def _engine(backends: Path, name: str, payload: int = 70_000) -> Path:
    d = backends / name
    (d / "lib").mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\n")
    (d / "lib" / "libllama.so").write_bytes(os.urandom(payload))
    (d / "metadata.json").write_text(json.dumps({"name": name}))
    return d


def _wrapper(backends: Path, name: str, engine: str, socket: str | None = None) -> Path:
    d = backends / name
    (d / "venv").mkdir(parents=True, exist_ok=True)
    (d / "run.sh").write_text("#!/bin/sh\n")
    (d / "metadata.json").write_text(json.dumps({"name": name}))
    (d / "vega.json").write_text(json.dumps({
        "backend": engine, "cards": {},
        "receipts_socket": socket or f"/backends/{CARD}/run/receipts.sock"}))
    return d


def _model(models: Path, name: str, backend: str, comment: str = "") -> None:
    (models / f"{name}.yaml").write_text(
        f"# {name}. JSON, which is YAML.{comment}\n"
        + json.dumps({"name": name, "backend": backend, "embeddings": True}, indent=2) + "\n")


@pytest.fixture
def node(tmp_path):
    """LocalAI's two paths as the host sees them, and a process table with
    one process (this test's own pid number is never used)."""
    backends, models = tmp_path / "backends", tmp_path / "models"
    models.mkdir()
    for engine in (LIVE, TWIN, OLD):
        _engine(backends, engine)
    _wrapper(backends, CARD, LIVE)
    _wrapper(backends, CPU, TWIN)
    (backends / ".downloading").mkdir()             # localai-bugs.md B15: never examined
    (backends / ".louise-llama-cpp-sycl_f16.tar").write_bytes(b"tar")
    _model(models, "vega-qwen3-embedding-4b", CARD,
           f"\n# was \"backend\": \"{OLD}\" before the swap")    # a comment names nothing
    _model(models, "vega-qwen3-embedding-4b-cpu", CPU)
    proc = tmp_path / "proc"
    _process(proc, 4242, cwd=tmp_path, exe=Path(sys.executable))
    return backends, models, proc


def _process(proc: Path, pid: int, cwd: Path, exe: Path, maps: list[Path] = (),
             fds: list[Path] = ()) -> Path:
    """/proc/<pid> as the kernel lays it out: cwd and exe as links, fd/ of
    links, maps naming each mapped file by device, inode and path."""
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True)
    os.symlink(cwd, d / "cwd")
    os.symlink(exe, d / "exe")
    for i, f in enumerate(fds):
        os.symlink(f, d / "fd" / str(i))
    lines = ["7ffd0000-7ffd2000 rw-p 00000000 00:00 0                          [stack]"]
    for f in maps:
        st = os.stat(f)
        lines.append(f"7f00{len(lines):04x}000-7f00{len(lines):04x}fff r-xp 00000000 "
                     f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x} {st.st_ino}  "
                     f"/backends/{f.relative_to(f.parents[2])}")
    (d / "maps").write_text("\n".join(lines) + "\n")
    return d


def _run(backends, models, proc, *flags) -> tuple[int, str]:
    out = io.StringIO()
    rc = rb.run(backends, models, "/backends", proc, delete="--delete" in flags, out=out)
    return rc, out.getvalue()


def test_only_the_engine_a_swap_retired_is_unreferenced(node):
    backends, models, proc = node
    inv = rb.inventory(backends, models, "/backends")
    assert [e.name for e in inv.retired] == [OLD]
    assert inv.live[CARD] == [f"{CPU}/vega.json receipts_socket", "vega-qwen3-embedding-4b.yaml"], \
        "the card wrapper holds both wrappers' receipts socket"
    assert inv.live[LIVE] == [f"{CARD}/vega.json backend"]
    assert inv.live[TWIN] == [f"{CPU}/vega.json backend"], "the CPU twin is kept: its wrapper names it"
    assert sorted(inv.not_examined) == [".downloading", ".louise-llama-cpp-sycl_f16.tar"]
    rc, out = _run(backends, models, proc)
    assert rc == 0 and f"retired  {OLD}" in out and "named by nothing" in out
    assert "retired: 1," in out and "--delete removes them" in out
    assert (backends / OLD).is_dir(), "a listing removes nothing"


def test_delete_removes_the_retired_engine_and_nothing_else(node):
    backends, models, proc = node
    before = {p.name for p in backends.iterdir()}
    size = rb.measure(rb.read_entries(backends)[0][OLD]).bytes
    assert size >= 70_000
    rc, out = _run(backends, models, proc, "--delete")
    assert rc == 0, out
    assert {p.name for p in backends.iterdir()} == before - {OLD}
    assert f"removed  {OLD}  {size:,} B" in out
    assert f"removed 1 of 1: {size:,} B" in out and "free space on" in out
    rc, out = _run(backends, models, proc, "--delete")
    assert rc == 0 and "retired: none; nothing removed" in out


def test_an_engine_a_running_process_uses_is_refused(node):
    backends, models, proc = node
    _process(proc, 5150, cwd=Path("/"), exe=Path(sys.executable),
             maps=[backends / OLD / "lib" / "libllama.so"])
    rc, out = _run(backends, models, proc, "--delete")
    assert rc == 1 and f"refused  {OLD}: in use by pid 5150 maps" in out
    assert (backends / OLD / "lib" / "libllama.so").is_file()
    assert "removed 0 of 1" in out


def test_a_process_is_found_by_its_cwd_and_its_open_files_too(node):
    backends, models, proc = node
    _process(proc, 6001, cwd=backends / OLD / "lib", exe=Path(sys.executable))
    _process(proc, 6002, cwd=Path("/"), exe=Path(sys.executable),
             fds=[backends / OLD / "run.sh"])
    items = [rb.measure(e) for e in rb.inventory(backends, models, "/backends").retired]
    assert rb.users(proc, items, ("/backends",)) == {OLD: ["pid 6001 cwd", "pid 6002 fd"]}


def test_a_mapping_of_a_file_already_unlinked_is_found_by_its_container_path(node):
    """The inode is gone from the tree; the path LocalAI's container maps it
    under still names the engine."""
    backends, models, proc = node
    d = _process(proc, 7001, cwd=Path("/"), exe=Path(sys.executable))
    (d / "maps").write_text(f"7f0000-7f1000 r-xp 00000000 00:2f 99999999 "
                            f"/backends/{OLD}/lib/libggml-sycl.so (deleted)\n")
    items = [rb.measure(e) for e in rb.inventory(backends, models, "/backends").retired]
    assert rb.users(proc, items, ("/backends",)) == {OLD: ["pid 7001 maps"]}


@pytest.mark.parametrize("table", ["missing", "empty", "unreadable"])
def test_an_unreadable_process_table_refuses_the_deletion_whole(node, monkeypatch, table, capsys):
    backends, models, proc = node
    if table == "missing":
        proc = proc.parent / "no-proc"
    elif table == "empty":
        proc = proc.parent / "empty-proc"
        proc.mkdir()
    else:
        real = os.listdir

        def listdir(path):
            if str(path).endswith("/fd"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        monkeypatch.setattr(rb.os, "listdir", listdir)
    rc = rb.main(["--backends", str(backends), "--models", str(models), "--proc", str(proc),
                  "--delete"])
    assert rc == 2
    assert "nothing removed" in capsys.readouterr().err
    assert (backends / OLD).is_dir()
    rc, out = _run(backends, models, proc)
    assert rc == 0 and "--delete would refuse" in out, "a listing says it, and lists"


def test_the_deployed_engine_is_read_from_the_wrappers_vega_json(node, capsys):
    backends, _models, _proc = node
    assert rb.deployed_engine(backends) == LIVE
    assert rb.deployed_engine(backends, CPU) == TWIN
    assert rb.main(["--backends", str(backends), "--engine"]) == 0
    assert capsys.readouterr().out == f"{LIVE}\n"
    (backends / CARD / "vega.json").write_text(json.dumps({"backend": "/backends/" + OLD,
                                                          "receipts_socket": "/x"}))
    assert rb.deployed_engine(backends) == OLD, "an absolute backend, by its directory"
    (backends / CARD / "vega.json").write_text(json.dumps({"backend": "gone", "receipts_socket": "/x"}))
    assert rb.main(["--backends", str(backends), "--engine", CARD]) == 2
    assert "is not installed" in capsys.readouterr().err


def test_a_wrapper_no_model_names_goes_with_the_engine_only_it_names(node):
    """The 2026-11-07 case: five louise-python engines with their
    vega-louise-python wrappers — wrapper and engine retired together."""
    backends, models, proc = node
    _engine(backends, "louise-python-20261021a")
    _wrapper(backends, "vega-louise-python-20261021a", "louise-python-20261021a")
    (backends / "backends-retired-20261102").mkdir()          # not a backend; retired too
    inv = rb.inventory(backends, models, "/backends")
    assert sorted(e.name for e in inv.retired) == sorted(
        ["backends-retired-20261102", OLD, "louise-python-20261021a",
         "vega-louise-python-20261021a"])
    assert inv.retired_by["louise-python-20261021a"] == ["vega-louise-python-20261021a"]
    rc, out = _run(backends, models, proc)
    assert "named only by vega-louise-python-20261021a (retired)" in out


def test_what_a_live_wrapper_needs_beside_its_engine_is_kept(node):
    """The receipts socket's directory (an old wrapper's run/), a meta
    backend's concrete one, an alias a model names, an unreadable config."""
    backends, models, proc = node
    _wrapper(backends, CARD, LIVE, socket="/backends/vega-llama-cpp/run/receipts.sock")
    (backends / "vega-llama-cpp" / "run").mkdir(parents=True)
    _engine(backends, "intel-sycl-f16-llama-cpp")
    (backends / "intel-sycl-f16-llama-cpp" / "metadata.json").write_text(
        json.dumps({"name": "intel-sycl-f16-llama-cpp", "alias": "llama-cpp"}))
    (backends / "localagi-meta").mkdir()
    (backends / "localagi-meta" / "metadata.json").write_text(
        json.dumps({"name": "localagi-meta", "meta_backend_for": "cpu-whisper"}))
    _engine(backends, "cpu-whisper")
    _model(models, "localagi-chat", "llama-cpp")
    _model(models, "localagi-ears", "localagi-meta")
    _engine(backends, "louise-broken")
    (backends / "louise-broken" / "metadata.json").write_text("{not json")
    inv = rb.inventory(backends, models, "/backends")
    assert [e.name for e in inv.retired] == [OLD]
    assert inv.live["vega-llama-cpp"] == [f"{CARD}/vega.json receipts_socket"]
    assert inv.live["intel-sycl-f16-llama-cpp"] == ["localagi-chat.yaml"]
    assert inv.live["cpu-whisper"] == ["localagi-meta/metadata.json meta_backend_for"]
    assert "unreadable" in inv.live["louise-broken"][0]


def test_a_model_naming_no_backend_keeps_every_upstream_backend(node):
    """LocalAI chooses that model's backend itself: an upstream one may be it.
    Ours (louise-, vega-) are named for their models and stay judged."""
    backends, models, proc = node
    _engine(backends, "cpu-llama-cpp")
    (models / "gallery-model.yaml").write_text("name: gallery-model\nparameters:\n  model: x.gguf\n")
    (models / f"{OLD}-measure.yaml.bak-20261107").write_text(
        f"name: nemo-par\nbackend: {OLD}\n")                  # a backup names its backend too
    inv = rb.inventory(backends, models, "/backends")
    assert inv.retired == []
    assert "names none" in inv.live["cpu-llama-cpp"][0]
    assert inv.live[OLD] == [f"{OLD}-measure.yaml.bak-20261107"]


def test_the_kernels_own_proc_entries_are_read_as_the_fakes_are(node, tmp_path):
    """Two real processes, read through the kernel's own /proc/<pid> (linked
    into a table of their own: a sandbox may hide pid 1 even from root): one
    whose cwd is the retired engine, one that maps its library — the maps
    line's device as the kernel writes it, matched to os.stat's."""
    backends, models, _proc = node
    lib = backends / OLD / "lib" / "libllama.so"
    mapper = ("import mmap, sys, time; f = open(sys.argv[1], 'rb'); "
              "m = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ); f.close(); "
              "print('mapped', flush=True); time.sleep(60)")
    sitting = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               cwd=lib.parent)
    mapping = subprocess.Popen([sys.executable, "-c", mapper, str(lib)], cwd="/",
                               stdout=subprocess.PIPE, text=True)
    try:
        assert mapping.stdout.readline() == "mapped\n"
        table = tmp_path / "real-proc"
        table.mkdir()
        for child in (sitting, mapping):
            os.symlink(f"/proc/{child.pid}", table / str(child.pid))
        items = [rb.measure(e) for e in rb.inventory(backends, models, "/backends").retired]
        found = rb.users(table, items, rb._prefixes(backends, "/backends"))
        assert set(found) == {OLD}
        assert f"pid {sitting.pid} cwd" in found[OLD]
        assert f"pid {mapping.pid} maps" in found[OLD], "Python's mmap also keeps an fd: maps alone is asserted"
        assert rb.users(table, items, ("/elsewhere",)) == found, \
            "by device and inode alone, as from the host for a container's process"
    finally:
        for child in (sitting, mapping):
            child.kill()
            child.wait()
