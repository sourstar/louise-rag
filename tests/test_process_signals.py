"""Every process we ship stops on SIGTERM, promptly, with its documented
status — tested on the real process, not on its handler.

docs/07 §7.7: the worker stops every collection at its unit and exits 130
on a signal. The web drains, then reports the signal. A model server we
build is PID 1 in its container, where SIGTERM has NO default action: the
MedCPT reranker ignored it and was SIGKILLed after Docker's 10 s at every
NAS poweroff until 2026-09-18 (tests/test_medcpt_rerank_server.py stops it
for real).
The handlers' installation is pinned in tests/test_stop_signals.py; this
file pins what the operator sees.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path: Path, corpus_dir: Path) -> dict:
    """A node of its own: one Codex gutenberg collection over CORPUS_DIR
    (acquisition off), naming an encoder no one answers, nothing shared
    with the test process. Its file names the encoder because a collection
    that names none is refused (no default encoder, operator 2026-09-18) —
    and this child process is beyond tests/conftest.py's test encoder."""
    from tests.conftest import codex_collection
    (tmp_path / "data").mkdir(exist_ok=True)
    codex_collection("gutenberg", louise_root=tmp_path / "data", config_dir=tmp_path / "config",
                     corpus_dir=corpus_dir, toml_extra='\n[encoder]\nurl = "http://127.0.0.1:9"\n')
    return {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(ROOT),
        "DATA_DIR": str(tmp_path / "data"), "CONFIG_DIR": str(tmp_path / "config"),
        "LOUISE_ROOT": str(tmp_path / "data"),
        "CORPUS_DIR": str(corpus_dir), "PARSE_PROCESSES": "1",
        "WORKER_IDLE_SLEEP": "0.2",
    }


class _Output:
    """A child's merged output, read as it arrives."""

    def __init__(self, proc: subprocess.Popen):
        self.lines: list[str] = []
        self._t = threading.Thread(target=self._pump, args=(proc,), daemon=True)
        self._t.start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.lines.append(line)

    def wait_for(self, text: str, proc: subprocess.Popen, timeout: float) -> None:
        deadline = time.time() + timeout
        while not any(text in line for line in self.lines):
            assert proc.poll() is None, f"exited {proc.returncode} before {text!r}:\n{self}"
            assert time.time() < deadline, f"no {text!r} in {timeout}s:\n{self}"
            time.sleep(0.1)

    def __str__(self) -> str:
        return "".join(self.lines[-40:])


def _stop(proc: subprocess.Popen, within: float) -> tuple[int, float]:
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(timeout=within)
    return code, time.monotonic() - t0


def test_the_worker_finishes_its_units_and_exits_130(tmp_path, corpus_dir):
    proc = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT,
                            env=_env(tmp_path, corpus_dir), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = _Output(proc)
    try:
        out.wait_for("worker started", proc, timeout=60)
        time.sleep(1.0)                      # mid-round: discovery, rdf/fts, index maintainer
        code, took = _stop(proc, within=60)
        out._t.join(5)
        assert code == 130, f"exit {code}, docs/07 §7.7 says 130:\n{out}"
        assert any("worker stopped (signal)" in line for line in out.lines), str(out)
        assert took < 30, f"{took:.1f}s to stop"
    finally:
        proc.kill()
        proc.wait()


def test_the_web_shuts_down_gracefully(tmp_path, corpus_dir):
    """uvicorn drains, runs the app's shutdown, then re-raises the signal so
    the status says what stopped it (-15 here; 143 from Docker, behind
    init). A kill without the drain would give the same status and no
    "Finished server process"."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api:app",
                             "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
                            env=_env(tmp_path, corpus_dir), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = _Output(proc)
    try:
        deadline = time.time() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
                break
            except OSError:
                assert proc.poll() is None and time.time() < deadline, str(out)
                time.sleep(0.2)
        code, took = _stop(proc, within=20)
        out._t.join(5)
        assert code in (0, -signal.SIGTERM), f"exit {code}:\n{out}"
        assert any("Finished server process" in line for line in out.lines), str(out)
        assert took < 10, f"{took:.1f}s to stop"
    finally:
        proc.kill()
        proc.wait()


def _argv(dockerfile: Path) -> list[str]:
    """What the container runs: the ENTRYPOINT if there is one, else CMD
    (exec form — the shell form would make /bin/sh PID 1)."""
    found = {}
    for kind, value in re.findall(r"^(ENTRYPOINT|CMD)\s+(\[.*\])\s*$",
                                  dockerfile.read_text(), flags=re.M):
        found[kind] = json.loads(value)
    return found.get("ENTRYPOINT") or found["CMD"]


def test_every_image_we_build_answers_sigterm_as_pid_1():
    """uvicorn installs its own handlers. A Python script of ours must
    install one: as PID 1 the kernel gives SIGTERM no default action."""
    dockerfiles = [ROOT / "Dockerfile", *sorted((ROOT / "deploy").glob("*/Dockerfile"))]
    assert len(dockerfiles) > 1
    for dockerfile in dockerfiles:
        argv = _argv(dockerfile)
        name = dockerfile.relative_to(ROOT)
        if Path(argv[0]).name == "uvicorn":
            continue
        assert Path(argv[0]).name in ("python", "python3"), f"{name}: {argv} — unknown PID 1"
        if argv[1] == "-m":
            # `python -m app.rage` (deploy/rage): the module of the repository,
            # its __main__ when it is a package
            module = ROOT.joinpath(*argv[2].split("."))
            script = module / "__main__.py" if module.is_dir() else module.with_suffix(".py")
        else:
            script = dockerfile.parent / Path(argv[1]).name
        assert script.is_file(), f"{name}: runs {argv[1:3]}, not found"
        # a HANDLER, not a mention: `os.kill(pid, signal.SIGTERM)` names the
        # signal too, and handles nothing
        assert "signal.signal(signal.SIGTERM" in script.read_text(encoding="utf-8"), \
            f"{script.relative_to(ROOT)} is PID 1 and never handles SIGTERM"


def test_the_node_services_run_under_an_init():
    """`python -m app.worker` is not PID 1 on the node: init reaps rsync's
    orphans (the worker warns when it finds itself PID 1) and forwards the
    stop signal. Checked on the reference compose the deploy is built from."""
    services: dict[str, list[str]] = {}
    current = None
    for line in (ROOT / "deploy" / "truenas-app.yaml").read_text().splitlines():
        m = re.match(r"^  ([A-Za-z][\w-]*):\s*(#.*)?$", line)
        if m:
            current = services.setdefault(m.group(1), [])
        elif current is not None and line.startswith("    "):
            current.append(line.strip())
        elif line and not line.startswith(" ") and not line.startswith("#"):
            current = None
    assert {"web", "worker"} <= set(services), sorted(services)
    for name in ("web", "worker"):
        assert any(re.match(r"init:\s*true\b", s) for s in services[name]), \
            f"deploy/truenas-app.yaml: `{name}` runs without init"
