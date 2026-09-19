"""A RAGE with nothing real under it: a directory for /proc, files for the
weights, and a process runner that "launches" a child by writing its
`cmdline` and `environ` the way the kernel would show them.

The runner is llama-swap as the front sees it (docs/notes/rage.md §4): a
model starts on the first request to `/upstream/<variant>/<health>`, gets a
port of llama-swap's choosing, and stops at `/api/models/unload/<variant>`.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import httpx
import pytest

from app.rage import model
from app.rage.model import PORT, WEIGHTS
from app.rage.placement import Placement
from app.rage.procfs import ProcFS
from app.rage.store import RunStore
from app.rage.swap import Swap

IMAGE = "sha256:" + "ab" * 32

RAGE_TOML = """\
[rage]
reserve_mib  = {{ xpu = 1000, cpu = 2000 }}
capacity_mib = {{ xpu = 10000, cpu = 20000 }}
device_names = {{ xpu = "Intel(R) Arc(TM) Pro B70 Graphics" }}

[models.bge-m3]
engine  = {{ name = "llama.cpp", build = "b10630", variant = "sycl" }}
weights = "{models}/bge-m3-FP16.gguf"
device  = "xpu"
memory  = {{ xpu = 1400, cpu = 2600 }}
host    = {{ xpu = 450 }}
env     = {{ LD_LIBRARY_PATH = "/opt/llama/lib" }}
cmd.xpu = ["/opt/llama/llama-server", "-m", "${{WEIGHTS}}", "--embedding", "-ngl", "99", "--port", "${{PORT}}"]
cmd.cpu = ["/opt/llama/llama-server", "-m", "${{WEIGHTS}}", "--embedding", "-ngl", "0", "--port", "${{PORT}}"]

[models.madlad]
engine  = {{ name = "louise-madlad-server", build = "2" }}
weights = "{models}/madlad400-3b-mt-bf16"
device  = "xpu"
memory  = {{ xpu = 8200, cpu = 12000 }}
host    = {{ xpu = 3400 }}
env     = {{ HF_HUB_OFFLINE = "1" }}
cmd.xpu = ["/opt/venv-madlad/bin/python", "-m", "servers.launch", "madlad", "--weights", "${{WEIGHTS}}", "--device", "xpu", "--host", "127.0.0.1", "--port", "${{PORT}}"]
cmd.cpu = ["/opt/venv-madlad/bin/python", "-m", "servers.launch", "madlad", "--weights", "${{WEIGHTS}}", "--device", "cpu", "--host", "127.0.0.1", "--port", "${{PORT}}"]
"""


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.proc = root / "proc"
        self.proc.mkdir()
        self.models = root / "models"
        self.models.mkdir()
        (self.models / "bge-m3-FP16.gguf").write_bytes(b"GGUF bge-m3 weights")
        madlad = self.models / "madlad400-3b-mt-bf16"
        madlad.mkdir()
        (madlad / "model.safetensors").write_bytes(b"madlad tensors")
        (madlad / "config.json").write_text('{"model_type": "t5"}')
        self.config = root / "config" / "rage.toml"
        self.config.parent.mkdir()
        self.write_config(RAGE_TOML)
        self.state = root / "state"
        self._pids = itertools.count(100)
        self._ports = itertools.count(10001)
        self.running: dict[str, int] = {}          # variant -> pid
        self.launches: list[str] = []
        self.unloads: list[str] = []
        self.requests: list[tuple[str, bytes]] = []
        self.load_delay = 0.0
        self.fail: set[str] = set()
        self.hostname = "container-1"
        self.during_request = None                 # a callable run inside an upstream call

    def write_config(self, template: str) -> None:
        self.config.write_text(template.format(models=self.models.as_posix()),
                               encoding="utf-8")

    # ---- the fake kernel -------------------------------------------------------

    def spawn(self, argv: list[str], env: dict[str, str], *, rss_kb: int = 512_000) -> int:
        pid = next(self._pids)
        d = self.proc / str(pid)
        (d / "fdinfo").mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(
            b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        (d / "status").write_text(f"Name:\tchild\nVmRSS:\t{rss_kb} kB\n")
        (d / "exe").write_bytes(b"the program")
        (d / "oom_score_adj").write_text("0")
        (d / "fdinfo" / "5").write_text(
            "drm-driver:\txe\ndrm-client-id:\t7\ndrm-resident-vram0:\t1400 MiB\n")
        (d / "fdinfo" / "6").write_text(           # the same client through a second fd
            "drm-driver:\txe\ndrm-client-id:\t7\ndrm-resident-vram0:\t1400 MiB\n")
        return pid

    def kill(self, variant: str) -> None:
        pid = self.running.pop(variant)
        for f in sorted((self.proc / str(pid)).rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        (self.proc / str(pid)).rmdir()

    def launch(self, variant: str) -> int:
        """What llama-swap does on the first request for a variant."""
        model_id, device = variant.split("@")
        entry = model.load(self.config).models[model_id]
        port = next(self._ports)
        argv = [entry.weights if a == WEIGHTS else str(port) if a == PORT else a
                for a in entry.template(device)]
        env = {"PATH": "/usr/bin", "HOSTNAME": self.hostname, **entry.env}
        pid = self.spawn(argv, env)
        self.running[variant] = pid
        self.launches.append(variant)
        return pid

    # ---- the fake llama-swap -----------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/api/models/unload/"):
                variant = path.rsplit("/", 1)[1]
                self.unloads.append(variant)
                if variant not in self.running:
                    return httpx.Response(404)
                self.kill(variant)
                return httpx.Response(200)
            _, _upstream, variant, rest = path.split("/", 3)
            if variant in self.fail:
                return httpx.Response(502, text="the child exited with 1")
            if variant not in self.running:
                if self.load_delay:
                    await asyncio.sleep(self.load_delay)
                self.launch(variant)
            if rest == "health":
                return httpx.Response(200, text="ok")
            self.requests.append((rest, request.content))
            if self.during_request is not None:
                self.during_request()
            if rest == "v1/chat/completions":
                async def tokens():
                    for word in (b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"):
                        yield word
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=tokens())
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}],
                                             "echo": json.loads(request.content or b"{}")})
        return httpx.MockTransport(handle)

    # ---- the front's parts ---------------------------------------------------------

    def swap(self) -> Swap:
        return Swap("http://llama-swap", transport=self.transport(), load_timeout_s=5.0)

    def placement(self, swap: Swap | None = None, *, image: str = IMAGE,
                  capacity=lambda device: None) -> Placement:
        versions = self.root / "runtime-versions"
        versions.write_text("libze-intel-gpu1 26.05.1\nintel-opencl-icd 26.05.1\n")
        sys_root, proc_root = self.root / "sys", self.root / "hostproc"
        (sys_root / "module" / "xe").mkdir(parents=True, exist_ok=True)
        (sys_root / "module" / "xe" / "srcversion").write_text("9F2C1A\n")
        (proc_root / "sys" / "kernel").mkdir(parents=True, exist_ok=True)
        (proc_root / "sys" / "kernel" / "osrelease").write_text("6.18.0-production\n")
        logs: list[str] = []
        p = Placement(config_path=self.config, yaml_path=self.state / "llama-swap.yaml",
                      image=image, runner=swap or self.swap(),
                      store=RunStore(self.state / "rage.sqlite"), procfs=ProcFS(self.proc),
                      capacity=capacity, sys_root=sys_root, proc_root=proc_root,
                      runtime_versions=versions, log=logs.append)
        p.logs = logs                               # for the tests to read
        return p


@pytest.fixture
def rage(tmp_path) -> Harness:
    return Harness(tmp_path)
